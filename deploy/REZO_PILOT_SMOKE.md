# Rezo Taxi Core pilot smoke runbook

Status: preparation runbook, no production mutation approved yet.
Last update: 2026-06-23.
Tracking: Rezo #37, APITaxi #18.

This document prepares the first controlled Rezo Taxi Live smoke test against
the APITaxi fork deployed as the internal Rezo Taxi Core service. It must be
used together with `deploy/REZO_PREPRODUCTION.md` and the Rezo repository
runbook `doc/TAXI_LIVE_PILOT_SMOKE.md`.

## 1. Current deployment

Current VPS state:

- deployed commit: `3936988c` ;
- deployed image tag: `3936988c` ;
- service account: `rezo-taxi-live-service@rezo.re` ;
- service account roles: `moteur`, `operateur` ;
- internal operator handoff enabled for the Rezo service account ;
- Flask port not published on the host ;
- Rezo calls `http://rezo-taxi-core-web:5000` over the private Docker network ;
- Rezo still keeps `REZO_TAXI_LIVE_ENABLED=0`.

Read-only production inspection on 2026-06-23 found:

- department `974` exists ;
- no `Town` row with an INSEE code starting with `974` exists ;
- point `lon=55.45`, `lat=-20.9` matches no town ;
- no ZUPC covers that point.

This is a blocking prerequisite. ADS creation for a Reunion INSEE code and taxi
search around a Reunion passenger point cannot work until Reunion towns and at
least one Rezo ZUPC are loaded.

## 2. Minimal core entities

The pilot taxi must be created in this order:

1. `POST /drivers`
   Creates or updates the pilot driver for the Rezo operator. Required fields
   include first name, last name, professional licence and department `974`.
2. `POST /vehicles`
   Creates or updates the vehicle and its operator-specific description.
   `licence_plate` is mandatory.
3. `POST /ads`
   Creates or updates the ADS. Required fields are `numero` and `insee`; the
   INSEE code must exist in `Town`, and the town must be attached to a ZUPC.
4. `POST /taxis`
   Links ADS, vehicle and driver into the APITaxi taxi object.
5. `PUT /taxis/{taxi_id}`
   Sets driver status and custom visibility radius.
6. `POST /geotaxi/`
   Publishes the fresh GPS position into Redis GEO.

The returned `taxi_id` must be linked in Rezo as the transporter's
`taxi_core_id`.

## 3. Geographic prerequisite

For the MVP, use a permissive Rezo ZUPC covering all Reunion communes. This
matches the sparse taxi supply and large-radius business rule. Passenger-facing
Rezo UI must still show approach distance and estimated waiting time.

Selected MVP approach:

- use the already documented Etalab cadastre source for department 974 ;
- read `cadastre-974-sections.json.gz`, which contains cadastral section
  `MultiPolygon` features with a `commune=974xx` property ;
- dissolve these sections by INSEE code to create one APITaxi `Town` shape per
  Reunion commune ;
- create or update a ZUPC named `REZO_REUNION_MVP` ;
- use the stable technical UUID `949cdf3e-9128-524e-a39f-db91c1ea0cc7` for
  `ZUPC.zupc_id` ;
- attach the 24 Reunion towns to that ZUPC ;
- run the import on production only after backup and explicit approval.

Documentation check:

- Rezo `DATA.md` documents Etalab cadastre layers ;
- global `DATA_SOURCES.md` marks cadastre as integrated ;
- global `DATA_SOURCES.md` marks `communes-millesime-france` as not integrated,
  with current coverage through cadastre ;
- `rn_commune_lareunion` exists but contains road `LineString` features, not
  town polygons, so it must not be used for APITaxi `Town.shape`.

Legacy APITaxi commands available today:

```bash
flask import_towns --contours-url <url> --contours-tmpdir <dir> --zupc-repo <zupc-repo>
flask zupc add <zupc-dir>
flask import_zupc --zupc-repo <zupc-repo>
```

The legacy path can work, but it may import a national town dataset and depends
on a ZUPC repository layout. For the pilot, the smaller Rezo-specific cadastre
import is safer and easier to audit.

Rezo command added for this flow:

```bash
flask rezo_import_reunion_cadastre \
  --cadastre-sections /imports/cadastre-974-sections.json.gz \
  --dry-run
```

Production execution must use a read-only bind mount for the source file and
must run `--dry-run` first:

```bash
mkdir -p /opt/rezo-taxi-core/imports
# Copy cadastre-974-sections.json.gz into /opt/rezo-taxi-core/imports/ first.

docker compose --env-file /etc/rezo-taxi-core/production.env \
  -f docker-compose.production.yml \
  run --rm -T \
  -v /opt/rezo-taxi-core/imports:/imports:ro \
  taxi-web \
  flask rezo_import_reunion_cadastre \
    --cadastre-sections /imports/cadastre-974-sections.json.gz \
    --dry-run
```

The mutating command is the same without `--dry-run`. Do not run it before a
database backup and explicit approval.

Minimum acceptance checks after import:

- `Town.query.filter(Town.insee.like('974%')).count()` returns `24` ;
- the pilot passenger point is contained in one imported town ;
- the pilot passenger point is covered by one ZUPC ;
- the ZUPC is named `REZO_REUNION_MVP` and has `24` allowed towns ;
- `POST /ads` accepts the pilot ADS INSEE code ;
- `GET /taxis?lon=...&lat=...` returns an empty list, not an error, before the
  pilot taxi is online.

## 4. Controlled smoke flow

Do not execute this section in production until the geographic prerequisite is
done and a backup exists.

Core flow:

1. Create or update the pilot driver.
2. Create or update the pilot vehicle.
3. Create or update the pilot ADS.
4. Create the taxi and record its `taxi_id`.
5. Link `taxi_id` in Rezo.
6. Publish GPS through Rezo driver endpoint or directly through `/geotaxi/`
   from an internal container.
7. Set status `free` and the selected pilot radius.
8. Call `GET /taxis?lon=<passenger_lon>&lat=<passenger_lat>`.
9. Create a hail through Rezo facade, not directly from browsers to APITaxi.
10. Verify APITaxi status transition `received` -> `received_by_taxi`.
11. Verify driver acceptance, refusal and timeout paths.
12. Verify Rezo hail mirror and passenger polling.

Expected Celery behavior:

- the Rezo internal operator handoff does not call an external operator URL ;
- the hail is moved to `received_by_taxi` ;
- driver timeout is scheduled with
  `REZO_TAXI_DRIVER_ACCEPTANCE_TIMEOUT_SECONDS` ;
- refusal or timeout can trigger reassignment if another taxi is eligible.

## 5. Safety rules

- Keep this service internal; never expose Flask directly to public web clients.
- Never print API keys, phone numbers or precise test coordinates in issues.
- Use placeholders in documentation and shell history.
- Create a database backup before any production import.
- Keep `REZO_TAXI_LIVE_ENABLED=0` until the controlled smoke succeeds.
- Prefer clearly prefixed pilot data so it can be identified later.
- Record each step in the Rezo backlog with issue, commit, command, result,
  decision and residual risk.

## 6. Rollback

Fast functional rollback:

1. Set Rezo `REZO_TAXI_LIVE_ENABLED=0`.
2. Restart the Rezo web container.
3. Set the pilot taxi status to `off`.
4. Confirm Rezo returns the directory fallback when no live taxi is available.

Data rollback:

- keep hail rows until investigation is finished ;
- do not delete pilot entities before exporting or recording their identifiers ;
- remove or overwrite Redis pilot positions only if they affect tests.

## 7. Next implementation task

Prepare the Reunion geographic import:

- copy the cadastre sections source into a read-only import directory on the VPS ;
- run the APITaxi import command in dry-run mode ;
- create the `REZO_REUNION_MVP` ZUPC through the command ;
- verify point-in-town and point-in-ZUPC coverage ;
- document the exact production commands before execution.
