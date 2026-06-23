import importlib


def reload_default_settings(monkeypatch, **env):
    keys = {
        'SQLALCHEMY_DATABASE_URI',
        'POSTGRESQL_ADDON_DIRECT_URI',
        'REDIS_URL',
        'CELERY_BROKER_URL',
        'CELERY_RESULT_BACKEND',
        'REDIS_DIRECT_URI',
    }
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import APITaxi2.default_settings as default_settings
    return importlib.reload(default_settings)


def test_standard_database_and_redis_env_are_loaded_when_aliases_are_missing(monkeypatch):
    settings = reload_default_settings(
        monkeypatch,
        SQLALCHEMY_DATABASE_URI='postgresql://primary-db',
        REDIS_URL='redis://redis/0',
        CELERY_BROKER_URL='redis://redis/1',
        CELERY_RESULT_BACKEND='redis://redis/2',
    )

    assert settings.SQLALCHEMY_DATABASE_URI == 'postgresql://primary-db'
    assert settings.REDIS_URL == 'redis://redis/0'
    assert settings.CELERY_BROKER_URL == 'redis://redis/1'
    assert settings.CELERY_RESULT_BACKEND == 'redis://redis/2'


def test_cloud_aliases_override_standard_env_when_present(monkeypatch):
    settings = reload_default_settings(
        monkeypatch,
        SQLALCHEMY_DATABASE_URI='postgresql://primary-db',
        POSTGRESQL_ADDON_DIRECT_URI='postgresql://alias-db',
        REDIS_URL='redis://redis/0',
        CELERY_BROKER_URL='redis://redis/1',
        CELERY_RESULT_BACKEND='redis://redis/2',
        REDIS_DIRECT_URI='redis://alias-redis/0',
    )

    assert settings.SQLALCHEMY_DATABASE_URI == 'postgresql://alias-db'
    assert settings.REDIS_URL == 'redis://alias-redis/0'
    assert settings.CELERY_BROKER_URL == 'redis://alias-redis/0'
    assert settings.CELERY_RESULT_BACKEND == 'redis://alias-redis/0'
