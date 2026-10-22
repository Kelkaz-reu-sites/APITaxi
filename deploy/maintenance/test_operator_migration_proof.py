"""Offline tests: no SSH, age execution, Docker or application dependencies."""

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
SPEC = importlib.util.spec_from_file_location(
    "migration_proof", Path(__file__).with_name("operator_migration_proof.py")
)
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


class OperatorMigrationProofTests(unittest.TestCase):
    def archive(self, *, wrong_dump=False, extra=False):
        dump = b"PGDMP" + b"dummy backup bytes" * 100
        proof = {
            "created_at": "20261022T190000Z",
            "database_image_id": "database",
            "application_image_id": "app",
            "source_checkout_sha": "a" * 40,
            "database_sha256": hashlib.sha256(dump).hexdigest(),
            "database_bytes": len(dump),
        }
        files = [
            ("database.dump", b"wrong" if wrong_dump else dump),
            ("roles.sql", b"CREATE ROLE dummy;"),
            ("manifest.json", json.dumps(proof).encode()),
        ]
        if extra:
            files.append(("../unexpected", b"dummy"))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, contents in files:
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                archive.addfile(info, io.BytesIO(contents))
        return output.getvalue(), proof

    def test_age_plaintext_tar_and_dump_are_verified_without_extraction(self):
        archive, proof = self.archive()
        PROOF.validate_decrypted_backup(archive, proof)

    def test_wrong_dump_unexpected_member_and_manifest_identity_are_rejected(self):
        for kwargs in ({"wrong_dump": True}, {"extra": True}):
            archive, proof = self.archive(**kwargs)
            with self.assertRaises(ValueError):
                PROOF.validate_decrypted_backup(archive, proof)
        archive, proof = self.archive()
        with self.assertRaises(ValueError):
            PROOF.validate_decrypted_backup(
                archive, proof | {"source_checkout_sha": "b" * 40}
            )

    def test_validation_recipe_is_complete_local_isolated_and_has_no_migration(self):
        commands = PROOF.validation_commands("a" * 40)
        self.assertEqual(
            tuple(name for name, _ in commands), PROOF.guards.MANUAL_CHECKS
        )
        for name, arguments in commands:
            self.assertNotIn("--execute", arguments)
            self.assertNotIn("sudo", arguments)
            self.assertNotIn("ssh", arguments)
            if arguments[:2] == ["docker", "compose"]:
                self.assertIn("rezo-taxi-validation-" + "a" * 12, arguments)
                self.assertIn("/dev/null", arguments)
            if name == "application_tests":
                self.assertIn("--no-deps", arguments)

    def test_private_receipt_is_0600_and_never_replaces_existing_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            PROOF.private_json(path, {"dummy": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                PROOF.private_json(path, {"dummy": False})
            self.assertEqual(json.loads(path.read_text()), {"dummy": True})

    def test_untrusted_archive_path_fails_before_network(self):
        with patch.object(PROOF.importlib.util, "spec_from_file_location") as load:
            with self.assertRaises(ValueError):
                PROOF.copy_backup(
                    "../outside.tar.age", "c" * 64, "a" * 40, False, False
                )
        load.assert_not_called()

    def test_workspace_contains_only_public_regular_source_without_local_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "public.py").write_text("pass")
            (source / ".env").write_text("DUMMY=not-a-real-secret")
            destination = root / "workspace"
            destination.mkdir()
            with (
                patch.object(PROOF, "REPOSITORY", source),
                patch.object(
                    PROOF.subprocess,
                    "check_output",
                    return_value=b"public.py\0.env\0.secrets/no-read\0",
                ),
            ):
                self.assertEqual(PROOF.prepare_source_workspace(destination), 1)
            self.assertEqual(
                [path.name for path in destination.iterdir()], ["public.py"]
            )


if __name__ == "__main__":
    unittest.main()
