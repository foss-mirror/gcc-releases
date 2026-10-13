# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify ordered publication can isolate failed native build pairs."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish

ROOT = Path(__file__).resolve().parent.parent


class AvailablePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = self.root / "plan"
        self.plan.mkdir()
        self.inputs = self.root / "packages"
        self.inputs.mkdir()
        versions = ["16.2.0", "8.5.0", "13.5.0", "12.5.0"]
        (self.plan / "plan.json").write_text(
            json.dumps(
                {
                    "publish": {"include": [{"release": "gcc-" + version} for version in versions]},
                }
            )
        )
        config = json.loads((ROOT / "build.json").read_text())
        for version in versions:
            resolved = copy.deepcopy(config)
            resolved["gcc"]["version"] = version
            resolved["gcc"]["tag"] = "releases/gcc-" + version
            (self.plan / f"gcc-{version}.json").write_text(json.dumps(resolved))
        for version in ("13.5.0", "16.2.0"):
            for arch in ("x64", "arm64"):
                self.upload(version, arch)

    def upload(self, version: str, arch: str) -> None:
        (self.inputs / f"gcc-{version}-linux-{arch}").mkdir()
        (self.inputs / f"attestation-gcc-{version}-{arch}.jsonl").write_text("bundle fixture\n")

    def test_failed_older_versions_do_not_block_ready_releases(self) -> None:
        self.upload("12.5.0", "x64")
        summary = self.root / "summary"
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--plan", str(self.plan), "--available"]),
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
            patch.object(publish, "publish") as write,
        ):
            publish.main()
        self.assertEqual([call.args[1]["gcc"]["version"] for call in write.call_args_list], ["13.5.0", "16.2.0"])
        self.assertIn("Skipped gcc-8.5.0", summary.read_text())
        self.assertIn("Skipped gcc-12.5.0: no completed upload for arm64", summary.read_text())

    def test_no_successful_pairs_performs_no_publication(self) -> None:
        for path in self.inputs.glob("attestation-*.jsonl"):
            path.unlink()
        for path in self.inputs.iterdir():
            path.rmdir()
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--plan", str(self.plan), "--available"]),
            patch.object(publish, "publish") as write,
        ):
            publish.main()
        write.assert_not_called()

    def test_ambiguous_or_partial_upload_is_not_treated_as_build_failure(self) -> None:
        (self.inputs / "attestation-gcc-13.5.0-arm64.jsonl").unlink()
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--plan", str(self.plan), "--available"]),
            patch.object(publish, "publish") as write,
            self.assertRaisesRegex(RuntimeError, "Incomplete or ambiguous"),
        ):
            publish.main()
        self.assertEqual([call.args[1]["gcc"]["version"] for call in write.call_args_list], ["16.2.0"])

    def test_publication_failure_is_reported_after_remaining_versions_are_attempted(self) -> None:
        summary = self.root / "summary"
        for failure in (RuntimeError("conflicting release"), subprocess.CalledProcessError(1, ["gh", "upload"])):
            with (
                self.subTest(failure=failure),
                patch("sys.argv", ["publish", str(self.inputs), "--plan", str(self.plan), "--available", "--resume"]),
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
                patch.object(publish, "publish", side_effect=[failure, None]) as write,
                self.assertRaisesRegex(RuntimeError, "Publication failed for gcc-13.5.0"),
            ):
                publish.main()
            self.assertEqual([call.args[1]["gcc"]["version"] for call in write.call_args_list], ["13.5.0", "16.2.0"])
            self.assertTrue(all(call.kwargs["resume"] for call in write.call_args_list))
        self.assertIn("Failed gcc-13.5.0", summary.read_text())

    def test_cancellation_stops_publication_immediately(self) -> None:
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--plan", str(self.plan), "--available"]),
            patch.object(publish, "publish", side_effect=KeyboardInterrupt()) as write,
            self.assertRaises(KeyboardInterrupt),
        ):
            publish.main()
        self.assertEqual(write.call_count, 1)

    def test_available_requires_a_plan(self) -> None:
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--config", str(ROOT / "build.json"), "--available"]),
            self.assertRaises(SystemExit),
        ):
            publish.main()
