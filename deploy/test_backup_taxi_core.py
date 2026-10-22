"""Configuration and redaction guards for complete encrypted database backups."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backup_taxi_core as backup
from backup_taxi_core import r2_environment, run


class BackupConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config"
        self.values = {
            "REZO_R2_ACCOUNT_ID": "a" * 32,
            "REZO_R2_ACCESS_KEY_ID": "test-access",
            "REZO_R2_SECRET_ACCESS_KEY": "test-secret",
            "REZO_R2_BUCKET_NAME": "public-images",
            "REZO_R2_PRIVATE_BUCKET": "private-documents",
        }

    def write_config(self):
        self.config.write_text(
            "\n".join(f'{key}="{value}"' for key, value in self.values.items())
        )

    def test_only_private_bucket_and_explicit_credentials(self):
        self.write_config()
        with patch.dict(
            os.environ,
            {"AWS_SESSION_TOKEN": "unrelated", "RCLONE_S3_ENDPOINT": "unrelated"},
        ):
            env, bucket = r2_environment(self.config)
        self.assertEqual(bucket, "private-documents")
        self.assertEqual(
            env["RCLONE_S3_ENDPOINT"],
            "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
        )
        self.assertNotIn("AWS_SESSION_TOKEN", env)
        self.assertEqual(env["RCLONE_CONFIG"], "/dev/null")

    def test_missing_or_public_destination_fails_closed(self):
        for private in ["", "public-images"]:
            self.values["REZO_R2_PRIVATE_BUCKET"] = private
            self.write_config()
            with self.assertRaises(RuntimeError):
                r2_environment(self.config)

    def test_invalid_account_cannot_change_storage_endpoint(self):
        self.values["REZO_R2_ACCOUNT_ID"] = "untrusted.example/path"
        self.write_config()
        with self.assertRaises(RuntimeError):
            r2_environment(self.config)

    def test_subprocess_failure_does_not_expose_output(self):
        logs = io.StringIO()
        with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
            with self.assertRaisesRegex(RuntimeError, "upload failed") as error:
                run(
                    "upload",
                    [
                        "python3",
                        "-c",
                        "import sys;sys.stderr.write('test-secret');sys.exit(2)",
                    ],
                )
        self.assertNotIn("test-secret", str(error.exception) + logs.getvalue())

    def test_local_mode_never_reads_r2_credentials_or_contacts_storage(self):
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        self.addCleanup(os.umask, previous_umask)
        recipient = Path(self.temp.name) / "recipient.pub"
        recipient.write_text("age1test-public-recipient\n")
        destination = Path(self.temp.name) / "local-backups"
        stages = []

        def local_command(stage, command, **kwargs):
            stages.append(stage)
            self.assertIn(command[0], {"docker", "git", "age"})
            if stage == "pg_dump":
                kwargs["stdout"].write(b"test-dump" * 256)
            elif stage == "role backup":
                kwargs["stdout"].write(b"CREATE ROLE apitaxi;\n")
            elif stage == "version inventory":
                return SimpleNamespace(stdout="14.6\ntimescaledb=2.9.2\n")
            elif stage == "image inventory":
                return SimpleNamespace(stdout="sha256:" + "a" * 64)
            elif stage == "source inventory":
                return SimpleNamespace(stdout="b" * 40)
            elif stage == "backup encryption":
                Path(command[command.index("--output") + 1]).write_bytes(
                    b"test-encrypted-archive"
                )
            return SimpleNamespace(returncode=0)

        arguments = [
            "backup",
            "--local-only",
            "--output-dir",
            str(destination),
            "--recipient-file",
            str(recipient),
            "--r2-config",
            str(Path(self.temp.name) / "missing-private-config"),
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", arguments),
            patch.object(
                backup,
                "r2_environment",
                side_effect=AssertionError("R2 credentials read"),
            ),
            patch.object(
                backup,
                "upload_and_verify",
                side_effect=AssertionError("Storage contacted"),
            ),
            patch.object(
                backup.subprocess,
                "Popen",
                side_effect=AssertionError("Network process started"),
            ),
            patch.object(backup, "run", side_effect=local_command),
            contextlib.redirect_stdout(output),
        ):
            backup.main()
        proofs = list(destination.glob("apitaxi-full-*.tar.age.json"))
        self.assertEqual(len(proofs), 1)
        proof = json.loads(proofs[0].read_text())
        self.assertIs(proof["offsite_verified"], False)
        self.assertEqual(proof["storage_mode"], "local-only")
        self.assertNotIn("object_path", proof)
        self.assertEqual(
            proof["encrypted_sha256"], backup.file_sha256(proofs[0].with_suffix(""))
        )
        self.assertIn("LOCAL_ONLY=true OFFSITE_VERIFIED=false", output.getvalue())
        self.assertNotIn("OFFSITE_VERIFIED=true", output.getvalue())
        self.assertIn("archive validation", stages)
        self.assertEqual(proofs[0].stat().st_mode & 0o077, 0)
        self.assertEqual(destination.stat().st_mode & 0o077, 0)

    def test_default_mode_fails_closed_if_r2_configuration_is_unavailable(self):
        with (
            patch.object(sys, "argv", ["backup"]),
            patch.object(
                backup, "r2_environment", side_effect=RuntimeError("R2 unavailable")
            ),
            patch.object(backup, "run") as command,
            self.assertRaisesRegex(RuntimeError, "R2 unavailable"),
        ):
            backup.main()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
