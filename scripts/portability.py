#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify one compiled archive on the baseline and configured native distributions."""

import argparse
import json
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from types import FrameType
from typing import Any

from distributions import distributions
from local_state import exclusive_lock, publish_directory
from native import native_platform, run, stop_owned_container
from recipe import snapshot

ROOT = Path(__file__).resolve().parent.parent


def prepare_verifiers(
    recipe_root: Path,
    recipe: str,
    architecture: str,
    network: str,
    docker_platform: str,
    distribution: str | None = None,
) -> dict[str, str]:
    config = json.loads((recipe_root / "build.json").read_text())
    targets = distributions(recipe_root, config)
    if distribution is not None:
        targets = [target for target in targets if target.name == distribution]
        if not targets:
            raise ValueError(f"Unknown verification distribution: {distribution}")
    images = {}
    for target in targets:
        name = target.name
        image = f"gcc-releases-verifier:{recipe[:16]}-{architecture}-{name}"
        dockerfile = "Dockerfile" if target.baseline else "Dockerfile.portability"
        options = (
            ["--target", "verifier", "--build-arg", f"ALMALINUX_VERSION={target.version}"]
            if target.baseline
            else [
                "--build-arg",
                f"DISTRIBUTION={target.distribution}",
                "--build-arg",
                f"DISTRIBUTION_VERSION={target.version}",
            ]
        )
        print(f"Prepare {name} verifier ({architecture})", flush=True)
        run(
            "docker",
            "build",
            "--platform",
            docker_platform,
            "--file",
            recipe_root / dockerfile,
            "--tag",
            image,
            "--network",
            "default" if network == "bridge" else "host",
            "--build-arg",
            f"BASE_IMAGE={target.image}",
            *options,
            recipe_root,
        )
        images[target.report] = image
    return images


def verify_archive(
    archive: Path,
    reports: Path,
    recipe_root: Path,
    images: dict[str, str],
    container_name: str,
    docker_platform: str,
) -> None:
    for report, image in images.items():
        print(f"Verify {archive.name} with {image}", flush=True)
        # Each container extracts the same compressed archive into a fresh path.
        # No build tree or original installation is visible, and tests are offline.
        run(
            "docker",
            "run",
            "--platform",
            docker_platform,
            "--rm",
            "--init",
            "--network=none",
            "--name",
            container_name,
            "--mount",
            f"type=bind,src={recipe_root},dst=/recipe,readonly",
            "--mount",
            f"type=bind,src={archive},dst=/input.tar.zst,readonly",
            "--mount",
            f"type=bind,src={reports},dst=/results",
            image,
            "/opt/relocated-toolchain",
            "--archive",
            "/input.tar.zst",
            "--output",
            f"/results/{report}",
        )


def interrupted(signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"Verification interrupted by signal {signum}")


def verify(archive: Path, output: Path, config: dict[str, Any], network: str, distribution: str | None = None) -> None:
    architecture, docker_platform = native_platform()
    output.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(output.with_name("." + output.name + ".lock")):
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"output already exists: {output}; choose a new --output directory")
        work = Path(tempfile.mkdtemp(prefix="gcc-portability-", dir="/tmp"))
        staging = Path(tempfile.mkdtemp(prefix=".gcc-portability-", dir=output.parent))
        try:
            recipe_root = work / "recipe"
            recipe = snapshot(ROOT, recipe_root, config)
            images = prepare_verifiers(recipe_root, recipe, architecture, network, docker_platform, distribution)
            verify_archive(archive, staging, recipe_root, images, work.name, docker_platform)
            publish_directory(staging, output)
            print(f"Verified on {distribution or 'all distributions'}: {output}", flush=True)
        finally:
            try:
                stop_owned_container(work.name)
            except (OSError, subprocess.CalledProcessError) as error:
                print(f"Container cleanup failed: {error}; retained {work} and {staging}", flush=True)
            else:
                for path in (work, staging):
                    try:
                        if path.exists():
                            shutil.rmtree(path)
                    except OSError as error:
                        print(f"Cleanup failed: {error}; retained {path}", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="locally built, trusted .tar.zst package")
    parser.add_argument("--output", required=True, type=Path, help="new directory for verification reports")
    parser.add_argument("--config", type=Path, default=ROOT / "build.json", help="baseline environment configuration")
    parser.add_argument("--network", choices=("bridge", "host"), default="bridge")
    parser.add_argument("--distribution", help="one configured distribution, such as ubuntu-26.04; default: all")
    options = parser.parse_args()
    if not options.archive.is_file():
        parser.error(f"archive does not exist: {options.archive}")
    verify(
        options.archive.resolve(),
        options.output.resolve(),
        json.loads(options.config.read_text()),
        options.network,
        options.distribution,
    )


if __name__ == "__main__":
    main()
