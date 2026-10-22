#!/usr/bin/env python3
"""Retired R2 CLI; historical verification helpers retained for audit tests."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import subprocess
import tempfile
import urllib.parse
from pathlib import Path


def private_json(path: Path, value: dict) -> None:
    if path.parent.is_symlink():
        raise RuntimeError("Refusing a symlink proof directory")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def signed_bucket_head(env: dict, bucket: str) -> tuple[int, str | None]:
    endpoint = urllib.parse.urlsplit(env["RCLONE_S3_ENDPOINT"])
    if (
        endpoint.scheme != "https"
        or not re.fullmatch(
            r"[a-fA-F0-9]{32}\.r2\.cloudflarestorage\.com", endpoint.netloc
        )
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
    ):
        raise RuntimeError("Unexpected R2 endpoint or bucket")
    now = dt.datetime.now(dt.timezone.utc)
    day, timestamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    payload = hashlib.sha256(b"").hexdigest()
    path = "/" + urllib.parse.quote(bucket, safe="")
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    headers = {
        "host": endpoint.netloc,
        "x-amz-content-sha256": payload,
        "x-amz-date": timestamp,
    }
    canonical_headers = "".join(
        f"{name}:{headers[name]}\n" for name in signed_headers.split(";")
    )
    canonical = "\n".join(
        ["HEAD", path, "", canonical_headers, signed_headers, payload]
    )
    scope = day + "/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + env["AWS_SECRET_ACCESS_KEY"]).encode()
    for message in (day, "auto", "s3", "aws4_request"):
        key = hmac.new(key, message.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 Credential="
        + env["AWS_ACCESS_KEY_ID"]
        + "/"
        + scope
        + ", SignedHeaders="
        + signed_headers
        + ", Signature="
        + signature
    )
    # HTTPSConnection never follows redirects. No key/signature is persisted.
    connection = http.client.HTTPSConnection(endpoint.netloc, timeout=15)
    try:
        connection.request("HEAD", path, headers=headers)
        response = connection.getresponse()
        request_id = response.getheader("x-amz-request-id")
        if not request_id or not re.fullmatch(r"[A-Za-z0-9_./:=+-]{1,256}", request_id):
            request_id = None
        return response.status, request_id
    finally:
        connection.close()


def verified_rclone_environment(env: dict, bucket: str, proof_path: Path) -> dict:
    proof = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": "HEAD",
        "authentication": "AWS4-HMAC-SHA256",
        "region": "auto",
        "service": "s3",
        "redirects_allowed": False,
        "endpoint_sha256": hashlib.sha256(
            env["RCLONE_S3_ENDPOINT"].encode()
        ).hexdigest(),
        "bucket_sha256": hashlib.sha256(bucket.encode()).hexdigest(),
        "existing_bucket_verified": False,
    }
    try:
        status, request_id = signed_bucket_head(env, bucket)
    except Exception as error:
        proof["error_type"] = type(error).__name__
        private_json(proof_path, proof)
        raise RuntimeError("Signed R2 bucket preflight failed") from None
    proof.update(
        http_status=status,
        request_id=request_id,
        existing_bucket_verified=status == 200,
    )
    private_json(proof_path, proof)
    if status != 200:
        raise RuntimeError(f"Signed R2 bucket preflight refused (HTTP {status})")
    checked = dict(env)
    # Cloudflare documents this for object-scoped tokens. The signed HEAD above
    # independently verifies existence before rclone can skip bucket creation.
    checked["RCLONE_S3_NO_CHECK_BUCKET"] = "true"
    return checked


def public_permission_probe(env: dict, bucket: str, root: Path) -> dict:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nonce = secrets.token_hex(24)
    stem = stamp + "-" + nonce
    preflight_path = root / (stem + ".head.json")
    checked = verified_rclone_environment(env, bucket, preflight_path)
    object_path = "operations/backups/taxi-core/permission-check/" + stem + ".txt"
    remote = ":s3:" + bucket + "/" + object_path
    payload = ("Public REZO backup permission probe; nonce=" + nonce + "\n").encode(
        "ascii"
    )
    expected = hashlib.sha256(payload).hexdigest()
    proof = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "object_path": object_path,
        "payload_is_generated_public_text": True,
        "payload_bytes": len(payload),
        "payload_sha256": expected,
        "preflight_sha256": hashlib.sha256(preflight_path.read_bytes()).hexdigest(),
        "uploaded": False,
        "readback_verified": False,
        "deleted": False,
        "database_accessed": False,
        "database_uploaded": False,
    }
    error = None

    def execute(arguments: list[str], **kwargs) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["rclone", "--retries", "1", "--low-level-retries", "1", *arguments],
            env=checked,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            **kwargs,
        )
        if result.returncode:
            # Persist fixed codes only, never storage output, URLs or keys.
            proof["failed_operation"] = arguments[0]
            proof["tool_exit_code"] = result.returncode
            diagnostic = getattr(result, "stderr", None) or b""
            status = re.search(rb"status code:\s*([1-5][0-9]{2})\b", diagnostic)
            if status:
                proof["storage_http_status"] = int(status[1])
            code = re.search(
                rb"\b(AccessDenied|InvalidAccessKeyId|SignatureDoesNotMatch|NoSuchBucket|"
                rb"NoSuchKey|InvalidRequest|SlowDown|RequestTimeTooSkewed|InternalError):",
                diagnostic,
            )
            if code:
                proof["storage_error_code"] = code[1].decode("ascii")
            raise RuntimeError("Public permission probe storage operation failed")
        return result

    try:
        with tempfile.TemporaryDirectory(prefix=".public-probe-", dir=root) as tmp:
            source = Path(tmp) / "public-permission-probe.txt"
            source.write_bytes(payload)
            source.chmod(0o600)
            execute(
                ["copyto", "--immutable", str(source), remote],
                stdout=subprocess.DEVNULL,
            )
            proof["uploaded"] = True
        result = execute(["cat", remote], stdout=subprocess.PIPE)
        if (
            len(result.stdout) != len(payload)
            or hashlib.sha256(result.stdout).hexdigest() != expected
        ):
            raise RuntimeError("Public permission probe readback checksum differs")
        proof["readback_verified"] = True
    except Exception as failure:
        error = type(failure).__name__
    finally:
        if proof["uploaded"]:
            try:
                execute(["deletefile", remote], stdout=subprocess.DEVNULL)
                proof["deleted"] = True
            except Exception as failure:
                error = type(failure).__name__
        proof["status"] = "passed" if error is None else "failed"
        if error:
            proof["error_type"] = error
        private_json(root / (stem + ".probe.json"), proof)
    if error:
        raise RuntimeError("Public R2 permission probe failed; inspect private proof")
    return proof


def main() -> None:
    # This proposal was declined on 2026-10-22. Keep the old evidence and pure
    # helpers for audit history, but never perform another CLI storage request.
    raise SystemExit("R2 backup tooling retired: Phil selected copies on his computer.")


if __name__ == "__main__":
    main()
