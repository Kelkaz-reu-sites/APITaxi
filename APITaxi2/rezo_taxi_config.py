from flask import current_app, has_app_context
from marshmallow import ValidationError


DEFAULTS = {
    'REZO_TAXI_RADIUS_MIN_METERS': 500,
    'REZO_TAXI_RADIUS_DEFAULT_METERS': 5000,
    'REZO_TAXI_RADIUS_MAX_METERS': 30000,
    'REZO_TAXI_GPS_FRESHNESS_SECONDS': 180,
    'REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS': 120,
    'REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS': 60,
    'REZO_TAXI_SEARCH_DISPLAY_LIMIT': 8,
    'REZO_TAXI_SEARCH_CANDIDATE_LIMIT': 20,
    'REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS': 15 * 60,
    'REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS': 10,
    'REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS': 10,
    'REZO_TAXI_PICKUP_TIMEOUT_SECONDS': 30 * 60,
    'REZO_TAXI_RIDE_TIMEOUT_SECONDS': 2 * 60 * 60,
}


def get(name):
    if has_app_context():
        return current_app.config[name]
    return DEFAULTS[name]


def radius_or_default(radius):
    if radius is None:
        return get('REZO_TAXI_RADIUS_DEFAULT_METERS')
    return radius


def validate_radius(radius):
    if radius is None:
        return

    min_radius = get('REZO_TAXI_RADIUS_MIN_METERS')
    max_radius = get('REZO_TAXI_RADIUS_MAX_METERS')
    if radius < min_radius or radius > max_radius:
        raise ValidationError(
            f'Must be greater than or equal to {min_radius} and less than or equal to {max_radius}.'
        )


def _positive_int(app, name):
    value = app.config.get(name)
    if not isinstance(value, int) or value <= 0:
        raise RuntimeError(f'{name} must be a positive integer')
    return value


def validate_app_config(app):
    values = {
        name: _positive_int(app, name)
        for name in DEFAULTS
    }

    if not (
        values['REZO_TAXI_RADIUS_MIN_METERS']
        <= values['REZO_TAXI_RADIUS_DEFAULT_METERS']
        <= values['REZO_TAXI_RADIUS_MAX_METERS']
    ):
        raise RuntimeError(
            'REZO_TAXI radius settings must satisfy min <= default <= max'
        )

    if values['REZO_TAXI_SEARCH_DISPLAY_LIMIT'] > values['REZO_TAXI_SEARCH_CANDIDATE_LIMIT']:
        raise RuntimeError(
            'REZO_TAXI_SEARCH_DISPLAY_LIMIT must be less than or equal to '
            'REZO_TAXI_SEARCH_CANDIDATE_LIMIT'
        )
