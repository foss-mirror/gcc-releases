# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the native execution and container shutdown boundaries."""

import subprocess
import unittest
from unittest.mock import patch

import native


class NativePlatformTests(unittest.TestCase):
    def test_supported_hosts_select_their_native_platform(self) -> None:
        for machine, expected in (("x86_64", ("x64", "linux/amd64")), ("aarch64", ("arm64", "linux/arm64"))):
            with (
                self.subTest(machine=machine),
                patch("native.platform.system", return_value="Linux"),
                patch("native.platform.machine", return_value=machine),
                patch("native.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=machine)),
            ):
                self.assertEqual(native.native_platform(), expected)

    def test_unsupported_hosts_stop_before_contacting_docker(self) -> None:
        for system, machine in (("Darwin", "aarch64"), ("Linux", "riscv64")):
            with (
                self.subTest(system=system, machine=machine),
                patch("native.platform.system", return_value=system),
                patch("native.platform.machine", return_value=machine),
                patch("native.subprocess.run") as run,
                self.assertRaisesRegex(ValueError, "native Linux"),
            ):
                native.native_platform()
            run.assert_not_called()

    def test_mismatched_daemon_is_rejected(self) -> None:
        with (
            patch("native.platform.system", return_value="Linux"),
            patch("native.platform.machine", return_value="x86_64"),
            patch("native.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="aarch64")),
            self.assertRaisesRegex(ValueError, "emulation"),
        ):
            native.native_platform()

    def test_daemon_failure_is_not_reported_as_a_supported_platform(self) -> None:
        with (
            patch("native.platform.system", return_value="Linux"),
            patch("native.platform.machine", return_value="x86_64"),
            patch("native.subprocess.run", side_effect=OSError("Docker unavailable")),
            self.assertRaises(OSError),
        ):
            native.native_platform()


class ContainerShutdownTests(unittest.TestCase):
    def test_only_exact_invocation_name_is_stopped(self) -> None:
        with patch(
            "native.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0, stdout="owned-id\n"), None]
        ) as run:
            native.stop_owned_container("owned-name")
        self.assertIn("name=^/owned-name$", run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", "owned-id"])

    def test_absent_container_needs_no_removal(self) -> None:
        with patch("native.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as run:
            native.stop_owned_container("owned-name")
        self.assertEqual(run.call_count, 1)

    def test_shutdown_failure_propagates_to_the_cleanup_policy(self) -> None:
        with (
            patch(
                "native.subprocess.run",
                side_effect=[subprocess.CompletedProcess([], 0, stdout="owned-id"), OSError("Docker down")],
            ),
            self.assertRaises(OSError),
        ):
            native.stop_owned_container("owned-name")
