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
Compose stack keeps the Gunicorn API, Celery worker, Celery beat,
PostgreSQL/TimescaleDB with PostGIS, and Redis separate.

Start dependencies and apply migrations:

```bash
docker compose up -d taxi-postgres taxi-redis
docker compose --profile tools run --rm taxi-migrate
```

Start the API and workers:

```bash
docker compose up -d taxi-api taxi-worker taxi-beat
```

Runtime commands used by the separated services:

```bash
gunicorn --config deploy/conf/gunicorn.conf.py 'APITaxi:create_app()'
celery --app=APITaxi2.celery_worker worker -E
celery --app=APITaxi2.celery_worker beat -s /tmp/celerybeat-schedule
```

The local `taxi-api` service runs Gunicorn with `--reload` for development.
Worker and beat stay in separate containers; Supervisor is kept only as legacy
reference under `deploy/supervisor/` and is not the durable REZO deployment
target.

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

Internal observability endpoints and headers:

* Rezo should send `X-Correlation-ID` on each internal call; APITaxi returns the
  same header or generates one when missing.
* API request logs are JSON records with `event`, `correlation_id`, `method`,
  `path`, `endpoint`, `status_code` and `duration_ms`.
* Logs reuse the APITaxi redaction layer so API keys, tokens, phone numbers,
  addresses and precise coordinates are not emitted.
* `/internal/metrics` exposes minimal Prometheus text metrics and requires the
  same `X-Internal-Key` header as `/internal/health`.
* `OBSERVABILITY_REQUEST_LOGS_ENABLED` and `OBSERVABILITY_METRICS_ENABLED`
  control the request log and in-process metrics collectors.

Worker health can be checked with Celery inspect:

```bash
docker compose exec taxi-worker celery --app=APITaxi2.celery_worker inspect ping --timeout=5
```

Production deployments must keep the API port on an internal network only. Do
not publish this service directly on the public web; publish only the Rezo
facade and keep the fork AGPL-3.0 source available according to the project
licence strategy documented in the Rezo repository.

Production image targets are separated:

```bash
docker build --target web -t rezo-taxi-core-web .
docker build --target worker -t rezo-taxi-core-worker .
docker build --target beat -t rezo-taxi-core-beat .
```

Production and preproduction deployment, secrets, backups, healthchecks,
incident response and rollback are documented in `deploy/REZO_PREPRODUCTION.md`.
Use `deploy/env.preproduction.example` for preproduction and
`deploy/env.production.example` for the VPS production env template.
The first real Rezo Taxi Live pilot and required Reunion town/ZUPC data are
prepared in `deploy/REZO_PILOT_SMOKE.md`. That runbook also records the
rollbackable production smoke validations for Reunion ADS creation and the
full APITaxi driver/vehicle/ADS/taxi entity chain.
Use `deploy/rezo-pilot-taxi.example.json` as the placeholder format for pilot
taxi data and `flask rezo_seed_pilot_taxi --pilot-file <path>` to validate the
pilot seed in rollback mode before any `--apply` run.

The production Compose stack is available in `docker-compose.production.yml`.
It does not publish the Flask port. The `taxi-web` service joins the external
Docker network `rezo-internal`, already used by the Rezo Next.js service, so
Rezo can call `http://rezo-taxi-core-web:5000` internally while browsers keep
using the public Rezo facade only.

## Rezo Taxi Core CI

The `Rezo Taxi Core CI` GitHub Actions workflow validates pushes and pull
requests on `main`, `master` and `rezo/**` branches. It checks Docker Compose
configuration, compiles Python sources, runs `pip-audit`, runs the Docker test
suite and builds the production `web`, `worker` and `beat` image targets. It
does not publish images.

## Rezo taxi business settings

The Rezo fork replaces the historical metropolitan le.taxi limits with
configurable Reunion-ready defaults:

| Setting | Default | Purpose |
|---------|---------|---------|
| `REZO_TAXI_RADIUS_MIN_METERS` | `500` | Smallest driver visibility radius accepted by the API. |
| `REZO_TAXI_RADIUS_DEFAULT_METERS` | `5000` | Radius used when a taxi has no custom radius. |
| `REZO_TAXI_RADIUS_MAX_METERS` | `30000` | Maximum search and driver visibility radius. |
| `REZO_TAXI_GPS_FRESHNESS_SECONDS` | `180` | Maximum accepted age for live GPS positions. |
| `REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS` | `120` | Time given to a driver to accept a hail. |
| `REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS` | `60` | Time given to the customer after driver acceptance. |
| `REZO_TAXI_SEARCH_DISPLAY_LIMIT` | `8` | Maximum taxis returned to the Rezo facade by default. |
| `REZO_TAXI_SEARCH_CANDIDATE_LIMIT` | `20` | Maximum Redis GEO candidates fetched before business filtering. |
| `REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS` | `900` | Threshold Rezo can use to flag a long waiting time. |

Additional internal timeout knobs are available for legacy operator handoff,
pickup and ride timeout behavior: `REZO_TAXI_OPERATOR_ACK_TIMEOUT_SECONDS`,
`REZO_TAXI_SEND_OPERATOR_MAX_DELAY_SECONDS`,
`REZO_TAXI_PICKUP_TIMEOUT_SECONDS` and `REZO_TAXI_RIDE_TIMEOUT_SECONDS`.
Startup validates that all values are positive integers, that radius settings
respect `min <= default <= max`, and that display limit does not exceed the
candidate limit.

When a driver declines a hail or does not answer before
`REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS`, Rezo Taxi Core creates a new hail
attempt in the same `session_id` if another available taxi is found. The search
reuses the Rezo radius, GPS freshness and ZUPC filters, excludes taxis already
contacted in the session and stops after `REZO_TAXI_SEARCH_CANDIDATE_LIMIT`
contacted taxis. If no candidate remains, the source hail keeps its final
`declined_by_taxi` or `timeout_taxi` status so the Rezo facade can fall back to
the taxi directory.

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
