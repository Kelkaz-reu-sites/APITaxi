#!/usr/bin/env python3
"""Submit the committed production lock to APITaxi's dependency graph."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

REPOSITORY = "Kelkaz-reu-sites/APITaxi"
BRANCH = "rezo/internal-service-base"
CORRELATOR = "apitaxi-production-requirements-lock"
DETECTOR = "apitaxi-requirements-lock"
ROOT = Path(__file__).resolve().parents[2]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_packages(content: str) -> dict[str, str]:
    packages = {}
    hashed = set()
    current = None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"--hash=sha256:[a-f0-9]{64}(?:\s+\\)?", line):
            if current is None:
                raise ValueError("Hash without a locked package")
            hashed.add(current)
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)(?:\s+\\)?", line)
        if not match:
            raise ValueError("Unsupported or unresolved production lock entry")
        current = canonical(match[1])
        if current in packages:
            raise ValueError("Duplicate production lock entry")
        packages[current] = match[2]
    if not packages or hashed != packages.keys():
        raise ValueError("Production lock must contain hashes for every package")
    return packages


def snapshot(lock: str, requirements: str, sha: str) -> dict:
    packages = locked_packages(lock)
    direct = set()
    for line in requirements.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?([<>=!~0-9A-Za-z.,*+_-]*)", line
        )
        if not match:
            raise ValueError("Unsupported direct requirement")
        name = canonical(match[1])
        if name not in packages:
            raise ValueError("Direct dependency missing from the production lock")
        if match[2].startswith("==") and match[2][2:] != packages[name]:
            raise ValueError("Direct pin and production lock disagree")
        direct.add(name)
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "version": 0,
        "sha": sha,
        "ref": "refs/heads/" + BRANCH,
        "job": {
            "id": sha + "-" + now,
            "correlator": CORRELATOR,
            "html_url": "https://github.com/" + REPOSITORY + "/commit/" + sha,
        },
        "detector": {
            "name": DETECTOR,
            "version": "1.0.0",
            "url": "https://github.com/" + REPOSITORY,
        },
        "scanned": now,
        "metadata": {
            "lock_sha256": hashlib.sha256(lock.encode()).hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements.encode()).hexdigest(),
            "production_lock": "requirements.lock",
            "python": "3.12",
        },
        "manifests": {
            "requirements.txt": {
                "name": "requirements.txt",
                "file": {"source_location": "requirements.txt"},
                "resolved": {
                    name: {
                        "package_url": "pkg:pypi/"
                        + name
                        + "@"
                        + quote(version, safe=""),
                        "relationship": "direct" if name in direct else "indirect",
                        "scope": "runtime",
                    }
                    for name, version in sorted(packages.items())
                },
            }
        },
    }


def git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments], capture_output=True, text=True, check=True
    )
    return result.stdout


def github(endpoint: str, payload: dict | None = None) -> dict:
    command = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        "repos/" + REPOSITORY + "/" + endpoint,
    ]
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        command,
        input=None if payload is None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("GitHub dependency graph API request failed")
    return json.loads(result.stdout)


def graph_matches(sbom: dict, expected: dict[str, str]) -> bool:
    actual = {}
    for package in sbom.get("sbom", {}).get("packages", []):
        actual.setdefault(canonical(package.get("name", "")), set()).add(
            package.get("versionInfo")
        )
    return all(actual.get(name) == {version} for name, version in expected.items())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Write one snapshot through the GitHub API; no Actions dispatch.",
    )
    args = parser.parse_args()
    sha = git("rev-parse", "HEAD").strip()
    lock = git("show", sha + ":requirements.lock")
    data = snapshot(lock, git("show", sha + ":requirements.txt"), sha)
    expected = locked_packages(lock)
    summary = {
        "source_sha": sha,
        "packages": len(expected),
        "manifest": "requirements.txt",
        "submitted": False,
    }
    if args.submit:
        if git("status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError("Refusing to submit from a dirty checkout")
        if git("branch", "--show-current").strip() not in (BRANCH, ""):
            raise RuntimeError("Unexpected local branch")
        if (
            os.environ.get("GITHUB_REPOSITORY", REPOSITORY).lower()
            != REPOSITORY.lower()
        ):
            raise RuntimeError("Unexpected workflow repository")
        if github("")["default_branch"] != BRANCH:
            raise RuntimeError("Production must be the default branch")
        if github("git/ref/heads/" + BRANCH)["object"]["sha"] != sha:
            raise RuntimeError(
                "Snapshot source must equal the current production branch HEAD"
            )
        response = github("dependency-graph/snapshots", data)
        if response.get("result") != "SUCCESS":
            raise RuntimeError("GitHub did not accept the dependency snapshot")
        summary.update(submitted=True, snapshot_id=response["id"])
        print(json.dumps(summary, sort_keys=True), flush=True)
        for attempt in range(7):
            if graph_matches(github("dependency-graph/sbom"), expected):
                print(
                    json.dumps(
                        {"graph_matches_lock": True, "packages": len(expected)},
                        sort_keys=True,
                    )
                )
                return
            if attempt < 6:
                time.sleep(5)
        raise RuntimeError(
            "Snapshot accepted but graph recalculation is not yet confirmed"
        )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
