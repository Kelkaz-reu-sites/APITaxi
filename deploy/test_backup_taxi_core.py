"""Configuration and redaction guards for complete encrypted database backups."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
