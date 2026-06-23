# Rezo Taxi Core preproduction runbook

This runbook describes how to deploy the APITaxi fork as the internal Rezo Taxi
Core preproduction service. It applies to the REZO fork:

```text
https://github.com/Kelkaz-reu-sites/APITaxi
```

The upstream `openmaraude/APITaxi` repository is historical reference only for
REZO operations. Do not deploy it directly. This fork remains AGPL-3.0 and must
stay publishable according to the REZO licence strategy.

## 1. Target boundary

Rezo Taxi Core is not a public web API. In preproduction and production:

- browsers and mobile clients call the public REZO facade only ;
- the REZO backend calls Rezo Taxi Core over a private network ;
- the `web`, `worker` and `beat` containers are built from the same Git commit ;
- PostgreSQL/PostGIS stores durable APITaxi data ;
- Redis stores live taxi positions and Celery queues/results ;
- Caddy, Nginx or a platform router must not expose the Flask port publicly ;
- `/internal/health` and `/internal/metrics` require `X-Internal-Key`.

## 2. Required runtime services

Minimum services:

- `rezo-taxi-core-web` : Gunicorn + Flask API ;
- `rezo-taxi-core-worker` : Celery worker ;
- `rezo-taxi-core-beat` : Celery beat ;
- PostgreSQL 14+ with PostGIS and `pgcrypto` ;
- Redis 7+ with persistence enabled for preproduction if the host supports it.

Recommended image tags:

```text
rezo-taxi-core-web:<git-sha>
rezo-taxi-core-worker:<git-sha>
rezo-taxi-core-beat:<git-sha>
```

The same `<git-sha>` must be used for the three application services.

## 3. Environment variables

Use `deploy/env.preproduction.example` as the non-secret preproduction template
and `deploy/env.production.example` as the non-secret VPS production template.
Store real values in the deployment secret store or in a host file with mode
`0600`.

Required secrets:

- `POSTGRES_PASSWORD` when the bundled Compose PostgreSQL service is used ;
- `SECRET_KEY` ;
- `SECURITY_PASSWORD_SALT` ;
- `SQLALCHEMY_DATABASE_URI` ;
- `REDIS_URL` ;
- `CELERY_BROKER_URL` ;
- `CELERY_RESULT_BACKEND` ;
- `INTERNAL_HEALTHCHECK_KEY` ;
- `SENTRY_DSN` when Sentry is enabled.

Required non-secret or operational settings:

- `CORS_ALLOWED_ORIGINS` should stay empty unless a specific internal origin is
  required for a controlled development environment ;
- `OBSERVABILITY_REQUEST_LOGS_ENABLED=true` ;
- `OBSERVABILITY_METRICS_ENABLED=true` ;
- `REZO_TAXI_RADIUS_MIN_METERS` ;
- `REZO_TAXI_RADIUS_DEFAULT_METERS` ;
- `REZO_TAXI_RADIUS_MAX_METERS` ;
- `REZO_TAXI_GPS_FRESHNESS_SECONDS` ;
- `REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS` ;
- `REZO_TAXI_CUSTOMER_CONFIRMATION_TIMEOUT_SECONDS` ;
- `REZO_TAXI_SEARCH_DISPLAY_LIMIT` ;
- `REZO_TAXI_SEARCH_CANDIDATE_LIMIT` ;
- `REZO_TAXI_LONG_WAIT_THRESHOLD_SECONDS` ;
- `GUNICORN_WORKERS` ;
- `GUNICORN_THREADS` ;
- `GUNICORN_TIMEOUT` ;
- `GUNICORN_FORWARDED_ALLOW_IPS`.

Optional settings:

- `SENTRY_SAMPLE_RATE` ;
- HTTP timeout overrides ;
- `FAKE_TAXI_ID` if the facade must avoid exposing internal taxi ids ;
- `CONSOLE_URL` and `SWAGGER_URL` if these legacy redirects are still used
  behind an internal admin boundary.

## 4. Secret generation and rotation

Generate strong values outside the repository:

```bash
openssl rand -base64 48
```

Rules:

- never commit a real `.env`, database URL, Redis URL, internal key, Sentry DSN
  or operator credential ;
- keep host env files readable only by the deployment user ;
- prefer a deployment secret store when available ;
- rotate `INTERNAL_HEALTHCHECK_KEY` with a short maintenance window because the
  app currently accepts one active internal key ;
- rotate database and Redis credentials by creating the new credential first,
  updating all app services, then revoking the old credential ;
- rotate operator API keys from the operator/user records and coordinate with
  the corresponding operator endpoint ;
- rotate `SECRET_KEY` and `SECURITY_PASSWORD_SALT` only during planned
  maintenance, as Flask-Security sessions and password reset flows can be
  invalidated.

If a secret is suspected leaked:

1. Remove public access to the affected service if any.
2. Rotate the exposed secret immediately.
3. Restart affected `web`, `worker` and `beat` services.
4. Check logs for unauthorized access.
5. Document the incident in the REZO incident log.

## 5. Pre-deploy checklist

Before deploying a commit:

```bash
git status --short
git rev-parse HEAD
gh run list --branch rezo/internal-service-base --limit 5
```

The target commit must have a green `Rezo Taxi Core CI` run. Locally, the same
checks can be reproduced with:

```bash
python3 -m compileall -q APITaxi APITaxi2 APITaxi_models2 deploy/conf
pip-audit -r requirements.txt --strict
docker compose config >/tmp/rezo-taxi-compose.yml
docker compose --profile test run --rm taxi-test
docker build --target web -t rezo-taxi-core-web:test .
docker build --target worker -t rezo-taxi-core-worker:test .
docker build --target beat -t rezo-taxi-core-beat:test .
```

## 6. Database backup before deploy

Create a PostgreSQL logical backup before migrations:

```bash
mkdir -p /var/backups/rezo-taxi-core
pg_dump "$SQLALCHEMY_DATABASE_URI" \
  --format=custom \
  --file="/var/backups/rezo-taxi-core/apitaxi-predeploy-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Record:

- backup path ;
- Git commit before deploy ;
- Git commit after deploy ;
- Alembic revision before deploy ;
- Alembic revision after deploy.

Redis contains live positions and Celery runtime data. For preproduction, a
Redis snapshot is useful before risky operations but should not be treated as
the durable source of truth:

```bash
redis-cli --rdb "/var/backups/rezo-taxi-core/redis-predeploy-$(date -u +%Y%m%dT%H%M%SZ).rdb"
```

## 7. Migration procedure

Run Alembic as a one-shot task from the same image tag as the deploy:

```bash
docker run --rm \
  --env-file /etc/rezo-taxi-core/preproduction.env \
  --network <private-network> \
  rezo-taxi-core-web:<git-sha> \
  bash -lc "cd APITaxi_models2 && alembic current && alembic upgrade head && alembic current"
```

If using Compose:

```bash
docker compose -f docker-compose.production.yml --profile tools run --rm -T taxi-migrate
```

Do not run migrations from a different source commit than the application image.
Use `-T` when running the command from SSH, a heredoc or a non-interactive
script: without it, `docker compose run` can consume stdin and prevent following
shell commands from running.

## 8. Start or update services

### Compose deployment on the Rezo VPS

The preferred VPS deployment path is `docker-compose.production.yml`. It keeps
PostgreSQL, Redis, web, worker and beat in one project, does not publish the
Flask port, and attaches only `taxi-web` to the external `rezo-internal`
network used by the Rezo Next.js service.

Prepare the host once:

```bash
sudo install -d -m 0750 -o "$USER" -g "$USER" /etc/rezo-taxi-core
sudo install -d -m 0750 -o "$USER" -g "$USER" /opt/rezo-taxi-core
docker network create rezo-internal || true
```

Copy `deploy/env.production.example` to
`/etc/rezo-taxi-core/production.env`, replace every placeholder secret, then
lock permissions:

```bash
chmod 0600 /etc/rezo-taxi-core/production.env
```

Build, migrate and start:

```bash
export REZO_TAXI_CORE_IMAGE_TAG="$(git rev-parse --short HEAD)"

docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml build
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml --profile tools run --rm -T taxi-migrate
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml up -d taxi-web taxi-worker taxi-beat
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml ps
```

Then configure the Rezo Next.js production environment with:

```text
REZO_TAXI_LIVE_ENABLED=0
REZO_TAXI_CORE_BASE_URL=http://rezo-taxi-core-web:5000
REZO_TAXI_CORE_API_KEY=<operator-or-service-api-key>
REZO_TAXI_CUSTOMER_HASH_SALT=<stable-random-salt>
```

Restart the Rezo web container after changing these variables and verify the
server readiness route from the Rezo runbook.

For the initial VPS connection, keep `REZO_TAXI_LIVE_ENABLED=0` until a pilot
taxi is attached and the operator callback behavior is validated. The internal
API key used by Rezo is the APITaxi `user.apikey` value, not
`operator_api_key`. `operator_api_key` is only the optional outbound header
value sent by APITaxi to an operator callback endpoint.

Current production service account:

- email: `rezo-taxi-live-service@rezo.re` ;
- roles: `moteur`, `operateur` ;
- secret storage: APITaxi database + `/opt/rezo/web/.env.local` only ;
- never print the key in logs, issues, documentation or shell history.

For the Rezo internal flow, set:

```text
REZO_TAXI_INTERNAL_OPERATOR_HANDOFF_ENABLED=true
REZO_TAXI_INTERNAL_OPERATOR_EMAILS=rezo-taxi-live-service@rezo.re
```

When this flag is enabled and the hail operator email is in the allowlist,
`send_request_operator` does not call an external operator endpoint. It moves
the hail from `received` to `received_by_taxi` and schedules the normal driver
acceptance timeout. Keep the flag disabled for legacy external operators.

Current Rezo VPS deployment note, 2026-06-23:

- code commit deployed: `d0cbc249d` ;
- image tag deployed: `d0cbc249d` ;
- `REZO_TAXI_INTERNAL_OPERATOR_HANDOFF_ENABLED=true` ;
- allowlisted operator: `rezo-taxi-live-service@rezo.re` ;
- `taxi-web`, `taxi-worker`, `taxi-beat`, `taxi-postgres` and `taxi-redis`
  healthy ;
- `/internal/health` and `/internal/metrics` validated with
  `X-Internal-Key` ;
- Celery worker validated with `celery inspect ping` ;
- Rezo can call `/taxis` from `rezo-web-1` over the private Docker network ;
- `REZO_TAXI_LIVE_ENABLED=0` remains intentionally set on Rezo until a pilot
  taxi dataset and driver flow are validated.
- next tracking issues: Rezo #37 and APITaxi #18.

Pilot smoke prerequisite discovered on 2026-06-23:

- the production database has department `974` ;
- no `Town` row for Reunion communes is currently loaded ;
- no ZUPC currently covers the Reunion test point ;
- a pilot ADS and live search cannot work until Reunion towns and a Rezo ZUPC
  are imported ;
- the dedicated runbook is `deploy/REZO_PILOT_SMOKE.md`.

Reunion cadastre import note, 2026-06-23:

- pre-deploy/pre-import backup:
  `/var/backups/rezo-taxi-core/apitaxi-predeploy-d0cbc24-20260623T133839Z.dump` ;
- source file on VPS:
  `/opt/rezo-taxi-core/imports/cadastre-974-sections.json.gz` ;
- checksum:
  `0a2e789f5249f12167175ef0055d11b6a045e3cdc1aef8d5f3c94e0b630c774e` ;
- dry-run command succeeded with
  `Reunion cadastre source validated: 24 towns` ;
- post dry-run counters stayed `town974=0` and `zupc_rezo=0` ;
- mutating import was then approved and executed without `--dry-run` ;
- post-import counters: `town974=24`, `zupc_allowed=24` ;
- ZUPC `949cdf3e-9128-524e-a39f-db91c1ea0cc7` is named
  `REZO_REUNION_MVP` ;
- Saint-Denis, Saint-Pierre and Saint-Paul test points are covered by a town
  and the Rezo ZUPC ;
- rollbackable `POST /ads` smoke succeeded for `insee=97411` through the Rezo
  service account, returned HTTP `201`, then rolled back with
  `ads_smoke_remaining=0` ;
- the next prerequisite is controlled pilot taxi data, not geography or ADS
  route acceptance.

### Generic container deployment

Start or update the three application services from the same commit:

```bash
docker run -d --name rezo-taxi-core-web \
  --env-file /etc/rezo-taxi-core/preproduction.env \
  --network <private-network> \
  rezo-taxi-core-web:<git-sha>

docker run -d --name rezo-taxi-core-worker \
  --env-file /etc/rezo-taxi-core/preproduction.env \
  --network <private-network> \
  rezo-taxi-core-worker:<git-sha>

docker run -d --name rezo-taxi-core-beat \
  --env-file /etc/rezo-taxi-core/preproduction.env \
  --network <private-network> \
  rezo-taxi-core-beat:<git-sha>
```

Equivalent orchestrator commands are acceptable if they preserve the same
separation between `web`, `worker` and `beat`.

## 9. Healthchecks and smoke tests

API health:

```bash
curl -fsS \
  -H "X-Internal-Key: $INTERNAL_HEALTHCHECK_KEY" \
  http://rezo-taxi-core-web:5000/internal/health
```

Metrics:

```bash
curl -fsS \
  -H "X-Internal-Key: $INTERNAL_HEALTHCHECK_KEY" \
  http://rezo-taxi-core-web:5000/internal/metrics | head
```

Worker:

```bash
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml exec -T taxi-worker \
  celery --app=APITaxi2.celery_worker inspect ping --timeout=5
```

Database:

```bash
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml exec -T taxi-web \
  bash -lc "cd APITaxi_models2 && alembic current"
```

Redis:

```bash
docker compose --env-file /etc/rezo-taxi-core/production.env -f docker-compose.production.yml exec -T taxi-redis \
  redis-cli ping
```

Expected result:

- API health returns HTTP 200 ;
- metrics returns Prometheus text ;
- Celery worker returns `pong` ;
- Alembic revision is `head` ;
- Redis returns `PONG` ;
- REZO facade smoke tests can call the internal service without exposing it
  publicly.

## 10. Rollback

### Image-only rollback

Use when no migration was applied or when the migration is known to be backward
compatible:

```bash
docker stop rezo-taxi-core-web rezo-taxi-core-worker rezo-taxi-core-beat
docker rm rezo-taxi-core-web rezo-taxi-core-worker rezo-taxi-core-beat
# restart the previous known-good image tags
```

Then rerun the healthchecks.

### Migration rollback

Alembic downgrades are not guaranteed to be safe in this legacy codebase. Before
any destructive migration, prepare a tested rollback script or a restore plan.

Preferred rollback order for preproduction:

1. Stop `web`, `worker` and `beat`.
2. Restore PostgreSQL from the predeploy dump if the migration is unsafe.
3. Redeploy the previous image tags.
4. Restore Redis snapshot only if live position or queue state must be replayed.
5. Run API, metrics, worker and REZO facade smoke tests.

PostgreSQL restore example:

```bash
createdb apitaxi_restore_check
pg_restore --dbname apitaxi_restore_check /var/backups/rezo-taxi-core/<backup>.dump

# destructive restore to the preproduction database only after validation
pg_restore --clean --if-exists --dbname "$SQLALCHEMY_DATABASE_URI" /var/backups/rezo-taxi-core/<backup>.dump
```

Do not run destructive restore commands on production without a second human
review.

## 11. Incident runbook

### API unhealthy

1. Check container status and logs.
2. Verify `INTERNAL_HEALTHCHECK_KEY`.
3. Verify PostgreSQL and Redis connectivity.
4. Check Alembic current revision.
5. Roll back to the previous image if the failure started after deploy.

### Worker unhealthy

1. Run Celery inspect ping.
2. Check Redis broker connectivity.
3. Check worker logs for import or migration mismatch.
4. Restart worker only if the web API is healthy and image tags match.

### Beat unhealthy

1. Check the beat process and schedule file.
2. Ensure only one beat instance runs for the environment.
3. Restart beat after confirming Redis and PostgreSQL are reachable.

### Database migration failed

1. Stop app services if the schema may be partially migrated.
2. Capture logs and current Alembic revision.
3. Decide between fix-forward and restore from predeploy backup.
4. Do not restart workers until the schema is consistent.

### Redis lost or flushed

1. Treat taxi live positions as stale.
2. Keep API up if PostgreSQL is healthy.
3. Ask REZO facade to display directory fallback until taxis publish fresh
   positions again.
4. Restart worker only if Celery queues are corrupted.

### High timeout or reassignment rate

1. Check Redis freshness and taxi GPS updates.
2. Check Celery queue delay.
3. Check operator API errors and HTTP timeout logs.
4. Lower traffic or disable live taxi in REZO facade if user experience becomes
   misleading.

## 12. Acceptance checklist

A preproduction deployment is acceptable when:

- CI is green for the deployed commit ;
- image tags for `web`, `worker` and `beat` match ;
- secrets are provided outside Git ;
- PostgreSQL backup exists before migration ;
- Alembic is at `head` ;
- API health and metrics are reachable only with the internal key ;
- Celery worker answers `pong` ;
- Redis returns `PONG` ;
- REZO facade can call the service from the private network ;
- rollback image tag and database backup are recorded ;
- no public route exposes Rezo Taxi Core directly.
