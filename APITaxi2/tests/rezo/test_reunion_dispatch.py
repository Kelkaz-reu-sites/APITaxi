from datetime import datetime
import time
from unittest import mock

from sqlalchemy.orm import joinedload

from APITaxi2 import tasks
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


def test_rezo_wide_radius_visible_and_over_max_invisible(app, moteur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    now = datetime.now()

    visible_taxi = TaxiFactory(
        vehicle__descriptions__radius=5000,
        vehicle__descriptions__last_update_at=now,
    )
    invisible_taxi = TaxiFactory(
        vehicle__descriptions__radius=30000,
        vehicle__descriptions__last_update_at=now,
    )

    visible_description = _vehicle_description_for(visible_taxi)
    invisible_description = _vehicle_description_for(invisible_taxi)

    # Around 4.8 km east of the client at this latitude.
    _store_taxi_position(app, visible_taxi, visible_description, CLIENT_LON + 0.066, CLIENT_LAT)
    # Around 33 km east: outside the configured 30 km maximum.
    _store_taxi_position(app, invisible_taxi, invisible_description, CLIENT_LON + 0.45, CLIENT_LAT)

    resp = moteur.client.get(f'/taxis?lon={CLIENT_LON}&lat={CLIENT_LAT}')

    assert resp.status_code == 200
    assert [taxi['id'] for taxi in resp.json['data']] == [visible_taxi.id]
    assert resp.json['data'][0]['crowfly_distance'] > 4500


def test_rezo_stale_gps_is_invisible(app, moteur):
    app.config['FAKE_TAXI_ID'] = False
    ZUPCFactory()
    taxi = TaxiFactory(vehicle__descriptions__last_update_at=datetime.now())
    vehicle_description = _vehicle_description_for(taxi)

    _store_taxi_position(
        app,
        taxi,
        vehicle_description,
        CLIENT_LON,
        CLIENT_LAT,
        age_seconds=app.config['REZO_TAXI_GPS_FRESHNESS_SECONDS'] + 1,
    )

    resp = moteur.client.get(f'/taxis?lon={CLIENT_LON}&lat={CLIENT_LAT}')

    assert resp.status_code == 200
    assert resp.json['data'] == []


def test_rezo_search_without_taxi_returns_empty_fallback(app, moteur):
    ZUPCFactory()

    resp = moteur.client.get(f'/taxis?lon={CLIENT_LON}&lat={CLIENT_LAT}')

    assert resp.status_code == 200
    assert resp.json['data'] == []


def test_rezo_driver_timeout_is_scheduled_at_120_seconds(app, operateur, moteur):
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_operator',
    )

    with mock.patch.object(tasks.handle_hail_timeout, 'apply_async') as mocked_timeout:
        resp = operateur.client.put(f'/hails/{hail.id}', json={'data': [{
            'status': 'received_by_taxi',
        }]})

    assert resp.status_code == 200
    mocked_timeout.assert_called_once_with(
        args=(hail.id, operateur.user.id),
        kwargs={
            'initial_hail_status': 'received_by_taxi',
            'new_hail_status': 'timeout_taxi',
            # Rezo D19: an unanswered hail pauses the driver, it does not end
            # their shift.
            'new_taxi_status': 'occupied',
        },
        countdown=120,
    )


def test_rezo_driver_timeout_pauses_the_taxi(app, operateur, moteur):
    """Rezo D19: after 120 s without an answer the taxi is paused, not offline."""
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )
    vehicle_description = _vehicle_description_for(hail.taxi)
    vehicle_description.status = 'free'
    hail_id = hail.id
    vehicle_description_id = vehicle_description.id

    tasks.handle_hail_timeout(
        hail.id,
        operateur.user.id,
        'received_by_taxi',
        'timeout_taxi',
        'occupied',
    )

    hail = db.session.get(Hail, hail_id)
    vehicle_description = db.session.get(VehicleDescription, vehicle_description_id)
    assert hail.status == 'timeout_taxi'
    assert hail.transition_log[-1]['reason'] == 'timeout'
    assert vehicle_description.status == 'occupied'
