#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Measure local validation or an encrypted PC copy before a reviewed DB window.

Preparing or invoking this helper never starts a database migration. Publishing a
receipt writes only a private JSON receipt on the existing REZO backup directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path

import cutover_postgres as guards

REPOSITORY = Path(__file__).resolve().parents[2]
DESTINATION = REPOSITORY.parent / ".maintenance-backups/taxi"
ARCHIVE_PATTERN = re.compile(r"apitaxi-full-\d{8}T\d{6}Z\.tar\.age")
MAX_ARCHIVE = 512 * 1024 * 1024

# The only remote mutation is this measured receipt, not a DB/storage operation.
REMOTE_RECEIPT = r"""
import datetime as dt, hashlib, json, os, pathlib, re, stat, subprocess, sys, tempfile
root=pathlib.Path('/var/backups/rezo-taxi-core/full')
try:
    if (root.is_symlink() or root.resolve()!=root or root.stat().st_uid!=0
        or root.stat().st_mode & 0o022): raise ValueError()
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536: raise ValueError()
    request=json.loads(raw); name=request['archive']
    if not re.fullmatch(r'apitaxi-full-\d{8}T\d{6}Z\.tar\.age',name): raise ValueError()
    def secure(path):
        mode=path.lstat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid!=0 or mode.st_mode & 0o077:
            raise ValueError()
    archive=root/name; sidecar=root/(name+'.json')
    secure(archive); secure(sidecar)
    backup=json.loads(sidecar.read_text())
    with archive.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if request['encrypted_sha256']!=digest or request['encrypted_bytes']!=archive.stat().st_size:
        raise ValueError()
    checkout=pathlib.Path('/opt/rezo-taxi-core')
    sha=subprocess.check_output(['git','-c','safe.directory='+str(checkout),'-C',str(checkout),'rev-parse','HEAD'],stderr=subprocess.DEVNULL).decode().strip()
    def image(name):
        return subprocess.check_output(['docker','inspect','--format','{{.Image}}',name],stderr=subprocess.DEVNULL).decode().strip()
    expected={'database_image_id':image('rezo-taxi-core-taxi-postgres-1'),
              'application_image_id':image('rezo-taxi-core-taxi-web-1'),
              'source_checkout_sha':sha,'created_at':backup['created_at'],
              'encrypted_sha256':backup['encrypted_sha256'],'encrypted_bytes':backup['encrypted_bytes']}
    if any(request.get(key)!=value for key,value in expected.items()): raise ValueError()
    if backup.get('complete_database') is not True: raise ValueError()
    for key in ('ssh_source_verified','pc_re_read_sha256_verified','age_decryption_verified','tar_inventory_verified'):
        if request.get(key) is not True: raise ValueError()
    if request.get('destination')!='operator_pc' or request.get('storage_api_used') is not False:
        raise ValueError()
    age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(request['verified_at'].replace('Z','+00:00'))).total_seconds()
    if not 0<=age<=300: raise ValueError()
    producer=checkout/'deploy/maintenance/operator_migration_proof.py'
    if hashlib.sha256(producer.read_bytes()).hexdigest()!=request['producer_sha256']: raise ValueError()
    if request.get('writers_frozen') is True:
        for service in ('taxi-web','taxi-worker','taxi-beat'):
            state=subprocess.check_output(['docker','inspect','--format','{{.State.Running}}','rezo-taxi-core-'+service+'-1'],stderr=subprocess.DEVNULL).decode().strip()
            if state!='false': raise ValueError()
    fd,temporary=tempfile.mkstemp(prefix='.operator-receipt-',dir=root)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(request,output,sort_keys=True);output.write('\n');output.flush();os.fsync(output.fileno())
        os.replace(temporary,root/(name+'.operator-pc.json'))
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)
    print('OPERATOR_PC_RECEIPT_ACCEPTED')
except Exception:
    print('Operator receipt refused',file=sys.stderr)
    raise SystemExit(1)
"""


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def environment() -> dict[str, str]:
    return {
        "HOME": "/home/phil",
        "PATH": "/home/phil/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONFIG": str(DESTINATION / "docker-public-config"),
        "SSL_CERT_FILE": "/home/phil/geo/immo/api/.venv/lib/python3.11/site-packages/certifi/cacert.pem",
    }


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing symlink proof directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def private_json(path: Path, value: dict) -> None:
    private_directory(path.parent)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2, sort_keys=True)
        output.write("\n")


def source_sha() -> str:
    value = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY)
        .decode()
        .strip()
    )
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=REPOSITORY
    )
    branch = (
        subprocess.check_output(["git", "branch", "--show-current"], cwd=REPOSITORY)
        .decode()
        .strip()
    )
    if (
        dirty
        or not re.fullmatch(r"[a-f0-9]{40}", value)
        or branch != "rezo/internal-service-base"
    ):
        raise ValueError("A clean exact source commit is required")
    return value


def validation_commands(sha: str) -> list[tuple[str, list[str]]]:
    tag = "rezo-taxi-manual-validation:" + sha
    compose = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-p",
        "rezo-taxi-validation-" + sha[:12],
    ]
    commands = [
        [*compose, "config"],
        [
            "python3",
            "-B",
            "-c",
            "import ast,pathlib; roots=['APITaxi','APITaxi2','APITaxi_models2','deploy/conf']; [ast.parse(p.read_text(),filename=str(p)) for root in roots for p in pathlib.Path(root).rglob('*.py')]",
        ],
        [
            "uvx",
            "--from",
            "pip-audit==2.10.1",
            "pip-audit",
            "-r",
            "requirements.lock",
            "--strict",
            "--require-hashes",
        ],
        ["bash", "-n", "deploy/backup_taxi_core.sh"],
        [
            "python3",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "deploy",
            "-p",
            "test_backup*.py",
        ],
        ["bash", "-n", "deploy/maintenance/test_postgres_upgrade.sh"],
        [
            "python3",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "deploy/maintenance",
            "-p",
            "test_*.py",
        ],
        [
            *compose,
            "--profile",
            "test",
            "run",
            "--rm",
            "--no-deps",
            "--build",
            "taxi-test",
        ],
        ["docker", "build", "--target", "web", "-t", tag + "-web", "."],
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/venv/bin/python",
            tag + "-web",
            "-B",
            "/app/deploy/check_runtime.py",
        ],
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/venv/bin/python",
            tag + "-web",
            "-B",
            "/app/deploy/check_ca_trust.py",
        ],
        ["docker", "build", "--target", "worker", "-t", tag + "-worker", "."],
        ["docker", "build", "--target", "beat", "-t", tag + "-beat", "."],
    ]
    return list(zip(guards.MANUAL_CHECKS, commands, strict=True))


def prepare_source_workspace(
    destination: Path, extra_paths: tuple[str, ...] = ()
) -> int:
    """Copy only reviewed source files, excluding every local runtime credential."""
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPOSITORY)
    names = {name.decode() for name in tracked.split(b"\0") if name} | set(extra_paths)
    copied = 0
    for name in sorted(names):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe tracked source path")
        if any(
            part in {".git", ".secrets"} or part == ".env" or part.startswith(".env.")
            for part in relative.parts
        ):
            continue
        source = REPOSITORY / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError("Non-regular source file refused")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied += 1
    return copied


def validate_release(expected: str, destination: Path) -> dict:
    # Fixed operator checkout prevents running the test Compose against production.
    if REPOSITORY != Path("/home/phil/geo/rezo-taxi-core") or source_sha() != expected:
        raise ValueError("Local validation is restricted to the reviewed PC checkout")
    private_directory(destination)
    public_config = Path(environment()["DOCKER_CONFIG"])
    private_directory(public_config)
    configuration = public_config / "config.json"
    if configuration.exists():
        if configuration.is_symlink() or json.loads(configuration.read_text()) != {
            "auths": {}
        }:
            raise ValueError(
                "Manual public-image validation requires an anonymous Docker config"
            )
    else:
        private_json(configuration, {"auths": {}})
    proof = {
        "status": "running",
        "started_at": timestamp(),
        "source_checkout_sha": expected,
        "source_branch": "rezo/internal-service-base",
        "recipe": guards.VALIDATION_RECIPE,
        "github_status": "not_used_no_credits",
        "workflow_sha256": guards.digest(
            REPOSITORY / ".github/workflows/rezo-taxi-core-ci.yml"
        ),
        "producer_sha256": guards.digest(Path(__file__)),
        "working_tree_clean": True,
        "checks": [],
    }
    temporary = tempfile.TemporaryDirectory(
        prefix=".validation-source-", dir=destination
    )
    workspace = Path(temporary.name)
    try:
        proof["source_files_copied"] = prepare_source_workspace(workspace)
        for name, command in validation_commands(expected):
            logfile = destination / (name + ".log")
            started = timestamp()
            descriptor = os.open(
                logfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=workspace,
                    env=environment(),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    code = process.wait(timeout=3600)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait()
            proof["checks"].append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": code,
                    "started_at": started,
                    "completed_at": timestamp(),
                    "log_sha256": guards.digest(logfile),
                }
            )
            if code:
                raise RuntimeError("Required manual validation failed: " + name)
        if source_sha() != expected:
            raise ValueError("Source changed during validation")
        proof["built_images"] = {}
        for target in ("web", "worker", "beat"):
            image = (
                subprocess.check_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        "rezo-taxi-manual-validation:" + expected + "-" + target,
                    ],
                    cwd=workspace,
                    env=environment(),
                )
                .decode()
                .strip()
            )
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise ValueError("Built image identity is invalid")
            proof["built_images"][target] = image
        proof["status"] = "passed"
    except Exception as error:
        proof["status"] = "failed"
        proof["failure_type"] = type(error).__name__
    finally:
        # This project prefix is exclusively the local test namespace.
        try:
            cleanup = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "-p",
                    "rezo-taxi-validation-" + expected[:12],
                    "--profile",
                    "test",
                    "down",
                    "-v",
                    "--remove-orphans",
                ],
                cwd=workspace,
                env=environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            if cleanup.returncode:
                proof.update(
                    status="failed", failure_type="test_project_cleanup_failed"
                )
        except (OSError, subprocess.TimeoutExpired):
            proof.update(
                status="failed",
                failure_type="test_project_cleanup_unavailable_or_timed_out",
            )
        temporary.cleanup()
    proof["completed_at"] = timestamp()
    private_json(destination / "manual-validation.json", proof)
    return proof


def validate_decrypted_backup(plaintext: bytes, proof: dict) -> None:
    if not 1024 <= len(plaintext) <= MAX_ARCHIVE:
        raise ValueError("Decrypted backup exceeds its bounds")
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
        members = archive.getmembers()
        if (
            len(members) != 3
            or {item.name for item in members}
            != {"database.dump", "roles.sql", "manifest.json"}
            or any(
                not item.isfile() or not 0 <= item.size <= MAX_ARCHIVE
                for item in members
            )
        ):
            raise ValueError("Unexpected backup TAR inventory")
        manifest_member = archive.getmember("manifest.json")
        if manifest_member.size > 65536:
            raise ValueError("Backup manifest exceeds its bounds")
        manifest = json.load(archive.extractfile(manifest_member))
        for key in (
            "created_at",
            "database_image_id",
            "application_image_id",
            "source_checkout_sha",
            "database_sha256",
            "database_bytes",
        ):
            if manifest.get(key) != proof.get(key):
                raise ValueError("Backup manifest identity differs")
        dump = archive.extractfile("database.dump")
        prefix = dump.read(5)
        digest = hashlib.sha256(prefix)
        for block in iter(lambda: dump.read(65536), b""):
            digest.update(block)
        if (
            prefix != b"PGDMP"
            or digest.hexdigest() != proof["database_sha256"]
            or archive.getmember("database.dump").size != proof["database_bytes"]
        ):
            raise ValueError("Decrypted PostgreSQL dump checksum differs")


def copy_backup(
    name: str, expected_digest: str, expected_sha: str, frozen: bool, publish: bool
) -> dict:
    if not ARCHIVE_PATTERN.fullmatch(name) or not re.fullmatch(
        r"[a-f0-9]{64}", expected_digest
    ):
        raise ValueError("An exact archive and reviewed ciphertext digest are required")
    spec = importlib.util.spec_from_file_location(
        "taxi_pull", REPOSITORY / "scripts/maintenance/pull_backups_to_operator.py"
    )
    pull = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pull)
    private_directory(DESTINATION)
    with (DESTINATION / ".pull.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Reuse the bounded transfer implementation, including its interruption
        # cleanup. There is no local-file shortcut that can invent a receipt.
        previous = signal.signal(signal.SIGALRM, pull.interrupted)
        signal.alarm(600)
        try:
            pull.pull(DESTINATION)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        target = DESTINATION / name
        proof = json.loads(target.with_name(name + ".json").read_text())
        if (
            proof.get("source_checkout_sha") != expected_sha
            or proof.get("complete_database") is not True
            or proof.get("encrypted_sha256") != expected_digest
            or target.stat().st_size != proof.get("encrypted_bytes")
            or target.stat().st_size > MAX_ARCHIVE
            or guards.digest(target) != expected_digest
        ):
            raise ValueError("Actual PC copy does not match this reviewed source")
        with target.open("rb") as source:
            if source.read(22) != b"age-encryption.org/v1\n":
                raise ValueError("Not an age ciphertext")
        decrypted = guards.call(
            [
                "/home/phil/.local/bin/age",
                "-d",
                "-i",
                "/home/phil/.ssh/id_ed25519",
                str(target),
            ],
            timeout=300,
        )
        validate_decrypted_backup(decrypted, proof)
        del decrypted
        receipt = {
            key: proof[key]
            for key in (
                "created_at",
                "database_image_id",
                "application_image_id",
                "source_checkout_sha",
                "encrypted_sha256",
                "encrypted_bytes",
            )
        }
        receipt.update(
            schema_version=1,
            destination="operator_pc",
            archive=name,
            verified_at=timestamp(),
            ssh_source_verified=True,
            pc_re_read_sha256_verified=True,
            age_decryption_verified=True,
            tar_inventory_verified=True,
            storage_api_used=False,
            writers_frozen=frozen,
            producer_sha256=guards.digest(Path(__file__)),
        )
        local = DESTINATION / (
            name
            + ".operator-pc-"
            + dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
        private_json(local, receipt)
        if publish:
            command = [
                "ssh",
                "-i",
                "/home/phil/.ssh/id_ed25519",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ConnectionAttempts=1",
                "debian@51.255.201.196",
                "sudo -n /usr/bin/python3 -B -c " + shlex.quote(REMOTE_RECEIPT),
            ]
            if (
                guards.call(
                    command, data=json.dumps(receipt).encode(), timeout=120
                ).strip()
                != b"OPERATOR_PC_RECEIPT_ACCEPTED"
            ):
                raise ValueError("Server did not acknowledge the measured PC receipt")
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    validation = sub.add_parser("validate")
    validation.add_argument("--expected-prod-sha", required=True)
    validation.add_argument("--output-dir", type=Path, required=True)
    backup = sub.add_parser("copy-backup")
    backup.add_argument("--archive", required=True)
    backup.add_argument("--sha256", required=True)
    backup.add_argument("--expected-prod-sha", required=True)
    backup.add_argument("--frozen", action="store_true")
    backup.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.expected_prod_sha):
        parser.error("An exact reviewed commit is required")
    os.umask(0o077)
    if args.mode == "validate":
        proof = validate_release(args.expected_prod_sha, args.output_dir)
        print(
            json.dumps(
                {
                    "status": proof["status"],
                    "github_status": proof["github_status"],
                    "source_checkout_sha": args.expected_prod_sha,
                    "proof_sha256": guards.digest(
                        args.output_dir / "manual-validation.json"
                    ),
                }
            )
        )
        return 0 if proof["status"] == "passed" else 1
    receipt = copy_backup(
        args.archive, args.sha256, args.expected_prod_sha, args.frozen, args.publish
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "archive": receipt["archive"],
                "encrypted_sha256": receipt["encrypted_sha256"],
                "copied_to_pc": True,
                "age_decryption_verified": True,
                "published": args.publish,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
