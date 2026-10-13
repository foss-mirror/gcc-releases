#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicitly refresh signed RPM locks for both native SDK architectures."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from types import FrameType
from typing import Any

from hashing import sha256
from local_state import exclusive_lock, replace_lock
from native import native_platform, stop_owned_container

ROOT = Path(__file__).resolve().parent.parent


def restore_ownership(work: Path, image: str, docker_platform: str) -> bool:
    """Confirm shutdown before touching resolver files; preserve the primary error."""
    try:
        stop_owned_container(work.name)
        subprocess.run(
            [
                "docker",
                "run",
                "--platform",
                docker_platform,
                "--rm",
                "--network=none",
                "--mount",
                f"type=bind,src={work},dst=/output",
                "--entrypoint",
                "chown",
                image,
                "-R",
                f"{os.getuid()}:{os.getgid()}",
                "/output",
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Resolver cleanup failed: {error}; retained {work}", flush=True)
        return False
    return True


def main() -> None:
    def interrupted(signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt(f"SDK refresh interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("bridge", "host"), default="bridge")
    args = parser.parse_args()
    config = json.loads((ROOT / "build.json").read_text())
    path = ROOT / "sdk-packages.json"
    try:
        _, docker_platform = native_platform()
    except ValueError as error:
        parser.error(str(error))
    with exclusive_lock(path.with_name("." + path.name + ".lock")):
        original = path.read_bytes()
        inventory = json.loads(original)
        refresh(config, path, original, inventory, args.network, docker_platform)


def refresh(
    config: dict[str, Any], path: Path, original: bytes, inventory: dict[str, Any], network: str, docker_platform: str
) -> None:
    work = Path(tempfile.mkdtemp(prefix="gcc-sdk-lock-", dir="/tmp"))
    safe_to_remove = False
    try:
        command = [
            "docker",
            "run",
            "--platform",
            docker_platform,
            "--rm",
            "--init",
            "--name",
            work.name,
            "--network",
            network,
            "--mount",
            f"type=bind,src={ROOT},dst=/recipe,readonly",
            "--mount",
            f"type=bind,src={work},dst=/output",
            config["build_image"],
            "bash",
            "/recipe/scripts/resolve-sdk.sh",
            config["almalinux"]["version"],
        ]
        try:
            subprocess.run(command, check=True)
        finally:
            safe_to_remove = restore_ownership(work, config["build_image"], docker_platform)
        if not safe_to_remove:
            raise RuntimeError(f"SDK refresh incomplete; retained resolver files at {work}")
        locks = {}
        for architecture in ("x86_64", "aarch64"):
            stages = {}
            for stage in inventory["requests"]:
                directory = work / architecture / stage
                packages = {}
                for url in (directory / "urls").read_text().splitlines():
                    package = directory / url.rsplit("/", 1)[1]
                    if package.name in packages:
                        continue
                    packages[package.name] = {"url": url, "sha256": sha256(package)}
                stages[stage] = list(packages.values())
            locks[architecture] = stages
        inventory["locks"] = locks
        # Publish only after every architecture and stage has resolved and verified.
        replace_lock(path, original, (json.dumps(inventory, indent=2) + "\n").encode())
    finally:
        if safe_to_remove:
            try:
                shutil.rmtree(work)
            except OSError as error:
                print(f"Resolver file cleanup failed: {error}; retained {work}", flush=True)


if __name__ == "__main__":
    main()
