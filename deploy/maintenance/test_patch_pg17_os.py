"""Reject an unrelated compose change, stale backup or wrong CI before activation."""

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import patch_pg17_os as patch


class PatchGuards(unittest.TestCase):
    def test_compose_may_only_replace_image(self):
        before = f"image: {patch.SOURCE}\nvolume: existing-data\n"
        after = before.replace(patch.SOURCE, patch.CANDIDATE)
        patch.validate_compose(before, after)
        for invalid in (
            after.replace("existing-data", "new-data"),
            after + "extra: changed\n",
            before,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                patch.validate_compose(before, invalid)

    def test_ci_must_be_successful_exact_source_and_production_branch(self):
        run = {
            "head_sha": "a" * 40,
            "head_branch": "rezo/internal-service-base",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "path": ".github/workflows/rezo-taxi-core-ci.yml",
        }
        patch.validate_ci(run, "a" * 40)
        for key, value in [
            ("head_sha", "b" * 40),
            ("head_branch", "main"),
            ("conclusion", "failure"),
            ("event", "pull_request"),
            ("path", "other.yml"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                patch.validate_ci({**run, key: value}, "a" * 40)

    def test_encrypted_backup_must_match_source_digest_and_freshness(self):
        now = dt.datetime(2026, 11, 4, 16, 30, tzinfo=dt.UTC)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "apitaxi-full-20261104T161731Z.tar.age"
            (root / name).write_bytes(b"fixture-ciphertext")
            digest = hashlib.sha256(b"fixture-ciphertext").hexdigest()
            proof = {
                "created_at": "20261104T161731Z",
                "complete_database": True,
                "database_image_id": patch.SOURCE,
                "encrypted_sha256": digest,
            }
            metadata = root / (name + ".json")
            metadata.write_text(json.dumps(proof))
            patch.validate_backup(root, name, digest, now)
            for changed in (
                {"created_at": "20261103T161731Z"},
                {"database_image_id": patch.CANDIDATE},
                {"complete_database": False},
            ):
                metadata.write_text(json.dumps({**proof, **changed}))
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    patch.validate_backup(root, name, digest, now)
            metadata.write_text(json.dumps(proof))
            (root / name).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                patch.validate_backup(root, name, digest, now)


if __name__ == "__main__":
    unittest.main()
