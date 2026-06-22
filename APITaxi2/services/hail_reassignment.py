from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import reduce

from flask import current_app
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from APITaxi_models2 import ADS, db, Hail, Taxi, Town, Vehicle, VehicleDescription, ZUPC
from APITaxi_models2.hail import HAIL_TERMINAL_STATUS

from .. import activity_logs, observability, redis_backend, rezo_taxi_config
from ..exclusions import ExclusionHelper
from .. import processes
from ..utils import get_short_uuid


DRIVER_REASSIGNABLE_STATUSES = ('declined_by_taxi', 'timeout_taxi')


@dataclass(frozen=True)
class ReassignmentCandidate:
    taxi: Taxi
    vehicle_description: VehicleDescription
    location: redis_backend.Location


@dataclass(frozen=True)
class ReassignmentResult:
    created: bool
    reason: str
    source_hail_id: str
    new_hail_id: str = None


def _session_hails_query(hail):
    return Hail.query.filter(
        Hail.added_by_id == hail.added_by_id,
        Hail.customer_id == hail.customer_id,
        Hail.session_id == hail.session_id,
    )


def _active_session_hail(hail):
    return _session_hails_query(hail).filter(
        Hail.id != hail.id,
        ~Hail.status.in_(HAIL_TERMINAL_STATUS),
    ).order_by(
        Hail.added_at.desc()
    ).first()


def _already_contacted_taxi_ids(hail):
    return {
        row[0]
        for row in _session_hails_query(hail).with_entities(Hail.taxi_id).all()
    }


def _allowed_insee_codes(lon, lat):
    towns = Town.query.filter(
        func.ST_Intersects(Town.shape, 'POINT(%s %s)' % (lon, lat)),
    ).all()
    if not towns:
        return set()

    town = towns[0]
    zupcs = ZUPC.query.options(
        joinedload(ZUPC.allowed)
    ).filter(
        ZUPC.allowed.contains(town)
    ).all()
    return {
        town.insee,
        *(allowed_town.insee for zupc in zupcs for allowed_town in zupc.allowed),
    }


def find_available_candidates(lon, lat, *, exclude_taxi_ids=None):
    exclude_taxi_ids = set(exclude_taxi_ids or ())

    if ExclusionHelper().is_at_excluded_zone(lon, lat):
        return []

    allowed_insee_codes = _allowed_insee_codes(lon, lat)
    if not allowed_insee_codes:
        return []

    locations = redis_backend.taxis_locations_by_operator(
        lon,
        lat,
        current_app.config['REZO_TAXI_RADIUS_MAX_METERS'],
        count=current_app.config['REZO_TAXI_SEARCH_CANDIDATE_LIMIT'],
    )
    if not locations:
        return []

    query = db.session.query(Taxi, VehicleDescription).join(
        ADS
    ).options(
        joinedload(Taxi.vehicle).joinedload(Vehicle.descriptions).joinedload(VehicleDescription.added_by),
        joinedload(Taxi.added_by),
        joinedload(VehicleDescription.added_by),
    ).filter(
        VehicleDescription.vehicle_id == Taxi.vehicle_id,
        Taxi.id.in_(locations.keys()),
        ADS.insee.in_(allowed_insee_codes),
    )

    data = {}
    now = datetime.now()
    for taxi, vehicle_description in query.all():
        if taxi.id in exclude_taxi_ids:
            continue

        operator_email = vehicle_description.added_by.email
        if operator_email not in locations[taxi.id]:
            continue

        if vehicle_description.status != 'free':
            continue

        location = locations[taxi.id][operator_email]
        if not location.update_date:
            continue
        if location.update_date + timedelta(
            seconds=current_app.config['REZO_TAXI_GPS_FRESHNESS_SECONDS']
        ) < now:
            continue
        if location.distance > rezo_taxi_config.radius_or_default(vehicle_description.radius):
            continue

        previous = data.get(taxi)
        if previous is None:
            data[taxi] = (vehicle_description, location)
            continue

        # Preserve historical behavior: when a taxi is reported by multiple
        # operators, keep the freshest operator description.
        data[taxi] = reduce(
            lambda a, b:
                a if a[0].last_update_at
                and b[0].last_update_at
                and a[0].last_update_at >= b[0].last_update_at
                else b,
            (previous, (vehicle_description, location)),
        )

    candidates = [
        ReassignmentCandidate(taxi, vehicle_description, location)
        for taxi, (vehicle_description, location) in data.items()
    ]
    return sorted(candidates, key=lambda candidate: candidate.location.distance)


def _load_hail(hail_id):
    return Hail.query.options(
        joinedload(Hail.added_by),
        joinedload(Hail.customer),
        joinedload(Hail.operateur),
        joinedload(Hail.taxi),
    ).filter(
        Hail.id == hail_id
    ).one_or_none()


def _lock_session_hails(hail):
    _session_hails_query(hail).with_entities(
        Hail.id,
    ).order_by(
        Hail.id,
    ).with_for_update().all()


def _new_fake_taxi_id(candidate):
    if current_app.config.get('FAKE_TAXI_ID'):
        return get_short_uuid()
    return candidate.taxi.id


def _create_reassignment_hail(source_hail, candidate):
    new_hail = Hail(
        id=get_short_uuid(),
        creation_datetime=func.NOW(),
        taxi_id=candidate.taxi.id,
        status=None,
        last_status_change=func.NOW(),
        customer_id=source_hail.customer_id,
        customer_lat=source_hail.customer_lat,
        customer_lon=source_hail.customer_lon,
        operateur_id=candidate.vehicle_description.added_by_id,
        customer_address=source_hail.customer_address,
        customer_phone_number=source_hail.customer_phone_number,
        initial_taxi_lon=candidate.location.lon,
        initial_taxi_lat=candidate.location.lat,
        fake_taxi_id=_new_fake_taxi_id(candidate),
        session_id=source_hail.session_id,
        added_by_id=source_hail.added_by_id,
        added_via='api',
        added_at=func.NOW(),
        source='reassignment',
        last_update_at=func.NOW(),
    )
    processes.change_status(
        new_hail,
        'received',
        reason='reassignment_from_%s' % source_hail.status,
    )
    db.session.add(new_hail)
    db.session.flush()

    candidate.vehicle_description.status = 'answering'
    activity_logs.log_customer_hail(
        source_hail.customer_id,
        candidate.taxi.id,
        new_hail.id,
        reassigned_from_hail_id=source_hail.id,
        reassigned_from_status=source_hail.status,
    )
    return new_hail


def _operator_request_args(hail, operator):
    return [
        hail.id,
        operator.hail_endpoint_production,
        operator.operator_header_name,
        operator.operator_api_key,
    ]


def _schedule_operator_request(args):
    from .. import tasks

    tasks.send_request_operator.apply_async(args=args)


def _log_result(result, **extra):
    observability.log_event(
        current_app.logger,
        'info',
        'hail_reassignment',
        created=result.created,
        reason=result.reason,
        source_hail_id=result.source_hail_id,
        new_hail_id=result.new_hail_id,
        **extra,
    )


def reassign_after_driver_unavailable(hail_id):
    source_hail = _load_hail(hail_id)
    if not source_hail:
        return ReassignmentResult(False, 'source_hail_not_found', hail_id)

    _lock_session_hails(source_hail)
    db.session.refresh(source_hail)

    if source_hail.status not in DRIVER_REASSIGNABLE_STATUSES:
        result = ReassignmentResult(False, 'source_status_not_reassignable', source_hail.id)
        _log_result(result, source_status=source_hail.status)
        return result

    active_hail = _active_session_hail(source_hail)
    if active_hail:
        result = ReassignmentResult(False, 'active_hail_already_exists', source_hail.id, active_hail.id)
        _log_result(result, source_status=source_hail.status)
        return result

    attempted_hails_count = _session_hails_query(source_hail).count()
    max_attempts = current_app.config['REZO_TAXI_SEARCH_CANDIDATE_LIMIT']
    if attempted_hails_count >= max_attempts:
        result = ReassignmentResult(False, 'max_attempts_reached', source_hail.id)
        _log_result(
            result,
            source_status=source_hail.status,
            attempted_hails_count=attempted_hails_count,
            max_attempts=max_attempts,
        )
        return result

    candidates = find_available_candidates(
        source_hail.customer_lon,
        source_hail.customer_lat,
        exclude_taxi_ids=_already_contacted_taxi_ids(source_hail),
    )
    if not candidates:
        result = ReassignmentResult(False, 'no_candidate', source_hail.id)
        _log_result(result, source_status=source_hail.status)
        return result

    candidate = candidates[0]
    source_hail_id = source_hail.id
    source_status = source_hail.status
    new_hail = _create_reassignment_hail(source_hail, candidate)
    operator_request_args = _operator_request_args(new_hail, candidate.vehicle_description.added_by)
    new_hail_id = new_hail.id
    taxi_id = candidate.taxi.id
    operator_id = candidate.vehicle_description.added_by_id
    db.session.commit()
    _schedule_operator_request(operator_request_args)

    redis_backend.log_hail(
        hail_id=new_hail_id,
        http_method='REASSIGN',
        request_payload={
            'reassigned_from_hail_id': source_hail_id,
            'reassigned_from_status': source_status,
        },
        hail_initial_status=None,
        hail_final_status='received',
    )

    result = ReassignmentResult(True, 'created', source_hail_id, new_hail_id)
    _log_result(
        result,
        source_status=source_status,
        taxi_id=taxi_id,
        operator_id=operator_id,
    )
    return result
