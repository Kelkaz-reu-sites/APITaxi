# Le.taxi API

API behind [le.taxi](https://le.taxi/).

## Installation

This project has several dependencies:

* PostgreSQL
* celery for asynchronous tasks
* redis as celery backend
* redis as database where taxis' locations are stored

To setup the API locally, use [APITaxi_devel](https://github.com/openmaraude/APITaxi_devel).

## Rezo Taxi Core local Docker Compose

This fork is used by REZO as the internal Rezo Taxi Core service. The local
Compose stack keeps the API, Celery worker, Celery beat, PostgreSQL/TimescaleDB
with PostGIS, and Redis separate.

Start dependencies and apply migrations:

```bash
docker compose up -d taxi-postgres taxi-redis
docker compose --profile tools run --rm taxi-migrate
```

Start the API and workers:

```bash
docker compose up -d taxi-api taxi-worker taxi-beat
```

Run the existing test suite in the Docker test environment:

```bash
docker compose --profile test run --rm taxi-test
```

The `taxi-test` service uses the Dockerfile `test-devenv` target. Tests create
their own temporary PostgreSQL database with `testing.postgresql`, so they need
PostgreSQL, PostGIS and TimescaleDB binaries inside the test container.

The first application startup can take a few minutes because the development
entrypoint creates and populates the shared `/venv` volume. API, worker and beat
serialize this installation with a lock to avoid concurrent `pip install`
races.

The local PostgreSQL service initializes the `postgis`, `pgcrypto` and
`timescaledb` extensions through `devenv/postgres-init/999_rezo_extensions.sql`.

Local ports:

* API: `http://127.0.0.1:5000`
* PostgreSQL: `127.0.0.1:15432`
* Redis: `127.0.0.1:16379`

These ports are bound to `127.0.0.1` only. They are intended for local
development and should not be exposed on the LAN or public networks.

## Rezo internal service boundary

Rezo Taxi Core must stay behind the Rezo backend. Browsers and mobile clients
must call the public Rezo facade, for example `/api/v1/taxi/*`; Rezo then calls
this Flask service over a private network with server-side credentials,
timeouts and idempotency keys.

By default, APITaxi does not enable CORS. Set `CORS_ALLOWED_ORIGINS` only for a
known development or internal origin. A wildcard origin is rejected outside Flask
debug mode to avoid exposing the internal API to arbitrary browser clients.

The internal healthcheck is available at `/internal/health` and requires the
`X-Internal-Key` header to match `INTERNAL_HEALTHCHECK_KEY`. The local Docker
Compose healthcheck uses this route with the non-secret development value from
`devenv/settings.py`.

Production deployments must keep the API port on an internal network only. Do
not publish this service directly on the public web; publish only the Rezo
facade and keep the fork AGPL-3.0 source available according to the project
licence strategy documented in the Rezo repository.

## Unittests

On push, tests are automatically run by cirleci. To run tests locally, assuming you are using APITaxi_devel:

```bash
$> docker-compose exec api bash
api@f4fd953d0667:/git/APITaxi: sudo -E /venv/bin/pip install -ve .[tests]
api@f4fd953d0667:/git/APITaxi: pytest -v -x -s
```

Before tests are executed, a PostgreSQL database is created and alembic migrations are applied. To improve speed, database is kept for subsequent runs in `/tmp/tests_<hash>`. If the database is corrupted because the previous tests run didn't end properly, remove `/tmp/tests_<hash>` and run tests again.

Example of error requiring to remove the database manually:

```
RuntimeError: *** failed to launch Postgresql ***
2020-10-15 08:49:33.269 UTC [1080] FATAL:  lock file "postmaster.pid" already exists
2020-10-15 08:49:33.269 UTC [1080] HINT:  Is another postmaster (PID 1028) running in data directory "/tmp/tests_fa54bbeddf53eb368fd05b9ca121dbc5/data"?
```

## Migrations

Migrations are versioned with alembic. To run migrations locally using the "api" container from APITaxi_devel, run the following commands:

```
# Connect to api container
$> docker-compose exec api bash

# Change directory to migrations directory
$> cd APITaxi_models2

# Run alembic commands: view current migration
$> alembic current

# Create a new revision file
$> alembic revision --autogenerate -m 'New revision'

# Apply migrations
$> alembic upgrade head
```

To apply migrations to production, connect with ssh to the PostgreSQL master server (taxis01.api.taxi or dev01.api.taxi as specified by [APITaxi_deploy](https://github.com/openmaraude/APITaxi_deploy)), then:

```
# Connect to api container
$> docker exec -ti api_taxi bash

# Change directory to migrations directory
$> cd APITaxi_models2

# Run alembic commands
$> alembic current
$> alembic upgrade head
```

## Production

To deploy to production, setup the following remote and push on the master branches.

```
git remote add clever-dev git+ssh://git@push-n2-par-clevercloud-customers.services.clever-cloud.com/app_89d4b1b8-08db-4e3c-9bb6-07fd2e48ff71.git
git remote add clever-prod git+ssh://git@push-n2-par-clevercloud-customers.services.clever-cloud.com/app_75718459-4a50-4386-a57a-a4e6a841e962.git
```

To connect to containers, install [CleverCloud CLI](https://www.clever-cloud.com/doc/reference/clever-tools/getting_started/) and run the following commands:

```
clever link app_75718459-4a50-4386-a57a-a4e6a841e962
clever link app_89d4b1b8-08db-4e3c-9bb6-07fd2e48ff71

# Outputs "dev-api" and "prod-api"
clever applications

clever ssh -a dev-api
clever ssh -a prod-api
```

Migrations are a bit tricky for some reason, you have to point to the Python environment:

```
PYTHONHOME=.. alembic upgrade head
```
