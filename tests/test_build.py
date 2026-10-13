# SPDX-License-Identifier: GPL-3.0-or-later
"""Check compiler configuration, cleanup, and source packaging."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build

ROOT = Path(__file__).resolve().parent.parent


class BuildRecipeTests(unittest.TestCase):
    def test_configuration_supports_consumer_layouts_and_legacy_fixincludes(self) -> None:
        probe = 'source "$1"; configure_gcc "$2" "$3" "$4"'
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "capture-configure"
            capture.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            capture.chmod(0o755)
            self.check_configure(probe, capture)

    def check_configure(self, probe: str, capture: Path) -> None:
        for major in range(8, 18):
            with self.subTest(major=major):
                result = subprocess.run(
                    [
                        "bash",
                        "-euo",
                        "pipefail",
                        "-c",
                        probe,
                        "configure-probe",
                        str(ROOT / "scripts/build-toolchain.sh"),
                        f"{major}.5.0",
                        "/opt/gcc trial",
                        str(capture),
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                arguments = result.stdout.splitlines()
                self.assertEqual("--disable-fixincludes" in arguments, major >= 13)
                self.assertIn("--prefix=/opt/gcc trial", arguments)
                self.assertIn("--enable-languages=c,c++", arguments)
                self.assertIn("--enable-multiarch", arguments)
                self.assertIn("--disable-multilib", arguments)

    def test_fixed_headers_retain_only_gcc_limit_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package with spaces"
            installed = package / "lib/gcc/x86_64-pc-linux-gnu/8.5.0"
            fixed = installed / "include-fixed"
            (fixed / "bits").mkdir(parents=True)
            for relative in ("limits.h", "syslimits.h", "README", "bits/statx.h", "bits/limits.h"):
                (fixed / relative).write_text(relative)
            intrinsic = installed / "include/immintrin.h"
            intrinsic.parent.mkdir()
            intrinsic.write_text("compiler intrinsic")
            outside = root / "outside"
            outside.mkdir()
            (outside / "header.h").write_text("untouched")
            (fixed / "linked-headers").symlink_to(outside, target_is_directory=True)
            subprocess.run(
                [
                    "bash",
                    "-euo",
                    "pipefail",
                    "-c",
                    'source "$1"; filter_fixed_headers "$2"; filter_fixed_headers "$2"',
                    "filter-probe",
                    str(ROOT / "scripts/build-toolchain.sh"),
                    str(package),
                ],
                check=True,
            )
            self.assertEqual(sorted(path.name for path in fixed.iterdir()), ["limits.h", "syslimits.h"])
            for name in ("limits.h", "syslimits.h"):
                self.assertEqual((fixed / name).read_text(), name)
            self.assertEqual(intrinsic.read_text(), "compiler intrinsic")
            self.assertEqual((outside / "header.h").read_text(), "untouched")


class BuildCleanupTests(unittest.TestCase):
    def test_failed_shutdown_preserves_workspace_and_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, staging = root / "work", root / "staging"
            work.mkdir()
            staging.mkdir()
            for error in (
                subprocess.CalledProcessError(1, ["docker", "container", "ls"]),
                OSError("cannot stop container"),
            ):
                with self.subTest(error=error), patch.object(build, "stop_owned_container", side_effect=error):
                    build.cleanup(
                        work, staging, "owned-container", "image", keep_work=False, docker_platform="linux/amd64"
                    )
                self.assertTrue(work.is_dir())
                self.assertTrue(staging.is_dir())

    def test_workspace_cleanup_failure_preserves_build_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, staging = root / "work", root / "staging"
            work.mkdir()
            staging.mkdir()
            with (
                patch.object(build, "stop_owned_container"),
                patch("build.subprocess.run", side_effect=OSError("Docker unavailable")),
                self.assertRaisesRegex(RuntimeError, "compiler failed"),
            ):
                try:
                    raise RuntimeError("compiler failed")
                finally:
                    build.cleanup(
                        work, staging, "owned-container", "image", keep_work=False, docker_platform="linux/amd64"
                    )
            self.assertTrue(work.is_dir())
            self.assertFalse(staging.exists())

    def test_keep_work_stops_owned_container_before_artifact_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work, staging = root / "work", root / "staging"
            work.mkdir()
            staging.mkdir()
            (staging / "partial-archive").write_bytes(b"partial")
            with patch.object(build, "stop_owned_container") as stop:
                build.cleanup(work, staging, "owned-container", "image", keep_work=True, docker_platform="linux/amd64")
            stop.assert_called_once_with("owned-container")
            self.assertTrue(work.is_dir())
            self.assertFalse(staging.exists())


class SourcePackagingTests(unittest.TestCase):
    def test_sources_preserve_original_archives_and_ignore_architecture_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = root / "recipe"
            recipe.mkdir()
            (recipe / "sdk-packages.json").write_text('{"both architectures":"locked"}\n')
            sources = []
            for arch in ("x64", "arm64"):
                work = root / arch
                downloads = work / "downloads"
                downloads.mkdir(parents=True)
                for name in ("gcc.tar.xz", "gcc.tar.xz.sig", "support.src.rpm"):
                    (downloads / name).write_bytes(name.encode())
                metadata = {
                    "release": "gcc-16.2.0",
                    "architecture": arch,
                    "packages": ["architecture-dependent " + arch],
                    "recipe_sha256": "b" * 64,
                    "configuration": {"pinned": True},
                }
                source = build.prepare_sources(work, recipe, metadata)
                sources.append(
                    {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                )
                provenance = json.loads((source / "provenance.json").read_text())
                self.assertEqual(set(provenance), {"release", "recipe_sha256", "configuration"})
                for path in downloads.iterdir():
                    self.assertEqual((source / "downloads" / path.name).read_bytes(), path.read_bytes())
            self.assertEqual(sources[0], sources[1])
