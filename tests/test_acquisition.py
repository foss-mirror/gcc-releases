# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise source acquisition using local curl and signature-process fixtures."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fetch
from fixture_tools import install_tool


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.responses = self.root / "responses.json"
        self.state = self.root / "state"
        self.signer = "A" * 40
        self.item = {
            "url": "https://example.invalid/gcc.tar.xz",
            "sha256": hashlib.sha256(b"good").hexdigest(),
            "key": "key.asc",
            "signer": self.signer,
        }
        (self.root / "key.asc").write_text("fixture public key\n")
        install_tool(self.root, "curl", "curl.py")
        install_tool(self.root, "gpg", "gpg.py")
        environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "GCC_TEST_RESPONSES": str(self.responses),
                "GCC_TEST_STATE": str(self.state),
                "GCC_TEST_SIGNER": self.signer,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        sleep = patch("fetch.time")
        self.sleep = sleep.start().sleep
        self.addCleanup(sleep.stop)
        self.responses.write_text(json.dumps([{"body": "good", "exit": 0}]))

    def test_partial_retry_restarts_file_and_streaming_digest(self) -> None:
        self.responses.write_text(json.dumps([{"body": "part", "exit": 28}, {"body": "good", "exit": 0}]))
        archive, evidence = fetch.acquire(self.item, self.root / "downloads", key_root=self.root)
        self.assertEqual(archive.read_bytes(), b"good")
        self.assertEqual(evidence, {"sha256": self.item["sha256"], "signer": self.signer, "signature_epoch": 123})
        self.assertEqual(self.state.read_text(), "2")
        self.sleep.assert_called_once_with(1)

    def test_retry_budget_is_bounded(self) -> None:
        self.responses.write_text(json.dumps([{"body": "part", "exit": 28}]))
        with self.assertRaisesRegex(RuntimeError, "curl exit 28"):
            fetch.acquire(self.item, self.root / "downloads", key_root=self.root)
        self.assertEqual(self.state.read_text(), "4")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2, 4])

    def test_permanent_transfer_failure_is_not_retried(self) -> None:
        self.responses.write_text(json.dumps([{"body": "part", "exit": 60}]))
        with self.assertRaisesRegex(RuntimeError, "curl exit 60"):
            fetch.acquire(self.item, self.root / "downloads", key_root=self.root)
        self.assertEqual(self.state.read_text(), "1")
        self.sleep.assert_not_called()

    def test_bad_hash_is_rejected_before_signature_download(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            fetch.acquire({**self.item, "sha256": "0" * 64}, self.root / "downloads", key_root=self.root)
        self.assertFalse((self.root / "downloads/gcc.tar.xz.sig").exists())

    def test_signature_failure_and_unexpected_signer_are_rejected(self) -> None:
        with patch.dict(os.environ, {"GCC_TEST_SIGNATURE_EXIT": "1"}), self.assertRaises(subprocess.CalledProcessError):
            fetch.acquire(self.item, self.root / "downloads", key_root=self.root)
        with (
            patch.dict(os.environ, {"GCC_TEST_SIGNER": "B" * 40}),
            self.assertRaisesRegex(RuntimeError, "Unexpected signer"),
        ):
            fetch.acquire(self.item, self.root / "downloads", key_root=self.root)

    def test_discovery_learns_hash_only_from_accepted_signature(self) -> None:
        item = {key: value for key, value in self.item.items() if key != "sha256"}
        _, evidence = fetch.acquire(item, self.root / "downloads", key_root=self.root, discover=True)
        self.assertEqual(evidence["sha256"], hashlib.sha256(b"good").hexdigest())

    def test_archive_size_limit_stops_transfer_without_retry(self) -> None:
        with patch.object(fetch, "MAX_ARCHIVE_BYTES", 3), self.assertRaisesRegex(RuntimeError, "exceeds"):
            fetch.acquire(self.item, self.root / "downloads", key_root=self.root)
        self.sleep.assert_not_called()

    def test_build_downloads_require_pinned_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "pinned SHA-256"):
            fetch.acquire({"url": "https://example.com/file"}, self.root)
