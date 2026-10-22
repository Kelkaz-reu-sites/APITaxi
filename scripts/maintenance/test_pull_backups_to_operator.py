"""Offline checks for bounded ciphertext copies; never access SSH or secrets."""

import ast
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pull_backups_to_operator as puller


class BackupPullTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name)
        self.payload = b"age-encryption.org/v1\n" + b"encrypted-test-only" * 128
        self.stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        self.name = "apitaxi-full-" + self.stamp + ".tar.age"
        self.proof = {
            "created_at": self.stamp,
            "complete_database": True,
            "encrypted_bytes": len(self.payload),
            "encrypted_sha256": hashlib.sha256(self.payload).hexdigest(),
            "storage_mode": "local-only",
            "offsite_verified": False,
        }

    def header(self, **overrides):
        row = {"name": self.name, "proof": self.proof, "transfer": True, **overrides}
        return json.dumps({"archives": [row]}).encode() + b"\n"

    def test_unsafe_duplicate_and_oversized_inventories_are_refused(self):
        row = {"name": self.name, "proof": self.proof, "transfer": True}
        samples = [
            self.header(name="../" + self.name),
            self.header(name=self.name + ".r2-preflight.json"),
            self.header(
                proof={**self.proof, "encrypted_bytes": puller.MAX_ARCHIVE_BYTES + 1}
            ),
            self.header(proof={**self.proof, "complete_database": False}),
            self.header(proof={**self.proof, "encrypted_sha256": "invalid"}),
            json.dumps({"archives": [row, row]}).encode() + b"\n",
            b"x" * (puller.MAX_HEADER_BYTES + 1),
        ]
        for header in samples:
            with self.subTest(header=header[:80]), self.assertRaises(ValueError):
                puller.validate_header(header)

    def test_remote_program_compiles_and_uses_the_same_strict_filename_pattern(self):
        tree = ast.parse(puller.REMOTE_CODE)
        compile(tree, "remote-backup-reader", "exec")
        assignment = next(
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pattern" for t in n.targets)
        )
        remote_pattern = puller.re.compile(ast.literal_eval(assignment.value.args[0]))
        self.assertIsNotNone(remote_pattern.fullmatch(self.name))
        self.assertIsNone(remote_pattern.fullmatch("../" + self.name))

    def test_aggregate_transfer_size_is_bounded(self):
        rows = []
        for second in range(5):
            stamp = "20261022T12000" + str(second) + "Z"
            rows.append(
                {
                    "name": "apitaxi-full-" + stamp + ".tar.age",
                    "proof": {
                        **self.proof,
                        "created_at": stamp,
                        "encrypted_bytes": puller.MAX_ARCHIVE_BYTES,
                    },
                    "transfer": True,
                }
            )
        with self.assertRaises(ValueError):
            puller.validate_header(json.dumps({"archives": rows}).encode() + b"\n")

    def test_ciphertext_is_verified_before_atomic_private_publication(self):
        copied = puller.receive_archive(
            io.BytesIO(self.payload), self.destination, self.name, self.proof
        )
        target = self.destination / self.name
        self.assertEqual(copied, len(self.payload))
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(len(list(self.destination.iterdir())), 1)
        with self.assertRaises(ValueError):
            puller.receive_archive(
                io.BytesIO(self.payload), self.destination, self.name, self.proof
            )
        self.assertEqual(target.read_bytes(), self.payload)

    def test_interrupted_corrupted_and_plaintext_streams_leave_no_archive(self):
        samples = [
            self.payload[:100],
            b"x" * len(self.payload),
            self.payload[:-1] + b"x",
        ]
        for payload in samples:
            with self.subTest(prefix=payload[:30]), self.assertRaises(ValueError):
                puller.receive_archive(
                    io.BytesIO(payload), self.destination, self.name, self.proof
                )
            self.assertEqual(list(self.destination.iterdir()), [])

    def fake_ssh(self, data, exit_code=0):
        class Process:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(data)

            def poll(self):
                return exit_code

            def wait(self, **kwargs):
                return exit_code

        return Process()

    def test_single_ssh_reads_only_ciphertext_and_preserves_source_proof(self):
        process = self.fake_ssh(self.header() + self.payload)
        with patch.object(puller.subprocess, "Popen", return_value=process) as ssh:
            result = puller.pull(self.destination)
        self.assertEqual(ssh.call_count, 1)
        command = ssh.call_args.args[0]
        self.assertIn("debian@51.255.201.196", command)
        self.assertIn("ConnectionAttempts=1", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertTrue(command[-1].startswith("sudo -n /usr/bin/python3 -B -c "))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["copied_archives"], 1)
        saved = json.loads((self.destination / (self.name + ".json")).read_text())
        self.assertFalse(saved["offsite_verified"])
        self.assertEqual(saved["storage_mode"], "local-only")

    def test_existing_verified_archive_is_reused_and_unverified_skip_fails(self):
        target = self.destination / self.name
        target.write_bytes(self.payload)
        process = self.fake_ssh(self.header(transfer=False))
        with patch.object(puller.subprocess, "Popen", return_value=process):
            result = puller.pull(self.destination)
        self.assertEqual(result["copied_archives"], 0)
        self.assertEqual(target.read_bytes(), self.payload)
        target.unlink()
        process = self.fake_ssh(self.header(transfer=False))
        with (
            patch.object(puller.subprocess, "Popen", return_value=process),
            self.assertRaises(ValueError),
        ):
            puller.pull(self.destination)

    def test_extra_stream_bytes_and_symlink_destinations_are_refused(self):
        process = self.fake_ssh(self.header() + self.payload + b"unexpected")
        with (
            patch.object(puller.subprocess, "Popen", return_value=process),
            self.assertRaises(ValueError),
        ):
            puller.pull(self.destination)
        link = self.destination / "link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            puller.private_directory(link)

    def test_a_failed_pull_retains_the_previous_success_state(self):
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        self.addCleanup(os.umask, previous_umask)
        with (
            patch.object(puller, "DESTINATION", self.destination),
            patch.object(puller.signal, "signal"),
            patch.object(puller.signal, "alarm"),
            patch.object(
                puller,
                "pull",
                side_effect=[
                    {"status": "passed", "copied_archives": 1},
                    RuntimeError("failed SSH"),
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(puller.main(), 0)
            self.assertEqual(puller.main(), 1)
        self.assertEqual(
            json.loads((self.destination / "pull-last-success.json").read_text())[
                "status"
            ],
            "passed",
        )
        self.assertEqual(
            json.loads((self.destination / "pull-last-failure.json").read_text())[
                "status"
            ],
            "failed",
        )
        self.assertEqual(
            json.loads((self.destination / "pull-latest.json").read_text())["status"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
