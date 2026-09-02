import pytest
from marshmallow import ValidationError

from APITaxi2 import rezo_taxi_config


def test_rezo_taxi_config_defaults(app):
    assert app.config['REZO_TAXI_RADIUS_DEFAULT_METERS'] == 5000
    assert app.config['REZO_TAXI_RADIUS_MAX_METERS'] == 30000
    assert app.config['REZO_TAXI_GPS_FRESHNESS_SECONDS'] == 180
    assert app.config['REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS'] == 120
    assert app.config['REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS'] == 60
    assert app.config['REZO_TAXI_SEARCH_DISPLAY_LIMIT'] == 8
    assert app.config['REZO_TAXI_SEARCH_CANDIDATE_LIMIT'] == 20
    assert app.config['REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS'] == 900


def test_pickup_timeout_allows_one_hour_approach(app):
    """Rezo D21: an approach may legitimately last up to one hour.

    With a 30 km maximum radius, the island relief and RN1 congestion, a long
    approach is not an anomaly. A lingering hail costs the service less than a
    ride cancelled while the taxi is still driving to the customer.
    """
    assert app.config['REZO_TAXI_PICKUP_TIMEOUT_SECONDS'] == 60 * 60


def test_rezo_taxi_config_rejects_invalid_radius_bounds(app):
    app.config['REZO_TAXI_RADIUS_DEFAULT_METERS'] = 400

    with pytest.raises(RuntimeError, match='radius settings'):
        rezo_taxi_config.validate_app_config(app)


def test_rezo_taxi_config_rejects_invalid_display_limit(app):
    app.config['REZO_TAXI_SEARCH_DISPLAY_LIMIT'] = 21

    with pytest.raises(RuntimeError, match='DISPLAY_LIMIT'):
        rezo_taxi_config.validate_app_config(app)


def test_rezo_taxi_radius_validation_uses_config(app):
    rezo_taxi_config.validate_radius(500)
    rezo_taxi_config.validate_radius(5000)
    rezo_taxi_config.validate_radius(30000)

    with pytest.raises(ValidationError):
        rezo_taxi_config.validate_radius(499)

    with pytest.raises(ValidationError):
        rezo_taxi_config.validate_radius(30001)
