#!/usr/bin/env python3
"""Offline regressions: stale controls must not appear healthy."""

import datetime as dt
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "check_maintenance", Path(__file__).with_name("check_maintenance.py")
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
NOW = dt.datetime(2026, 10, 22, 12, tzinfo=dt.UTC)


class MaintenanceChecks(unittest.TestCase):
    def test_support_warning_does_not_block_critical_deployment(self):
        policy = {
            "review_due": "2026-11-22",
            "runtimes": [{"name": "PG14", "eol": "2026-11-12"}],
            "warning_days_before_eol": 90,
            "reviews": [],
        }
        self.assertEqual(
            [level for level, _ in CHECK.date_checks(policy, NOW.date())], ["warning"]
        )

    def test_expired_reviews_and_support_are_failures(self):
        policy = {
            "review_due": "2026-10-01",
            "runtimes": [{"name": "old", "eol": "2026-09"}],
            "warning_days_before_eol": 90,
            "reviews": [{"name": "exception", "due": "2026-10-01", "owner": "Phil"}],
        }
        self.assertEqual(
            [level for level, _ in CHECK.date_checks(policy, NOW.date())],
            ["error", "error", "error"],
        )

    def test_fresh_success_does_not_hide_a_more_recent_failed_audit(self):
        config = {
            "branch": "main",
            "audit_workflow": "security.yml",
            "audit_max_age_hours": 36,
            "dependabot_max_age_hours": 240,
        }
        calls = [
            {
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "created_at": "2026-10-22T09:00:00Z",
                        "html_url": "https://example.test/failed",
                    },
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-10-21T09:00:00Z",
                        "html_url": "https://example.test/good",
                    },
                ]
            },
            {"workflows": [{"path": "dynamic/dependabot/dependabot-updates", "id": 1}]},
            {
                "workflow_runs": [
                    {"conclusion": "success", "created_at": "2026-10-21T09:00:00Z"}
                ]
            },
        ]
        with patch.object(CHECK, "github", side_effect=calls):
            findings = CHECK.workflow_checks("owner/repo", config, NOW)
        self.assertTrue(
            any(level == "error" and "failed" in text for level, text in findings)
        )

    def test_missing_scheduled_audit_and_stale_dependabot_are_failures(self):
        config = {
            "branch": "main",
            "audit_workflow": "security.yml",
            "audit_max_age_hours": 36,
            "dependabot_max_age_hours": 240,
        }
        with patch.object(
            CHECK,
            "github",
            side_effect=[
                {"workflow_runs": []},
                {
                    "workflows": [
                        {"path": "dynamic/dependabot/dependabot-updates", "id": 1}
                    ]
                },
                {
                    "workflow_runs": [
                        {"conclusion": "success", "created_at": "2026-09-06T09:00:00Z"}
                    ]
                },
            ],
        ):
            findings = CHECK.workflow_checks("owner/repo", config, NOW)
        self.assertEqual(sum(level == "error" for level, _ in findings), 2)

    def test_old_pull_request_does_not_look_recent_after_a_bot_rebase(self):
        pull = {
            "user": {"login": "dependabot[bot]"},
            "created_at": "2026-06-12T00:00:00Z",
            "updated_at": "2026-10-22T10:00:00Z",
            "html_url": "https://example.test/pr",
        }
        with patch.object(CHECK, "github", return_value=[pull]):
            findings = CHECK.pull_request_checks(
                "owner/repo",
                {"pull_request_warning_days": 14, "pull_request_max_days": 30},
                NOW,
            )
        self.assertEqual(findings[0][0], "error")

    def test_graph_mismatch_remains_visible_and_matching_graph_is_clean(self):
        lock = b'[[package]]\nname="Pillow"\nversion="13.0.0"\nsource={registry="https://pypi.org/simple"}\n'
        for version, expected in (("12.3.0", 1), ("13.0.0", 0)):
            graph = {
                "sbom": {
                    "packages": [
                        {
                            "externalRefs": [
                                {"referenceLocator": f"pkg:pypi/pillow@{version}"}
                            ]
                        }
                    ]
                }
            }
            with (
                patch.object(Path, "open", return_value=io.BytesIO(lock)),
                patch.object(CHECK, "github", return_value=graph),
            ):
                findings = CHECK.graph_checks(
                    "owner/repo", {"uv_locks": ["api/uv.lock"]}, Path("/unused")
                )
            self.assertEqual(len(findings), expected)


if __name__ == "__main__":
    unittest.main()
