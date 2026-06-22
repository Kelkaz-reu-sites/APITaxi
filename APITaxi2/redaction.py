import json
import re


REDACTED = '[REDACTED]'

_SAFE_KEYS = {
    'apikey_belongs_to',
}

_SENSITIVE_KEYS = {
    'apikey',
    'api_key',
    'x_api_key',
    'x-api-key',
    'http_x_api_key',
    'authorization',
    'proxy_authorization',
    'proxy-authorization',
    'cookie',
    'cookies',
    'set_cookie',
    'set-cookie',
    'password',
    'operator_api_key',
    'access_token',
    'refresh_token',
    'client_secret',
}

_PERSONAL_KEYS = {
    'customer_phone_number',
    'taxi_phone_number',
    'phone_number',
    'phone_number_customer',
    'phone_number_technical',
    'customer_address',
    'customer_lat',
    'customer_lon',
    'initial_taxi_lat',
    'initial_taxi_lon',
    'lat',
    'lon',
}

_SENSITIVE_FRAGMENTS = (
    'password',
    'secret',
    'token',
)

_HEADER_RE = re.compile(
    r'(?im)^((?:x-api-key|authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)(.+)$'
)
_FIELD_RE = re.compile(
    r'''(?ix)
    (
        ["']?
        (?:apikey|api_key|x-api-key|x_api_key|http_x_api_key|authorization|
           password|operator_api_key|access_token|refresh_token|client_secret|
           customer_phone_number|taxi_phone_number|phone_number_customer|
           phone_number_technical|customer_address|customer_lat|customer_lon|
           initial_taxi_lat|initial_taxi_lon|lat|lon)
        ["']?
        \s*[:=]\s*
    )
    (
        ["'][^"']*["']|[^,&\s}\]]+
    )
    '''
)


def _normalise_key(key):
    return str(key).strip().lower().replace('-', '_')


def is_sensitive_key(key):
    normalised = _normalise_key(key)
    if normalised in _SAFE_KEYS:
        return False
    if normalised in _SENSITIVE_KEYS or normalised in _PERSONAL_KEYS:
        return True
    return any(fragment in normalised for fragment in _SENSITIVE_FRAGMENTS)


def redact(value):
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def redact_text(text):
    if not text:
        return text

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        redacted = _HEADER_RE.sub(r'\1%s' % REDACTED, text)
        return _FIELD_RE.sub(r'\1"%s"' % REDACTED, redacted)

    return json.dumps(redact(parsed), ensure_ascii=False, indent=2)


def redact_bytes(data):
    if not data:
        return data
    text = data.decode('utf8', errors='replace')
    return redact_text(text).encode('utf8')


def redact_log_value(value):
    if isinstance(value, bytes):
        return redact_bytes(value).decode('utf8', errors='replace')
    if isinstance(value, str):
        return redact_text(value)
    return redact(value)


def redact_sentry_event(event, hint=None):
    event = redact(event)

    request_info = event.get('request') if isinstance(event, dict) else None
    if isinstance(request_info, dict):
        for key in ('data', 'query_string', 'url'):
            if key in request_info:
                request_info[key] = redact_log_value(request_info[key])

    return event
