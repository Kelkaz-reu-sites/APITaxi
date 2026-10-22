"""Offline checks for production snapshots; never contact GitHub."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import submit_dependency_graph as graph


class ProductionSnapshotTests(unittest.TestCase):
    def test_repository_metadata_endpoint_has_no_trailing_slash(self):
        with patch.object(
            graph.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=0, stdout='{"default_branch":"rezo/internal-service-base"}'
            ),
        ) as request:
            result = graph.github("")
        self.assertEqual(
            request.call_args.args[0][-1], "repos/Kelkaz-reu-sites/APITaxi"
        )
        self.assertEqual(result["default_branch"], graph.BRANCH)

    def test_full_lock_is_exactly_the_83_corrected_runtime_versions(self):
        root = Path(__file__).resolve().parents[2]
        lock = (root / "requirements.lock").read_text()
        data = graph.snapshot(lock, (root / "requirements.txt").read_text(), "a" * 40)
        resolved = data["manifests"]["requirements.txt"]["resolved"]
        self.assertEqual(len(resolved), 83)
        self.assertEqual(resolved["werkzeug"]["package_url"], "pkg:pypi/werkzeug@3.1.9")
        self.assertEqual(resolved["geopy"]["package_url"], "pkg:pypi/geopy@2.5.0")
        self.assertEqual({x["scope"] for x in resolved.values()}, {"runtime"})
        self.assertEqual(data["ref"], "refs/heads/rezo/internal-service-base")
        self.assertEqual(data["job"]["correlator"], graph.CORRELATOR)
        self.assertEqual(data["detector"]["name"], graph.DETECTOR)
        self.assertEqual(resolved["werkzeug"]["relationship"], "direct")

    def test_unresolved_duplicate_or_unhashed_entries_are_rejected(self):
        for lock in (
            "werkzeug>=3\n",
            "werkzeug==3.1.9\n",
            "werkzeug @ https://example.test/package\n",
            "Werkzeug==3.1.9\nwerkzeug==3.1.9\n",
        ):
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                graph.locked_packages(lock)

    def test_direct_pin_must_match_and_sbom_cannot_hide_an_old_version(self):
        lock = "werkzeug==3.1.9 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        with self.assertRaises(ValueError):
            graph.snapshot(lock, "Werkzeug==3.1.6\n", "a" * 40)
        expected = {"werkzeug": "3.1.9"}
        self.assertTrue(
            graph.graph_matches(
                {"sbom": {"packages": [{"name": "Werkzeug", "versionInfo": "3.1.9"}]}},
                expected,
            )
        )
        self.assertFalse(
            graph.graph_matches(
                {
                    "sbom": {
                        "packages": [
                            {"name": "Werkzeug", "versionInfo": "3.1.9"},
                            {"name": "Werkzeug", "versionInfo": "3.1.6"},
                        ]
                    }
                },
                expected,
            )
        )


if __name__ == "__main__":
    unittest.main()
