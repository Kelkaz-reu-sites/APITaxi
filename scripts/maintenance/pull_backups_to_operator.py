#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Copy encrypted Taxi backups to Phil's PC; no storage API or decryption."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
from pathlib import Path

ARCHIVE = re.compile(r"apitaxi-full-(\d{8}T\d{6}Z)\.tar\.age")
MAX_ARCHIVES = 64
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
DESTINATION = Path(__file__).resolve().parents[3] / ".maintenance-backups/taxi"

# The SSH command and remote root are fixed. stdin contains only names/digests
# of local ciphertext, never a pathname to execute or read remotely.
REMOTE_CODE = r"""
import datetime as dt, json, pathlib, re, stat, sys
root=pathlib.Path('/var/backups/rezo-taxi-core/full')
pattern=re.compile(r'apitaxi-full-(\d{8}T\d{6}Z)\.tar\.age')
try:
    if root.is_symlink() or root.resolve()!=root: raise ValueError()
    request=json.loads(sys.stdin.buffer.read(16385))
    if not isinstance(request,dict) or len(request)>64: raise ValueError()
    for name,value in request.items():
        if not pattern.fullmatch(name) or not isinstance(value,dict): raise ValueError()
    rows=[]; total=0
    for path in sorted(root.glob('apitaxi-full-????????T??????Z.tar.age.json')):
        name=path.name[:-5]; match=pattern.fullmatch(name)
        if not match: raise ValueError()
        stamp=dt.datetime.strptime(match[1],'%Y%m%dT%H%M%SZ')
        source=root/name
        for item in (path,source):
            mode=item.lstat()
            if (not stat.S_ISREG(mode.st_mode) or mode.st_uid!=0
                or mode.st_mode & 0o077): raise ValueError()
        if path.stat().st_size>65536: raise ValueError()
        proof=json.loads(path.read_text())
        size=source.stat().st_size
        digest=proof.get('encrypted_sha256','')
        if (proof.get('complete_database') is not True
            or proof.get('created_at')!=match[1]
            or type(proof.get('encrypted_bytes')) is not int
            or proof['encrypted_bytes']!=size or not 1024<=size<=536870912
            or not re.fullmatch('[a-f0-9]{64}',digest)): raise ValueError()
        local=request.get(name,{})
        transfer=local.get('sha256')!=digest or local.get('bytes')!=size
        rows.append({'name':name,'proof':proof,'transfer':transfer})
        if transfer: total+=size
    if not rows or len(rows)>64 or total>2147483648: raise ValueError()
    header=json.dumps({'archives':rows},separators=(',',':')).encode()+b'\n'
    if len(header)>1048576: raise ValueError()
    sys.stdout.buffer.write(header);sys.stdout.buffer.flush()
    for row in rows:
        if not row['transfer']: continue
        with (root/row['name']).open('rb') as source:
            remaining=row['proof']['encrypted_bytes']
            while remaining:
                block=source.read(min(65536,remaining))
                if not block: raise ValueError()
                sys.stdout.buffer.write(block);remaining-=len(block)
    sys.stdout.buffer.flush()
except Exception:
    print('Backup transfer refused or interrupted',file=sys.stderr)
    raise SystemExit(1)
"""


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlink destination")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def file_sha256(path: Path) -> str:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Refusing a non-regular local file")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def private_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pull-proof-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink():
            raise ValueError("Refusing a symlink proof")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_header(raw: bytes) -> list[dict]:
    if len(raw) > MAX_HEADER_BYTES or not raw.endswith(b"\n"):
        raise ValueError("Missing or oversized backup inventory")
    rows = json.loads(raw).get("archives")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ARCHIVES:
        raise ValueError("Invalid backup inventory")
    names = set()
    total = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValueError("Invalid backup entry")
        match = ARCHIVE.fullmatch(row["name"])
        if not match or row["name"] in names:
            raise ValueError("Unsafe or duplicate backup filename")
        names.add(row["name"])
        dt.datetime.strptime(match[1], "%Y%m%dT%H%M%SZ")
        proof = row.get("proof")
        if (
            not isinstance(proof, dict)
            or proof.get("complete_database") is not True
            or proof.get("created_at") != match[1]
            or type(proof.get("encrypted_bytes")) is not int
            or not 1024 <= proof["encrypted_bytes"] <= MAX_ARCHIVE_BYTES
            or not isinstance(proof.get("encrypted_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", proof["encrypted_sha256"])
            or type(row.get("transfer")) is not bool
        ):
            raise ValueError("Invalid backup proof")
        if row["transfer"]:
            total += proof["encrypted_bytes"]
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Transfer size limit exceeded")
    return rows


def receive_archive(stream, destination: Path, name: str, proof: dict) -> int:
    if not ARCHIVE.fullmatch(name):
        raise ValueError("Unsafe backup filename")
    target = destination / name
    if target.exists() or target.is_symlink():
        raise ValueError("Refusing to replace an existing ciphertext")
    fd, temporary = tempfile.mkstemp(prefix=".pull-archive-", dir=destination)
    digest = hashlib.sha256()
    remaining = proof["encrypted_bytes"]
    prefix = b""
    try:
        with os.fdopen(fd, "wb") as output:
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise ValueError("Encrypted archive transfer interrupted")
                if len(prefix) < 32:
                    prefix += block[: 32 - len(prefix)]
                output.write(block)
                digest.update(block)
                remaining -= len(block)
            if not prefix.startswith(b"age-encryption.org/v1\n"):
                raise ValueError("The archive is not an age ciphertext")
            if digest.hexdigest() != proof["encrypted_sha256"]:
                raise ValueError("Encrypted archive checksum differs")
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publication is atomic and refuses any concurrent overwrite.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return proof["encrypted_bytes"]


def pull(destination: Path) -> dict:
    known = {}
    paths = sorted(destination.glob("apitaxi-full-????????T??????Z.tar.age"))[
        -MAX_ARCHIVES:
    ]
    for path in paths:
        if not ARCHIVE.fullmatch(path.name):
            raise ValueError("Unsafe local archive name")
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Local archive size limit exceeded")
        known[path.name] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}
    command = [
        "/usr/bin/ssh",
        "-i",
        str(Path.home() / ".ssh/id_ed25519"),
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
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "debian@51.255.201.196",
        "sudo -n /usr/bin/python3 -B -c " + shlex.quote(REMOTE_CODE),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.stdin.write(json.dumps(known).encode())
        process.stdin.close()
        rows = validate_header(process.stdout.readline(MAX_HEADER_BYTES + 1))
        copied = 0
        copied_bytes = 0
        for row in rows:
            name, proof = row["name"], row["proof"]
            if row["transfer"]:
                copied_bytes += receive_archive(
                    process.stdout, destination, name, proof
                )
                copied += 1
            elif known.get(name) != {
                "sha256": proof["encrypted_sha256"],
                "bytes": proof["encrypted_bytes"],
            }:
                raise ValueError("Server skipped an unverified local archive")
            private_json(destination / (name + ".json"), proof)
        if process.stdout.read(1) or process.wait(timeout=30):
            raise ValueError("SSH stream did not finish successfully")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
        process.stdout.close()
    latest = max(row["proof"]["created_at"] for row in rows)
    created = dt.datetime.strptime(latest, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    age = (dt.datetime.now(dt.UTC) - created).total_seconds()
    return {
        "status": "passed" if -300 <= age <= 36 * 3600 else "failed",
        "latest_archive": "apitaxi-full-" + latest + ".tar.age",
        "latest_created_at": created.isoformat(),
        "freshness_passed": -300 <= age <= 36 * 3600,
        "verified_archives": len(rows),
        "copied_archives": copied,
        "copied_bytes": copied_bytes,
        "destination": str(destination),
        "storage_api_used": False,
        "decrypted": False,
    }


def interrupted(signum, frame) -> None:
    raise TimeoutError("Operator backup transfer interrupted or timed out")


def main() -> int:
    os.umask(0o077)
    private_directory(DESTINATION)
    with (DESTINATION / ".pull.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGALRM, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        signal.alarm(10 * 60)
        started = dt.datetime.now(dt.UTC).isoformat()
        try:
            proof = pull(DESTINATION)
        except Exception as error:
            proof = {
                "status": "failed",
                "error_type": type(error).__name__,
                "storage_api_used": False,
            }
        finally:
            signal.alarm(0)
        proof.update(
            started_at=started, finished_at=dt.datetime.now(dt.UTC).isoformat()
        )
        private_json(DESTINATION / "pull-latest.json", proof)
        private_json(
            DESTINATION
            / (
                "pull-last-success.json"
                if proof["status"] == "passed"
                else "pull-last-failure.json"
            ),
            proof,
        )
        print(json.dumps(proof, sort_keys=True))
        return 0 if proof["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
