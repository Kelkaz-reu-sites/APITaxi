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
