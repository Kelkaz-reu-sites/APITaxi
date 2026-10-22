#!/usr/bin/env python3
"""Offline guards: no Docker calls, network access or production mutation."""

import datetime as dt
import importlib.util
import io
import json
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "cutover_postgres", Path(__file__).with_name("cutover_postgres.py")
)
CUTOVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CUTOVER)


class CutoverGuards(unittest.TestCase):
    def test_environment_guard_rejects_unintended_operational_setting_change(self):
        current = {
            "Config": {"Env": ["PASSWORD=test-secret", "HANDOFF=true", "PATH=/bin"]}
        }
        CUTOVER.verify_service_environment(
            {"PASSWORD": "test-secret", "HANDOFF": "true"}, current
        )
        with self.assertRaises(ValueError) as failure:
            CUTOVER.verify_service_environment(
                {"PASSWORD": "another-secret", "HANDOFF": "false"}, current
            )
        self.assertNotIn("test-secret", str(failure.exception))
        self.assertNotIn("another-secret", str(failure.exception))

    def test_upstream_retention_job_replacement_preserves_pause_state(self):
        for scheduled in (True, False):
            with self.subTest(scheduled=scheduled):
                desired = CUTOVER.desired_job_state(
                    [
                        {
                            "id": 2,
                            "proc_name": "policy_job_error_retention",
                            "scheduled": scheduled,
                        }
                    ],
                    [
                        {
                            "id": 3,
                            "proc_name": "policy_job_stat_history_retention",
                            "scheduled": True,
                        }
                    ],
                )
                self.assertEqual(desired[0]["scheduled"], scheduled)
                self.assertEqual(desired[0]["id"], 3)

    def test_job_resume_preserves_original_pause_state_and_checks_identity(self):
        original = [
            {"id": 1, "proc_name": "compression", "scheduled": True},
            {"id": 2, "proc_name": "retention", "scheduled": False},
        ]
        restored = [
            {"id": 1, "proc_name": "compression", "scheduled": False},
            {"id": 2, "proc_name": "retention", "scheduled": False},
            {"id": 3, "proc_name": "new_system_job", "scheduled": True},
        ]
        self.assertEqual(
            [j["scheduled"] for j in CUTOVER.desired_job_state(original, restored)],
            [True, False, True],
        )
        with self.assertRaises(ValueError):
            CUTOVER.desired_job_state(
                original, [{**restored[0], "proc_name": "unexpected"}, *restored[1:]]
            )

    def test_ci_requires_exact_commit_branch_workflow_and_success(self):
        good = {
            "head_sha": "a" * 40,
            "head_branch": "rezo/internal-service-base",
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/rezo-taxi-core-ci.yml",
        }
        with patch.object(
            CUTOVER.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(good).encode()),
        ):
            CUTOVER.verify_ci(123, "a" * 40)
        for field, value in (
            ("head_sha", "b" * 40),
            ("head_branch", "master"),
            ("conclusion", "failure"),
            ("path", ".github/workflows/other.yml"),
        ):
            with (
                self.subTest(field=field),
                patch.object(
                    CUTOVER.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(
                        json.dumps({**good, field: value}).encode()
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                CUTOVER.verify_ci(123, "a" * 40)

    def test_backup_requires_fresh_verified_digest_and_matching_source_inventory(self):
        good = {
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            "offsite_verified": True,
            "encrypted_sha256": "c" * 64,
            "database_image_id": "database",
            "application_image_id": "app",
            "source_checkout_sha": "a" * 40,
        }
        path = MagicMock(spec=Path)
        for changes, valid in (
            ({}, True),
            ({"offsite_verified": False}, False),
            ({"encrypted_sha256": "d" * 64}, False),
            ({"created_at": "20200101T000000Z"}, False),
            ({"database_image_id": "another"}, False),
            ({"source_checkout_sha": "b" * 40}, False),
        ):
            path.read_text.return_value = json.dumps({**good, **changes})
            with (
                self.subTest(changes=changes),
                patch.object(CUTOVER, "protected_file"),
                patch.object(CUTOVER, "digest", return_value="c" * 64),
            ):
                if valid:
                    CUTOVER.verify_backup(
                        path,
                        "c" * 64,
                        {"Image": "database"},
                        {"Image": "app"},
                        "a" * 40,
                    )
                else:
                    with self.assertRaises(ValueError):
                        CUTOVER.verify_backup(
                            path,
                            "c" * 64,
                            {"Image": "database"},
                            {"Image": "app"},
                            "a" * 40,
                        )

    def test_ciphertext_must_still_match_after_offsite_proof(self):
        proof = {
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            "offsite_verified": True,
            "encrypted_sha256": "c" * 64,
            "database_image_id": "database",
            "application_image_id": "app",
            "source_checkout_sha": "a" * 40,
        }
        path = MagicMock(spec=Path)
        path.read_text.return_value = json.dumps(proof)
        with (
            patch.object(CUTOVER, "protected_file"),
            patch.object(CUTOVER, "digest", return_value="d" * 64),
            self.assertRaises(ValueError),
        ):
            CUTOVER.verify_backup(
                path, "c" * 64, {"Image": "database"}, {"Image": "app"}, "a" * 40
            )

    def test_pass_marker_does_not_replace_four_matching_evidence_files(self):
        path = Path("/var/backups/rezo-maintenance-test-reviewed")
        with (
            patch.object(CUTOVER, "protected_file"),
            patch.object(
                CUTOVER,
                "digest",
                side_effect=lambda p: (
                    "b" * 64 if p.name == "pg17-final.evidence" else "a" * 64
                ),
            ),
            self.assertRaises(ValueError),
        ):
            CUTOVER.verify_test(path, "a" * 64)

    def test_symlink_and_world_readable_proofs_are_rejected(self):
        path = MagicMock(spec=Path)
        for mode in (stat.S_IFLNK | 0o600, stat.S_IFREG | 0o644):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=0)
            with self.assertRaises(ValueError):
                CUTOVER.protected_file(path)


if __name__ == "__main__":
    unittest.main()
