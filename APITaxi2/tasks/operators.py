from datetime import timedelta
import json

from celery import shared_task
from flask import current_app
import requests
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from APITaxi_models2 import db, Hail, Taxi, Vehicle, VehicleDescription

from .. import activity_logs, http_client, observability, redis_backend, schemas, processes
from ..services import hail_reassignment, hail_state_machine


def _request_exception_failure_reason(exc):
    if isinstance(exc, requests.exceptions.Timeout):
        return 'Operator API request timed out.'
    return 'Unable to contact operator API.'


def _schedule_timeout_for_current_status(hail, vehicle_description_added_by_id):
    timeout = hail_state_machine.timeout_for_status(hail.status)
    if not timeout:
        return

    handle_hail_timeout.apply_async(
        args=(hail.id, vehicle_description_added_by_id),
        kwargs={
            'initial_hail_status': timeout.initial_hail_status,
            'new_hail_status': timeout.new_hail_status,
            'new_taxi_status': timeout.new_taxi_status,
        },
        countdown=timeout.countdown(current_app.config),
    )


def _uses_internal_operator_handoff(hail):
    if not current_app.config.get('REZO_TAXI_INTERNAL_OPERATOR_HANDOFF_ENABLED'):
        return False

    operator_emails = current_app.config.get('REZO_TAXI_INTERNAL_OPERATOR_EMAILS') or []
    return hail.operateur and hail.operateur.email in operator_emails


def _handoff_to_internal_rezo_operator(hail, vehicle_description):
    observability.log_event(
        current_app.logger,
        'info',
        'operator_internal_handoff',
        hail_id=hail.id,
        operator_id=hail.operateur_id,
    )
    redis_backend.log_hail(
        hail_id=hail.id,
        http_method='internal operator handoff',
        request_payload={'mode': 'rezo_internal_operator_handoff'},
        hail_initial_status=hail.status,
        request_user=None,
        response_payload={'status': 'received_by_taxi'},
        response_status_code=200,
        hail_final_status='received_by_taxi',
    )
    processes.change_status(
        hail,
        'received_by_taxi',
        reason='Rezo internal operator handoff',
    )
    vehicle_description_added_by_id = vehicle_description.added_by_id
    db.session.commit()
    _schedule_timeout_for_current_status(hail, vehicle_description_added_by_id)
    return True


@shared_task(name='handle_hail_timeout')
def handle_hail_timeout(hail_id, operateur_id,
                        initial_hail_status, new_hail_status,
                        new_taxi_status=None):
    """This task is called asynchronously. If hail status is still
    `initial_hail_status`, then we log a warning message and set the hail
    status to `new_hail_status` (required) and the taxi status to
    `new_taxi_status` (optional).
    """
    res = db.session.query(
        Hail, VehicleDescription
    ).options(
        joinedload(Hail.taxi)
    ).filter(
        Hail.taxi_id == Taxi.id,
        Taxi.vehicle_id == Vehicle.id,
        VehicleDescription.vehicle_id == Vehicle.id,
        Hail.id == hail_id,
        VehicleDescription.added_by_id == int(operateur_id)
    ).one_or_none()
    if not res:
        observability.log_event(
            current_app.logger,
            'warning',
            'hail_timeout_not_found',
            hail_id=hail_id,
            operateur_id=operateur_id,
        )
        return

    hail, vehicle_description = res

    # Hail status is different from the status which triggers the timeout.
    if hail.status != initial_hail_status:
        return

    observability.log_event(
        current_app.logger,
        'warning',
        'hail_timeout_reached',
        hail_id=hail_id,
        taxi_id=hail.taxi_id,
        operator_id=hail.added_by_id,
        initial_hail_status=initial_hail_status,
        new_hail_status=new_hail_status,
        new_taxi_status=new_taxi_status,
    )

    hail_state_machine.apply_timeout_transition(
        hail,
        vehicle_description,
        new_hail_status,
        new_taxi_status=new_taxi_status,
    )

    db.session.commit()

    if initial_hail_status == 'received_by_taxi' and new_hail_status == 'timeout_taxi':
        hail_reassignment.reassign_after_driver_unavailable(hail_id)


@shared_task(name='send_request_operator')
def send_request_operator(hail_id, endpoint, operator_header_name, operator_api_key):
    """Send the hail request to the operator's API.

    If this task is called with too much delay because of production issues,
    mark the hail as failed.
    """
    res = db.session.query(
        Hail, VehicleDescription
    ).options(
        joinedload(Hail.taxi).joinedload(Taxi.vehicle),
        joinedload(Hail.taxi).joinedload(Taxi.driver),
        joinedload(Hail.added_by),
        joinedload(Hail.operateur),
        joinedload(Hail.customer)
    ).join(
        Taxi, Taxi.id == Hail.taxi_id
    ).join(
        Vehicle
    ).filter(
        Hail.taxi_id == Taxi.id,
        Taxi.vehicle_id == Vehicle.id,
        VehicleDescription.vehicle_id == Vehicle.id,
        VehicleDescription.added_by_id == Hail.operateur_id,
        Hail.id == hail_id
    ).one_or_none()
    if not res:
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_hail_not_found',
            hail_id=hail_id,
        )
        return False

    hail, vehicle_description = res

    if hail.status != 'received':
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_hail_unexpected_status',
            hail_id=hail.id,
            status=hail.status,
        )
        return False

    # This task has been called long after the hail has been created, probably
    # because of a production outage or because an ongoing deployment.
    # Cancel the hail, but set the taxi status back to free.
    max_delay = current_app.config['REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS']
    if db.session.query(func.NOW() - hail.added_at).scalar() > timedelta(seconds=+max_delay):
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_hail_task_too_late',
            hail_id=hail.id,
            max_delay_seconds=max_delay,
        )
        processes.change_status(
            hail,
            'failure',
            reason=f'Task send_request_operator called after more than {max_delay} seconds.'
        )
        old_taxi_status = vehicle_description.status
        new_taxi_status = 'free'
        vehicle_description.status = new_taxi_status
        activity_logs.log_taxi_status(
            hail.taxi_id,
            old_taxi_status,
            new_taxi_status,
            task='send_request_operator',
        )
        db.session.commit()
        return False

    if _uses_internal_operator_handoff(hail):
        return _handoff_to_internal_rezo_operator(hail, vehicle_description)

    schema = schemas.DataHailSchema()
    # The other parameters of dump are None because we do not want to provide
    # the taxi's location or vehicle details to user now.
    # Location and vehicle details can be retrieved later with GET /hails/:id
    # after taxi accepts the request.
    payload = schema.dump({'data': [(hail, None, None)]})

    # Custom headers to send to operator's API.
    headers = {}
    if operator_header_name:
        headers[operator_header_name] = operator_api_key

    # Send request.
    try:
        resp = requests.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=http_client.request_timeout('OPERATOR_API'),
        )
    # If operator's API is unavailable, log the error, set hail as failure and
    # abort.
    except requests.exceptions.RequestException as exc:
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_api_request_failed',
            hail_id=hail.id,
            operator_id=hail.operateur_id,
            error_type=exc.__class__.__name__,
        )
        redis_backend.log_hail(
            hail_id=hail.id,
            http_method='POST to operator',
            request_payload=json.dumps(payload, indent=2),
            hail_initial_status=hail.status,
            hail_final_status='failure',
            request_user=None,
            response_payload=str(exc),
            response_status_code=None
        )
        processes.change_status(hail, 'failure', reason=_request_exception_failure_reason(exc))
        old_taxi_status = vehicle_description.status
        new_taxi_status = 'free'
        vehicle_description.status = new_taxi_status
        activity_logs.log_taxi_status(
            hail.taxi_id,
            old_taxi_status,
            new_taxi_status,
            task='send_request_operator',
        )
        db.session.commit()
        return False

    # Operator's API should return a JSON response. If it doesn't, log an
    # error, set hail as failure and abort.
    try:
        response_payload = json.dumps(resp.json(), indent=2)
    except json.decoder.JSONDecodeError as exc:
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_api_invalid_json',
            hail_id=hail.id,
            operator_id=hail.operateur_id,
            response_status_code=resp.status_code,
        )
        redis_backend.log_hail(
            hail_id=hail.id,
            http_method='POST to operator',
            request_payload=json.dumps(payload, indent=2),
            hail_initial_status=hail.status,
            hail_final_status='failure',
            request_user=None,
            response_payload='Response should be valid JSON, but the API response was: %s' % resp.text,
            response_status_code=resp.status_code
        )
        processes.change_status(hail, 'failure', reason=str(exc))
        old_taxi_status = vehicle_description.status
        new_taxi_status = 'free'
        vehicle_description.status = new_taxi_status
        activity_logs.log_taxi_status(
            hail.taxi_id,
            old_taxi_status,
            new_taxi_status,
            task='send_request_operator',
        )
        db.session.commit()
        return False

    # If the operator's API isn't successful, log the response, set hail as
    # failure and abort.
    if resp.status_code < 200 or resp.status_code >= 300:
        observability.log_event(
            current_app.logger,
            'warning',
            'operator_api_unexpected_status',
            hail_id=hail.id,
            operator_id=hail.operateur_id,
            response_status_code=resp.status_code,
        )
        redis_backend.log_hail(
            hail_id=hail.id,
            http_method='POST to operator',
            request_payload=json.dumps(payload, indent=2),
            hail_initial_status=hail.status,
            hail_final_status='failure',
            request_user=None,
            response_payload=response_payload,
            response_status_code=resp.status_code
        )
        processes.change_status(hail, 'failure', reason="HTTP response status code %s" % resp.status_code)
        old_taxi_status = vehicle_description.status
        new_taxi_status = 'free'
        vehicle_description.status = new_taxi_status
        activity_logs.log_taxi_status(
            hail.taxi_id,
            old_taxi_status,
            new_taxi_status,
            task='send_request_operator',
        )
        db.session.commit()
        return False

    # Log this successful request
    observability.log_event(
        current_app.logger,
        'info',
        'operator_hail_sent',
        hail_id=hail.id,
        operator_id=hail.operateur_id,
        response_status_code=resp.status_code,
    )
    redis_backend.log_hail(
        hail_id=hail.id,
        http_method='POST to operator',
        request_payload=json.dumps(payload, indent=2),
        hail_initial_status=hail.status,
        hail_final_status='received_by_operator',
        request_user=None,
        response_payload=response_payload,
        response_status_code=resp.status_code
    )

    # As all our API responses, the operator's API response is expected to be a
    # dictionary with one key, "data", which is a list of one element.
    #
    # If this is the case and the key "taxi_phone_number" is present, store the
    # phone number.
    #
    # According to our documentation, operators should provide the taxi's phone
    # number with a PUT request on /hails/:id, but some of them do not follow
    # the documentation and return the phone number here.
    data = resp.json()
    if isinstance(data, dict) and len(data.get('data', [])) >= 1 and 'taxi_phone_number' in data['data'][0]:
        hail.taxi_phone_number = data['data'][0]['taxi_phone_number']

    processes.change_status(hail, 'received_by_operator')

    vehicle_description_added_by_id = vehicle_description.added_by_id

    db.session.commit()

    # If hail is still "received_by_operator" and not "received_by_taxi" after
    # the configured delay, timeout.
    _schedule_timeout_for_current_status(hail, vehicle_description_added_by_id)

    return True
