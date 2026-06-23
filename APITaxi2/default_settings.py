import os

from celery.schedules import crontab
from flask_security.utils import uia_username_mapper

from .rezo_taxi_config import DEFAULTS as REZO_TAXI_DEFAULTS


# SQLALCHEMY_ECHO = True

# Warning is displayed when SQLALCHEMY_TRACK_MODIFICATIONS is the default.
# Future SQLAlchemy version will set this value to False by default anyway.
SQLALCHEMY_TRACK_MODIFICATIONS = False

_ONE_MINUTE = 60
_ONE_HOUR = _ONE_MINUTE * 60
_ONE_DAY = _ONE_HOUR * 24
_SEVEN_DAYS = _ONE_DAY * 7

CELERY_BEAT_SCHEDULE = {
    'clean-geoindex-timestamps': {
        'task': 'clean_geoindex_timestamps',
        # Every 10 minutes
        'schedule': _ONE_MINUTE * 10
    },

    # Every minute, store the list of taxis available the last minute.
    'store-active-taxis-last-minute': {
        'task': 'store_active_taxis',
        'schedule': _ONE_MINUTE,
        'args': (1,),
    },

    # Every hour, store the list of taxis available the last hour.
    'store-active-taxis-last-hour': {
        'task': 'store_active_taxis',
        'schedule': _ONE_HOUR,
        'args': (60,)
    },

    # Every day, store the list of taxis available the last day.
    'store-active-taxis-last-day': {
        'task': 'store_active_taxis',
        'schedule': _ONE_DAY,
        'args': (1440,)
    },

    # Every day, store the list of taxis available the last 7 days.
    'store-active-taxis-last-seven-days': {
        'task': 'store_active_taxis',
        'schedule': _ONE_DAY,
        'args': (10080,)
    },

    # crontab

    'blur-geotaxi': {
        'task': 'blur_geotaxi',
        'schedule': crontab(hour=4, minute=0),
    },
    'blur-hails': {
        'task': 'blur_hails',
        'schedule': crontab(hour=4, minute=2),
    },
    'delete-old-hails': {
        'task': 'delete_old_hails',
        'schedule': crontab(hour=4, minute=4),
    },
    'delete-old-taxis': {
        'task': 'delete_old_taxis',
        'schedule': crontab(hour=4, minute=6),
    },
    'delete-old-orphans': {
        'task': 'delete_old_orphans',
        'schedule': crontab(hour=4, minute=8),
    },
    'compute-stats-hails': {
        'task': 'compute_stats_hails',
        'schedule': crontab(hour=4, minute=10),
    },
}

SQLALCHEMY_ENGINE_OPTIONS = {
    'pool_pre_ping': True
}

HTTP_CONNECT_TIMEOUT = 3.05
HTTP_READ_TIMEOUT = 10.0

# These values are used as (connect_timeout, read_timeout) with requests.
REVERSE_GEOCODE_HTTP_CONNECT_TIMEOUT = 2.0
REVERSE_GEOCODE_HTTP_READ_TIMEOUT = 4.0
OPERATOR_API_HTTP_CONNECT_TIMEOUT = 3.05
OPERATOR_API_HTTP_READ_TIMEOUT = 10.0
DOWNLOAD_HTTP_CONNECT_TIMEOUT = 5.0
DOWNLOAD_HTTP_READ_TIMEOUT = 120.0
CLIENT_HTTP_CONNECT_TIMEOUT = 3.05
CLIENT_HTTP_READ_TIMEOUT = 10.0

CORS_ALLOWED_ORIGINS = []
INTERNAL_HEALTHCHECK_KEY = None
OBSERVABILITY_REQUEST_LOGS_ENABLED = True
OBSERVABILITY_METRICS_ENABLED = True

REZO_TAXI_RADIUS_MIN_METERS = REZO_TAXI_DEFAULTS['REZO_TAXI_RADIUS_MIN_METERS']
REZO_TAXI_RADIUS_DEFAULT_METERS = REZO_TAXI_DEFAULTS['REZO_TAXI_RADIUS_DEFAULT_METERS']
REZO_TAXI_RADIUS_MAX_METERS = REZO_TAXI_DEFAULTS['REZO_TAXI_RADIUS_MAX_METERS']
REZO_TAXI_GPS_FRESHNESS_SECONDS = REZO_TAXI_DEFAULTS['REZO_TAXI_GPS_FRESHNESS_SECONDS']
REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS = REZO_TAXI_DEFAULTS[
    'REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS'
]
REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS = REZO_TAXI_DEFAULTS[
    'REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS'
]
REZO_TAXI_SEARCH_DISPLAY_LIMIT = REZO_TAXI_DEFAULTS['REZO_TAXI_SEARCH_DISPLAY_LIMIT']
REZO_TAXI_SEARCH_CANDIDATE_LIMIT = REZO_TAXI_DEFAULTS['REZO_TAXI_SEARCH_CANDIDATE_LIMIT']
REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS = REZO_TAXI_DEFAULTS['REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS']
REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS = REZO_TAXI_DEFAULTS['REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS']
REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS = REZO_TAXI_DEFAULTS[
    'REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS'
]
REZO_TAXI_PICKUP_TIMEOUT_SECONDS = REZO_TAXI_DEFAULTS['REZO_TAXI_PICKUP_TIMEOUT_SECONDS']
REZO_TAXI_RIDE_TIMEOUT_SECONDS = REZO_TAXI_DEFAULTS['REZO_TAXI_RIDE_TIMEOUT_SECONDS']
REZO_TAXI_INTERNAL_OPERATOR_HANDOFF_ENABLED = False
REZO_TAXI_INTERNAL_OPERATOR_EMAILS = []


# Flask-Security-Too introduced email validation, but we use the email field as a username
SECURITY_USER_IDENTITY_ATTRIBUTES = [
    {"email": {"mapper": uia_username_mapper, "case_insensitive": True}}
]


def parse_env_bool(value):
    """Convert the string value to a boolean."""
    if value is None:
        return None
    elif value.lower() in ('yes', 'true', '1', 't'):
        return True
    elif value.lower() in ('no', 'false', '0', 'f', ''):
        return False
    raise ValueError(f'Invalid boolean value "{value}" in environment')


def parse_env_list(type_):
    def _parse_env_list(value):
        return [type_(v.strip()) for v in value.split(',')]
    return _parse_env_list


# The following code reads environment to create settings.
#
# The first entry of the list is the name of the setting to create, and also
# the name of the environment variable to get the value from.
#
# The second entry is an optional alternative name. It is used to deploy on
# clevercloud, where it is not possible to rename variables exposed by addons.
#
# The "algorithm" works as follow, for example for SQLALCHEMY_DATABASE_URI:
# - if the environment variable SQLALCHEMY_DATABASE_URI is set, create a global
#   variable named SQLALCHEMY_DATABASE_URI with it's value.
# - otherwise, create a global variable SQLALCHEMY_DATABASE_URI with the value of
#   the environment variable POSTGRESQL_ADDON_URI.
# - if both the environment variable and the alternative name exist,
#   the alternative name has priority. So REDIS_URL will be overwritten by
#   REDIS_DIRECT_URI if defined (for cloud hosting).
#
for _env_var, _alt_name, _env_type in (
    ('DEBUG', None, parse_env_bool),
    ('SERVER_NAME', None, str),
    ('INTEGRATION_ENABLED', None, parse_env_bool),
    ('INTEGRATION_ACCOUNT_EMAIL', None, str),
    ('GEOTAXI_HOST', None, str),
    ('GEOTAXI_PORT', None, int),
    ('SECRET_KEY', None, str),
    ('SQLALCHEMY_DATABASE_URI', 'POSTGRESQL_ADDON_DIRECT_URI', str),
    ('REDIS_URL', 'REDIS_DIRECT_URI', str),
    ('SECURITY_PASSWORD_SALT', None, str),
    ('CELERY_BROKER_URL', 'REDIS_DIRECT_URI', str),
    ('CELERY_RESULT_BACKEND', 'REDIS_DIRECT_URI', str),
    ('SENTRY_DSN', None, str),
    ('SENTRY_SAMPLE_RATE', None, float),
    ('HTTP_CONNECT_TIMEOUT', None, float),
    ('HTTP_READ_TIMEOUT', None, float),
    ('REVERSE_GEOCODE_HTTP_CONNECT_TIMEOUT', None, float),
    ('REVERSE_GEOCODE_HTTP_READ_TIMEOUT', None, float),
    ('OPERATOR_API_HTTP_CONNECT_TIMEOUT', None, float),
    ('OPERATOR_API_HTTP_READ_TIMEOUT', None, float),
    ('DOWNLOAD_HTTP_CONNECT_TIMEOUT', None, float),
    ('DOWNLOAD_HTTP_READ_TIMEOUT', None, float),
    ('CLIENT_HTTP_CONNECT_TIMEOUT', None, float),
    ('CLIENT_HTTP_READ_TIMEOUT', None, float),
    ('CORS_ALLOWED_ORIGINS', None, parse_env_list(str)),
    ('INTERNAL_HEALTHCHECK_KEY', None, str),
    ('OBSERVABILITY_REQUEST_LOGS_ENABLED', None, parse_env_bool),
    ('OBSERVABILITY_METRICS_ENABLED', None, parse_env_bool),
    ('REZO_TAXI_RADIUS_MIN_METERS', None, int),
    ('REZO_TAXI_RADIUS_DEFAULT_METERS', None, int),
    ('REZO_TAXI_RADIUS_MAX_METERS', None, int),
    ('REZO_TAXI_GPS_FRESHNESS_SECONDS', None, int),
    ('REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS', None, int),
    ('REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS', None, int),
    ('REZO_TAXI_SEARCH_DISPLAY_LIMIT', None, int),
    ('REZO_TAXI_SEARCH_CANDIDATE_LIMIT', None, int),
    ('REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS', None, int),
    ('REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS', None, int),
    ('REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS', None, int),
    ('REZO_TAXI_PICKUP_TIMEOUT_SECONDS', None, int),
    ('REZO_TAXI_RIDE_TIMEOUT_SECONDS', None, int),
    ('REZO_TAXI_INTERNAL_OPERATOR_HANDOFF_ENABLED', None, parse_env_bool),
    ('REZO_TAXI_INTERNAL_OPERATOR_EMAILS', None, parse_env_list(str)),
    ('CONSOLE_URL', None, str),
    ('SWAGGER_URL', None, str),
    ('NEUTRAL_OPERATOR', None, parse_env_bool),
    ('FAKE_TAXI_ID', None, parse_env_bool),
    ('HAIL_TAXI_VEHICLE_DETAILS', None, parse_env_list(int)),
):
    _val = os.getenv(_env_var)
    if _alt_name and os.getenv(_alt_name):
        _val = os.getenv(_alt_name)
    if not _val:
        continue

    globals()[_env_var] = _env_type(_val)
