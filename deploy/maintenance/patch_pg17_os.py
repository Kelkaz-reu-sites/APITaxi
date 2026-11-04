#!/usr/bin/env python3
"""Apply the reviewed Brotli-only PG17 image with the existing data volume.

No migration, restore or volume creation/deletion is performed. An activation
failure returns to the previous engine-identical image on the SAME data volume.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path("/opt/rezo-taxi-core")
BASE = "61b9154b374e8520df4c0bc04be776c0cee93070"
SOURCE = "sha256:f180ded02bf7e10135f62ae8e6ab8b59425d406dbe1eacaa33a1439046e39c0a"
CANDIDATE = "sha256:c71149e092dfa75876bbb7d9c5bc5ca8fd40c7d6b6ce24f7de63d0b279ff721c"
VOLUME = "rezo-taxi-core_taxi-postgres-17-data"
DATABASE = "rezo-taxi-core-taxi-postgres-1"
WRITERS = ("taxi-web", "taxi-worker", "taxi-beat")
COMPOSE = [
    "docker",
    "compose",
    "-p",
    "rezo-taxi-core",
    "-f",
    "docker-compose.production.yml",
]
ENGINES = {
    "/usr/lib/postgresql/17/bin/postgres": "08243b402581cded406b3b619921021537a81e74fc99c80a499c468a2e3144c3",
    "/usr/lib/postgresql/17/lib/timescaledb-2.31.0.so": "35c96370b9e4854ff5f64dcd37fe0fbcad8f3f876bc76df32dacfebf744705af",
    "/usr/lib/postgresql/17/lib/postgis-3.so": "f91bf098ab06f8e3016fc42acb0ab2e680241d223ec2b6b93b92188f0852a27f",
}


def command(*args, input=None):
    result = subprocess.run(
        args, input=input, capture_output=True, text=True, check=False, timeout=1200
    )
    if result.returncode:
        # Command output can contain environment/configuration: keep it private.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def git(*args):
    return command("git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args)


def validate_ci(run, sha):
    if not (
        run.get("head_sha") == sha
        and run.get("head_branch") == "rezo/internal-service-base"
        and run.get("event") in {"push", "workflow_dispatch", "schedule"}
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("path") == ".github/workflows/rezo-taxi-core-ci.yml"
    ):
        raise ValueError("A successful full CI run for this exact source is required")


def validate_compose(before, after):
    if before.count(SOURCE) != 1 or after != before.replace(SOURCE, CANDIDATE, 1):
        raise ValueError("The Compose change must only replace the reviewed PG17 image")


def validate_backup(root, basename, expected_digest, now):
    if not re.fullmatch(r"apitaxi-full-\d{8}T\d{6}Z\.tar\.age", basename):
        raise ValueError("Unexpected encrypted backup name")
    archive = root / basename
    metadata = archive.with_suffix(".age.json")
    if archive.is_symlink() or metadata.is_symlink():
        raise ValueError("Backup symlinks are forbidden")
    proof = json.loads(metadata.read_text())
    created = dt.datetime.strptime(proof["created_at"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=dt.UTC
    )
    if not (
        proof.get("complete_database") is True
        and proof.get("database_image_id") == SOURCE
        and -300 <= (now - created).total_seconds() <= 3600
        and proof.get("encrypted_sha256") == expected_digest
        and hashlib.sha256(archive.read_bytes()).hexdigest() == expected_digest
    ):
        raise ValueError(
            "A complete, fresh backup matching the PC-verified ciphertext is required"
        )


def inspect_database():
    info = json.loads(
        command(
            "docker",
            "inspect",
            "--format",
            '{"image":{{json .Image}},"mounts":{{json .Mounts}}}',
            DATABASE,
        )
    )
    volumes = [
        m for m in info["mounts"] if m["Destination"] == "/home/postgres/pgdata/data"
    ]
    if (
        len(volumes) != 1
        or volumes[0].get("Type") != "volume"
        or volumes[0].get("Name") != VOLUME
    ):
        raise ValueError("The existing PG17 data volume must be preserved")
    return info["image"]


def data_fingerprint():
    evidence = command(
        "docker",
        "exec",
        "-i",
        "-u",
        "postgres",
        DATABASE,
        "psql",
        "-XqAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "apitaxi",
        "-d",
        "apitaxi",
        input=(ROOT / "deploy/maintenance/database_evidence.sql").read_text(),
    )
    tables = sorted(row for row in evidence.splitlines() if row.startswith("TABLE|"))
    if not tables:
        raise ValueError("No application tables were verified")
    return hashlib.sha256(("\n".join(tables) + "\n").encode()).hexdigest()


def patch(sha, ci_run, backup, digest):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"[0-9a-f]{64}", digest
    ):
        raise ValueError("Exact source and backup SHA-256 are required")
    os.chdir(ROOT)
    # The existing deploy lock is owned by debian. Open without O_CREAT so
    # Linux protected_regular does not reject root's access in sticky /tmp.
    with Path("/tmp/rezo-production-deploy.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with urllib.request.urlopen(
            f"https://api.github.com/repos/Kelkaz-reu-sites/APITaxi/actions/runs/{ci_run}",
            timeout=30,
        ) as response:
            validate_ci(json.load(response), sha)
        if git("diff", "--name-only", "HEAD"):
            raise ValueError("Tracked production changes require review")
        git("fetch", "origin", "rezo/internal-service-base")
        if git("rev-parse", "origin/rezo/internal-service-base") != sha:
            raise ValueError("The reviewed source is no longer the current branch head")
        git("merge-base", "--is-ancestor", "HEAD", sha)
        if Path(__file__).read_text().strip() != git(
            "show", f"{sha}:deploy/maintenance/patch_pg17_os.py"
        ):
            raise ValueError("The installer differs from the reviewed source")
        for path in git("diff", "--name-only", BASE, sha).splitlines():
            if not (
                path.endswith(".md")
                or path
                in {
                    "docker-compose.production.yml",
                    "deploy/maintenance/Dockerfile.pg17-ca",
                    "deploy/maintenance/patch_pg17_os.py",
                    "deploy/maintenance/test_patch_pg17_os.py",
                }
            ):
                raise ValueError("Unrelated source changes require a normal deployment")
        validate_compose(
            git("show", f"{BASE}:docker-compose.production.yml"),
            git("show", f"{sha}:docker-compose.production.yml"),
        )
        validate_backup(
            Path("/var/backups/rezo-taxi-core/full"),
            backup,
            digest,
            dt.datetime.now(dt.UTC),
        )
        if inspect_database() != SOURCE:
            raise ValueError(
                "The expected source database image changed; review before retrying"
            )
        hashes = command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sha256sum",
            CANDIDATE,
            *ENGINES,
        )
        if (
            dict(
                (path, value)
                for value, path in (line.split() for line in hashes.splitlines())
            )
            != ENGINES
        ):
            raise ValueError("PostgreSQL or extension engine bytes changed")
        command(
            "trivy",
            "image",
            "--quiet",
            "--timeout",
            "10m",
            "--scanners",
            "vuln",
            "--severity",
            "HIGH,CRITICAL",
            "--ignorefile",
            "/dev/null",
            "--config",
            "/dev/null",
            "--exit-code",
            "10",
            CANDIDATE,
        )
        for service in WRITERS:
            if (
                command(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    f"rezo-taxi-core-{service}-1",
                )
                != "true"
            ):
                raise ValueError(
                    "Writers must be healthy before this maintenance starts"
                )
        git("reset", "--hard", sha)
        command("docker", "image", "tag", SOURCE, "rezo-taxi-pg17:rollback-20261104")
        state = Path("/var/backups/rezo-taxi-core/os-patch-20261104")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".activation-", dir=state) as temporary:
            rollback = Path(temporary) / "rollback.yml"
            rollback.write_text(f"services:\n  taxi-postgres:\n    image: {SOURCE}\n")
            switched = False
            try:
                command(*COMPOSE, "stop", "-t", "60", *WRITERS)
                before = data_fingerprint()
                switched = True
                command(
                    *COMPOSE,
                    "up",
                    "-d",
                    "--no-deps",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    "taxi-postgres",
                )
                if inspect_database() != CANDIDATE or data_fingerprint() != before:
                    raise ValueError(
                        "Database identity or application data fingerprints differ"
                    )
                command(*COMPOSE, "start", *WRITERS)
                command(
                    "docker",
                    "exec",
                    "-w",
                    "/app",
                    "rezo-taxi-core-taxi-web-1",
                    "python",
                    "-B",
                    "/app/deploy/check_runtime.py",
                )
            except BaseException:
                if switched:
                    command(
                        *COMPOSE,
                        "-f",
                        str(rollback),
                        "up",
                        "-d",
                        "--no-deps",
                        "--wait",
                        "--wait-timeout",
                        "180",
                        "taxi-postgres",
                    )
                command(*COMPOSE, "start", *WRITERS)
                raise
        receipt = {
            "source_sha": sha,
            "ci_run": ci_run,
            "image_id": CANDIDATE,
            "previous_image_id": SOURCE,
            "data_volume": VOLUME,
            "application_fingerprint_sha256": before,
            "encrypted_backup_sha256": digest,
            "engine_bytes_unchanged": True,
            "runtime_checks_passed": True,
            "completed_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        (state / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print("PG17_OS_PATCH_DEPLOYED=" + sha, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run", type=int, required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--backup-sha256", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Use the reviewed sudo -n operation on the REZO VPS only")
    os.umask(0o077)
    patch(args.source_sha, args.ci_run, args.backup, args.backup_sha256)
