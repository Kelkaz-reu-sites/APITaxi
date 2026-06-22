import json
from pathlib import Path

from flask import request

import APITaxi2
from APITaxi2 import activity_logs, redis_backend
from APITaxi2.redaction import REDACTED, redact_log_value, redact_sentry_event
from APITaxi_models2.activity_logs import ActivityLog


def test_redact_log_value_handles_json_strings():
    payload = json.dumps({
        'apikey': 'SECRET_API_KEY',
        'apikey_belongs_to': '42',
        'data': [{
            'password': 'SECRET_PASSWORD',
            'customer_phone_number': '+262692000000',
            'customer_address': '1 rue des Secrets',
            'customer_lat': -21.1151,
            'customer_lon': 55.5364,
        }],
    })

    redacted = redact_log_value(payload)

    assert 'SECRET_API_KEY' not in redacted
    assert 'SECRET_PASSWORD' not in redacted
    assert '+262692000000' not in redacted
    assert '1 rue des Secrets' not in redacted

    parsed = json.loads(redacted)
    assert parsed['apikey'] == REDACTED
    assert parsed['apikey_belongs_to'] == '42'
    assert parsed['data'][0]['password'] == REDACTED
    assert parsed['data'][0]['customer_phone_number'] == REDACTED
    assert parsed['data'][0]['customer_address'] == REDACTED
    assert parsed['data'][0]['customer_lat'] == REDACTED
    assert parsed['data'][0]['customer_lon'] == REDACTED


def test_redis_hail_logs_redact_sensitive_payloads(app, moteur):
    redis_backend.log_hail(
        'hail_id',
        'POST',
        {
            'data': [{
                'apikey': 'SECRET_API_KEY',
                'password': 'SECRET_PASSWORD',
                'customer_phone_number': '+262692000000',
                'customer_address': '1 rue des Secrets',
            }],
        },
        'received',
        request_user=moteur.user,
        response_payload=json.dumps({
            'data': [{
                'taxi_phone_number': '+262693000000',
                'operator_api_key': 'SECRET_OPERATOR_KEY',
            }],
        }),
        response_status_code=200,
        hail_final_status='failure'
    )

    raw = app.redis.zrange('hail:hail_id', 0, -1)[0].decode('utf8')

    assert 'SECRET_API_KEY' not in raw
    assert 'SECRET_PASSWORD' not in raw
    assert '+262692000000' not in raw
    assert '1 rue des Secrets' not in raw
    assert '+262693000000' not in raw
    assert 'SECRET_OPERATOR_KEY' not in raw
    assert REDACTED in raw

    entry = json.loads(raw)
    returned = json.loads(entry['return'])
    assert entry['payload']['data'][0]['apikey'] == REDACTED
    assert entry['payload']['data'][0]['password'] == REDACTED
    assert returned['data'][0]['taxi_phone_number'] == REDACTED
    assert returned['data'][0]['operator_api_key'] == REDACTED


def test_activity_logs_redact_sensitive_extra(app, moteur):
    activity_logs.log_user_auth_apikey(
        moteur.user.id,
        method='GET',
        location='/users',
        apikey='SECRET_API_KEY',
        password='SECRET_PASSWORD',
        customer_phone_number='+262692000000',
        apikey_belongs_to=str(moteur.user.id),
    )

    log = ActivityLog.query.one()

    assert log.extra['apikey'] == REDACTED
    assert log.extra['password'] == REDACTED
    assert log.extra['customer_phone_number'] == REDACTED
    assert log.extra['apikey_belongs_to'] == str(moteur.user.id)


def test_sentry_events_are_redacted():
    event = {
        'request': {
            'headers': {
                'X-Api-Key': 'SECRET_API_KEY',
                'Authorization': 'Bearer SECRET_TOKEN',
            },
            'cookies': {
                'session': 'SECRET_SESSION',
            },
            'data': json.dumps({
                'password': 'SECRET_PASSWORD',
                'operator_api_key': 'SECRET_OPERATOR_KEY',
            }),
            'query_string': 'apikey=SECRET_QUERY_KEY&regular=ok',
        },
        'extra': {
            'customer_phone_number': '+262692000000',
        },
    }

    redacted = json.dumps(redact_sentry_event(event))

    assert 'SECRET_API_KEY' not in redacted
    assert 'Bearer SECRET_TOKEN' not in redacted
    assert 'SECRET_SESSION' not in redacted
    assert 'SECRET_PASSWORD' not in redacted
    assert 'SECRET_OPERATOR_KEY' not in redacted
    assert 'SECRET_QUERY_KEY' not in redacted
    assert '+262692000000' not in redacted
    assert REDACTED in redacted


def test_debug_requests_redact_headers_and_json_bodies(app, monkeypatch, capfd):
    monkeypatch.setenv('DEBUG_REQUESTS', '1')
    debug_app = APITaxi2.create_app()

    @debug_app.route('/redaction_echo', methods=['POST'])
    def redaction_echo():
        return request.get_json()

    with debug_app.test_client() as client:
        response = client.post(
            '/redaction_echo',
            json={
                'password': 'SECRET_PASSWORD',
                'operator_api_key': 'SECRET_OPERATOR_KEY',
                'customer_phone_number': '+262692000000',
            },
            headers={
                'X-Api-Key': 'SECRET_API_KEY',
                'Authorization': 'Bearer SECRET_TOKEN',
            },
        )

    assert response.status_code == 200
    captured = capfd.readouterr().err

    assert 'SECRET_API_KEY' not in captured
    assert 'Bearer SECRET_TOKEN' not in captured
    assert 'SECRET_PASSWORD' not in captured
    assert 'SECRET_OPERATOR_KEY' not in captured
    assert '+262692000000' not in captured
    assert REDACTED in captured


def test_uwsgi_log_format_does_not_log_api_key_header():
    content = Path('deploy/conf/uwsgi.ini').read_text()

    assert 'HTTP_X_API_KEY' not in content
