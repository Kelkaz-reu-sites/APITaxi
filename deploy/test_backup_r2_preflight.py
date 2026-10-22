"""Storage authorization guards; all requests and subprocesses are mocked."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import backup_taxi_core as backup
import r2_bucket_preflight as preflight


class R2PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "private-proof"
        self.env = {
            "AWS_ACCESS_KEY_ID": "test-access-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "RCLONE_S3_ENDPOINT": "https://" + "a" * 32 + ".r2.cloudflarestorage.com",
        }

    def test_retired_cli_never_reads_configuration_or_contacts_storage(self):
        for arguments in (
            ["r2-preflight"],
            ["r2-preflight", "--help"],
            [
                "r2-preflight",
                "--r2-config",
                "/must-not-read-private-config",
                "--execute-public-probe",
            ],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", arguments),
                patch.object(Path, "read_text") as read,
                patch.object(backup, "r2_environment") as credentials,
                patch.object(preflight.http.client, "HTTPSConnection") as network,
                patch.object(preflight.subprocess, "run") as process,
                patch.object(preflight, "verified_rclone_environment") as head,
                patch.object(preflight, "public_permission_probe") as probe,
                self.assertRaisesRegex(SystemExit, "tooling retired"),
            ):
                preflight.main()
            for operation in (read, credentials, network, process, head, probe):
                operation.assert_not_called()

    def test_signed_head_is_exact_and_credentials_are_not_persisted(self):
        response = SimpleNamespace(status=200, getheader=lambda name: "request-123")
        with patch.object(preflight.http.client, "HTTPSConnection") as factory:
            connection = factory.return_value
            connection.getresponse.return_value = response
            checked = preflight.verified_rclone_environment(
                self.env, "private-documents", self.root / "head.json"
            )
        method, path = connection.request.call_args.args
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual((method, path), ("HEAD", "/private-documents"))
        self.assertIn("AWS4-HMAC-SHA256", headers["Authorization"])
        self.assertIn("/auto/s3/aws4_request", headers["Authorization"])
        connection.close.assert_called_once()
        saved = (self.root / "head.json").read_text()
        self.assertNotIn("test-access-key", saved)
        self.assertNotIn("test-secret-key", saved)
        self.assertNotIn("Authorization", saved)
        self.assertEqual(checked["RCLONE_S3_NO_CHECK_BUCKET"], "true")
        self.assertNotIn("RCLONE_S3_NO_CHECK_BUCKET", self.env)
        self.assertEqual((self.root / "head.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_failed_head_and_redirect_cannot_enable_any_storage_write(self):
        for status in (301, 307, 403, 404, 500):
            with (
                self.subTest(status=status),
                patch.object(
                    preflight, "signed_bucket_head", return_value=(status, None)
                ),
                patch.object(preflight.subprocess, "run") as storage,
                self.assertRaises(RuntimeError),
            ):
                preflight.public_permission_probe(
                    self.env, "private-documents", self.root / str(status)
                )
            storage.assert_not_called()
            proof = json.loads(
                next((self.root / str(status)).glob("*.head.json")).read_text()
            )
            self.assertFalse(proof["existing_bucket_verified"])
            self.assertEqual(proof["http_status"], status)

    def test_unexpected_endpoint_and_bucket_fail_before_network(self):
        for endpoint, bucket in (
            ("https://untrusted.example", "private-documents"),
            (self.env["RCLONE_S3_ENDPOINT"] + "/other", "private-documents"),
            (self.env["RCLONE_S3_ENDPOINT"], "private-documents/path"),
        ):
            with (
                self.subTest(endpoint=endpoint, bucket=bucket),
                patch.object(preflight.http.client, "HTTPSConnection") as network,
                self.assertRaises(RuntimeError),
            ):
                preflight.signed_bucket_head(
                    {**self.env, "RCLONE_S3_ENDPOINT": endpoint}, bucket
                )
            network.assert_not_called()

    def test_network_failure_does_not_disclose_credentials(self):
        with (
            patch.object(
                preflight, "signed_bucket_head", side_effect=OSError("test-secret-key")
            ),
            self.assertRaisesRegex(RuntimeError, "preflight failed") as caught,
        ):
            preflight.verified_rclone_environment(
                self.env, "private-documents", self.root / "failed.json"
            )
        self.assertNotIn("test-secret-key", str(caught.exception))
        self.assertNotIn("test-secret-key", (self.root / "failed.json").read_text())

    def test_database_archive_upload_is_also_blocked_by_failed_head(self):
        archive = self.root / "encrypted-test.tar.age"
        with (
            patch.object(preflight, "signed_bucket_head", return_value=(403, None)),
            patch.object(backup, "run") as upload,
            patch.object(backup.subprocess, "Popen") as download,
            self.assertRaises(RuntimeError),
        ):
            backup.upload_and_verify(
                archive,
                "20261022T000000Z",
                "test-digest",
                self.env,
                "private-documents",
            )
        upload.assert_not_called()
        download.assert_not_called()

    def test_encrypted_archive_upload_keeps_head_proof_and_verifies_readback(self):
        archive = self.root / "encrypted-test.tar.age"
        payload = b"test-ciphertext"
        import hashlib

        digest = hashlib.sha256(payload).hexdigest()

        def upload(stage, command, **kwargs):
            self.assertEqual(kwargs["env"]["RCLONE_S3_NO_CHECK_BUCKET"], "true")
            proof = json.loads(
                archive.with_name(archive.name + ".r2-preflight.json").read_text()
            )
            self.assertEqual(proof["http_status"], 200)

        with (
            patch.object(preflight, "signed_bucket_head", return_value=(200, None)),
            patch.object(backup, "run", side_effect=upload),
            patch.object(backup.subprocess, "Popen") as download,
        ):
            stream = download.return_value.__enter__.return_value
            stream.stdout = io.BytesIO(payload)
            stream.wait.return_value = 0
            remote = backup.upload_and_verify(
                archive, "20261022T000000Z", digest, self.env, "private-documents"
            )
        self.assertEqual(
            remote, "operations/backups/taxi-core/2026/10/encrypted-test.tar.age"
        )

    def public_probe(self, corrupt=False):
        calls = []
        uploaded = {}

        def storage(command, **kwargs):
            self.assertEqual(command[0], "rclone")
            self.assertEqual(kwargs["env"]["RCLONE_S3_NO_CHECK_BUCKET"], "true")
            self.assertEqual(len(list(self.root.glob("*.head.json"))), 1)
            self.assertTrue(
                json.loads(next(self.root.glob("*.head.json")).read_text())[
                    "existing_bucket_verified"
                ]
            )
            self.assertNotIn("--s3-no-head", command)
            action = next(x for x in command if x in {"copyto", "cat", "deletefile"})
            calls.append((action, command[-1]))
            self.assertTrue(
                command[-1].startswith(
                    ":s3:private-documents/operations/backups/taxi-core/permission-check/"
                )
            )
            if action == "copyto":
                self.assertIn("--immutable", command)
                uploaded["payload"] = Path(command[-2]).read_bytes()
                self.assertTrue(
                    uploaded["payload"].startswith(
                        b"Public REZO backup permission probe; nonce="
                    )
                )
                self.assertLess(len(uploaded["payload"]), 256)
            return SimpleNamespace(
                returncode=0, stdout=b"corrupted" if corrupt else uploaded["payload"]
            )

        with (
            patch.object(
                preflight, "signed_bucket_head", return_value=(200, "request-123")
            ),
            patch.object(preflight.subprocess, "run", side_effect=storage),
        ):
            if corrupt:
                with self.assertRaises(RuntimeError):
                    preflight.public_permission_probe(
                        self.env, "private-documents", self.root
                    )
            else:
                preflight.public_permission_probe(
                    self.env, "private-documents", self.root
                )
        self.assertEqual(
            [action for action, remote in calls], ["copyto", "cat", "deletefile"]
        )
        self.assertEqual(len({remote for action, remote in calls}), 1)
        return json.loads(next(self.root.glob("*.probe.json")).read_text())

    def test_probe_can_only_send_generated_public_text_and_delete_its_exact_key(self):
        proof = self.public_probe()
        self.assertEqual(proof["status"], "passed")
        self.assertTrue(proof["readback_verified"])
        self.assertTrue(proof["deleted"])
        self.assertFalse(proof["database_accessed"])
        self.assertFalse(proof["database_uploaded"])

    def test_corrupted_probe_is_failed_but_its_exact_key_is_still_removed(self):
        proof = self.public_probe(corrupt=True)
        self.assertEqual(proof["status"], "failed")
        self.assertFalse(proof["readback_verified"])
        self.assertTrue(proof["deleted"])

    def test_storage_errors_keep_only_fixed_codes_without_private_output(self):
        with (
            patch.object(preflight, "signed_bucket_head", return_value=(200, None)),
            patch.object(
                preflight.subprocess,
                "run",
                return_value=SimpleNamespace(
                    returncode=3,
                    stderr=(
                        b"AccessDenied: secret-url test-secret-key "
                        b"status code: 403, request id: private"
                    ),
                ),
            ),
            self.assertRaises(RuntimeError),
        ):
            preflight.public_permission_probe(self.env, "private-documents", self.root)
        saved = next(self.root.glob("*.probe.json")).read_text()
        proof = json.loads(saved)
        self.assertEqual(proof["failed_operation"], "copyto")
        self.assertEqual(proof["tool_exit_code"], 3)
        self.assertEqual(proof["storage_http_status"], 403)
        self.assertEqual(proof["storage_error_code"], "AccessDenied")
        for private in ["test-secret-key", "secret-url", "request id: private"]:
            self.assertNotIn(private, saved)


if __name__ == "__main__":
    unittest.main()
