#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Read-only maintenance checks. No notifications, mutations or dependency installs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import quote, unquote


def github(path: str) -> object:
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode:
        # Do not echo the CLI environment, response bodies or credentials.
        raise RuntimeError(f"GitHub GET failed: {path}")
    return json.loads(result.stdout)


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def date_due(value: str) -> dt.date:
    # Month-only upstream EOL dates trigger conservatively on the first day.
    return dt.date.fromisoformat(value + "-01" if len(value) == 7 else value)


def date_checks(policy: dict, today: dt.date) -> list[tuple[str, str]]:
    findings = []
    if today > date_due(policy["review_due"]):
        findings.append(
            (
                "error",
                "Maintenance inventory review is overdue; verify versions and support dates.",
            )
        )
    for runtime in policy["runtimes"]:
        remaining = (date_due(runtime["eol"]) - today).days
        if remaining < 0:
            findings.append(
                (
                    "error",
                    f"End of support reached: {runtime['name']} ({runtime['eol']}).",
                )
            )
        elif remaining <= policy["warning_days_before_eol"]:
            findings.append(
                (
                    "warning",
                    f"Support ends in {remaining} days: {runtime['name']} ({runtime['eol']}).",
                )
            )
    for review in policy["reviews"]:
        remaining = (date_due(review["due"]) - today).days
        if remaining < 0:
            findings.append(
                (
                    "error",
                    f"Review overdue: {review['name']}; owner {review['owner']}; due {review['due']}.",
                )
            )
        elif remaining <= 7:
            findings.append(
                (
                    "warning",
                    f"Review due {review['due']}: {review['name']}; owner {review['owner']}.",
                )
            )
    return findings


def scheduled_audit_checks(
    repo: str, config: dict, now: dt.datetime, workflow: str
) -> list[tuple[str, str]]:
    findings = []
    endpoint = f"repos/{repo}/actions/workflows/{quote(workflow, safe='')}/runs"
    data = github(
        f"{endpoint}?event=schedule&branch={quote(config['branch'], safe='')}&per_page=20"
    )
    runs = data["workflow_runs"]
    completed = [run for run in runs if run["status"] == "completed"]
    if completed and completed[0]["conclusion"] != "success":
        findings.append(
            (
                "error",
                f"Latest scheduled audit failed ({workflow}): {completed[0]['html_url']}",
            )
        )
    successful = [run for run in completed if run["conclusion"] == "success"]
    if not successful:
        findings.append(
            ("error", f"{repo}: no successful scheduled dependency audit ({workflow}).")
        )
    else:
        age = (now - parse_time(successful[0]["created_at"])).total_seconds() / 3600
        if age > config["audit_max_age_hours"]:
            findings.append(
                (
                    "error",
                    f"{repo}: last successful scheduled audit is {age:.1f} hours old: {successful[0]['html_url']}",
                )
            )
    return findings


def workflow_checks(repo: str, config: dict, now: dt.datetime) -> list[tuple[str, str]]:
    findings = []
    for workflow in [config["audit_workflow"], *config.get("additional_workflows", [])]:
        findings.extend(scheduled_audit_checks(repo, config, now, workflow))
    workflows = github(f"repos/{repo}/actions/workflows?per_page=100")["workflows"]
    updater = next(
        (w for w in workflows if w["path"] == "dynamic/dependabot/dependabot-updates"),
        None,
    )
    if updater is None:
        findings.append(
            (
                "error",
                f"{repo}: no Dependabot update workflow found; check configuration and settings.",
            )
        )
    else:
        updates = github(
            f"repos/{repo}/actions/workflows/{updater['id']}/runs?per_page=100"
        )["workflow_runs"]
        successful_updates = [run for run in updates if run["conclusion"] == "success"]
        if (
            not successful_updates
            or (now - parse_time(successful_updates[0]["created_at"])).total_seconds()
            / 3600
            > config["dependabot_max_age_hours"]
        ):
            findings.append(
                (
                    "error",
                    f"{repo}: no successful Dependabot update within {config['dependabot_max_age_hours']} hours.",
                )
            )
        if updates and updates[0]["conclusion"] == "failure":
            # security_update_not_needed is also displayed as a failed run.
            findings.append(
                (
                    "warning",
                    f"Inspect latest Dependabot result (may mean update no longer needed): {updates[0]['html_url']}",
                )
            )
    return findings


def pull_request_checks(
    repo: str, config: dict, now: dt.datetime
) -> list[tuple[str, str]]:
    findings = []
    pulls = github(f"repos/{repo}/pulls?state=open&per_page=100")
    if len(pulls) == 100:
        findings.append(
            (
                "error",
                f"{repo}: queue contains at least 100 open PRs; complete review required.",
            )
        )
    for pull in pulls:
        if pull["user"]["login"] != "dependabot[bot]":
            continue
        age = (now - parse_time(pull["created_at"])).days
        if age > config["pull_request_max_days"]:
            findings.append(
                (
                    "error",
                    f"Dependabot PR awaits review for {age} days: {pull['html_url']}",
                )
            )
        elif age > config["pull_request_warning_days"]:
            findings.append(
                (
                    "warning",
                    f"Dependabot PR awaits review for {age} days: {pull['html_url']}",
                )
            )
    return findings


def graph_checks(repo: str, config: dict, root: Path) -> list[tuple[str, str]]:
    findings = []
    if not config["uv_locks"]:
        return findings
    local = {}
    for filename in config["uv_locks"]:
        with (root / filename).open("rb") as handle:
            for package in tomllib.load(handle)["package"]:
                if "registry" in package.get("source", {}):
                    local.setdefault(canonical(package["name"]), set()).add(
                        package["version"]
                    )
    graph = {}
    for package in github(f"repos/{repo}/dependency-graph/sbom")["sbom"]["packages"]:
        for reference in package.get("externalRefs", []):
            locator = reference.get("referenceLocator", "")
            if locator.startswith("pkg:pypi/") and "@" in locator:
                name, version = locator.removeprefix("pkg:pypi/").split("@", 1)
                graph.setdefault(canonical(unquote(name)), set()).add(unquote(version))
    for name, versions in sorted(local.items()):
        observed = graph.get(name, set())
        if versions != observed:
            findings.append(
                (
                    "error",
                    f"Dependency graph differs from lockfile for {name}: lock={','.join(sorted(versions))}, graph={','.join(sorted(observed)) or 'missing'}. Reconcile the graph; do not dismiss alerts without evidence.",
                )
            )
    return findings


def emit(level: str, message: str) -> None:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{escaped}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=Path(__file__).with_name("policy.json")
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "Kelkaz-reu-sites/kelkaz")
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Check support/review dates only; never calls GitHub.",
    )
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    now = dt.datetime.now(dt.UTC)
    findings = date_checks(policy, now.date())
    if not args.offline:
        config = policy["repositories"][args.repo]
        for check in (workflow_checks, pull_request_checks):
            try:
                findings.extend(check(args.repo, config, now))
            except (
                RuntimeError,
                KeyError,
                ValueError,
                subprocess.TimeoutExpired,
            ) as error:
                findings.append(
                    (
                        "error",
                        f"Maintenance check incomplete: {type(error).__name__} ({check.__name__}).",
                    )
                )
        try:
            findings.extend(graph_checks(args.repo, config, args.root))
        except (
            OSError,
            RuntimeError,
            KeyError,
            ValueError,
            subprocess.TimeoutExpired,
        ) as error:
            findings.append(
                (
                    "error",
                    f"Graph reconciliation check incomplete: {type(error).__name__}.",
                )
            )
    for level, message in findings:
        emit(level, message)
    errors = sum(level == "error" for level, _ in findings)
    print(
        f"Maintenance: {errors} error(s), {sum(level == 'warning' for level, _ in findings)} warning(s). No change or notification sent."
    )
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
