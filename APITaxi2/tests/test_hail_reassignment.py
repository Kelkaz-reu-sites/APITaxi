import time
from unittest import mock
import uuid

from sqlalchemy.orm import joinedload

from APITaxi2 import tasks
from APITaxi2.services import hail_reassignment
from APITaxi_models2 import db, Hail, VehicleDescription
from APITaxi_models2.unittest.factories import HailFactory, TaxiFactory, ZUPCFactory


CLIENT_LON = 2.35
CLIENT_LAT = 48.86


def _vehicle_description_for(taxi):
    return VehicleDescription.query.options(
        joinedload(VehicleDescription.added_by)
    ).filter_by(
        vehicle_id=taxi.vehicle_id,
        added_by_id=taxi.added_by_id,
    ).one()


def _store_taxi_position(app, taxi, vehicle_description, lon, lat, *, age_seconds=0):
    redis_key = f'{taxi.id}:{vehicle_description.added_by.email}'
    timestamp = int(time.time() - age_seconds)
    app.redis.geoadd('geoindex_2', [lon, lat, redis_key])
    app.redis.zadd('timestamps', {redis_key: timestamp})


def _hail_waiting_for_reassignment(moteur, operateur, status='declined_by_taxi'):
    return HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status=status,
        session_id=uuid.uuid4(),
        customer_lon=CLIENT_LON,
        customer_lat=CLIENT_LAT,
    )


def test_reassignment_creates_next_hail_and_skips_previous_taxi(app, moteur, operateur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    source_hail = _hail_waiting_for_reassignment(moteur, operateur)

    next_taxi = TaxiFactory(added_by=operateur.user)
    next_description = _vehicle_description_for(next_taxi)
    _store_taxi_position(app, next_taxi, next_description, CLIENT_LON + 0.01, CLIENT_LAT)

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        result = hail_reassignment.reassign_after_driver_unavailable(source_hail.id)

    assert result.created is True
    assert result.reason == 'created'
    assert mocked_send.call_count == 1

    new_hail = Hail.query.filter(Hail.id != source_hail.id).one()
    assert new_hail.taxi_id == next_taxi.id
    assert new_hail.status == 'received'
    assert new_hail.session_id == source_hail.session_id
    assert new_hail.transition_log[-1]['reason'] == 'reassignment_from_declined_by_taxi'

    next_description = db.session.get(VehicleDescription, next_description.id)
    assert next_description.status == 'answering'
    assert len(app.redis.zrange('hail:%s' % new_hail.id, 0, -1)) == 1


def test_reassignment_is_idempotent_when_active_hail_exists(app, moteur, operateur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    source_hail = _hail_waiting_for_reassignment(moteur, operateur)
    active_hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received',
        session_id=source_hail.session_id,
        customer=source_hail.customer,
    )

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        result = hail_reassignment.reassign_after_driver_unavailable(source_hail.id)

    assert result.created is False
    assert result.reason == 'active_hail_already_exists'
    assert result.new_hail_id == active_hail.id
    mocked_send.assert_not_called()
    assert Hail.query.count() == 2


def test_reassignment_respects_max_contacted_taxis(app, moteur, operateur):
    app.config['REZO_TAXI_SEARCH_CANDIDATE_LIMIT'] = 1
    source_hail = _hail_waiting_for_reassignment(moteur, operateur)

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        result = hail_reassignment.reassign_after_driver_unavailable(source_hail.id)

    assert result.created is False
    assert result.reason == 'max_attempts_reached'
    mocked_send.assert_not_called()
    assert Hail.query.count() == 1


def test_reassignment_returns_no_candidate_without_available_taxi(app, moteur, operateur):
    ZUPCFactory()
    source_hail = _hail_waiting_for_reassignment(moteur, operateur)

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        result = hail_reassignment.reassign_after_driver_unavailable(source_hail.id)

    assert result.created is False
    assert result.reason == 'no_candidate'
    mocked_send.assert_not_called()
    assert Hail.query.count() == 1


def test_driver_refusal_reassigns_from_hails_endpoint(app, moteur, operateur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    source_hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
        session_id=uuid.uuid4(),
        customer_lon=CLIENT_LON,
        customer_lat=CLIENT_LAT,
    )
    next_taxi = TaxiFactory(added_by=operateur.user)
    next_description = _vehicle_description_for(next_taxi)
    _store_taxi_position(app, next_taxi, next_description, CLIENT_LON + 0.01, CLIENT_LAT)

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        resp = operateur.client.put(f'/hails/{source_hail.id}', json={'data': [{
            'status': 'declined_by_taxi',
        }]})

    assert resp.status_code == 200
    assert mocked_send.call_count == 1

    source_hail = db.session.get(Hail, source_hail.id)
    new_hail = Hail.query.filter(Hail.id != source_hail.id).one()
    assert source_hail.status == 'declined_by_taxi'
    assert new_hail.status == 'received'
    assert new_hail.taxi_id == next_taxi.id


def test_driver_timeout_reassigns_from_celery_task(app, moteur, operateur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    source_hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
        session_id=uuid.uuid4(),
        customer_lon=CLIENT_LON,
        customer_lat=CLIENT_LAT,
    )
    next_taxi = TaxiFactory(added_by=operateur.user)
    next_description = _vehicle_description_for(next_taxi)
    _store_taxi_position(app, next_taxi, next_description, CLIENT_LON + 0.01, CLIENT_LAT)

    with mock.patch.object(tasks.send_request_operator, 'apply_async') as mocked_send:
        tasks.handle_hail_timeout(
            source_hail.id,
            operateur.user.id,
            'received_by_taxi',
            'timeout_taxi',
            'off',
        )

    assert mocked_send.call_count == 1

    source_hail = db.session.get(Hail, source_hail.id)
    new_hail = Hail.query.filter(Hail.id != source_hail.id).one()
    assert source_hail.status == 'timeout_taxi'
    assert new_hail.status == 'received'
    assert new_hail.taxi_id == next_taxi.id
