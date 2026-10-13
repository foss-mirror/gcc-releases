# SPDX-License-Identifier: GPL-3.0-or-later
"""Check package inspection and runtime containment."""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import verify

ROOT = Path(__file__).resolve().parent.parent


class VerificationTests(unittest.TestCase):
    def test_named_runtime_must_resolve_inside_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / "relocated toolchain"
            prefix.mkdir()
            library = prefix / "libstdc++.so.6"
            library.write_bytes(b"fixture")
            loaded = f"libstdc++.so.6 => {library} (0x1234)\n"
            self.assertTrue(verify.uses_packaged_library(loaded, "libstdc++.so.6", prefix))
            outside = root / "libstdc++.so.6"
            outside.write_bytes(b"host runtime")
            for invalid in (
                f"libgcc_s.so.1 => {library} (0x1234)\nlibstdc++.so.6 => {outside} (0x5678)\n",
                "libstdc++.so.6 => not found\n",
                f"libstdc++.so.6 => {prefix / 'missing'} (0x1234)\n",
                "statically linked\n",
            ):
                with self.subTest(loaded=invalid):
                    self.assertFalse(verify.uses_packaged_library(invalid, "libstdc++.so.6", prefix))
            library.unlink()
            library.symlink_to(outside)
            self.assertFalse(verify.uses_packaged_library(loaded, "libstdc++.so.6", prefix))


class PackageInspectionTests(unittest.TestCase):
    def test_verifier_sdk_cannot_hide_an_undeclared_runtime_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "payload").write_bytes(b"\x7fELFfixture")
            for dependency, shipped, allowed in (
                ("libc.so.6", False, True),
                ("libz.so.1", False, True),
                ("liblzma.so.5", False, False),
                ("liblzma.so.5", True, True),
            ):
                if shipped:
                    (prefix / dependency).write_bytes(b"fixture library")
                loaded = prefix / dependency if shipped else Path("/usr/lib64") / dependency
                verifier = verify.Verifier(prefix, {"glibc_minimum": "2.28"})
                with (
                    self.subTest(dependency=dependency, shipped=shipped),
                    patch.object(
                        verifier,
                        "run",
                        side_effect=[
                            "GLIBC_2.28",
                            f"(NEEDED) Shared library: [{dependency}]",
                            f"{dependency} => {loaded} (0x1234)",
                        ],
                    ),
                ):
                    if allowed:
                        verify.inspect_contents(verifier)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "declared runtime dependencies.*liblzma"):
                            verify.inspect_contents(verifier)

    def test_elf_baseline_can_be_checked_without_a_compiler_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            (prefix / "payload").write_bytes(b"\x7fELFfixture")
            verifier = verify.Verifier(prefix, {"glibc_minimum": "2.28"})
            with (
                patch.object(verifier, "run", return_value="GLIBC_2.29"),
                self.assertRaisesRegex(RuntimeError, "glibc baseline"),
            ):
                verify.inspect_contents(verifier)

    def test_plain_files_and_symlinks_preserve_report_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            path = prefix / "header.h"
            path.write_bytes(b"header")
            (prefix / "link.h").symlink_to("header.h")
            verifier = verify.Verifier(prefix, {"glibc_minimum": "2.28"})
            with patch.object(verifier, "run", side_effect=AssertionError("plain files need no process")):
                inspection = verify.inspect_contents(verifier)
            self.assertEqual(inspection.requirements, {})
            self.assertEqual(inspection.dependencies, {})
            self.assertEqual(inspection.contents[0]["sha256"], hashlib.sha256(b"header").hexdigest())
            self.assertEqual(inspection.contents[1], {"path": "link.h", "link": "header.h"})
            output = prefix / "verification.json"
            verify.write_report(output, verifier, inspection)
            self.assertEqual(
                set(json.loads(output.read_text())),
                {"archive_sha256", "environment", "checks", "elf_glibc_requirements", "elf_dependencies", "contents"},
            )


class VerifierCommandTests(unittest.TestCase):
    def test_failed_commands_print_captured_diagnostics_even_when_quiet(self) -> None:
        verifier = verify.Verifier(Path("/unused"), {})
        for quiet in (False, True):
            output = io.StringIO()
            with (
                self.subTest(quiet=quiet),
                patch(
                    "verify.subprocess.run",
                    return_value=subprocess.CompletedProcess(["gcc"], 1, stdout="lto-wrapper: make is missing\n"),
                ),
                redirect_stdout(output),
                self.assertRaises(subprocess.CalledProcessError) as caught,
            ):
                verifier.run("gcc", "-flto=2", quiet=quiet)
            self.assertIn("gcc -flto=2", output.getvalue())
            self.assertIn("lto-wrapper: make is missing", output.getvalue())
            self.assertEqual(caught.exception.output, "lto-wrapper: make is missing\n")

    def test_successful_quiet_commands_return_output_without_printing(self) -> None:
        verifier = verify.Verifier(Path("/unused"), {})
        output = io.StringIO()
        with (
            patch(
                "verify.subprocess.run", return_value=subprocess.CompletedProcess(["readelf"], 0, stdout="ELF details")
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(verifier.run("readelf", quiet=True), "ELF details")
        self.assertEqual(output.getvalue(), "")


class GcovOutputTests(unittest.TestCase):
    def test_json_option_is_unambiguous_on_every_supported_major(self) -> None:
        for major in range(9, 17):
            with self.subTest(major=major), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                verifier = verify.Verifier(work / "compiler", {"gcc": {"version": f"{major}.5.0"}})
                obj = work / "answer.o"
                with patch.object(verifier, "run") as run:
                    run.side_effect = lambda *args, output=work / "answer.gcov.json.gz", **kwargs: output.write_bytes(
                        b"fixture"
                    )
                    verify.check_gcov_output(verifier, obj, work)
                run.assert_called_once_with(verifier.prefix / "bin/gcov", "--json-format", obj, cwd=work)
                self.assertEqual(verifier.checks, ["gcov JSON output"])

    def test_gcc_8_retains_annotated_source_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            verifier = verify.Verifier(work / "compiler", {"gcc": {"version": "8.5.0"}})
            obj = work / "answer.o"
            with patch.object(verifier, "run") as run:
                run.side_effect = lambda *args, **kwargs: (work / "answer.c.gcov").write_text("fixture")
                verify.check_gcov_output(verifier, obj, work)
            run.assert_called_once_with(verifier.prefix / "bin/gcov", obj, cwd=work)
            self.assertEqual(verifier.checks, ["gcov annotated source output"])

    def test_missing_json_is_rejected_even_when_gcov_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            verifier = verify.Verifier(work / "compiler", {"gcc": {"version": "10.5.0"}})
            # This is the annotated output produced by the incorrect -j option.
            (work / "answer.c.gcov").write_text("fixture")
            with (
                patch.object(verifier, "run", return_value=""),
                self.assertRaisesRegex(RuntimeError, "gcov JSON output"),
            ):
                verify.check_gcov_output(verifier, work / "answer.o", work)
