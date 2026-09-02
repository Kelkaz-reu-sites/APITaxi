"""Rezo D16: a taxi can hold at most one non-terminal hail.

Upstream le.taxi relies on reading `VehicleDescription.status == 'free'` before
writing `'answering'`, with no lock in between. Two simultaneous requests can
both read `free` and both retain the same taxi.

The guarantee therefore has to live in the database, not in the view: a partial
unique index makes the forbidden state unrepresentable, whichever code path
tries to create it — API call, reassignment, or a future one.
"""
import time
from unittest import mock

import pytest
from sqlalchemy.exc import IntegrityError

from APITaxi2 import tasks
from APITaxi_models2 import db, VehicleDescription
from APITaxi_models2.unittest.factories import HailFactory, TaxiFactory


def test_second_active_hail_on_same_taxi_is_refused(operateur, moteur):
    active = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )

    with pytest.raises(IntegrityError):
        HailFactory(
            added_by=moteur.user,
            operateur=operateur.user,
            taxi=active.taxi,
            status='received',
        )

    db.session.rollback()


def test_terminal_hails_do_not_block_a_new_one(operateur, moteur):
    """History must stay possible: only non-terminal hails are exclusive."""
    finished = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='finished',
    )
    HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        taxi=finished.taxi,
        status='declined_by_taxi',
    )

    fresh = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        taxi=finished.taxi,
        status='received',
    )

    assert fresh.id is not None
    db.session.rollback()


def test_closing_a_hail_frees_the_taxi_immediately(operateur, moteur):
    """A driver who finishes a ride must be reachable for the next one."""
    active = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        status='received_by_taxi',
    )

    active.status = 'finished'
    db.session.flush()

    following = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        taxi=active.taxi,
        status='received',
    )

    assert following.id is not None
    db.session.rollback()


def test_racing_creation_is_refused_cleanly(app, moteur, operateur):
    """A collision must read as a business refusal, not a server error.

    The partial unique index makes the forbidden state impossible, but on its
    own it surfaces as an IntegrityError — a 500 for the customer. Under the
    row lock taken at creation, the view sees the active hail and answers like
    any other unavailable taxi.

    The state simulated here is exactly what a race produces: a taxi whose
    description still reads `free` while a non-terminal hail already exists.
    """
    taxi = TaxiFactory(added_by=operateur.user)
    vehicle_description = VehicleDescription.query.filter_by(
        vehicle_id=taxi.vehicle_id,
        added_by_id=operateur.user.id,
    ).one()
    vehicle_description.status = 'free'

    HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        taxi=taxi,
        status='received_by_taxi',
    )

    app.redis.hset(
        'taxi:%s' % taxi.id,
        operateur.user.email,
        '%s 48.84 2.35 free phone 2' % int(time.time()),
    )

    with mock.patch.object(tasks.send_request_operator, 'apply_async'):
        resp = moteur.client.post('/hails', json={
            'data': [{
                'customer_address': '23 avenue de Ségur, 75007 Paris',
                'customer_id': 'customer_race',
                'customer_lon': 2.3098,
                'customer_lat': 48.851,
                'customer_phone_number': '+336868686',
                'taxi_id': taxi.id,
                'operateur': operateur.user.email,
            }]
        })

    assert resp.status_code == 400
    assert resp.json['errors']['data']['0']['taxi_id'] == ['Taxi is not free.']
