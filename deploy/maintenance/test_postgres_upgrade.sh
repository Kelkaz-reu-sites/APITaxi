#!/usr/bin/env bash
# Run on the REZO VPS as root. Reads production, writes only isolated clones.
# Never switches Compose, stops production, or migrates the production database.
set -euo pipefail
umask 077

[[ ${EUID} -eq 0 ]] || { echo 'Run through sudo on the REZO VPS.' >&2; exit 2; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_CONTAINER=rezo-taxi-core-taxi-postgres-1
OLD_IMAGE=sha256:c6879ffd8d6167c82b2aa8df1d3941f586ce37ad485935cb6a46b68dd46b6b53
PG14_IMAGE=timescale/timescaledb-ha:pg14-ts2.19-all@sha256:127e0220562ee2e5b1abe001deb87670465bc55fca75aeaeaaf861b55734af47
PG17_IMAGE=timescale/timescaledb-ha:pg17.11-ts2.31.0-all@sha256:26e355e055c97d0a51afc415b71fa728dd85664fb24165d595cc8cd0c485b2fc
RUN_DIR="${1:-/var/backups/rezo-maintenance-test-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ $RUN_DIR =~ ^/var/backups/rezo-maintenance-test-[A-Za-z0-9_-]+$ && ! -e $RUN_DIR ]] || { echo 'Use a new protected rezo-maintenance-test directory.' >&2; exit 2; }
[[ $(docker inspect --format '{{.Image}}' "$SOURCE_CONTAINER") == "$OLD_IMAGE" ]] || { echo 'Production image changed: re-review the protocol.' >&2; exit 2; }
mkdir -m 0700 "$RUN_DIR"
exec 9>"$RUN_DIR/operation.lock"
flock -n 9 || exit 2
CLONE14="rezo-maintenance-test-pg14-$(date +%s)"
CLONE17="rezo-maintenance-test-pg17-$(date +%s)"
SNAPSHOT_INPUT_FD=""
cleanup() {
  if [[ -n $SNAPSHOT_INPUT_FD ]]; then
    printf 'ROLLBACK;\n\\q\n' >&"$SNAPSHOT_INPUT_FD" 2>/dev/null || true
  fi
  # Fixed generated names only; never production containers or data directories.
  for clone in "$CLONE14" "$CLONE17"; do
    [[ $clone == rezo-maintenance-test-* ]] && docker rm -f "$clone" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

psql_clone() {
  local clone="$1"; shift
  [[ $clone == rezo-maintenance-test-* ]] || return 2
  docker exec -i -u postgres "$clone" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d apitaxi "$@"
}

start_clone() {
  local clone="$1" image="$2" data="$3"
  [[ $clone == rezo-maintenance-test-* && $data == "$RUN_DIR/"* ]] || return 2
  install -d -m 0700 -o 1000 -g 1000 "$data"
  docker run -d --name "$clone" --network none --memory 768m --memory-swap 768m \
    --cpus 1 --pids-limit 256 --shm-size 128m --cap-drop ALL \
    --security-opt no-new-privileges --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=128m \
    --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m,uid=1000,gid=1000 \
    --env HOME=/tmp --env PGDATA=/home/postgres/pgdata/data \
    --env POSTGRES_USER=apitaxi --env POSTGRES_DB=postgres \
    --env POSTGRES_HOST_AUTH_METHOD=trust --env TIMESCALEDB_TELEMETRY=off \
    --mount "type=bind,source=$data,target=/home/postgres/pgdata/data" \
    "$image" postgres -c shared_buffers=128MB -c max_connections=20 \
    -c work_mem=4MB -c maintenance_work_mem=64MB \
    -c max_worker_processes=8 -c timescaledb.max_background_workers=0 \
    > "$RUN_DIR/${clone}.container-id"
  for attempt in $(seq 1 60); do
    # initdb briefly starts a temporary postmaster. Wait for the entrypoint to
    # exec the final server before any restore (otherwise its shutdown races us).
    if [[ $(docker exec "$clone" cat /proc/1/comm 2>/dev/null) == postgres ]] \
      && docker exec -u postgres "$clone" pg_isready -U apitaxi -d postgres >/dev/null 2>&1; then
      local started previous sample docker_started
      docker_started=$(date -d "$(docker inspect --format '{{.State.StartedAt}}' "$clone")" +%s)
      previous=""
      for sample in 1 2 3; do
        started=$(docker exec -u postgres "$clone" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d postgres -c 'SELECT extract(epoch FROM pg_postmaster_start_time())::bigint;')
        [[ $started =~ ^[0-9]+$ && $started -ge $docker_started ]] || return 1
        [[ -z $previous || $started == "$previous" ]] || return 1
        previous=$started
        sleep 1
      done
      return 0
    fi
    sleep 1
  done
  docker logs "$clone" > "$RUN_DIR/${clone}.startup.log" 2>&1
  echo "Clone did not become ready; inspect protected startup log in $RUN_DIR." >&2
  return 1
}

evidence() {
  local clone="$1" output="$2"
  psql_clone "$clone" < "$SCRIPT_DIR/database_evidence.sql" | LC_ALL=C sort > "$output"
}

restore() {
  local clone="$1" backup="$2" ts_version="$3"
  [[ $clone == rezo-maintenance-test-* && $ts_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 2
  docker exec -i -u postgres "$clone" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d postgres \
    -c 'CREATE DATABASE apitaxi TEMPLATE template0 OWNER apitaxi;'
  psql_clone "$clone" -c "CREATE EXTENSION timescaledb VERSION '$ts_version';"
  psql_clone "$clone" -c 'SELECT timescaledb_pre_restore();' >/dev/null
  docker exec -i -u postgres "$clone" pg_restore -U apitaxi -d apitaxi --exit-on-error \
    < "$backup" > "$RUN_DIR/${clone}.restore.log" 2>&1
  psql_clone "$clone" -c 'SELECT timescaledb_post_restore();' >/dev/null
}

echo "Protected test directory: $RUN_DIR"
# Keep one read-only MVCC snapshot alive so source counts/hashes and pg_dump
# describe the same instant while application writes continue normally.
coproc SOURCE_SNAPSHOT { docker exec -i -u postgres "$SOURCE_CONTAINER" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d apitaxi; }
SNAPSHOT_PID=$SOURCE_SNAPSHOT_PID
SNAPSHOT_INPUT_FD=${SOURCE_SNAPSHOT[1]}
printf '%s\n' 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' 'SELECT pg_export_snapshot();' >&"${SOURCE_SNAPSHOT[1]}"
IFS= read -r snapshot <&"${SOURCE_SNAPSHOT[0]}"
[[ $snapshot =~ ^[A-Fa-f0-9-]+$ ]] || { echo 'Invalid exported snapshot.' >&2; exit 2; }
{ printf "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '%s';\n" "$snapshot"; cat "$SCRIPT_DIR/database_evidence.sql"; printf '\nCOMMIT;\n'; } \
  | docker exec -i -u postgres "$SOURCE_CONTAINER" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d apitaxi \
  | LC_ALL=C sort > "$RUN_DIR/source.evidence"
docker exec -u postgres "$SOURCE_CONTAINER" pg_dump -U apitaxi -d apitaxi -Fc --snapshot "$snapshot" --lock-wait-timeout=3s \
  > "$RUN_DIR/source.dump" 2> "$RUN_DIR/source.dump.log"
printf 'COMMIT;\n\\q\n' >&"${SOURCE_SNAPSHOT[1]}"
wait "$SNAPSHOT_PID"
SNAPSHOT_INPUT_FD=""
docker exec -u postgres "$SOURCE_CONTAINER" pg_dumpall -U apitaxi --globals-only --no-role-passwords \
  > "$RUN_DIR/globals-no-passwords.sql" 2> "$RUN_DIR/globals.log"
sha256sum "$RUN_DIR/source.dump" > "$RUN_DIR/source.dump.sha256"

start_clone "$CLONE14" "$OLD_IMAGE" "$RUN_DIR/pg14"
# The bootstrap role already exists, with no production password copied.
sed '/^CREATE ROLE apitaxi;$/d' "$RUN_DIR/globals-no-passwords.sql" \
  | docker exec -i -u postgres "$CLONE14" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d postgres \
  > "$RUN_DIR/globals-restore.log" 2>&1
restore "$CLONE14" "$RUN_DIR/source.dump" 2.9.2
evidence "$CLONE14" "$RUN_DIR/old-clone.evidence"
cmp "$RUN_DIR/source.evidence" "$RUN_DIR/old-clone.evidence"
echo 'PASS: complete source dump restored on the original image, matching all application tables, rows, indexes, constraints, hypertables and chunks.'

# Stop only the isolated PG14 clone, then reuse only its test data directory.
docker rm -f "$CLONE14" >/dev/null
start_clone "$CLONE14" "$PG14_IMAGE" "$RUN_DIR/pg14"
psql_clone "$CLONE14" -c 'ALTER EXTENSION timescaledb UPDATE;' > "$RUN_DIR/pg14-upgrade.log" 2>&1
psql_clone "$CLONE14" -c 'ALTER EXTENSION timescaledb_toolkit UPDATE;' >> "$RUN_DIR/pg14-upgrade.log" 2>&1
psql_clone "$CLONE14" -c 'SELECT postgis_extensions_upgrade();' >> "$RUN_DIR/pg14-upgrade.log" 2>&1
evidence "$CLONE14" "$RUN_DIR/pg14-upgraded.evidence"
cmp "$RUN_DIR/source.evidence" "$RUN_DIR/pg14-upgraded.evidence"
ts_bridge="$(psql_clone "$CLONE14" -c "SELECT extversion FROM pg_extension WHERE extname='timescaledb';")"
docker exec -u postgres "$CLONE14" pg_dump -U apitaxi -d apitaxi -Fc > "$RUN_DIR/bridge.dump" 2> "$RUN_DIR/bridge.dump.log"
echo "PASS: isolated PG14 extensions upgraded; Timescale bridge=$ts_bridge."

start_clone "$CLONE17" "$PG17_IMAGE" "$RUN_DIR/pg17"
sed '/^CREATE ROLE apitaxi;$/d' "$RUN_DIR/globals-no-passwords.sql" \
  | docker exec -i -u postgres "$CLONE17" psql -X -qAt -v ON_ERROR_STOP=1 -U apitaxi -d postgres \
  > "$RUN_DIR/globals-restore-pg17.log" 2>&1
restore "$CLONE17" "$RUN_DIR/bridge.dump" "$ts_bridge"
evidence "$CLONE17" "$RUN_DIR/pg17-restored.evidence"
cmp "$RUN_DIR/source.evidence" "$RUN_DIR/pg17-restored.evidence"
psql_clone "$CLONE17" -c 'ALTER EXTENSION timescaledb UPDATE;' > "$RUN_DIR/pg17-upgrade.log" 2>&1
psql_clone "$CLONE17" -c 'ALTER EXTENSION timescaledb_toolkit UPDATE;' >> "$RUN_DIR/pg17-upgrade.log" 2>&1
psql_clone "$CLONE17" -c 'SELECT postgis_extensions_upgrade();' >> "$RUN_DIR/pg17-upgrade.log" 2>&1
evidence "$CLONE17" "$RUN_DIR/pg17-final.evidence"
cmp "$RUN_DIR/source.evidence" "$RUN_DIR/pg17-final.evidence"
psql_clone "$CLONE17" -c "SELECT json_build_object('server', current_setting('server_version'), 'extensions', (SELECT json_agg(json_build_object('name',extname,'version',extversion)) FROM pg_extension));" \
  | tee "$RUN_DIR/final-versions.json"
printf 'PASS: all clone stages preserve the complete evidence. No production change.\n' | tee "$RUN_DIR/PASS"
