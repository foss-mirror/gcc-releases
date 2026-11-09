# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise cached and newly acquired check tools using real archive extraction."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Literal

from fixture_tools import install_tool

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-check-tools.sh"


class CheckToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.archives = self.root / "archives"
        self.destination = self.root / "installed"
        self.log = self.root / "calls.jsonl"
        self.environment = {
            **os.environ,
            "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
            "GCC_TEST_LINTER_LOG": str(self.log),
            "GCC_TEST_LINTER_UPSTREAM": str(self.upstream),
            "LINTER_CACHE_HIT": "false",
        }
        for tool in ("curl", "gh"):
            install_tool(self.root, tool, "check_tools.py")
        archives: tuple[tuple[str, Literal["w:gz", "w:xz"], str, str], ...] = (
            ("actionlint.tar.gz", "w:gz", "actionlint", "ACTIONLINT_SHA256"),
            ("shellcheck.tar.xz", "w:xz", "shellcheck-v0.11.0/shellcheck", "SHELLCHECK_SHA256"),
        )
        for filename, mode, member, variable in archives:
            archive = self.upstream / filename
            content = b"#!/bin/sh\nexit 0\n"
            with tarfile.open(archive, mode) as output:
                info = tarfile.TarInfo(member)
                info.size = len(content)
                info.mode = 0o755
                output.addfile(info, io.BytesIO(content))
            self.environment[variable] = hashlib.sha256(archive.read_bytes()).hexdigest()

    def run_installer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), str(self.archives), str(self.destination)],
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def populate_cache(self) -> None:
        shutil.copytree(self.upstream, self.archives)
        self.environment["LINTER_CACHE_HIT"] = "true"

    def assert_installed(self) -> None:
        for name in ("actionlint", "shellcheck"):
            path = self.destination / name
            self.assertEqual(path.read_bytes(), b"#!/bin/sh\nexit 0\n")
            self.assertTrue(os.access(path, os.X_OK))

    def test_cache_hit_installs_without_download_or_attestation_requests(self) -> None:
        self.populate_cache()
        self.environment["GCC_TEST_LINTER_CURL_EXIT"] = "22"
        self.environment["GCC_TEST_LINTER_GH_EXIT"] = "1"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assert_installed()

    def test_cache_miss_downloads_and_authenticates_before_installation(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ["curl", "curl", "gh"])
        self.assertEqual(
            calls[-1][1:],
            [
                "attestation",
                "verify",
                str(self.archives / "actionlint.tar.gz"),
                "--repo",
                "rhysd/actionlint",
                "--signer-workflow",
                "rhysd/actionlint/.github/workflows/release.yaml",
                "--source-ref",
                "refs/tags/v1.7.12",
            ],
        )
        self.assert_installed()

    def test_bad_hash_rejects_cached_and_downloaded_archives(self) -> None:
        for cached in (False, True):
            with self.subTest(cached=cached):
                self.log.unlink(missing_ok=True)
                if cached:
                    self.environment["LINTER_CACHE_HIT"] = "true"
                self.environment["SHELLCHECK_SHA256"] = "0" * 64
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.destination / "actionlint").exists())
                self.assertNotIn("gh", [call[0] for call in self.calls()])

    def test_download_failure_stops_before_attestation_and_extraction(self) -> None:
        self.environment["GCC_TEST_LINTER_CURL_EXIT"] = "22"
        result = self.run_installer()
        self.assertEqual(result.returncode, 22)
        self.assertEqual([call[0] for call in self.calls()], ["curl"])
        self.assertFalse((self.destination / "actionlint").exists())

    def test_attestation_failure_prevents_extraction(self) -> None:
        self.environment["GCC_TEST_LINTER_GH_EXIT"] = "1"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.destination / "actionlint").exists())
        self.assertFalse((self.destination / "shellcheck").exists())

    def test_incomplete_cache_is_rejected_without_network_access(self) -> None:
        self.populate_cache()
        (self.archives / "shellcheck.tar.xz").unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.destination / "actionlint").exists())
