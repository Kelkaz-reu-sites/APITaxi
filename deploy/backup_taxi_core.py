#!/usr/bin/env python3
"""Create a complete Timescale backup, encrypt for Phil, and copy to private R2."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256")
    return digest.hexdigest()


def run(stage: str, command: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(command, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        # Database errors and storage responses may contain confidential values.
        raise RuntimeError(f"{stage} failed (exit {result.returncode})")
    return result


def r2_environment(path: Path) -> tuple[dict[str, str], str]:
    names = {
        "REZO_R2_ACCOUNT_ID",
        "REZO_R2_ACCESS_KEY_ID",
        "REZO_R2_SECRET_ACCESS_KEY",
        "REZO_R2_PRIVATE_BUCKET",
        "REZO_R2_BUCKET_NAME",
    }
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.match(r"(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=(.*)", line)
        if not match or match[1] not in names:
            continue
        tokens = shlex.split(match[2], comments=True)
        if len(tokens) != 1:
            raise RuntimeError("Invalid R2 configuration")
        values[match[1]] = tokens[0]
    for key in names - {"REZO_R2_BUCKET_NAME"}:
        if not values.get(key):
            raise RuntimeError("Private R2 backup configuration is incomplete")
    bucket = values["REZO_R2_PRIVATE_BUCKET"]
    if bucket == values.get("REZO_R2_BUCKET_NAME"):
        raise RuntimeError("The backup bucket must differ from the public bucket")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("Invalid private bucket name")
    account = values["REZO_R2_ACCOUNT_ID"]
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise RuntimeError("Invalid R2 account identifier")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "RCLONE_"))}
    env.update(
        {
            "AWS_ACCESS_KEY_ID": values["REZO_R2_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": values["REZO_R2_SECRET_ACCESS_KEY"],
            "RCLONE_CONFIG": "/dev/null",
            "RCLONE_S3_PROVIDER": "Cloudflare",
            "RCLONE_S3_ENV_AUTH": "true",
            "RCLONE_S3_REGION": "auto",
            "RCLONE_S3_ENDPOINT": f"https://{account}.r2.cloudflarestorage.com",
        }
    )
    return env, bucket


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("/home/debian/backups/taxi-core")
    )
    parser.add_argument(
        "--recipient-file",
        type=Path,
        default=Path("/etc/rezo-maintenance/backup-recipient.pub"),
    )
    parser.add_argument(
        "--r2-config", type=Path, default=Path("/opt/rezo/web/.env.local")
    )
    parser.add_argument("--container", default="rezo-taxi-core-taxi-postgres-1")
    args = parser.parse_args()
    os.umask(0o077)
    env, bucket = r2_environment(args.r2_config)
    recipient = args.recipient_file.read_text().strip()
    if not recipient.startswith(("ssh-ed25519 ", "age1")):
        raise RuntimeError("A supported public encryption recipient is required")
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output_dir.chmod(0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = args.output_dir / f"apitaxi-full-{stamp}.tar.age"
    if archive.exists():
        raise RuntimeError("Backup destination already exists")
    with tempfile.TemporaryDirectory(prefix=".full-", dir=args.output_dir) as tmp:
        work = Path(tmp)
        dump = work / "database.dump"
        with dump.open("xb") as out:
            run(
                "pg_dump",
                [
                    "docker",
                    "exec",
                    "--user",
                    "postgres",
                    args.container,
                    "pg_dump",
                    "-w",
                    "-U",
                    "apitaxi",
                    "-d",
                    "apitaxi",
                    "--format=custom",
                ],
                stdout=out,
            )
        if dump.stat().st_size < 1024:
            raise RuntimeError("Database backup is unexpectedly small")
        with dump.open("rb") as source:
            run(
                "archive validation",
                [
                    "docker",
                    "exec",
                    "-i",
                    "--user",
                    "postgres",
                    args.container,
                    "pg_restore",
                    "--list",
                ],
                stdin=source,
                stdout=subprocess.DEVNULL,
            )
        with (work / "roles.sql").open("xb") as out:
            run(
                "role backup",
                [
                    "docker",
                    "exec",
                    "--user",
                    "postgres",
                    args.container,
                    "pg_dumpall",
                    "-w",
                    "-U",
                    "apitaxi",
                    "--roles-only",
                    "--no-role-passwords",
                ],
                stdout=out,
            )
        result = run(
            "version inventory",
            [
                "docker",
                "exec",
                "--user",
                "postgres",
                args.container,
                "psql",
                "-w",
                "-XAt",
                "-U",
                "apitaxi",
                "-d",
                "apitaxi",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "SELECT current_setting('server_version')",
                "-c",
                "SELECT extname || '=' || extversion FROM pg_extension ORDER BY extname",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        manifest = {
            "created_at": stamp,
            "source_container": args.container,
            "versions": result.stdout.splitlines(),
            "complete_database": True,
            "database_sha256": file_sha256(dump),
            "database_bytes": dump.stat().st_size,
        }
        for field, container in [
            ("database_image_id", args.container),
            ("application_image_id", "rezo-taxi-core-taxi-web-1"),
        ]:
            image = run(
                "image inventory",
                ["docker", "inspect", "--format", "{{.Image}}", container],
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
                raise RuntimeError("Invalid source image identity")
            manifest[field] = image
        repo = Path(__file__).resolve().parents[1]
        manifest["source_checkout_sha"] = run(
            "source inventory",
            [
                "git",
                "-c",
                f"safe.directory={repo}",
                "-C",
                str(repo),
                "rev-parse",
                "HEAD",
            ],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        bundle = work / "backup.tar"
        with tarfile.open(bundle, "w") as tar:
            for name in ["database.dump", "roles.sql", "manifest.json"]:
                tar.add(work / name, arcname=name, recursive=False)
        run(
            "backup encryption",
            ["age", "--recipient", recipient, "--output", str(archive), str(bundle)],
            stdout=subprocess.DEVNULL,
        )
    object_path = (
        f"operations/backups/taxi-core/{stamp[:4]}/{stamp[4:6]}/{archive.name}"
    )
    remote = f":s3:{bucket}/{object_path}"
    run(
        "private R2 upload",
        [
            "rclone",
            "copyto",
            "--retries",
            "3",
            "--low-level-retries",
            "3",
            str(archive),
            remote,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
    )
    local_digest = file_sha256(archive)
    with subprocess.Popen(
        ["rclone", "cat", remote],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as download:
        assert download.stdout is not None
        remote_digest = hashlib.file_digest(download.stdout, "sha256").hexdigest()
        if download.wait():
            raise RuntimeError("Private R2 verification failed")
    if remote_digest != local_digest:
        raise RuntimeError("The encrypted off-site backup checksum differs")
    proof = {
        **manifest,
        "encrypted_sha256": local_digest,
        "encrypted_bytes": archive.stat().st_size,
        "offsite_verified": True,
        "object_path": object_path,
        "recipient": recipient,
    }
    archive.with_suffix(archive.suffix + ".json").write_text(
        json.dumps(proof, indent=2) + "\n"
    )
    # Rotate only backups created by this program, after off-site verification.
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 14 * 86400
    for old in args.output_dir.glob("apitaxi-full-????????T??????Z.tar.age*"):
        if old.is_file() and not old.is_symlink() and old.stat().st_mtime < cutoff:
            old.unlink()
    print(f"BACKUP_OK={archive.name} OFFSITE_VERIFIED=true SHA256={local_digest}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        # Never include subprocess output or configuration contents in cron logs.
        print(f"BACKUP_FAILED={type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
