# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise local processes, retry boundaries, cleanup, and concurrent writers."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import local_state
import refresh_sdk

ROOT = Path(__file__).resolve().parent.parent


class LocalWriterTests(unittest.TestCase):
    def test_lock_excludes_another_process_and_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "target.lock"
            with local_state.exclusive_lock(path):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from local_state import exclusive_lock; import sys; "
                        "\nwith exclusive_lock(__import__('pathlib').Path(sys.argv[1])): pass",
                        str(path),
                    ],
                    env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another invocation", result.stderr)
            with local_state.exclusive_lock(path):
                self.assertTrue(path.is_file())

    def test_directory_publication_never_replaces_existing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, output = root / "staging", root / "output"
            staging.mkdir()
            (staging / "verified").write_text("artifact")
            output.mkdir()
            with self.assertRaises(FileExistsError):
                local_state.publish_directory(staging, output)
            self.assertEqual(list(output.iterdir()), [])
            self.assertTrue((staging / "verified").is_file())
            output.rmdir()
            output.symlink_to(root / "missing")
            with self.assertRaises(FileExistsError):
                local_state.publish_directory(staging, output)
            output.unlink()
            local_state.publish_directory(staging, output)
            self.assertEqual((output / "verified").read_text(), "artifact")
            self.assertFalse(staging.exists())

    def test_lock_replacement_preserves_edits_and_cleans_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sdk.json"
            path.write_bytes(b"edited")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "preserving edits"):
                local_state.replace_lock(path, b"original", b"replacement")
            self.assertEqual(path.read_bytes(), b"edited")
            self.assertEqual(list(path.parent.iterdir()), [path])
            local_state.replace_lock(path, b"edited", b"replacement")
            self.assertEqual(path.read_bytes(), b"replacement")
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)


class ResolverCleanupTests(unittest.TestCase):
    def test_successful_resolution_replaces_lock_only_after_all_stages(self) -> None:
        config = json.loads((ROOT / "build.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "sdk.json"
            original = json.dumps({"requests": {"builder": [], "source": []}}).encode()
            lock.write_bytes(original)
            workspaces = []

            def resolve(command: list[str], **_options: Any) -> None:
                work = Path(next(value for value in command if "dst=/output" in value).split("src=")[1].split(",")[0])
                workspaces.append(work)
                self.assertEqual(lock.read_bytes(), original)
                for arch in ("x86_64", "aarch64"):
                    for stage in ("builder", "source"):
                        directory = work / arch / stage
                        directory.mkdir(parents=True)
                        (directory / "urls").write_text("https://example.invalid/fixture.rpm\n")
                        (directory / "fixture.rpm").write_bytes(b"authenticated fixture")

            with (
                patch("refresh_sdk.subprocess.run", side_effect=resolve),
                patch.object(refresh_sdk, "restore_ownership", return_value=True),
            ):
                refresh_sdk.refresh(config, lock, original, json.loads(original), "bridge", "linux/amd64")
            inventory = json.loads(lock.read_text())
            expected = hashlib.sha256(b"authenticated fixture").hexdigest()
            for arch in ("x86_64", "aarch64"):
                for stage in ("builder", "source"):
                    self.assertEqual(inventory["locks"][arch][stage][0]["sha256"], expected)
            self.assertTrue(all(not work.exists() for work in workspaces))

    def test_shutdown_failure_retains_workspace_and_primary_error(self) -> None:
        config = json.loads((ROOT / "build.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            lock = root / "sdk.json"
            lock.write_bytes(b"original")
            with (
                patch("refresh_sdk.tempfile.mkdtemp", return_value=str(work)),
                patch("refresh_sdk.subprocess.run", side_effect=[KeyboardInterrupt(), OSError("Docker down")]),
                patch("refresh_sdk.shutil.rmtree") as remove,
                self.assertRaises(KeyboardInterrupt),
            ):
                refresh_sdk.refresh(config, lock, b"original", {}, "bridge", "linux/amd64")
            remove.assert_not_called()
            self.assertTrue(work.is_dir())
            self.assertEqual(lock.read_bytes(), b"original")

    def test_ownership_failure_is_reported_without_claiming_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            with patch(
                "refresh_sdk.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CalledProcessError(1, ["chown"]),
                ],
            ):
                self.assertFalse(refresh_sdk.restore_ownership(work, "image", "linux/amd64"))
            self.assertTrue(work.is_dir())

    def test_file_cleanup_failure_does_not_mask_resolver_error(self) -> None:
        config = json.loads((ROOT / "build.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("refresh_sdk.tempfile.mkdtemp", return_value=str(root)),
                patch("refresh_sdk.subprocess.run", side_effect=RuntimeError("resolver failed")),
                patch.object(refresh_sdk, "restore_ownership", return_value=True),
                patch.object(shutil, "rmtree", side_effect=OSError("cleanup failed")),
                self.assertRaisesRegex(RuntimeError, "resolver failed"),
            ):
                refresh_sdk.refresh(config, root / "lock", b"original", {}, "bridge", "linux/amd64")
