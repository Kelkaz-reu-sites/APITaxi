#!/usr/bin/env python3
"""Smoke the optional PG17 tools in a disposable --network none container only.

No database is started and no production volume, configuration or secret is read.
pgBackRest uses an empty config stub. PgBouncer's exporter requires a configured
pooler before starting its HTTP endpoint; only its CLI is exercised here.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import subprocess
import time
from pathlib import Path

LIMIT = 2 * 1024 * 1024
TOOLS = (
    (
        "pgbackrest_exporter",
        "/usr/bin/pgbackrest_exporter",
        19854,
        "pgbackrest_exporter_info",
        ["--collect.interval=3600", "--backrest.config=/dev/null"],
    ),
)


def file_sha256(path: str) -> str:
    # The upstream HA image has Python3.10 (no hashlib.file_digest).
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_metrics(body: bytes, metric: str) -> None:
    if len(body) > LIMIT or not re.search(
        rb"^" + metric.encode() + rb"\{[^\n]*\} 1(?:\.0)?\s*$", body, re.MULTILINE
    ):
        raise ValueError("Exporter did not expose its expected build/info metric")


def exporter_smoke(path: str, port: int, metric: str, arguments: list[str]) -> None:
    environment = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin", "TZ": "UTC"}
    process = subprocess.Popen(
        [path, "--web.listen-address=127.0.0.1:" + str(port), *arguments],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if process.poll() is not None:
                raise ValueError("Exporter exited before its loopback smoke test")
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                connection.request("GET", "/metrics")
                response = connection.getresponse()
                body = response.read(LIMIT + 1)
                if response.status == 200:
                    validate_metrics(body, metric)
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        raise ValueError("Exporter loopback endpoint did not become ready")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    hashes = {}
    for name, path, port, metric, arguments in TOOLS:
        exporter_smoke(path, port, metric, arguments)
        hashes[name] = file_sha256(path)
    pgbouncer = "/usr/local/bin/pgbouncer_exporter"
    version = subprocess.run(
        [pgbouncer, "--version"], capture_output=True, check=True, timeout=5
    )
    description = version.stdout + version.stderr
    if b"version 0.9.0" not in description or b"go1.27.3" not in description:
        raise ValueError("PgBouncer exporter source version/toolchain differs")
    hashes["pgbouncer_exporter"] = file_sha256(pgbouncer)
    yq = "/usr/local/bin/yq"
    result = subprocess.run(
        [yq, "-o=json", ".settings.value", "-"],
        input=b"settings:\n  value: 42\n",
        capture_output=True,
        check=True,
        timeout=5,
        env={"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    if json.loads(result.stdout) != 42:
        raise ValueError("yq did not preserve YAML-to-JSON query semantics")
    hashes["yq"] = file_sha256(yq)
    print(
        json.dumps(
            {
                "status": "passed",
                "pgbackrest_loopback_metrics": True,
                "pgbouncer_cli": True,
                "pgbouncer_live_connection_tested": False,
                "yq_yaml_json": True,
                "sha256": hashes,
                "pid": os.getpid(),
                "database_started": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
