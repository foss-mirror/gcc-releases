# SPDX-License-Identifier: GPL-3.0-or-later
"""Require complete, matching distribution evidence before a candidate can be attested."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import finalize
from distributions import distributions
from hashing import sha256

ROOT = Path(__file__).resolve().parent.parent


class FinalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "candidate"
        self.package.mkdir()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.config = json.loads((ROOT / "build.json").read_text())
        self.binary = self.package / "gcc-16.2.0-linux-x64.tar.zst"
        self.binary.write_bytes(b"the exact compiled archive")
        (self.package / "gcc-16.2.0-source.tar").write_bytes(b"corresponding source")
        (self.package / "provenance.json").write_text(
            json.dumps({"release": "gcc-16.2.0", "architecture": "x64", "configuration": self.config})
        )
        (self.package / "SHA256SUMS").write_text(
            "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(self.package.iterdir()))
        )
        for target in distributions(ROOT, self.config):
            (self.reports / target.report).write_text(
                json.dumps(
                    {
                        "archive_sha256": sha256(self.binary),
                        "environment": {
                            "architecture": "x86_64",
                            "distribution": {"ID": target.distribution, "VERSION_ID": target.version},
                        },
                        "checks": ["compiler and runtime probes passed"],
                    }
                )
            )
        self.original = {path.name: path.read_bytes() for path in self.package.iterdir()}

    def execute(self) -> None:
        finalize.finalize(self.package, self.reports, self.config, "x64")

    def assert_unchanged(self) -> None:
        self.assertEqual({path.name: path.read_bytes() for path in self.package.iterdir()}, self.original)

    def test_complete_matching_reports_preserve_packages_and_extend_manifest(self) -> None:
        self.execute()
        self.assertEqual(self.binary.read_bytes(), self.original[self.binary.name])
        for report in self.reports.iterdir():
            self.assertEqual((self.package / report.name).read_bytes(), report.read_bytes())
        manifest = dict(line.split("  ", 1)[::-1] for line in (self.package / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(set(manifest), {path.name for path in self.package.iterdir()} - {"SHA256SUMS"})
        for name, digest in manifest.items():
            self.assertEqual(sha256(self.package / name), digest)

    def test_each_distribution_is_required(self) -> None:
        for report in self.reports.iterdir():
            data = report.read_bytes()
            report.unlink()
            with self.subTest(report=report.name), self.assertRaisesRegex(ValueError, "distribution reports"):
                self.execute()
            self.assert_unchanged()
            report.write_bytes(data)

    def test_wrong_archive_architecture_distribution_or_empty_checks_are_rejected(self) -> None:
        path = self.reports / "verification-debian-13.json"
        original = json.loads(path.read_text())
        mutations = [copy.deepcopy(original) for _ in range(5)]
        mutations[0]["archive_sha256"] = "0" * 64
        mutations[1]["environment"]["architecture"] = "aarch64"
        mutations[2]["environment"]["distribution"]["ID"] = "ubuntu"
        mutations[3]["environment"]["distribution"]["VERSION_ID"] = "12"
        mutations[4]["checks"] = []
        for changed in mutations:
            path.write_text(json.dumps(changed))
            with self.subTest(report=changed), self.assertRaisesRegex(ValueError, "does not verify"):
                self.execute()
            self.assert_unchanged()

    def test_changed_binary_or_source_cannot_reuse_reports(self) -> None:
        for filename in (self.binary.name, "gcc-16.2.0-source.tar"):
            path = self.package / filename
            path.write_bytes(b"changed after build")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.execute()
            self.assertFalse(list(self.package.glob("verification*.json")))
            path.write_bytes(self.original[filename])
        self.assert_unchanged()

    def test_wrong_plan_cannot_finalize_a_candidate(self) -> None:
        self.config["gcc"]["commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "resolved release"):
            self.execute()
        self.assert_unchanged()

    def test_partial_duplicate_or_traversing_checksum_manifest_is_rejected(self) -> None:
        path = self.package / "SHA256SUMS"
        lines = path.read_text().splitlines()
        for contents in ("\n".join(lines[:-1]), "\n".join([*lines, lines[0]]), "0" * 64 + "  ../outside"):
            path.write_text(contents)
            with self.subTest(manifest=contents), self.assertRaises(ValueError):
                self.execute()
            self.assertFalse(list(self.package.glob("verification*.json")))
        path.write_bytes(self.original["SHA256SUMS"])
        self.assert_unchanged()

    def test_unexpected_or_symlinked_reports_are_rejected(self) -> None:
        extra = self.reports / "verification-unknown.json"
        extra.write_text("{}")
        with self.assertRaisesRegex(ValueError, "distribution reports"):
            self.execute()
        extra.unlink()
        report = self.reports / "verification.json"
        moved = self.root / "moved.json"
        report.rename(moved)
        report.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.execute()
        self.assert_unchanged()
