# SPDX-License-Identifier: GPL-3.0-or-later
"""Check SDK locks and interrupted refreshes."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import refresh_sdk
from fixture_tools import install_tool

ROOT = Path(__file__).resolve().parent.parent


class SdkDownloadTests(unittest.TestCase):
    def test_transport_retries_replace_partial_files_and_preserve_failure_status(self) -> None:
        for responses, expected_status, expected_body, attempts in (
            ([{"body": "partial", "exit": 56}, {"body": "complete RPM", "exit": 0}], 0, b"complete RPM", 2),
            ([{"body": "partial", "exit": 56}], 56, b"partial", 4),
            ([{"body": "untrusted", "exit": 60}], 60, b"untrusted", 1),
        ):
            with self.subTest(responses=responses), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                install_tool(root, "curl", "curl.py")
                state = root / "state"
                response_file = root / "responses.json"
                response_file.write_text(json.dumps(responses))
                target = root / "package.rpm"
                target.write_bytes(b"stale incomplete download")
                result = subprocess.run(
                    [
                        "bash",
                        "-euo",
                        "pipefail",
                        "-c",
                        'sleep() { :; }; source "$1"; download_rpm https://example.invalid/package.rpm "$2"',
                        "sdk-download",
                        str(ROOT / "scripts/download-rpm.sh"),
                        str(target),
                    ],
                    env={
                        **os.environ,
                        "PATH": str(root) + os.pathsep + os.environ["PATH"],
                        "GCC_TEST_STATE": str(state),
                        "GCC_TEST_RESPONSES": str(response_file),
                    },
                    check=False,
                )
                self.assertEqual(result.returncode, expected_status)
                self.assertEqual(target.read_bytes(), expected_body)
                self.assertEqual(int(state.read_text()), attempts)


class SdkLockTests(unittest.TestCase):
    def test_interrupted_refresh_stops_container_and_preserves_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.json").write_text((ROOT / "build.json").read_text())
            lock = root / "sdk-packages.json"
            original = (ROOT / "sdk-packages.json").read_bytes()
            lock.write_bytes(original)
            with (
                patch.object(refresh_sdk, "ROOT", root),
                patch("sys.argv", ["refresh_sdk"]),
                patch("refresh_sdk.signal.signal"),
                patch("native.platform.machine", return_value="x86_64"),
                patch("native.platform.system", return_value="Linux"),
                patch(
                    "refresh_sdk.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, stdout="x86_64\n"),
                        KeyboardInterrupt(),
                        subprocess.CompletedProcess([], 0, stdout="container-id\n"),
                        None,
                        None,
                    ],
                ) as run,
                self.assertRaises(KeyboardInterrupt),
            ):
                refresh_sdk.main()
            commands = [call.args[0] for call in run.call_args_list][1:]
            name = commands[0][commands[0].index("--name") + 1]
            self.assertIn(f"name=^/{name}$", commands[1])
            self.assertEqual(commands[2], ["docker", "rm", "--force", "container-id"])
            self.assertIn("chown", commands[3])
            self.assertIn("--network=none", commands[3])
            self.assertEqual(lock.read_bytes(), original)

    def test_every_stage_has_hash_locked_https_rpms_on_both_architectures(self) -> None:
        inventory = json.loads((ROOT / "sdk-packages.json").read_text())
        self.assertEqual(set(inventory["locks"]), {"x86_64", "aarch64"})
        for stages in inventory["locks"].values():
            self.assertEqual(set(stages), set(inventory["requests"]))
            for packages in stages.values():
                self.assertTrue(packages)
                self.assertEqual(len(packages), len({p["url"].rsplit("/", 1)[1] for p in packages}))
                for package in packages:
                    self.assertTrue(package["url"].startswith("https://"))
                    self.assertTrue(package["url"].endswith(".rpm"))
                    self.assertRegex(package["sha256"], r"^[a-f0-9]{64}$")

    def test_parallel_lto_verifiers_include_the_existing_locked_make_on_both_architectures(self) -> None:
        inventory = json.loads((ROOT / "sdk-packages.json").read_text())
        self.assertIn("make", inventory["requests"]["verifier"])
        for arch, stages in inventory["locks"].items():
            with self.subTest(architecture=arch):
                builder = [item for item in stages["builder"] if item["url"].rsplit("/", 1)[-1].startswith("make-")]
                verifier = [item for item in stages["verifier"] if item["url"].rsplit("/", 1)[-1].startswith("make-")]
                self.assertEqual(len(verifier), 1)
                self.assertEqual(verifier, builder)
