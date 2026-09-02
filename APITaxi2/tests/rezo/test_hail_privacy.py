"""Rezo D10 and D11: what the customer may see of the taxi, and when.

Upstream le.taxi exposes the taxi position for seven hail statuses, from
`received` — before the customer confirmed anything — up to and including
`customer_on_board`. Rezo narrows that to the approach alone.

The approach distance is deliberately kept throughout: it is a circle rather
than a point, the customer already saw it when choosing this taxi, and ADR 0023
requires it so that a distant taxi is not presented as equivalent to a close
one.
"""
import time

from APITaxi_models2 import db
from APITaxi_models2.unittest.factories import HailFactory, TaxiFactory

def _hail_with_position(app, moteur, operateur, status):
    taxi = TaxiFactory(added_by=operateur.user)
    hail = HailFactory(
        added_by=moteur.user,
        operateur=operateur.user,
        taxi=taxi,
        status=status,
    )
    app.redis.hset(
        'taxi:%s' % taxi.id,
        operateur.user.email,
        '%s 48.84 2.35 free phone 2' % int(time.time()),
    )
    return hail


def test_position_is_hidden_until_the_customer_confirms(app, moteur, operateur):
    """Rezo D10: the taxi's exact position is the driver's, not the customer's.

    It is revealed only once the customer has committed to the ride, and only
    for the approach.
    """
    for status in ('received', 'received_by_taxi', 'accepted_by_taxi'):
        hail = _hail_with_position(app, moteur, operateur, status)

        resp = moteur.client.get('/hails/%s' % hail.id)

        assert resp.status_code == 200
        assert resp.json['data'][0]['taxi']['position'] == {'lon': None, 'lat': None}, status
        db.session.rollback()


def test_position_is_shown_during_the_approach(app, moteur, operateur):
    hail = _hail_with_position(app, moteur, operateur, 'accepted_by_customer')

    resp = moteur.client.get('/hails/%s' % hail.id)

    assert resp.status_code == 200
    assert resp.json['data'][0]['taxi']['position'] == {'lon': 2.35, 'lat': 48.84}
    db.session.rollback()


def test_position_sharing_stops_at_pickup(app, moteur, operateur):
    """Rezo D11: Rezo does not follow the commercial ride."""
    hail = _hail_with_position(app, moteur, operateur, 'customer_on_board')

    resp = moteur.client.get('/hails/%s' % hail.id)

    assert resp.status_code == 200
    assert resp.json['data'][0]['taxi']['position'] == {'lon': None, 'lat': None}
    db.session.rollback()


def test_approach_distance_stays_visible_before_confirmation(app, moteur, operateur):
    """ADR 0023 requires an approach distance and a waiting estimate.

    A distance is a circle, not a point: it does not locate the driver, and the
    customer already saw it when picking this taxi. Without it they would commit
    to a ride within 60 seconds without knowing whether it arrives in 3 or 25
    minutes.
    """
    hail = _hail_with_position(app, moteur, operateur, 'received_by_taxi')

    resp = moteur.client.get('/hails/%s' % hail.id)

    assert resp.status_code == 200
    assert resp.json['data'][0]['taxi']['crowfly_distance'] is not None
