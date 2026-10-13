# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify authenticated publication and recovery without GitHub writes."""

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import publish
from hashing import sha256

ROOT = Path(__file__).resolve().parent.parent


def operation(command: list[str]) -> str:
    if command[1:3] == ["attestation", "verify"]:
        return "verify"
    if command[1:4] == ["api", "--method", "PATCH"]:
        return "publish"
    if command[4].startswith("https://uploads.github.com/"):
        return "upload"
    return "create"


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = json.loads((ROOT / "build.json").read_text())
        self.config_path = self.directory / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.inputs = self.directory / "packages"
        for arch in ("x64", "arm64"):
            name = f"gcc-16.2.0-linux-{arch}"
            root = self.inputs / name
            root.mkdir(parents=True)
            for filename in (
                name + ".tar.zst",
                "gcc-16.2.0-source.tar",
                "verification.json",
                "verification-ubuntu-26.04.json",
                "verification-debian-13.json",
                "verification-fedora-44.json",
            ):
                (root / filename).write_bytes(b"fixture")
            (root / "provenance.json").write_text(
                json.dumps(
                    {
                        "release": "gcc-16.2.0",
                        "architecture": arch,
                        "configuration": self.config,
                        "recipe_sha256": "b" * 64,
                    }
                )
            )
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in sorted(root.iterdir())
                )
            )
            (self.inputs / f"attestation-gcc-16.2.0-{arch}.jsonl").write_text(f"bundle fixture {arch}\n")

    def execute(
        self,
        existing: list[dict[str, Any]] | None = None,
        *,
        verify_failure: bool = False,
        existing_release: dict[str, Any] | None = None,
        remote_assets: list[dict[str, Any]] | None = None,
        tag_target: dict[str, str] | None = None,
        resume: bool = False,
        upload_failure: bool = False,
        accepted_before_failure: bool = False,
        created_release: dict[str, Any] | None = None,
    ) -> list[list[str]]:
        uploaded = list(remote_assets or [])
        self.uploaded_contents: dict[str, bytes] = {}
        self.release_requests: list[dict[str, Any]] = []

        def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
            action = operation(command)
            if action == "verify" and verify_failure:
                raise subprocess.CalledProcessError(1, command)
            if action in ("create", "publish"):
                self.release_requests.append(json.loads(options["input"]))
            if action == "create":
                current = created_release or {
                    "id": 1,
                    "tag_name": "releases/gcc-16.2.0",
                    "draft": True,
                    "prerelease": False,
                    "target_commitish": "c" * 40,
                }
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(current))
            if action == "upload":
                self.assertTrue(command[4].startswith("https://uploads.github.com/repos/owner/repo/releases/1/assets?"))
                path = Path(command[command.index("--input") + 1])
                self.uploaded_contents[path.name] = path.read_bytes()
                if not upload_failure or accepted_before_failure:
                    uploaded.append(
                        {
                            "name": path.name,
                            "size": path.stat().st_size,
                            "digest": "sha256:" + sha256(path),
                            "state": "uploaded",
                        }
                    )
                if upload_failure:
                    raise subprocess.CalledProcessError(1, command)
            if action == "publish":
                self.assertEqual(command[4], "repos/owner/repo/releases/1")
            return subprocess.CompletedProcess(command, 0, stdout="")

        arguments = ["publish", str(self.inputs), "--config", str(self.config_path)]
        if resume:
            arguments.append("--resume")
        with (
            patch.dict(
                os.environ,
                {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "c" * 40},
                clear=False,
            ),
            patch("sys.argv", arguments),
            patch.object(
                publish,
                "get",
                side_effect=lambda _repo, endpoint, **_kw: (
                    uploaded if "/assets?" in endpoint else {"object": tag_target} if tag_target else None
                ),
            ),
            patch.object(
                publish,
                "releases",
                # Model a listing that never shows a newly created draft.
                side_effect=lambda _repo: iter([*(existing or []), *([existing_release] if existing_release else [])]),
            ),
            patch("publish.subprocess.run", side_effect=run) as commands,
        ):
            publish.main()
        return [call.args[0] for call in commands.call_args_list]

    def assert_no_remote_writes(self) -> None:
        self.assertEqual(self.release_requests, [])
        self.assertEqual(self.uploaded_contents, {})

    def test_preparation_is_independent_and_remote_checks_reuse_verified_hashes(self) -> None:
        staging = self.directory / "staging"
        staging.mkdir()
        with (
            patch.dict(os.environ, {"GITHUB_SHA": "c" * 40}),
            patch.object(publish, "get", side_effect=AssertionError("preparation must not query GitHub")),
            patch.object(publish, "verify_attestation") as attest,
            patch.object(publish, "copy_asset", wraps=publish.copy_asset) as copy_asset,
            patch.object(publish, "sha256", wraps=sha256) as measure,
        ):
            files = publish.prepare_assets(self.inputs, self.config, staging, "owner/repo")
        self.assertEqual(len(files), 9)
        self.assertEqual(attest.call_count, 4)
        self.assertEqual(measure.call_count, 3)
        self.assertTrue(all(call.args[0].name.endswith(".sha256") for call in measure.call_args_list))
        archives = [
            call.args[0] for call in copy_asset.call_args_list if call.args[0].name.endswith((".tar", ".tar.zst"))
        ]
        self.assertEqual(len(archives), 4)
        self.assertEqual(len(set(archives)), 4)
        remote = [
            {"name": asset.path.name, "size": asset.size, "digest": "sha256:" + asset.sha256, "state": "uploaded"}
            for asset in files
        ]
        with (
            patch.object(publish, "get", return_value=remote),
            patch.object(publish, "sha256", side_effect=AssertionError("remote checks must not rehash")),
        ):
            self.assertEqual(publish.missing_assets("owner/repo", 1, files), [])
            self.assertEqual(publish.missing_assets("owner/repo", 1, files), [])
        original = self.inputs / "gcc-16.2.0-linux-x64/gcc-16.2.0-linux-x64.tar.zst"
        original.write_bytes(b"changed after private staging")
        binary = next(asset for asset in files if asset.path.name == original.name)
        self.assertEqual(binary.path.read_bytes(), b"fixture")
        self.assertEqual(sha256(binary.path), binary.sha256)

    def test_attestations_are_verified_before_creating_release(self) -> None:
        commands = self.execute()
        self.assertEqual(
            [operation(command) for command in commands],
            ["verify"] * 4 + ["create"] + ["upload"] * 9 + ["publish"],
        )
        self.assertEqual(commands[4][4], "repos/owner/repo/releases")
        self.assertEqual(self.release_requests[0]["tag_name"], "releases/gcc-16.2.0")
        self.assertEqual(self.release_requests[0]["target_commitish"], "c" * 40)
        self.assertTrue(self.release_requests[0]["draft"])
        notes = self.release_requests[0]["body"]
        self.assertIn("GCC 16.2.0 with Binutils " + self.config["binutils"]["version"], notes)
        self.assertIn("glibc " + self.config["glibc_minimum"] + "+", notes)
        self.assertIn("`gcc-16.2.0-source.tar`", notes)
        self.assertIn("contain this packaging repository only", notes)
        self.assertIn("https://github.com/owner/repo/", notes)
        self.assertEqual(self.release_requests[-1], {"draft": False, "make_latest": "true"})
        for command in commands[:4]:
            for flag, expected in (
                ("--repo", "owner/repo"),
                ("--signer-workflow", "owner/repo/.github/workflows/build.yml"),
                ("--source-ref", "refs/heads/main"),
                ("--source-digest", "c" * 40),
            ):
                self.assertEqual(command[command.index(flag) + 1], expected)
            self.assertIn("--deny-self-hosted-runners", command)

    def test_accepted_upload_survives_client_failure_without_clobbering(self) -> None:
        commands = self.execute(upload_failure=True, accepted_before_failure=True)
        self.assertEqual(sum(operation(command) == "upload" for command in commands), 9)
        self.assertEqual(operation(commands[-1]), "publish")
        self.assertTrue(all("--clobber" not in command for command in commands))

    def test_invalid_creation_response_prevents_asset_upload(self) -> None:
        draft = {
            "id": 1,
            "tag_name": "releases/gcc-16.2.0",
            "draft": True,
            "prerelease": False,
            "target_commitish": "c" * 40,
        }
        for changes in (
            {"tag_name": "releases/gcc-15.3.0"},
            {"draft": False},
            {"prerelease": True},
            {"target_commitish": "d" * 40},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "Expected a draft"):
                self.execute(created_release={**draft, **changes})
            self.assertEqual(self.uploaded_contents, {})

    def test_unaccepted_upload_failure_leaves_draft(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(upload_failure=True)

    def test_draft_recovery_requires_explicit_selection_and_exact_commit(self) -> None:
        draft = {
            "id": 1,
            "tag_name": "releases/gcc-16.2.0",
            "draft": True,
            "prerelease": False,
            "target_commitish": "c" * 40,
        }
        with self.assertRaisesRegex(RuntimeError, "overwrite"):
            self.execute(existing_release=draft)
        commands = self.execute(existing_release=draft, resume=True)
        self.assertFalse(any(operation(command) == "create" for command in commands))
        self.assertEqual(operation(commands[-1]), "publish")
        for changes in ({"target_commitish": "d" * 40}, {"prerelease": True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "overwrite"):
                self.execute(existing_release={**draft, **changes}, resume=True)

    def published_fixture(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.execute()
        assets = [
            {
                "name": name,
                "size": len(data),
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "state": "uploaded",
            }
            for name, data in self.uploaded_contents.items()
        ]
        return {
            "id": 1,
            "tag_name": "releases/gcc-16.2.0",
            "draft": False,
            "prerelease": False,
            "target_commitish": "c" * 40,
        }, assets

    def test_recovery_verifies_published_release_without_mutating_it(self) -> None:
        existing, assets = self.published_fixture()
        commands = self.execute(
            existing_release=existing,
            remote_assets=assets,
            resume=True,
            tag_target={"type": "commit", "sha": "c" * 40},
        )
        self.assertEqual([operation(command) for command in commands], ["verify"] * 4)
        self.assertEqual(self.uploaded_contents, {})
        with self.assertRaisesRegex(RuntimeError, "overwrite"):
            self.execute(existing_release=existing, remote_assets=assets)

    def test_published_recovery_rejects_missing_or_mismatched_assets_and_tag(self) -> None:
        existing, assets = self.published_fixture()
        for remote, target, message in (
            (assets, None, "has no tag"),
            (assets, {"type": "commit", "sha": "d" * 40}, "exact build commit"),
            (assets[:-1], {"type": "commit", "sha": "c" * 40}, "missing assets"),
            (
                [{**assets[0], "digest": "sha256:" + "0" * 64}, *assets[1:]],
                {"type": "commit", "sha": "c" * 40},
                "mismatched release asset",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                self.execute(existing_release=existing, remote_assets=remote, tag_target=target, resume=True)

    def test_draft_recovery_checks_any_existing_tag(self) -> None:
        existing = {
            "id": 1,
            "tag_name": "releases/gcc-16.2.0",
            "draft": True,
            "prerelease": False,
            "target_commitish": "c" * 40,
        }
        commands = self.execute(
            existing_release=existing,
            resume=True,
            tag_target={"type": "commit", "sha": "c" * 40},
        )
        self.assertEqual(operation(commands[-1]), "publish")
        with self.assertRaisesRegex(RuntimeError, "exact build commit"):
            self.execute(
                existing_release=existing,
                resume=True,
                tag_target={"type": "commit", "sha": "d" * 40},
            )

    def test_annotated_tag_recovery_resolves_actual_commit(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_SHA": "c" * 40}),
            patch.object(
                publish,
                "get",
                side_effect=[
                    {"object": {"type": "tag", "sha": "a" * 40}},
                    {"object": {"type": "commit", "sha": "c" * 40}},
                ],
            ) as get,
        ):
            publish.verify_build_tag("owner/repo", "releases/gcc-16.2.0", required=True)
        self.assertEqual(get.call_args.args[1], "git/tags/" + "a" * 40)
        with (
            patch.object(publish, "get", return_value={"object": {"type": "tag", "sha": "a" * 40}}),
            self.assertRaisesRegex(RuntimeError, "Invalid annotated tag chain"),
        ):
            publish.verify_build_tag("owner/repo", "releases/gcc-16.2.0", required=True)

    def test_existing_assets_require_exact_bytes_and_complete_upload(self) -> None:
        path = self.directory / "asset"
        path.write_bytes(b"fixture")
        matching = {"name": path.name, "size": 7, "digest": "sha256:" + sha256(path), "state": "uploaded"}
        with patch.object(publish, "get", return_value=[matching]):
            self.assertEqual(publish.missing_assets("owner/repo", 1, [publish.Asset.inspect(path)]), [])
        for changes in (
            {"name": "extra"},
            {"size": 8},
            {"digest": None},
            {"digest": "sha256:" + "0" * 64},
            {"state": "starter"},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(publish, "get", return_value=[{**matching, **changes}]),
                self.assertRaisesRegex(RuntimeError, "mismatched"),
            ):
                publish.missing_assets("owner/repo", 1, [publish.Asset.inspect(path)])

    def test_matching_existing_assets_are_not_uploaded(self) -> None:
        path = self.directory / "asset"
        path.write_bytes(b"fixture")
        matching = {"name": path.name, "size": 7, "digest": "sha256:" + sha256(path), "state": "uploaded"}
        with patch.object(publish, "get", return_value=[matching]), patch("publish.subprocess.run") as run:
            publish.upload_assets("owner/repo", 1, [publish.Asset.inspect(path)])
        run.assert_not_called()

    def test_incomplete_upload_is_rejected_before_publication(self) -> None:
        path = self.directory / "asset"
        path.write_bytes(b"fixture")
        with (
            patch.object(publish, "get", return_value=[]),
            patch("publish.subprocess.run"),
            self.assertRaisesRegex(RuntimeError, "incomplete"),
        ):
            publish.upload_assets("owner/repo", 1, [publish.Asset.inspect(path)])

    def test_historical_backfill_is_not_latest(self) -> None:
        commands = self.execute([{"tag_name": "releases/gcc-17.1.0", "draft": False, "prerelease": False}])
        self.assertEqual(operation(commands[-1]), "publish")
        self.assertEqual(self.release_requests[-1], {"draft": False, "make_latest": "false"})

    def test_corrupt_archive_prevents_publication(self) -> None:
        archive = self.inputs / "gcc-16.2.0-linux-x64" / "gcc-16.2.0-linux-x64.tar.zst"
        archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.execute()
        self.assert_no_remote_writes()

    def test_mismatched_build_identity_prevents_publication(self) -> None:
        root = self.inputs / "gcc-16.2.0-linux-arm64"
        provenance = root / "provenance.json"
        original = json.loads(provenance.read_text())
        for field, value, message in (
            ("configuration", {}, "Unexpected arm64 provenance"),
            ("release", "gcc-15.3.0", "Unexpected arm64 provenance"),
            ("architecture", "x64", "Unexpected arm64 provenance"),
            ("recipe_sha256", "d" * 64, "different recipes"),
        ):
            provenance.write_text(json.dumps({**original, field: value}))
            (root / "SHA256SUMS").write_text(
                "".join(
                    f"{sha256(path)}  {path.name}\n" for path in sorted(root.iterdir()) if path.name != "SHA256SUMS"
                )
            )
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, message):
                self.execute()
            self.assert_no_remote_writes()

    def test_invalid_checksum_manifest_prevents_publication(self) -> None:
        manifest = self.inputs / "gcc-16.2.0-linux-x64/SHA256SUMS"
        original = manifest.read_text()
        first = original.splitlines(keepends=True)[0]
        for contents in (original + first, original.replace(first, ""), original + "0" * 64 + "  ../escape\n"):
            manifest.write_text(contents)
            with self.subTest(manifest=contents), self.assertRaisesRegex(RuntimeError, "checksum"):
                self.execute()
            self.assert_no_remote_writes()

    def test_release_publishes_one_source_and_per_archive_sidecars(self) -> None:
        commands = self.execute()
        files = {
            Path(command[command.index("--input") + 1]).name for command in commands if operation(command) == "upload"
        }
        archives = {"gcc-16.2.0-source.tar", "gcc-16.2.0-linux-x64.tar.zst", "gcc-16.2.0-linux-arm64.tar.zst"}
        self.assertEqual(files, {name + suffix for name in archives for suffix in ("", ".sha256", ".jsonl")})
        for name in archives:
            expected = hashlib.sha256(self.uploaded_contents[name]).hexdigest()
            self.assertEqual(self.uploaded_contents[name + ".sha256"], f"{expected}  {name}\n".encode())
        for arch in ("x64", "arm64"):
            self.assertEqual(
                self.uploaded_contents[f"gcc-16.2.0-linux-{arch}.tar.zst.jsonl"], f"bundle fixture {arch}\n".encode()
            )
        self.assertEqual(self.uploaded_contents["gcc-16.2.0-source.tar.jsonl"], b"bundle fixture x64\n")

    def test_different_source_archives_prevent_publication(self) -> None:
        root = self.inputs / "gcc-16.2.0-linux-arm64"
        (root / "gcc-16.2.0-source.tar").write_bytes(b"different source")
        (root / "SHA256SUMS").write_text(
            "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(root.iterdir()) if path.name != "SHA256SUMS")
        )
        with self.assertRaisesRegex(RuntimeError, "different corresponding-source"):
            self.execute()
        self.assert_no_remote_writes()

    def test_missing_architecture_prevents_publication(self) -> None:
        (self.inputs / "gcc-16.2.0-linux-arm64").rename(self.inputs / "missing")
        with self.assertRaisesRegex(RuntimeError, "Incomplete or ambiguous.*arm64"):
            self.execute()
        self.assert_no_remote_writes()

    def test_missing_attestation_prevents_publication(self) -> None:
        (self.inputs / "attestation-gcc-16.2.0-x64.jsonl").unlink()
        with self.assertRaisesRegex(RuntimeError, "attestation"):
            self.execute()
        self.assert_no_remote_writes()

    def test_invalid_attestation_prevents_release_creation(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute(verify_failure=True)
        self.assert_no_remote_writes()

    def test_other_releases_cannot_collide_with_attestation_names(self) -> None:
        for arch in ("x64", "arm64"):
            (self.inputs / f"attestation-gcc-8.5.0-{arch}.jsonl").write_text("other release\n")
        commands = self.execute()
        self.assertEqual(
            [Path(command[command.index("--bundle") + 1]).name for command in commands[:4]],
            ["attestation-gcc-16.2.0-x64.jsonl"] * 2 + ["attestation-gcc-16.2.0-arm64.jsonl"] * 2,
        )

    def test_batch_publication_is_numeric_oldest_to_newest(self) -> None:
        plan = self.directory / "plan"
        plan.mkdir()
        versions = ["16.2.0", "8.5.0", "10.1.0"]
        (plan / "plan.json").write_text(
            json.dumps({"publish": {"include": [{"release": "gcc-" + v} for v in versions]}})
        )
        for version in versions:
            config = copy.deepcopy(self.config)
            config["gcc"]["version"] = version
            config["gcc"]["tag"] = "releases/gcc-" + version
            (plan / ("gcc-" + version + ".json")).write_text(json.dumps(config))
        with (
            patch("sys.argv", ["publish", str(self.inputs), "--plan", str(plan)]),
            patch.object(publish, "publish") as write,
        ):
            publish.main()
        self.assertEqual(
            [call.args[1]["gcc"]["version"] for call in write.call_args_list], ["8.5.0", "10.1.0", "16.2.0"]
        )

    def test_existing_release_or_tag_is_rejected(self) -> None:
        with patch.object(publish, "get", return_value={"id": 1}), self.assertRaisesRegex(RuntimeError, "overwrite"):
            publish.require_absent("owner/repo", "releases/tags/existing")
