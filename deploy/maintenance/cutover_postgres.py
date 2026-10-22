#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Guarded PostgreSQL cutover. Preview by default; execute only after review."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SOURCE = "rezo-taxi-core-taxi-postgres-1"
WEB = "rezo-taxi-core-taxi-web-1"
OLD_IMAGE = "sha256:c6879ffd8d6167c82b2aa8df1d3941f586ce37ad485935cb6a46b68dd46b6b53"
NEW_IMAGE = "timescale/timescaledb-ha:pg17.11-ts2.31.0-all@sha256:26e355e055c97d0a51afc415b71fa728dd85664fb24165d595cc8cd0c485b2fc"
NEW_VOLUME = "rezo-taxi-core_taxi-postgres-17-data"
CHECKOUT = Path("/opt/rezo-taxi-core")
DBPATH = "/home/postgres/pgdata/data"


def call(command: list[str], *, data: bytes | None = None, timeout: int = 120) -> bytes:
    result = subprocess.run(
        command, input=data, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        # Never include SQL, environment, database stderr or storage responses.
        raise RuntimeError(f"Operation failed: {command[0]} (exit {result.returncode})")
    return result.stdout


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def protected_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError(
            "Proof/backup must be a root-owned regular file with mode 0600."
        )


def inspect(container: str) -> dict:
    return json.loads(call(["docker", "inspect", container]))[0]


def verify_backup(
    path: Path, expected: str, source: dict, web: dict, checkout_sha: str
) -> dict:
    protected_file(path)
    proof = json.loads(path.read_text())
    if (
        proof.get("offsite_verified") is not True
        or proof.get("encrypted_sha256") != expected
    ):
        raise ValueError("Off-site backup proof does not match the reviewed digest.")
    age = dt.datetime.now(dt.UTC) - dt.datetime.strptime(
        proof["created_at"], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=dt.UTC)
    if age.total_seconds() < 0 or age.total_seconds() > 3600:
        raise ValueError("Verified off-site backup must be less than one hour old.")
    if (
        proof.get("database_image_id") != source["Image"]
        or proof.get("application_image_id") != web["Image"]
        or proof.get("source_checkout_sha") != checkout_sha
    ):
        raise ValueError("Off-site backup inventory differs from current production.")
    encrypted = path.with_suffix("")
    protected_file(encrypted)
    if digest(encrypted) != expected:
        raise ValueError(
            "Local encrypted backup digest differs from its verified off-site proof."
        )
    return proof


def verify_ci(run_id: int, sha: str) -> None:
    # This fork is public; no cross-repository PAT or credential is needed.
    request = urllib.request.Request(
        f"https://api.github.com/repos/Kelkaz-reu-sites/APITaxi/actions/runs/{run_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "rezo-reviewed-postgres-cutover",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not (
        result.get("head_sha") == sha
        and result.get("head_branch") == "rezo/internal-service-base"
        and result.get("status") == "completed"
        and result.get("conclusion") == "success"
        and result.get("path") == ".github/workflows/rezo-taxi-core-ci.yml"
    ):
        raise ValueError(
            "The exact production checkout must have a successful complete APITaxi CI run."
        )


def verify_test(path: Path, expected: str) -> None:
    if not re.fullmatch(
        r"/var/backups/rezo-maintenance-test-[A-Za-z0-9_-]+", str(path)
    ):
        raise ValueError("Unexpected isolated-test path.")
    for name in (
        "PASS",
        "source.evidence",
        "old-clone.evidence",
        "pg14-upgraded.evidence",
        "pg17-restored.evidence",
        "pg17-final.evidence",
    ):
        protected_file(path / name)
    if digest(path / "source.evidence") != expected or any(
        digest(path / name) != expected
        for name in (
            "old-clone.evidence",
            "pg14-upgraded.evidence",
            "pg17-restored.evidence",
            "pg17-final.evidence",
        )
    ):
        raise ValueError(
            "Reviewed full restoration evidence is missing or differs between stages."
        )


def compose(extra: Path | None = None) -> list[str]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(CHECKOUT),
        "--env-file",
        str(CHECKOUT / ".env"),
        "-f",
        str(CHECKOUT / "docker-compose.production.yml"),
    ]
    if extra:
        command.extend(["-f", str(extra)])
    return command


def wait_healthy(container: str) -> None:
    for _ in range(90):
        details = inspect(container)
        if details["State"].get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(2)
    raise RuntimeError("Service health did not become healthy within three minutes.")


def verify_service_environment(configured: dict, current: dict) -> None:
    active = dict(
        item.split("=", 1) for item in current["Config"]["Env"] if "=" in item
    )
    if any(str(value) != active.get(key) for key, value in configured.items()):
        raise ValueError(
            "Reviewed Compose and running service environments differ; preserve current operational settings before migration."
        )


def verify_running_database(clone_dir: Path, work: Path) -> None:
    evidence = call(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "postgres",
            SOURCE,
            "psql",
            "-XqAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "apitaxi",
            "-d",
            "apitaxi",
        ],
        data=(CHECKOUT / "deploy/maintenance/database_evidence.sql").read_bytes(),
    )
    actual = work / "production-pg17.evidence"
    actual.write_bytes(b"\n".join(sorted(evidence.splitlines())) + b"\n")
    if digest(actual) != digest(clone_dir / "source.evidence"):
        raise ValueError(
            "Running PG17 data/schema evidence differs from the frozen source."
        )


def application_database_smoke() -> None:
    # The HTTP health endpoint checks only its internal key. Exercise the actual
    # application's database credentials and Redis connection before exposure.
    probe = """from APITaxi import create_app
from APITaxi_models2 import db
from sqlalchemy import text
app = create_app()
with app.app_context():
    with db.engine.connect() as connection:
        assert connection.execute(text('SELECT current_database()')).scalar_one() == 'apitaxi'
        assert connection.execute(text("SHOW server_version_num")).scalar_one().startswith('17')
        assert connection.execute(text("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")).scalar_one() == '2.31.0'
    assert app.redis.ping()
"""
    call(["docker", "exec", "-i", WEB, "python3", "-"], data=probe.encode())


def jobs_state(container: str) -> list[dict]:
    if container != SOURCE and not container.startswith("rezo-maintenance-test-"):
        raise ValueError("Unexpected job-control container.")
    sql = "SELECT coalesce(json_agg(json_build_object('id',job_id,'scheduled',scheduled,'proc_name',proc_name)), '[]'::json) FROM timescaledb_information.jobs;"
    result = json.loads(
        call(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                container,
                "psql",
                "-XqAt",
                "-U",
                "apitaxi",
                "-d",
                "apitaxi",
                "-c",
                sql,
            ]
        )
    )
    if any(
        not isinstance(job["id"], int) or not isinstance(job["scheduled"], bool)
        for job in result
    ):
        raise ValueError("Unexpected Timescale job state.")
    return result


def set_jobs(container: str, jobs: list[dict]) -> None:
    if container != SOURCE and not container.startswith("rezo-maintenance-test-"):
        raise ValueError("Unexpected job-control container.")
    for job in jobs:
        if not isinstance(job["id"], int) or not isinstance(job["scheduled"], bool):
            raise TypeError("Invalid Timescale job state.")
        sql = f"SELECT alter_job({job['id']}, scheduled => {'true' if job['scheduled'] else 'false'});"
        call(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                container,
                "psql",
                "-XqAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "apitaxi",
                "-d",
                "apitaxi",
                "-c",
                sql,
            ]
        )


def wait_no_jobs(container: str) -> None:
    sql = "SELECT count(*) FROM timescaledb_information.job_stats WHERE lower(job_status)='running';"
    for _ in range(60):
        count = (
            call(
                [
                    "docker",
                    "exec",
                    "-u",
                    "postgres",
                    container,
                    "psql",
                    "-XqAt",
                    "-U",
                    "apitaxi",
                    "-d",
                    "apitaxi",
                    "-c",
                    sql,
                ]
            )
            .decode()
            .strip()
        )
        if count == "0":
            return
        time.sleep(1)
    raise RuntimeError("Timescale jobs did not finish after scheduling was paused.")


def desired_job_state(source_jobs: list[dict], target_jobs: list[dict]) -> list[dict]:
    source = {job["id"]: job for job in source_jobs}
    target = {job["id"]: job for job in target_jobs}
    # Upstream 2.14.2 -> 2.15.0 replaces error retention (2) with job history
    # retention (3). Preserve its operator-selected pause state explicitly.
    legacy = source.get(2)
    replacement = target.get(3)
    if (
        2 not in target
        and legacy
        and legacy["proc_name"] == "policy_job_error_retention"
        and replacement
        and replacement["proc_name"] == "policy_job_stat_history_retention"
    ):
        source[3] = {
            **source.pop(2),
            "id": 3,
            "proc_name": replacement["proc_name"],
        }
    if any(
        job_id not in target or target[job_id]["proc_name"] != job["proc_name"]
        for job_id, job in source.items()
    ):
        raise ValueError(
            "Timescale job identities changed during upgrade; review before resuming."
        )
    return [
        {**job, "scheduled": source.get(job["id"], job)["scheduled"]}
        for job in target_jobs
    ]


def credentials_and_authentication(
    data_dir: Path, work: Path, source: dict, original_jobs: list[dict] | None = None
) -> list[dict]:
    environment = dict(
        item.split("=", 1) for item in source["Config"]["Env"] if "=" in item
    )
    password = environment.get("POSTGRES_PASSWORD", "")
    if environment.get("POSTGRES_USER") != "apitaxi" or not password:
        raise ValueError(
            "Expected source database credential is unavailable; do not guess or reset it."
        )
    # Test-clone trust authentication must never reach the production network.
    auth = data_dir / "pg_hba.conf"
    auth.write_text("local all all trust\nhost all all all scram-sha-256\n")
    auth.chmod(0o600)
    os.chown(auth, 1000, 1000)
    clone = f"rezo-maintenance-test-auth-{int(time.time())}"
    try:
        call(
            [
                "docker",
                "run",
                "-d",
                "--name",
                clone,
                "--network",
                "none",
                "--memory",
                "768m",
                "--memory-swap",
                "768m",
                "--cpus",
                "1",
                "--pids-limit",
                "256",
                "--shm-size",
                "128m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=128m",
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,size=16m,uid=1000,gid=1000",
                "--env",
                "HOME=/tmp",
                "--env",
                f"PGDATA={DBPATH}",
                "--mount",
                f"type=bind,source={data_dir},target={DBPATH}",
                NEW_IMAGE,
                "postgres",
                "-c",
                "shared_buffers=128MB",
                "-c",
                "max_connections=20",
                "-c",
                "work_mem=4MB",
                "-c",
                "maintenance_work_mem=64MB",
                "-c",
                "timescaledb.max_background_workers=0",
            ]
        )
        for _ in range(60):
            if (
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-u",
                        "postgres",
                        clone,
                        "pg_isready",
                        "-U",
                        "apitaxi",
                        "-d",
                        "apitaxi",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Authentication clone is not ready.")
        sql = (
            "SET password_encryption='scram-sha-256'; ALTER ROLE apitaxi PASSWORD '"
            + password.replace("'", "''")
            + "';\n"
        )
        call(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                "postgres",
                clone,
                "psql",
                "-XqAt",
                "-U",
                "apitaxi",
                "-d",
                "apitaxi",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            data=sql.encode(),
        )
        probe = "import json,os,subprocess,sys; d=json.load(sys.stdin); e=dict(os.environ,PGPASSWORD=d['password']); p=subprocess.run(['psql','-XqAt','-h','127.0.0.1','-U','apitaxi','-d','apitaxi','-c','SELECT 1'],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE); sys.exit(0 if p.returncode==d['expected_return'] else 1)"
        call(
            ["docker", "exec", "-i", clone, "python3", "-c", probe],
            data=json.dumps({"password": password, "expected_return": 0}).encode(),
        )
        # A deliberately wrong password must fail, proving TCP isn't using trust.
        bad = "rezo-maintenance-invalid-" + os.urandom(24).hex()
        rejected = "import json,os,subprocess,sys; d=json.load(sys.stdin); e=dict(os.environ,PGPASSWORD=d['password']); p=subprocess.run(['psql','-XqAt','-h','127.0.0.1','-U','apitaxi','-d','apitaxi','-c','SELECT 1'],env=e,stdout=subprocess.PIPE,stderr=subprocess.PIPE); sys.exit(0 if p.returncode!=0 else 1)"
        call(
            ["docker", "exec", "-i", clone, "python3", "-c", rejected],
            data=json.dumps({"password": bad}).encode(),
        )
        target_jobs = jobs_state(clone)
        (work / "target-job-identities.json").write_text(
            json.dumps(target_jobs, indent=2) + "\n"
        )
        desired_jobs = desired_job_state(original_jobs or target_jobs, target_jobs)
        set_jobs(clone, [{**job, "scheduled": False} for job in target_jobs])
        wait_no_jobs(clone)
        (work / "target-jobs-to-resume.json").write_text(
            json.dumps(desired_jobs, indent=2) + "\n"
        )
        call(["docker", "stop", "--time", "60", clone])
        call(["docker", "rm", clone])
        (work / "AUTHENTICATION_PASS").write_text(
            "SCRAM: correct password accepted; wrong password rejected; clone stopped cleanly.\n"
        )
        return desired_jobs
    finally:
        subprocess.run(
            ["docker", "rm", "-f", clone],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def preserve_capacity(data_dir: Path, work: Path) -> None:
    names = [
        "max_connections",
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "effective_cache_size",
        "max_worker_processes",
        "max_parallel_workers",
        "max_parallel_workers_per_gather",
        "timescaledb.max_background_workers",
    ]
    literals = ",".join("'" + name + "'" for name in names)
    sql = f"SELECT json_object_agg(name,current_setting(name)) FROM pg_settings WHERE name IN ({literals});"
    settings = json.loads(
        call(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                SOURCE,
                "psql",
                "-XqAt",
                "-U",
                "apitaxi",
                "-d",
                "apitaxi",
                "-c",
                sql,
            ]
        )
    )
    if set(settings) != set(names) or any(
        not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:B|kB|MB|GB|TB)?", value)
        for value in settings.values()
    ):
        raise ValueError("Source capacity settings require manual review.")
    configuration = data_dir / "postgresql.auto.conf"
    with configuration.open("a") as output:
        output.write(
            "\n# Preserve reviewed pre-migration connection/memory/worker capacity.\n"
        )
        for name, value in sorted(settings.items()):
            output.write(f"{name} = '{value}'\n")
    configuration.chmod(0o600)
    os.chown(configuration, 1000, 1000)
    (work / "preserved-capacity.json").write_text(json.dumps(settings, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-prod-sha", required=True)
    parser.add_argument("--ci-run", type=int, required=True)
    parser.add_argument("--offsite-proof", type=Path, required=True)
    parser.add_argument("--offsite-sha256", required=True)
    parser.add_argument("--tested-directory", type=Path, required=True)
    parser.add_argument("--test-evidence-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", args.expected_prod_sha):
        raise ValueError("Root and an exact reviewed production commit are required.")
    os.umask(0o077)
    with Path("/run/rezo-postgres-maintenance.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checkout_sha = (
            call(
                [
                    "git",
                    "-c",
                    f"safe.directory={CHECKOUT}",
                    "-C",
                    str(CHECKOUT),
                    "rev-parse",
                    "HEAD",
                ]
            )
            .decode()
            .strip()
        )
        if checkout_sha != args.expected_prod_sha or call(
            [
                "git",
                "-c",
                f"safe.directory={CHECKOUT}",
                "-C",
                str(CHECKOUT),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ]
        ):
            raise ValueError(
                "Production checkout must match the reviewed commit and have no tracked modifications."
            )
        source, web = inspect(SOURCE), inspect(WEB)
        if (
            source["Image"] != OLD_IMAGE
            or source["State"].get("Health", {}).get("Status") != "healthy"
        ):
            raise ValueError(
                "Expected original healthy PostgreSQL image is not running."
            )
        verify_ci(args.ci_run, checkout_sha)
        verify_test(args.tested_directory, args.test_evidence_sha256)
        verify_backup(
            args.offsite_proof, args.offsite_sha256, source, web, checkout_sha
        )
        configuration = json.loads(call(compose() + ["config", "--format", "json"]))
        db_config = configuration["services"]["taxi-postgres"]
        if db_config["image"] != NEW_IMAGE:
            raise ValueError(
                "Commit the reviewed PG17 image and new volume configuration before cutover."
            )
        for service in ("taxi-web", "taxi-worker", "taxi-beat"):
            verify_service_environment(
                configuration["services"][service].get("environment", {}),
                inspect(f"rezo-taxi-core-{service}-1"),
            )
        source_environment = dict(
            item.split("=", 1) for item in source["Config"]["Env"] if "=" in item
        )
        if any(
            db_config["environment"].get(key) != source_environment.get(key)
            for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")
        ):
            raise ValueError(
                "Reviewed Compose and running database credentials differ; do not reset them during migration."
            )
        application_image = configuration["services"]["taxi-web"]["image"]
        if (
            json.loads(call(["docker", "image", "inspect", application_image]))[0]["Id"]
            != web["Image"]
        ):
            raise ValueError(
                "The application image tag changed since the reviewed running service."
            )
        login_count = (
            call(
                [
                    "docker",
                    "exec",
                    "-u",
                    "postgres",
                    SOURCE,
                    "psql",
                    "-XqAt",
                    "-U",
                    "apitaxi",
                    "-d",
                    "apitaxi",
                    "-c",
                    "SELECT count(*) FROM pg_roles WHERE rolcanlogin AND rolname <> 'apitaxi';",
                ]
            )
            .decode()
            .strip()
        )
        if login_count != "0":
            raise ValueError(
                "Additional login roles need their own credential-preservation plan."
            )
        configured = next(v for v in db_config["volumes"] if v["target"] == DBPATH)
        if (
            configured["type"] != "volume"
            or configuration["volumes"][configured["source"]]["name"] != NEW_VOLUME
        ):
            raise ValueError("The reviewed new PG17 named volume is not configured.")
        old_volume = next(
            v["Name"] for v in source["Mounts"] if v["Destination"] == DBPATH
        )
        if old_volume == NEW_VOLUME:
            raise ValueError("Original and target volumes must be different.")
        existing = subprocess.run(
            ["docker", "volume", "inspect", NEW_VOLUME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if existing.returncode == 0:
            raise ValueError(
                "Target volume already exists; never overwrite a previous migration."
            )
        if not args.execute:
            print(
                "PRECHECK_PASS: exact CI/checkout, original image, fresh off-site proof and full restore evidence verified. No production service changed. Review before adding --execute."
            )
            return 0
        work = Path(
            f"/var/backups/rezo-maintenance-test-cutover-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}"
        )
        work.mkdir(mode=0o700)
        private = work / "private-web.override.yml"
        private.write_text(
            "services:\n  taxi-web:\n    networks: !override\n      default: {}\n"
        )
        rollback = work / "rollback.override.yml"
        rollback.write_text(
            "services:\n  taxi-postgres:\n    image: "
            + json.dumps(OLD_IMAGE)
            + "\n    volumes: !override\n      - type: volume\n        source: postgres-rollback\n        target: "
            + DBPATH
            + "\nvolumes:\n  postgres-rollback:\n    external: true\n    name: "
            + json.dumps(old_volume)
            + "\n"
        )
        committed = False
        frozen = False
        backend_start_attempted = False
        original_jobs = jobs_state(SOURCE)
        (work / "original-timescale-jobs.json").write_text(
            json.dumps(original_jobs, indent=2) + "\n"
        )
        try:
            frozen = True
            call(
                compose()
                + ["stop", "--timeout", "60", "taxi-beat", "taxi-worker", "taxi-web"],
                timeout=240,
            )
            # Fresh encrypted off-site backup while external requests and workers
            # are stopped, in addition to the reviewed precondition backup.
            output = call(
                [
                    "python3",
                    str(CHECKOUT / "deploy/backup_taxi_core.py"),
                    "--output-dir",
                    str(args.offsite_proof.parent),
                ],
                timeout=600,
            ).decode()
            match = re.search(
                r"BACKUP_OK=(apitaxi-full-[0-9]{8}T[0-9]{6}Z\.tar\.age) OFFSITE_VERIFIED=true SHA256=([0-9a-f]{64})",
                output,
            )
            if not match:
                raise RuntimeError(
                    "Fresh frozen-writer off-site backup was not verified."
                )
            (work / "fresh-offsite.json").write_text(
                json.dumps({"archive": match[1], "sha256": match[2]})
            )
            # Compression/retention/telemetry are writers too. Pause scheduling
            # after the off-site backup (which retains normal job schedules),
            # then wait for already-running jobs before the final MVCC dump.
            set_jobs(SOURCE, [{**job, "scheduled": False} for job in original_jobs])
            wait_no_jobs(SOURCE)
            clone_dir = Path(str(work) + "-clones")
            call(
                [
                    "bash",
                    str(CHECKOUT / "deploy/maintenance/test_postgres_upgrade.sh"),
                    str(clone_dir),
                ],
                timeout=1200,
            )
            target_jobs = credentials_and_authentication(
                clone_dir / "pg17", work, source, original_jobs
            )
            preserve_capacity(clone_dir / "pg17", work)
            call(["docker", "volume", "create", NEW_VOLUME])
            destination = Path(
                json.loads(call(["docker", "volume", "inspect", NEW_VOLUME]))[0][
                    "Mountpoint"
                ]
            )
            if destination != Path("/var/lib/docker/volumes") / NEW_VOLUME / "_data":
                raise ValueError("Unexpected target Docker volume path.")
            shutil.copytree(
                clone_dir / "pg17", destination, dirs_exist_ok=True, symlinks=True
            )
            for base, directories, files in os.walk(destination, followlinks=False):
                os.chown(base, 1000, 1000)
                for name in directories + files:
                    os.chown(Path(base) / name, 1000, 1000, follow_symlinks=False)
            call(
                compose()
                + [
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "taxi-postgres",
                ],
                timeout=180,
            )
            wait_healthy(SOURCE)
            verify_running_database(clone_dir, work)
            backend_start_attempted = True
            call(
                compose(private)
                + [
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "taxi-web",
                ],
                timeout=180,
            )
            backend = inspect(WEB)
            if "rezo-internal" in backend["NetworkSettings"]["Networks"]:
                committed = True
                (work / "UNEXPECTED_EXPOSURE").write_text(
                    "Backend may have received writes; automatic old-volume rollback disabled.\n"
                )
                raise RuntimeError(
                    "Backend was exposed before validation; stop and review."
                )
            wait_healthy(WEB)
            application_database_smoke()
            # This point is the write/exposure boundary. Never automatically
            # revert to the old volume after it, even if a later step fails.
            committed = True
            (work / "COMMITTED").write_text(
                json.dumps(
                    {
                        "sha": checkout_sha,
                        "new_volume": NEW_VOLUME,
                        "old_volume": old_volume,
                        "time": dt.datetime.now(dt.UTC).isoformat(),
                    }
                )
            )
            # Resuming internal jobs is also a write boundary, so it occurs only
            # after the backend is healthy and automatic rollback is disabled.
            set_jobs(SOURCE, target_jobs)
            call(
                [
                    "docker",
                    "network",
                    "connect",
                    "--alias",
                    "rezo-taxi-core-web",
                    "rezo-internal",
                    WEB,
                ]
            )
            call(
                compose()
                + [
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--pull",
                    "never",
                    "taxi-worker",
                    "taxi-beat",
                ],
                timeout=180,
            )
            wait_healthy("rezo-taxi-core-taxi-worker-1")
            wait_healthy("rezo-taxi-core-taxi-beat-1")
            print(
                f"CUTOVER_PASS: PostgreSQL17 committed; old volume retained; receipt {work}/COMMITTED"
            )
            return 0
        except BaseException:
            if backend_start_attempted and not committed:
                try:
                    if "rezo-internal" in inspect(WEB)["NetworkSettings"]["Networks"]:
                        committed = True
                except (OSError, RuntimeError, ValueError):
                    # Ambiguous partial network exposure must never discard
                    # possible new writes by reverting the original volume.
                    committed = True
            if frozen and not committed:
                # No application writers have reached the new database yet.
                call(
                    compose(private) + ["stop", "--timeout", "30", "taxi-web"],
                    timeout=90,
                )
                call(
                    compose(rollback)
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "taxi-postgres",
                    ],
                    timeout=180,
                )
                wait_healthy(SOURCE)
                set_jobs(SOURCE, original_jobs)
                call(
                    compose(rollback)
                    + [
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        "taxi-web",
                        "taxi-worker",
                        "taxi-beat",
                    ],
                    timeout=240,
                )
                print(
                    "CUTOVER_ABORTED: original volume restored before new-database exposure; new volume retained for investigation.",
                    file=sys.stderr,
                )
            elif committed:
                print(
                    "CUTOVER_COMMITTED_REVIEW_REQUIRED: keep PostgreSQL17; never automatically revert the old volume after writers may have resumed.",
                    file=sys.stderr,
                )
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"CUTOVER_FAILED={type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
