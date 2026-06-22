import json
import re
from unittest import mock

from APITaxi2 import observability


CORRELATION_ID_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,128}$')


def test_correlation_id_is_returned_when_provided(anonymous):
    resp = anonymous.client.get('/internal/health', headers={
        'X-Internal-Key': 'test-internal-key',
        'X-Correlation-ID': 'rezo-test-123',
    })

    assert resp.status_code == 200
    assert resp.headers['X-Correlation-ID'] == 'rezo-test-123'


def test_correlation_id_is_generated_when_missing(anonymous):
    resp = anonymous.client.get('/internal/health', headers={
        'X-Internal-Key': 'test-internal-key',
    })

    assert resp.status_code == 200
    assert CORRELATION_ID_RE.match(resp.headers['X-Correlation-ID'])


def test_invalid_correlation_id_is_replaced(anonymous):
    resp = anonymous.client.get('/internal/health', headers={
        'X-Internal-Key': 'test-internal-key',
        'X-Correlation-ID': 'bad value!',
    })

    assert resp.status_code == 200
    assert resp.headers['X-Correlation-ID'] != 'bad value!'
    assert CORRELATION_ID_RE.match(resp.headers['X-Correlation-ID'])


def test_request_logs_are_json_and_do_not_include_request_secrets(app, anonymous):
    app.config['OBSERVABILITY_REQUEST_LOGS_ENABLED'] = True

    with mock.patch.object(app.logger, 'info') as mocked_info:
        resp = anonymous.client.post('/internal/auth', json={
            'data': [{
                'email': 'driver@example.test',
                'apikey': 'SECRET_API_KEY',
            }],
        }, headers={
            'X-Correlation-ID': 'rezo-log-123',
        })

    assert resp.status_code == 401
    logged_messages = [call.args[0] for call in mocked_info.call_args_list]
    serialized = '\n'.join(logged_messages)

    assert 'SECRET_API_KEY' not in serialized

    event = json.loads(logged_messages[-1])
    assert event['event'] == 'http_request'
    assert event['correlation_id'] == 'rezo-log-123'
    assert event['endpoint'] == 'internal_auth.auth'
    assert event['status_code'] == 401


def test_request_logs_can_be_disabled(app, anonymous):
    app.config['OBSERVABILITY_REQUEST_LOGS_ENABLED'] = False

    with mock.patch.object(app.logger, 'info') as mocked_info:
        resp = anonymous.client.get('/internal/health', headers={
            'X-Internal-Key': 'test-internal-key',
        })

    assert resp.status_code == 200
    mocked_info.assert_not_called()


def test_log_event_redacts_structured_fields():
    logger = mock.Mock()

    observability.log_event(
        logger,
        'warning',
        'operator_api_request_failed',
        hail_id='hail-123',
        operator_api_key='SECRET_OPERATOR_KEY',
        customer_phone_number='+262692000000',
    )

    payload = logger.warning.call_args.args[0]
    assert 'SECRET_OPERATOR_KEY' not in payload
    assert '+262692000000' not in payload

    event = json.loads(payload)
    assert event['event'] == 'operator_api_request_failed'
    assert event['hail_id'] == 'hail-123'
    assert event['operator_api_key'] == '[REDACTED]'
    assert event['customer_phone_number'] == '[REDACTED]'


def test_internal_metrics_requires_internal_key(anonymous):
    resp = anonymous.client.get('/internal/metrics')

    assert resp.status_code == 401


def test_internal_metrics_exposes_http_counters(app, anonymous):
    observability.reset_metrics()

    health_resp = anonymous.client.get('/internal/health', headers={
        'X-Internal-Key': 'test-internal-key',
    })
    metrics_resp = anonymous.client.get('/internal/metrics', headers={
        'X-Internal-Key': 'test-internal-key',
    })

    assert health_resp.status_code == 200
    assert metrics_resp.status_code == 200
    assert metrics_resp.mimetype == 'text/plain'

    metrics = metrics_resp.text
    assert 'apitaxi_http_requests_total' in metrics
    assert 'endpoint="internal_health.health"' in metrics
    assert 'status="200"' in metrics


def test_internal_metrics_can_be_unconfigured(app, anonymous):
    app.config['INTERNAL_HEALTHCHECK_KEY'] = None

    resp = anonymous.client.get('/internal/metrics', headers={
        'X-Internal-Key': 'test-internal-key',
    })

    assert resp.status_code == 503
    assert resp.json == {'status': 'unconfigured'}
