#!/usr/bin/env python3
"""Activate the three CI-validated APT-patched applications, with rollback.

PostgreSQL, Redis, volumes and schemas are never recreated by this operation.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import urllib.request
from pathlib import Path

import patch_pg17_os as guards
import cutover_postgres

PREVIOUS = {
    "taxi-web": "sha256:f9554b2a7dc781322d4cf251f1f26e7d982f2e4e7660251ae1359a26e315b48a",
    "taxi-worker": "sha256:452d657d2d4b81034cb511ae82650d85bafb1495427fc2eab5cb909d40a7f794",
    "taxi-beat": "sha256:b3a7da5e20a877670880d97d73eaf11ec89aed52bbcb412f1be166058758fcf8",
}


def validate_image(info, image_id, sha):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Immutable image IDs are required")
    if (
        info["Id"] != image_id
        or info.get("Config", {})
        .get("Labels", {})
        .get("org.opencontainers.image.revision")
        != sha
    ):
        raise ValueError("The image must be built from the exact validated source")


def activate(sha, ci_run, images):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("An exact source SHA is required")
    os.chdir(guards.ROOT)
    # This is the measured production tag retained since the October cutover.
    os.environ["REZO_TAXI_CORE_IMAGE_TAG"] = "8827452"
    with Path("/tmp/rezo-production-deploy.lock").open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with urllib.request.urlopen(
            f"https://api.github.com/repos/Kelkaz-reu-sites/APITaxi/actions/runs/{ci_run}",
            timeout=30,
        ) as response:
            guards.validate_ci(json.load(response), sha)
        if guards.git("rev-parse", "HEAD") != sha or guards.git(
            "diff", "--name-only", "HEAD"
        ):
            raise ValueError("Production must contain the exact clean validated source")
        if Path(__file__).read_text().strip() != guards.git(
            "show", f"{sha}:deploy/maintenance/patch_app_os.py"
        ):
            raise ValueError("The installer differs from the reviewed source")
        if guards.inspect_database() != guards.CANDIDATE:
            raise ValueError("Activate the reviewed PG17 OS patch first")
        compose = json.loads(
            guards.command(*guards.COMPOSE, "config", "--format", "json")
        )
        references = {}
        for service, image_id in images.items():
            running = json.loads(
                guards.command(
                    "docker",
                    "inspect",
                    "--format",
                    '{"image":{{json .Image}},"reference":{{json .Config.Image}}}',
                    f"rezo-taxi-core-{service}-1",
                )
            )
            if (
                running["image"] != PREVIOUS[service]
                or compose["services"][service]["image"] != running["reference"]
            ):
                raise ValueError(
                    "The reviewed application image or Compose tag changed"
                )
            references[service] = running["reference"]
            info = json.loads(guards.command("docker", "image", "inspect", image_id))[0]
            validate_image(info, image_id, sha)
            guards.command(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-c",
                "dpkg --compare-versions \"$(dpkg-query -W -f='${Version}' apt)\" ge 2.8.4ubuntu0.1",
            )
            guards.command(
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
                image_id,
            )
            guards.command(
                "docker",
                "image",
                "tag",
                PREVIOUS[service],
                f"rezo-{service}:rollback-20261105",
            )
        guards.command(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/venv/bin/python",
            images["taxi-web"],
            "-B",
            "/app/deploy/check_runtime.py",
        )
        try:
            for service, image_id in images.items():
                guards.command("docker", "image", "tag", image_id, references[service])
            guards.command(
                *guards.COMPOSE,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "240",
                *guards.WRITERS,
            )
            for service, image_id in images.items():
                if (
                    guards.command(
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Image}}",
                        f"rezo-taxi-core-{service}-1",
                    )
                    != image_id
                ):
                    raise ValueError("The activated image differs")
            if guards.inspect_database() != guards.CANDIDATE:
                raise ValueError("The database identity changed")
            cutover_postgres.application_database_smoke()
            guards.command(
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
            for service, image_id in PREVIOUS.items():
                guards.command("docker", "image", "tag", image_id, references[service])
            guards.command(
                *guards.COMPOSE,
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "240",
                *guards.WRITERS,
            )
            raise
        state = Path("/var/backups/rezo-taxi-core/os-patch-20261105")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (state / "application-receipt.json").write_text(
            json.dumps(
                {
                    "source_sha": sha,
                    "ci_run": ci_run,
                    "images": images,
                    "previous_images": PREVIOUS,
                    "database_image_id": guards.CANDIDATE,
                    "runtime_checks_passed": True,
                    "completed_at": dt.datetime.now(dt.UTC).isoformat(),
                },
                indent=2,
            )
            + "\n"
        )
        print("TAXI_APP_OS_PATCH_DEPLOYED=" + sha, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--ci-run", type=int, required=True)
    for service in ("web", "worker", "beat"):
        parser.add_argument(f"--{service}-image", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Use the reviewed sudo -n operation on the REZO VPS only")
    os.umask(0o077)
    activate(
        args.source_sha,
        args.ci_run,
        {
            "taxi-web": args.web_image,
            "taxi-worker": args.worker_image,
            "taxi-beat": args.beat_image,
        },
    )
