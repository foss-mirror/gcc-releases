# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise release discovery without network access."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import discover
from release import package_name, release_tag

ROOT = Path(__file__).resolve().parent.parent


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads((ROOT / "build.json").read_text())
        self.tags = {
            "8.5.0": "eight",
            "9.5.0": "nine",
            "10.5.0": "ten",
            "11.5.0": "eleven",
            "12.5.0": "twelve",
            "13.5.0": "old",
            "14.4.0": "fourteen",
            "16.1.0": "earlier",
            "16.2.0": self.config["gcc"]["commit"],
            "16.3.0": "future",
        }

    def test_discovery_selects_latest_point_release_per_major_above_minimum(self) -> None:
        self.tags.update({"7.5.0": "too-old", "8.1.0": "earlier-eight", "15.3.0": "fifteen", "17.1.0": "future-major"})
        expected = ["8.5.0", "9.5.0", "10.5.0", "11.5.0", "12.5.0", "13.5.0", "14.4.0", "15.3.0", "16.3.0", "17.1.0"]
        self.assertEqual(discover.select_versions(self.tags, self.config["discovery"], None), expected)

    def test_no_eligible_releases_stops_discovery(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No stable tags"):
            discover.select_versions({"7.5.0": "too-old"}, self.config["discovery"], None)

    def test_latest_point_release_uses_numeric_order(self) -> None:
        self.tags["16.10.0"] = "future-ten"
        selected = discover.select_versions(self.tags, self.config["discovery"], None)
        self.assertEqual([version for version in selected if version.startswith("16.")], ["16.10.0"])

    def test_backfills_are_numeric_sorted_and_deduplicated(self) -> None:
        self.tags["16.10.0"] = "future-ten"
        self.assertEqual(
            discover.select_versions(self.tags, self.config["discovery"], "16.10.0,14.4.0,16.2.0,14.4.0"),
            ["14.4.0", "16.2.0", "16.10.0"],
        )

    def test_rejects_unstable_missing_and_out_of_policy_versions(self) -> None:
        for version in ("7.5.0", "14.5.0", "16.3.0-rc1", "16.2.0/../file", "16.2.0,"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                discover.select_versions(self.tags, self.config["discovery"], version)

    def test_historical_floor_includes_only_final_gcc_8_releases(self) -> None:
        tags = {"7.5.0": "old", "8.1.0": "eight", "8.5.0": "last-eight"}
        self.assertEqual(discover.select_versions(tags, self.config["discovery"], "8.5.0,8.1.0"), ["8.1.0", "8.5.0"])

    def test_tag_discovery_peels_annotated_tags_and_accepts_lightweight_tags(self) -> None:
        response = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "object\trefs/tags/releases/gcc-16.2.0\n"
                "commit\trefs/tags/releases/gcc-16.2.0^{}\n"
                "lightweight\trefs/tags/releases/gcc-16.3.0\n"
                "rc\trefs/tags/releases/gcc-16.4.0-rc1\n"
                "rc\trefs/tags/releases/gcc-8.1.0-RC-20180427\n"
                "snapshot\trefs/tags/releases/gcc-8-20180427\n"
            ),
        )
        with patch("discover.subprocess.run", return_value=response):
            self.assertEqual(
                discover.stable_tags("https://example.com/gcc.git"), {"16.2.0": "commit", "16.3.0": "lightweight"}
            )

    def test_configured_release_rejects_moved_tag(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "tag moved"):
            discover.resolve(self.config, "16.2.0", "different-commit")

    def test_new_recipe_uses_authenticated_source_evidence(self) -> None:
        evidence = {"sha256": "a" * 64, "signer": self.config["gcc"]["signer"], "signature_epoch": 123}
        with patch.object(discover, "acquire", return_value=(Path("archive"), evidence)) as acquire:
            resolved = discover.resolve(self.config, "16.3.0", "future")
        self.assertEqual(resolved["gcc"]["sha256"], evidence["sha256"])
        self.assertEqual(resolved["gcc"]["commit"], "future")
        self.assertEqual(resolved["source_date_epoch"], 123)
        self.assertTrue(acquire.call_args.kwargs["discover"])
        self.assertEqual(self.config["gcc"]["version"], "16.2.0")
        self.assertEqual(release_tag(resolved), "releases/gcc-16.3.0")
        self.assertEqual(package_name(resolved), "gcc-16.3.0")
        self.assertNotIn("revision", resolved["gcc"])

    def test_invalid_signature_prevents_recipe_resolution(self) -> None:
        with (
            patch.object(discover, "acquire", side_effect=RuntimeError("invalid signature")),
            self.assertRaisesRegex(RuntimeError, "invalid signature"),
        ):
            discover.resolve(self.config, "16.3.0", "future")

    def prepare(
        self,
        directory: Path,
        existing: list[dict[str, Any]],
        *,
        configured: bool = True,
        versions: str | None = None,
        repository: bool = True,
    ) -> dict[str, Any]:
        output = directory / "plan"
        arguments = ["discover", "--output", str(output)]
        if repository:
            arguments.extend(["--repository", "owner/repo"])
        if configured:
            arguments.append("--configured")
        if versions:
            arguments.extend(["--versions", versions])
        with (
            patch.object(discover, "stable_tags", return_value=self.tags),
            patch.object(discover, "releases", return_value=iter(existing)) as listed,
            patch("sys.argv", arguments),
            patch.dict(os.environ, {"GITHUB_OUTPUT": str(directory / "outputs")}, clear=False),
        ):
            discover.main()
        if not repository:
            listed.assert_not_called()
        plan: dict[str, Any] = json.loads((output / "plan.json").read_text())
        return plan

    def test_one_release_produces_two_native_builds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self.prepare(Path(directory), [])
            rows = plan["build"]["include"]
            self.assertEqual([row["arch"] for row in rows], ["x64", "arm64"])
            self.assertEqual(plan["publish"]["include"], [{"release": "gcc-16.2.0"}])
            self.assertEqual([row["release"] for row in rows], ["gcc-16.2.0"] * 2)
            self.assertIn("has_releases=true", (Path(directory) / "outputs").read_text())
            self.assertEqual(
                plan["verify"]["include"],
                [
                    {**row, "distribution": name}
                    for row in rows
                    for name in ("almalinux-8.10", "ubuntu-26.04", "debian-13", "fedora-44")
                ],
            )
            outputs = dict(line.split("=", 1) for line in (Path(directory) / "outputs").read_text().splitlines())
            self.assertEqual(json.loads(outputs["verify"]), plan["verify"])

    def test_initial_discovery_prepares_nine_releases_and_eighteen_native_builds(self) -> None:
        self.tags.pop("16.3.0")
        self.tags["15.3.0"] = "fifteen"
        evidence = {"sha256": "a" * 64, "signer": self.config["gcc"]["signer"], "signature_epoch": 123}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(discover, "acquire", return_value=(Path("archive"), evidence)),
        ):
            plan = self.prepare(Path(directory), [], configured=False)
        expected = ["8.5.0", "9.5.0", "10.5.0", "11.5.0", "12.5.0", "13.5.0", "14.4.0", "15.3.0", "16.2.0"]
        self.assertEqual(plan["selected"], expected)
        self.assertEqual(plan["pending"], expected)
        self.assertEqual(plan["publish"]["include"], [{"release": "gcc-" + version} for version in expected])
        self.assertEqual(
            plan["build"]["include"],
            [
                {"release": "gcc-" + version, "arch": arch, "runner": runner}
                for version in expected
                for arch, runner in (("x64", "ubuntu-24.04"), ("arm64", "ubuntu-24.04-arm"))
            ],
        )

    def test_published_release_produces_empty_matrices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = self.prepare(
                Path(directory), [{"tag_name": "releases/gcc-16.2.0", "draft": False, "prerelease": False}]
            )
            self.assertEqual(plan["build"]["include"], [])
            self.assertEqual(plan["verify"]["include"], [])
            self.assertEqual(plan["publish"]["include"], [])
            self.assertIn("has_releases=false", (Path(directory) / "outputs").read_text())

    def test_drafts_require_recovery_without_rebuilding_even_when_requested(self) -> None:
        for configured in (True, False):
            with (
                self.subTest(configured=configured),
                tempfile.TemporaryDirectory() as directory,
                patch.object(discover, "resolve") as resolve,
            ):
                plan = self.prepare(
                    Path(directory),
                    [{"tag_name": "releases/gcc-16.2.0", "draft": True, "prerelease": False}],
                    configured=configured,
                    versions=None if configured else "16.2.0",
                )
                resolve.assert_not_called()
                self.assertEqual(plan["needs_recovery"], ["16.2.0"])
                self.assertIn("original publication job", plan["recovery_instruction"])
                self.assertEqual(plan["already_published"], [])
                self.assertEqual(plan["pending"], [])
                self.assertEqual(plan["build"]["include"], [])
                self.assertEqual(plan["publish"]["include"], [])
                self.assertIn("has_releases=false", (Path(directory) / "outputs").read_text())

    def test_draft_does_not_block_other_unpublished_versions(self) -> None:
        self.tags = {v: self.tags[v] for v in ("8.5.0", "14.4.0", "16.2.0")}
        evidence = {"sha256": "a" * 64, "signer": self.config["gcc"]["signer"], "signature_epoch": 123}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(discover, "acquire", return_value=(Path("archive"), evidence)),
        ):
            plan = self.prepare(
                Path(directory),
                [
                    {"tag_name": "releases/gcc-8.5.0", "draft": False, "prerelease": False},
                    {"tag_name": "releases/gcc-16.2.0", "draft": True, "prerelease": False},
                ],
                configured=False,
            )
        self.assertEqual(plan["already_published"], ["8.5.0"])
        self.assertEqual(plan["needs_recovery"], ["16.2.0"])
        self.assertEqual(plan["pending"], ["14.4.0"])
        self.assertEqual([row["release"] for row in plan["build"]["include"]], ["gcc-14.4.0"] * 2)
        self.assertEqual(plan["publish"]["include"], [{"release": "gcc-14.4.0"}])

    def test_validation_can_rebuild_published_versions_without_listing_releases(self) -> None:
        for configured in (True, False):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                plan = self.prepare(
                    Path(directory),
                    [{"tag_name": "releases/gcc-16.2.0", "draft": False, "prerelease": False}],
                    configured=configured,
                    versions=None if configured else "16.2.0",
                    repository=False,
                )
                self.assertEqual(plan["selected"], ["16.2.0"])
                self.assertEqual(plan["pending"], ["16.2.0"])
                self.assertEqual(plan["already_published"], [])
                self.assertEqual(plan["needs_recovery"], [])
                self.assertEqual(len(plan["build"]["include"]), 2)

    def test_mirror_disagreement_prevents_build_plan(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(discover, "stable_tags", side_effect=[self.tags, {}]),
            patch("sys.argv", ["discover", "--configured", "--output", str(Path(directory) / "plan")]),
            self.assertRaisesRegex(RuntimeError, "tags disagree"),
        ):
            discover.main()

    def test_builder_eol_blocks_new_executions(self) -> None:
        config = copy.deepcopy(self.config)
        config["almalinux"]["security_support_end"] = "2000-01-01"
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "build.json"
            config_path.write_text(json.dumps(config))
            with patch.object(discover, "ROOT", Path(directory)), self.assertRaisesRegex(RuntimeError, "baseline"):
                self.prepare(Path(directory), [])
