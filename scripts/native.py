# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Docker execution and invocation-owned container shutdown."""

import platform
import subprocess
from typing import Any


def run(*arguments: object, **options: Any) -> subprocess.CompletedProcess[Any]:
    print("+", " ".join(map(str, arguments)), flush=True)
    return subprocess.run(list(map(str, arguments)), check=True, **options)


def native_platform() -> tuple[str, str]:
    machine = platform.machine()
    supported = {"x86_64": ("x64", "linux/amd64"), "aarch64": ("arm64", "linux/arm64")}
    if platform.system() != "Linux" or machine not in supported:
        raise ValueError("Execution requires a native Linux x64 or ARM64 Docker host")
    docker_arch = subprocess.run(
        ["docker", "info", "--format", "{{.Architecture}}"], check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    if docker_arch != machine:
        raise ValueError("Docker architecture differs from the host; emulation is unsupported")
    return supported[machine]


def stop_owned_container(name: str) -> None:
    """Confirm shutdown before a caller touches the container's private storage."""
    owned = subprocess.run(
        ["docker", "container", "ls", "--all", "--quiet", "--filter", f"name=^/{name}$"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.split()
    if owned:
        subprocess.run(["docker", "rm", "--force", *owned], check=True)
