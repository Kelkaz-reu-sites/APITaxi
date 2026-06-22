import pytest
from flask import Flask

import APITaxi2


def _preflight(client, origin):
    return client.options('/swagger.json', headers={
        'Origin': origin,
        'Access-Control-Request-Method': 'GET',
    })


def test_cors_disabled_by_default(anonymous):
    resp = _preflight(anonymous.client, 'https://rezo.re')

    assert resp.status_code == 200
    assert 'Access-Control-Allow-Origin' not in resp.headers


def test_cors_allows_configured_origin(app, anonymous):
    app.config['CORS_ALLOWED_ORIGINS'] = ['https://rezo.re']
    APITaxi2.configure_cors(app)

    resp = _preflight(anonymous.client, 'https://rezo.re')
    assert resp.status_code == 200
    assert resp.headers['Access-Control-Allow-Origin'] == 'https://rezo.re'

    resp = _preflight(anonymous.client, 'https://example.invalid')
    assert resp.status_code == 200
    assert 'Access-Control-Allow-Origin' not in resp.headers


def test_cors_wildcard_rejected_outside_debug():
    app = Flask(__name__)
    app.debug = False
    app.config['CORS_ALLOWED_ORIGINS'] = ['*']

    with pytest.raises(RuntimeError, match='wildcard'):
        APITaxi2.configure_cors(app)
