# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise build orchestration and verifier failures with disposable tool fixtures."""

import json
import os
import platform
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build
import portability
import verify
from fixture_tools import install_tool
from hashing import sha256

ROOT = Path(__file__).resolve().parent.parent


class BuildExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recipe = self.root / "recipe"
        self.recipe.mkdir()
        self.config = self.recipe / "build.json"
        self.config.write_text((ROOT / "build.json").read_text())
        (self.recipe / "portability.json").write_text((ROOT / "portability.json").read_text())
        (self.recipe / "recipe-files.txt").write_text("recipe-files.txt\nbuild.json\nportability.json\n")
        self.output = self.root / "results"
        self.calls = self.root / "calls.jsonl"
        install_tool(self.root, "docker", "docker.py")
        environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                "GCC_TEST_DOCKER_CALLS": str(self.calls),
                "GCC_TEST_DOCKER_ARCH": "x86_64",
                "DOCKER_DEFAULT_PLATFORM": "linux/arm64",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, handler)
        self.enterContext(patch.object(platform, "system", return_value="Linux"))
        self.enterContext(patch.object(platform, "machine", return_value="x86_64"))

    def execute(self, *arguments: str) -> None:
        with (
            patch.object(build, "ROOT", self.recipe),
            patch(
                "sys.argv",
                ["build", "--config", str(self.config), "--output", str(self.output), "--jobs", "4", *arguments],
            ),
        ):
            build.main()

    def test_full_orchestration_publishes_only_verified_results_on_native_platform(self) -> None:
        self.execute()
        output = self.output / "gcc-16.2.0-linux-x64"
        self.assertEqual((output / "verification.json").read_text(), "verified\n")
        for line in (output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(sha256(output / name), digest)
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        for command in commands:
            if command[0] in ("build", "run"):
                self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        compile_command = next(command for command in commands if "JOBS=4" in command)
        self.assertEqual(compile_command[compile_command.index("--cpus") + 1], "4")
        preparation = [index for index, command in enumerate(commands) if command[0] == "build"]
        self.assertEqual(len(preparation), 5)
        self.assertLess(max(preparation), commands.index(compile_command))
        self.assert_distribution_checks(commands, output)
        with self.assertRaises(SystemExit):
            self.execute()
        self.assertTrue(output.is_dir())

    def test_candidate_build_defers_all_distribution_checks_and_retains_checksums(self) -> None:
        self.execute("--defer-verification")
        output = self.output / "gcc-16.2.0-linux-x64"
        self.assertFalse(list(output.glob("verification*.json")))
        self.assertTrue((output / "gcc-16.2.0-linux-x64.tar.zst").is_file())
        self.assertTrue((output / "gcc-16.2.0-source.tar").is_file())
        for line in (output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(sha256(output / name), digest)
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum(command[0] == "build" for command in commands), 1)
        self.assertFalse(any("--archive" in command for command in commands))

    def assert_distribution_checks(self, commands: list[list[str]], output: Path) -> None:
        reports = {
            "verification.json",
            "verification-ubuntu-26.04.json",
            "verification-debian-13.json",
            "verification-fedora-44.json",
        }
        self.assertEqual({path.name for path in output.glob("verification*.json")}, reports)
        verifications = [command for command in commands if "--archive" in command]
        self.assertEqual(len(verifications), len(reports))
        archives = set()
        for verification in verifications:
            self.assertIn("--network=none", verification)
            prefix = verification[verification.index("--archive") - 1]
            self.assertNotIn(" ", prefix)
            self.assertNotEqual(prefix, "/opt/gcc-builds/gcc-16.2.0")
            mounts = [verification[i + 1] for i, value in enumerate(verification) if value == "--mount"]
            self.assertEqual(
                {mount.split(",dst=")[1].split(",")[0] for mount in mounts},
                {"/recipe", "/results", "/input.tar.zst"},
            )
            archive = next(mount for mount in mounts if "dst=/input.tar.zst" in mount)
            self.assertTrue(archive.endswith(",readonly"))
            archives.add(archive)
            report = Path(verification[verification.index("--output") + 1]).name
            self.assertEqual((output / report).read_text(), "verified\n")
        self.assertEqual(len(archives), 1)

    def test_standalone_verification_uses_full_gate_without_rebuilding_compiler(self) -> None:
        archive = self.root / "gcc-16.2.0-linux-x64.tar.zst"
        archive.write_bytes(b"locally built archive")
        with patch.object(portability, "ROOT", self.recipe):
            portability.verify(archive, self.output, json.loads(self.config.read_text()), "host")
            with self.assertRaises(FileExistsError):
                portability.verify(archive, self.output, json.loads(self.config.read_text()), "host")
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assert_distribution_checks(commands, self.output)
        self.assertFalse(any("JOBS=4" in command or "builder" in command for command in commands))
        for command in commands:
            if command[0] == "build":
                self.assertEqual(command[command.index("--network") + 1], "host")

    def test_standalone_consumer_failure_leaves_no_completed_report_directory(self) -> None:
        archive = self.root / "gcc-16.2.0-linux-x64.tar.zst"
        archive.write_bytes(b"locally built archive")
        with (
            patch.object(portability, "ROOT", self.recipe),
            patch.dict(os.environ, {"GCC_TEST_BUILD_FAIL": "verification-debian-13.json"}),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            portability.verify(archive, self.output, json.loads(self.config.read_text()), "bridge")
        self.assertFalse(self.output.exists())
        self.assertFalse(any(self.root.glob(".gcc-portability-*")))

    def test_single_distribution_cli_runs_only_its_verifier(self) -> None:
        archive = self.root / "gcc-16.2.0-linux-x64.tar.zst"
        archive.write_bytes(b"locally built archive")
        for distribution, report in (
            ("almalinux-8.10", "verification.json"),
            ("debian-13", "verification-debian-13.json"),
        ):
            output = self.root / distribution
            with (
                self.subTest(distribution=distribution),
                patch.object(portability, "ROOT", self.recipe),
                patch(
                    "sys.argv",
                    [
                        "portability",
                        str(archive),
                        "--output",
                        str(output),
                        "--config",
                        str(self.config),
                        "--distribution",
                        distribution,
                    ],
                ),
            ):
                portability.main()
            self.assertEqual({path.name for path in output.iterdir()}, {report})
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum(command[0] == "build" for command in commands), 2)
        self.assertEqual(sum("--archive" in command for command in commands), 2)

    def test_unknown_distribution_cannot_silently_skip_verification(self) -> None:
        with (
            patch.object(portability, "ROOT", self.recipe),
            self.assertRaisesRegex(ValueError, "Unknown verification distribution"),
        ):
            portability.verify(
                self.root / "archive", self.output, json.loads(self.config.read_text()), "bridge", "typo"
            )
        self.assertFalse(self.output.exists())
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any(command[0] in ("build", "run") for command in commands))

    def test_standalone_failed_shutdown_preserves_storage_and_verification_error(self) -> None:
        work, staging = self.root / "work", self.root / "staging"
        work.mkdir()
        staging.mkdir()
        with (
            patch.object(portability, "ROOT", self.recipe),
            patch("portability.tempfile.mkdtemp", side_effect=[str(work), str(staging)]),
            patch.object(portability, "verify_archive", side_effect=RuntimeError("compiler verification failed")),
            patch.object(portability, "stop_owned_container", side_effect=OSError("Docker unavailable")),
            self.assertRaisesRegex(RuntimeError, "compiler verification failed"),
        ):
            portability.verify(self.root / "archive", self.output, json.loads(self.config.read_text()), "bridge")
        self.assertTrue((work / "recipe").is_dir())
        self.assertTrue(staging.is_dir())
        self.assertFalse(self.output.exists())

    def test_failures_and_interruptions_leave_no_published_or_partial_artifacts(self) -> None:
        failures = ("build", "compile", "pack", "verify", "verification-debian-13.json", "interrupt")
        for failure in failures:
            with self.subTest(failure=failure), patch.dict(os.environ, {"GCC_TEST_BUILD_FAIL": failure}):
                expected = (
                    KeyboardInterrupt if failure == "interrupt" else (subprocess.CalledProcessError, RuntimeError)
                )
                with self.assertRaises(expected):
                    self.execute()
            self.assertFalse((self.output / "gcc-16.2.0-linux-x64").exists())
            self.assertFalse(any(self.output.glob(".gcc-builds-*")))
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        workspaces = [
            Path(argument.split("src=", 1)[1].split(",dst=", 1)[0])
            for command in commands
            for argument in command
            if "dst=/cleanup" in argument
        ]
        self.assertEqual(len(workspaces), len(failures))
        self.assertTrue(all(not work.exists() for work in workspaces))

    def test_remote_daemon_architecture_mismatch_stops_before_build(self) -> None:
        with patch.dict(os.environ, {"GCC_TEST_DOCKER_ARCH": "aarch64"}), self.assertRaises(SystemExit):
            self.execute()
        commands = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], "info")


class VerifierExecutionTests(unittest.TestCase):
    def test_elf_and_symlink_failures_prevent_verification_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "relocated toolchain"
            (prefix / "share").mkdir(parents=True)
            (prefix / "share/build.json").write_text((ROOT / "build.json").read_text())
            (prefix / "bin").mkdir()
            (prefix / "lib64").mkdir()
            for name in ("libstdc++.so.6", "libgcc_s.so.1", "libzstd.so.1"):
                (prefix / "lib64" / name).write_text("fixture runtime")
            for name in ("cc1", "cc1plus", "collect2", "lto1", "liblto_plugin.so"):
                (prefix / "bin" / name).write_text("fixture program")

            for name in (
                "gcc",
                "g++",
                "as",
                "ld",
                "ar",
                "ranlib",
                "readelf",
                "objcopy",
                "strip",
                "gcov",
                "gcc-ar",
                "gcc-nm",
                "gcc-ranlib",
            ):
                install_tool(prefix / "bin", name, "compiler.py")
            (prefix / "bin/payload").write_bytes(b"\x7fELFfixture")
            # ldd is deliberately an independent process, selected by the verifier's PATH.
            install_tool(prefix / "bin", "ldd", "ldd.py")
            report = root / "verification.json"
            for fixture, message in (
                ({"version": "GLIBC_PRIVATE"}, "no private glibc ABI"),
                ({"version": "GLIBC_2.29"}, "glibc baseline"),
                ({"dynamic": "(RUNPATH) Library runpath: [/work/build/lib]"}, "no build-path runpath"),
                ({}, "resolved dependencies"),
                ({"loaded": "libstdc++.so.6 => /usr/lib/libstdc++.so.6 (0x1234)"}, "packaged dependency"),
            ):
                (prefix / "share/fixture.json").write_text(json.dumps(fixture))
                with (
                    self.subTest(fixture=fixture),
                    patch("sys.argv", ["verify", str(prefix), "--output", str(report)]),
                    self.assertRaisesRegex(RuntimeError, message),
                ):
                    verify.main()
                self.assertFalse(report.exists())
            (prefix / "bin/escape").symlink_to(root)
            with (
                patch("sys.argv", ["verify", str(prefix), "--output", str(report)]),
                self.assertRaisesRegex(RuntimeError, "contained symlink"),
            ):
                verify.main()
            self.assertFalse(report.exists())

    def test_fixture_compiler_version_mismatch_prevents_verification_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "relocated toolchain"
            (prefix / "share").mkdir(parents=True)
            (prefix / "share/build.json").write_text((ROOT / "build.json").read_text())
            (prefix / "bin").mkdir()
            compiler = prefix / "bin/gcc"
            compiler.write_text("#!/bin/sh\nprintf '%s\\n' 0.0.0\n")
            compiler.chmod(0o755)
            report = root / "verification.json"
            with (
                patch("sys.argv", ["verify", str(prefix), "--output", str(report)]),
                self.assertRaisesRegex(RuntimeError, "compiler version"),
            ):
                verify.main()
            self.assertFalse(report.exists())

    def test_failed_archive_extraction_prevents_verification_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "verification.json"
            (root / "bad.tar.zst").write_bytes(b"invalid archive")
            with (
                patch(
                    "sys.argv",
                    ["verify", str(root / "prefix"), "--archive", str(root / "bad.tar.zst"), "--output", str(report)],
                ),
                patch("verify.subprocess.run", side_effect=subprocess.CalledProcessError(2, ["tar"])),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                verify.main()
            self.assertFalse(report.exists())
