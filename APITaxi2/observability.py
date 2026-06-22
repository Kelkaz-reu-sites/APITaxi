import json
import re
import time
import uuid
from collections import defaultdict
from threading import Lock

from flask import current_app, g, has_request_context, request

from .redaction import redact


CORRELATION_ID_HEADER = 'X-Correlation-ID'
REQUEST_ID_HEADER = 'X-Request-ID'

_SAFE_CORRELATION_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')
_METRICS_LOCK = Lock()
_HTTP_REQUESTS = defaultdict(int)
_HTTP_DURATIONS_MS = defaultdict(float)


def _new_correlation_id():
    return uuid.uuid4().hex


def normalize_correlation_id(value):
    if value and _SAFE_CORRELATION_ID_RE.match(value):
        return value
    return _new_correlation_id()


def get_correlation_id():
    if has_request_context():
        return getattr(g, 'correlation_id', None)
    return None


def _request_endpoint():
    return request.endpoint or 'unknown'


def _request_started_at():
    return getattr(g, 'request_started_at', None)


def _duration_ms():
    started_at = _request_started_at()
    if started_at is None:
        return None
    return round((time.perf_counter() - started_at) * 1000, 3)


def _metric_key(response):
    return (
        request.method,
        _request_endpoint(),
        str(response.status_code),
    )


def _record_http_metrics(response, duration_ms):
    if not current_app.config.get('OBSERVABILITY_METRICS_ENABLED'):
        return

    key = _metric_key(response)
    with _METRICS_LOCK:
        _HTTP_REQUESTS[key] += 1
        if duration_ms is not None:
            _HTTP_DURATIONS_MS[key] += duration_ms


def _log_http_request(app, response, duration_ms):
    if not app.config.get('OBSERVABILITY_REQUEST_LOGS_ENABLED'):
        return

    log_event(
        app.logger,
        'info',
        'http_request',
        correlation_id=get_correlation_id(),
        method=request.method,
        path=request.path,
        endpoint=_request_endpoint(),
        status_code=response.status_code,
        duration_ms=duration_ms,
    )


def log_event(logger, level, event, **fields):
    payload = {'event': event}
    correlation_id = fields.pop('correlation_id', None) or get_correlation_id()
    if correlation_id:
        payload['correlation_id'] = correlation_id
    payload.update(fields)

    log_method = getattr(logger, level)
    log_method(json.dumps(redact(payload), sort_keys=True))


def configure_observability(app):
    @app.before_request
    def set_correlation_id():
        g.request_started_at = time.perf_counter()
        g.correlation_id = normalize_correlation_id(
            request.headers.get(CORRELATION_ID_HEADER)
            or request.headers.get(REQUEST_ID_HEADER)
        )

    @app.after_request
    def observe_request(response):
        response.headers[CORRELATION_ID_HEADER] = get_correlation_id()
        duration_ms = _duration_ms()
        _record_http_metrics(response, duration_ms)
        _log_http_request(app, response, duration_ms)
        return response


def reset_metrics():
    with _METRICS_LOCK:
        _HTTP_REQUESTS.clear()
        _HTTP_DURATIONS_MS.clear()


def _escape_label(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


def _format_labels(labels):
    formatted = ','.join(
        '%s="%s"' % (key, _escape_label(value))
        for key, value in sorted(labels.items())
    )
    return '{%s}' % formatted


def render_metrics():
    lines = [
        '# HELP apitaxi_http_requests_total HTTP requests served by Rezo Taxi Core.',
        '# TYPE apitaxi_http_requests_total counter',
    ]

    with _METRICS_LOCK:
        request_items = list(_HTTP_REQUESTS.items())
        duration_items = list(_HTTP_DURATIONS_MS.items())

    for (method, endpoint, status), count in request_items:
        labels = _format_labels({
            'method': method,
            'endpoint': endpoint,
            'status': status,
        })
        lines.append('apitaxi_http_requests_total%s %s' % (labels, count))

    lines.extend([
        '# HELP apitaxi_http_request_duration_ms_sum Total HTTP request duration in milliseconds.',
        '# TYPE apitaxi_http_request_duration_ms_sum counter',
    ])

    for (method, endpoint, status), duration_sum in duration_items:
        labels = _format_labels({
            'method': method,
            'endpoint': endpoint,
            'status': status,
        })
        lines.append(
            'apitaxi_http_request_duration_ms_sum%s %.3f' % (
                labels,
                duration_sum,
            )
        )

    lines.extend([
        '# HELP apitaxi_http_request_duration_ms_count Count of HTTP request durations.',
        '# TYPE apitaxi_http_request_duration_ms_count counter',
    ])

    for (method, endpoint, status), count in request_items:
        labels = _format_labels({
            'method': method,
            'endpoint': endpoint,
            'status': status,
        })
        lines.append('apitaxi_http_request_duration_ms_count%s %s' % (labels, count))

    return '\n'.join(lines) + '\n'
