#!/usr/bin/env python3
"""Offline guards: no Docker calls, network access or production mutation."""

import datetime as dt
import importlib.util
import io
import json
import stat
import tempfile
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
            "offsite_verified": False,
            "storage_mode": "local-only",
            "complete_database": True,
            "encrypted_sha256": "c" * 64,
            "database_image_id": "database",
            "application_image_id": "app",
            "source_checkout_sha": "a" * 40,
        }
        path = MagicMock(spec=Path)
        for changes, valid in (
            ({}, True),
            ({"offsite_verified": True}, False),
            ({"complete_database": False}, False),
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
                patch.object(CUTOVER, "verify_operator_receipt"),
            ):
                if valid:
                    CUTOVER.verify_backup(
                        path,
                        "c" * 64,
                        {"Image": "database"},
                        {"Image": "app"},
                        "a" * 40,
                        Path("/private/operator-pc.json"),
                    )
                else:
                    with self.assertRaises(ValueError):
                        CUTOVER.verify_backup(
                            path,
                            "c" * 64,
                            {"Image": "database"},
                            {"Image": "app"},
                            "a" * 40,
                            Path("/private/operator-pc.json"),
                        )

    def test_ciphertext_must_still_match_after_offsite_proof(self):
        proof = {
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            "offsite_verified": False,
            "storage_mode": "local-only",
            "complete_database": True,
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

    def test_offsite_flag_alone_never_replaces_measured_pc_receipt(self):
        proof = {
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            "offsite_verified": False,
            "storage_mode": "local-only",
            "complete_database": True,
            "encrypted_sha256": "c" * 64,
            "database_image_id": "database",
            "application_image_id": "app",
            "source_checkout_sha": "a" * 40,
        }
        path = MagicMock(spec=Path)
        path.read_text.return_value = json.dumps(proof)
        with (
            patch.object(CUTOVER, "protected_file"),
            patch.object(CUTOVER, "digest", return_value="c" * 64),
            self.assertRaisesRegex(ValueError, "measured operator PC receipt"),
        ):
            CUTOVER.verify_backup(
                path, "c" * 64, {"Image": "database"}, {"Image": "app"}, "a" * 40
            )

    def test_operator_receipt_binds_real_copy_age_source_and_frozen_state(self):
        backup = {
            "created_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
            "encrypted_sha256": "c" * 64,
            "encrypted_bytes": 2048,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "apitaxi-full-20261022T190000Z.tar.age"
            receipt = root / (archive.name + ".operator-pc.json")
            good = {
                **backup,
                "schema_version": 1,
                "destination": "operator_pc",
                "archive": archive.name,
                "verified_at": dt.datetime.now(dt.UTC).isoformat(),
                "source_checkout_sha": "a" * 40,
                "database_image_id": "database",
                "application_image_id": "app",
                "ssh_source_verified": True,
                "pc_re_read_sha256_verified": True,
                "age_decryption_verified": True,
                "tar_inventory_verified": True,
                "storage_api_used": False,
                "writers_frozen": True,
                "producer_sha256": "e" * 64,
            }
            for change, valid in (
                ({}, True),
                ({"age_decryption_verified": False}, False),
                ({"source_checkout_sha": "b" * 40}, False),
                ({"encrypted_sha256": "d" * 64}, False),
                ({"writers_frozen": False}, False),
                ({"verified_at": "2020-01-01T00:00:00+00:00"}, False),
            ):
                receipt.write_text(json.dumps(good | change))
                with (
                    self.subTest(change=change),
                    patch.object(CUTOVER, "protected_file"),
                    patch.object(CUTOVER, "digest", return_value="e" * 64),
                ):
                    if valid:
                        CUTOVER.verify_operator_receipt(
                            receipt,
                            archive,
                            backup,
                            {"Image": "database"},
                            {"Image": "app"},
                            "a" * 40,
                            require_frozen=True,
                        )
                    else:
                        with self.assertRaises(ValueError):
                            CUTOVER.verify_operator_receipt(
                                receipt,
                                archive,
                                backup,
                                {"Image": "database"},
                                {"Image": "app"},
                                "a" * 40,
                                require_frozen=True,
                            )

    def test_manual_validation_is_exact_sha_and_never_a_fake_github_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github/workflows/rezo-taxi-core-ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("reviewed workflow")
            producer = root / "deploy/maintenance/operator_migration_proof.py"
            producer.parent.mkdir(parents=True)
            producer.write_text("reviewed producer")
            path = root / "manual-validation.json"
            rows = []
            for name in CUTOVER.MANUAL_CHECKS:
                log = root / (name + ".log")
                log.write_text("actual command output")
                rows.append(
                    {"name": name, "exit_code": 0, "log_sha256": CUTOVER.digest(log)}
                )
            good = {
                "status": "passed",
                "completed_at": dt.datetime.now(dt.UTC).isoformat(),
                "source_checkout_sha": "a" * 40,
                "recipe": CUTOVER.VALIDATION_RECIPE,
                "source_branch": "rezo/internal-service-base",
                "working_tree_clean": True,
                "github_status": "not_used_no_credits",
                "workflow_sha256": CUTOVER.digest(workflow),
                "producer_sha256": CUTOVER.digest(producer),
                "checks": rows,
                "built_images": {
                    name: "sha256:" + "f" * 64 for name in ("web", "worker", "beat")
                },
            }
            for change, valid in (
                ({}, True),
                ({"github_status": "success"}, False),
                ({"source_checkout_sha": "b" * 40}, False),
                ({"checks": rows[:-1]}, False),
                ({"checks": [dict(rows[0], exit_code=1), *rows[1:]]}, False),
            ):
                path.write_text(json.dumps(good | change))
                with (
                    self.subTest(change=change),
                    patch.object(CUTOVER, "CHECKOUT", root),
                    patch.object(CUTOVER, "protected_file"),
                ):
                    if valid:
                        CUTOVER.verify_manual_validation(
                            path, CUTOVER.digest(path), "a" * 40
                        )
                    else:
                        with self.assertRaises(ValueError):
                            CUTOVER.verify_manual_validation(
                                path, CUTOVER.digest(path), "a" * 40
                            )

    def test_missing_frozen_copy_times_out_before_database_exposure(self):
        with (
            patch.object(CUTOVER.time, "monotonic", side_effect=[0, 601]),
            self.assertRaises(TimeoutError),
        ):
            CUTOVER.wait_for_frozen_pc_copy(
                Path("/private/backup.tar.age"),
                {"encrypted_sha256": "c" * 64},
                {"Image": "database"},
                {"Image": "app"},
                "a" * 40,
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

    def test_clone_proof_binds_exact_image_recipe_and_freshness(self):
        proof = {
            "schema_version": 1,
            "status": "passed",
            "completed_at": dt.datetime.now(dt.UTC).isoformat(),
            "source_image": CUTOVER.OLD_IMAGE,
            "target_image": CUTOVER.NEW_IMAGE,
            "evidence_sha256": "a" * 64,
            "source_dump_sha256": "a" * 64,
            "test_script_sha256": "a" * 64,
            "evidence_sql_sha256": "a" * 64,
            "production_changed": False,
        }
        for change, valid in (
            ({}, True),
            ({"target_image": "sha256:" + "b" * 64}, False),
            ({"test_script_sha256": "b" * 64}, False),
            ({"production_changed": True}, False),
            ({"completed_at": (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).isoformat()}, False),
        ):
            with (
                self.subTest(change=change),
                patch.object(CUTOVER, "protected_file"),
                patch.object(CUTOVER, "digest", return_value="a" * 64),
                patch.object(Path, "read_text", return_value=json.dumps(proof | change)),
            ):
                if valid:
                    CUTOVER.verify_test(Path("/var/backups/rezo-maintenance-test-reviewed"), "a" * 64)
                else:
                    with self.assertRaises(ValueError):
                        CUTOVER.verify_test(Path("/var/backups/rezo-maintenance-test-reviewed"), "a" * 64)

    def test_symlink_and_world_readable_proofs_are_rejected(self):
        path = MagicMock(spec=Path)
        for mode in (stat.S_IFLNK | 0o600, stat.S_IFREG | 0o644):
            path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=0)
            with self.assertRaises(ValueError):
                CUTOVER.protected_file(path)


if __name__ == "__main__":
    unittest.main()
