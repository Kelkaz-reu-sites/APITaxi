"""Characterisation of argon2 password hashing, guarding dependency upgrades.

Production hashes service-account passwords with argon2. The Rezo facade
authenticates against the core with one of them, so a hashing regression takes
Taxi Live down entirely rather than merely breaking a login screen.

The shared `app` fixture configures `plaintext` for speed, so **no existing
test exercises the scheme production actually runs**. That gap is why the
Flask-Security 5.7.0 upgrade — which replaced passlib with libpass and changed
bcrypt input truncation — could not be assessed from a green suite alone.

These tests build a second application configured like production and pin the
behaviour an upgrade must preserve: a digest produced before the bump has to
keep verifying after it. The scheme cannot be switched on the shared fixture
after the fact — the crypt context is built during initialisation, and a later
override silently produces digests that never verify.
"""
import os

import pytest
from flask_security.utils import hash_password, verify_password

import APITaxi2


PASSWORD = 'rezo-migration-probe'
SALT = 'rezo-characterisation-salt'


@pytest.fixture
def argon2_app(tmp_path, postgresql, postgresql_empty, redis_server):
    settings_file = tmp_path / 'argon2_settings.py'
    settings_file.write_text('''
SQLALCHEMY_DATABASE_URI = '%(database)s'
SECRET_KEY = 's3cr3t'
REDIS_URL = '%(redis)s'
SECURITY_PASSWORD_HASH = 'argon2'
SECURITY_PASSWORD_SALT = '%(salt)s'
DEBUG = True
TESTING = True
CONSOLE_URL = 'http://console'
NEUTRAL_OPERATOR = True
INTERNAL_HEALTHCHECK_KEY = 'test-internal-key'
OBSERVABILITY_REQUEST_LOGS_ENABLED = False
''' % {
        'database': postgresql.sqlalchemy_url(),
        'redis': 'unix://%s' % redis_server,
        'salt': SALT,
    })
    os.environ['APITAXI_CONFIG_FILE'] = settings_file.as_posix()

    app = APITaxi2.create_app()
    with app.app_context():
        yield app
        postgresql_empty()
        app.redis.flushall()


def test_production_scheme_produces_an_argon2_digest(argon2_app):
    digest = hash_password(PASSWORD)

    assert digest.startswith('$argon2'), digest
    assert PASSWORD not in digest


def test_argon2_digest_verifies(argon2_app):
    digest = hash_password(PASSWORD)

    assert verify_password(PASSWORD, digest)


def test_argon2_rejects_a_wrong_password(argon2_app):
    digest = hash_password(PASSWORD)

    assert not verify_password(PASSWORD + 'x', digest)
