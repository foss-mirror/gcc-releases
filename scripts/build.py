#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build, relocate, verify, and package with native Docker execution."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import FrameType
from typing import Any

from hashing import sha256
from local_state import exclusive_lock, publish_directory
from native import native_platform, run, stop_owned_container
from portability import prepare_verifiers, verify_archive
from recipe import snapshot
from release import package_name

ROOT = Path(__file__).resolve().parent.parent


def interrupted(signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"Build interrupted by signal {signum}")


def prepare_sources(work: Path, recipe_root: Path, metadata: dict[str, Any]) -> Path:
    source = work / "corresponding-source"
    source.mkdir()
    shutil.copytree(work / "downloads", source / "downloads")
    shutil.copytree(recipe_root, source / "recipe")
    # SDK locks contain both architectures. Keep this bundle identical across
    # builders; architecture-specific installed-package reports remain CI evidence.
    common = {key: metadata[key] for key in ("release", "recipe_sha256", "configuration")}
    (source / "provenance.json").write_text(json.dumps(common, indent=2) + "\n")
    return source


def cleanup(
    work: Path, staging: Path, container_name: str, image: str, *, keep_work: bool, docker_platform: str
) -> None:
    """Preserve the build error and retain storage if container shutdown fails."""
    try:
        # Docker containers outlive a killed client unless explicitly stopped.
        # Stop this invocation before deleting any files it could still use.
        stop_owned_container(container_name)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Container cleanup failed: {error}; retained {work} and {staging}", flush=True)
        return
    try:
        # Docker writes root-owned files. Remove only this invocation's private
        # workspace, through Docker, so callers need no sudo on the host.
        if keep_work:
            print(f"Retained workspace: {work}", flush=True)
        else:
            cleanup = subprocess.run(
                [
                    "docker",
                    "run",
                    "--platform",
                    docker_platform,
                    "--rm",
                    "--network=none",
                    "--entrypoint",
                    "sh",
                    "--mount",
                    f"type=bind,src={work},dst=/cleanup",
                    image,
                    "-c",
                    "find /cleanup -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                ]
            )
            if cleanup.returncode == 0:
                work.rmdir()
            else:
                print(f"Cleanup failed; workspace retained at {work}", flush=True)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Workspace cleanup failed: {error}; retained {work}", flush=True)
    try:
        if staging.exists():
            shutil.rmtree(staging)
    except OSError as error:
        print(f"Artifact cleanup failed: {error}; retained {staging}", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--output", type=Path, default=Path("/tmp/gcc-builds-output"))
    parser.add_argument("--keep-work", action="store_true", help="retain /tmp workspace for diagnosis")
    parser.add_argument(
        "--defer-verification", action="store_true", help="write unverified candidates for separate distribution jobs"
    )
    parser.add_argument(
        "--network",
        choices=("bridge", "host"),
        default="bridge",
        help="network for acquisition; verification is always offline",
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "build.json", help="resolved release configuration from discovery"
    )
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error("--jobs must be positive")
    config = json.loads(options.config.read_text())
    if date.today().isoformat() > config["almalinux"]["security_support_end"]:
        parser.error("Builder security support has ended; review and advance the AlmaLinux baseline")
    if not 1 <= config["zstd_level"] <= 19:
        parser.error("zstd_level must be between 1 and 19")
    try:
        architecture, docker_platform = native_platform()
    except ValueError as error:
        parser.error(str(error))
    release = package_name(config)
    output = options.output.resolve() / f"{release}-linux-{architecture}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(output.with_name("." + output.name + ".lock")):
        if output.exists() or output.is_symlink():
            parser.error(f"output already exists: {output}; choose a new --output directory")
        build(
            config,
            BuildOptions(options.jobs, options.network, options.keep_work, options.defer_verification),
            architecture,
            docker_platform,
            output,
        )


@dataclass(frozen=True)
class BuildOptions:
    jobs: int
    network: str
    keep_work: bool
    defer_verification: bool = False


def prepare_builder(
    config: dict[str, Any], recipe_root: Path, recipe: str, architecture: str, network: str, docker_platform: str
) -> str:
    builder = f"gcc-builds-builder:{recipe[:16]}-{architecture}"
    run(
        "docker",
        "build",
        "--platform",
        docker_platform,
        "--target",
        "builder",
        "--tag",
        builder,
        "--network",
        "default" if network == "bridge" else "host",
        "--build-arg",
        f"BASE_IMAGE={config['build_image']}",
        "--build-arg",
        f"ALMALINUX_VERSION={config['almalinux']['version']}",
        recipe_root,
    )
    return builder


def compile_toolchain(
    work: Path, recipe_root: Path, builder: str, container_name: str, options: BuildOptions, docker_platform: str
) -> None:
    run(
        "docker",
        "run",
        "--platform",
        docker_platform,
        "--rm",
        "--init",
        "--name",
        container_name,
        "--cpus",
        options.jobs,
        "--network",
        options.network,
        "--mount",
        f"type=bind,src={recipe_root},dst=/recipe,readonly",
        "--mount",
        f"type=bind,src={work},dst=/work",
        "--env",
        f"JOBS={options.jobs}",
        builder,
    )


def install_metadata(
    work: Path, metadata: dict[str, Any], builder: str, container_name: str, docker_platform: str
) -> None:
    (work / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
    # Installation files belong to the container's user. Write through that
    # same boundary rather than assuming host/root UID mappings agree.
    run(
        "docker",
        "run",
        "--platform",
        docker_platform,
        "--rm",
        "--network=none",
        "--name",
        container_name,
        "--entrypoint",
        "cp",
        "--mount",
        f"type=bind,src={work},dst=/input",
        builder,
        "/input/provenance.json",
        "/input/toolchain/share/provenance.json",
    )


def pack_archive(
    work: Path,
    content: Path,
    destination: Path,
    builder: str,
    container_name: str,
    config: dict[str, Any],
    docker_platform: str,
) -> str:
    with destination.open("wb") as stream:
        command = list(
            map(
                str,
                [
                    "docker",
                    "run",
                    "--platform",
                    docker_platform,
                    "--rm",
                    "--network=none",
                    "--name",
                    container_name,
                    "--entrypoint",
                    "bash",
                    "--mount",
                    f"type=bind,src={work},dst=/input,readonly",
                    "--env",
                    f"SOURCE_DATE_EPOCH={config['source_date_epoch']}",
                    "--env",
                    f"ZSTD_LEVEL={config['zstd_level']}",
                    builder,
                    "-euo",
                    "pipefail",
                    "-c",
                    'tar --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 '
                    '--numeric-owner -cf - -C /input "$1"'
                    + (' | zstd -T1 -"$ZSTD_LEVEL" -c' if destination.name.endswith(".zst") else ""),
                    "archive",
                    content.name,
                ],
            )
        )
        digest = hashlib.sha256()
        with subprocess.Popen(command, stdout=subprocess.PIPE) as packing:
            packed_stream = packing.stdout
            if packed_stream is None:
                raise RuntimeError("Packaging stdout pipe is unavailable")
            try:
                while block := packed_stream.read(1024 * 1024):
                    digest.update(block)
                    stream.write(block)
            except BaseException:
                packing.kill()
                raise
            if packing.wait() != 0:
                raise RuntimeError(f"Packaging failed: {destination.name}")
        return digest.hexdigest()


def build(config: dict[str, Any], options: BuildOptions, architecture: str, docker_platform: str, output: Path) -> None:
    release = package_name(config)
    work = Path(tempfile.mkdtemp(prefix="gcc-builds-", dir="/tmp"))
    container_name = work.name
    staging = Path(tempfile.mkdtemp(prefix=".gcc-builds-", dir=output.parent))
    print(f"Workspace: {work}", flush=True)
    try:
        recipe_root = work / "recipe"
        recipe = snapshot(ROOT, recipe_root, config)
        builder = prepare_builder(config, recipe_root, recipe, architecture, options.network, docker_platform)
        verifiers = (
            {}
            if options.defer_verification
            else prepare_verifiers(recipe_root, recipe, architecture, options.network, docker_platform)
        )
        compile_toolchain(work, recipe_root, builder, container_name, options, docker_platform)
        metadata = {
            "release": release,
            "architecture": architecture,
            "recipe_sha256": recipe,
            "configuration": config,
            "packages": (work / "packages.tsv").read_text().splitlines(),
        }
        install_metadata(work, metadata, builder, container_name, docker_platform)
        # Include every dependency archive and the exact build recipe as
        # corresponding source; do not rely on a future download remaining online.
        source = prepare_sources(work, recipe_root, metadata)
        archive_digests = {}
        for content, filename in (
            (work / "toolchain", f"{release}-linux-{architecture}.tar.zst"),
            (source, f"{release}-source.tar"),
        ):
            archive_digests[filename] = pack_archive(
                work, content, staging / filename, builder, container_name, config, docker_platform
            )
        if not options.defer_verification:
            verify_archive(
                staging / f"{release}-linux-{architecture}.tar.zst",
                staging,
                recipe_root,
                verifiers,
                container_name,
                docker_platform,
            )
        (staging / "provenance.json").write_text(json.dumps(metadata, indent=2) + "\n")
        files = sorted(path for path in staging.iterdir() if path.is_file())
        (staging / "SHA256SUMS").write_text(
            "".join(
                f"{archive_digests[path.name] if path.name in archive_digests else sha256(path)}  {path.name}\n"
                for path in files
            )
        )
        sizes = {path.name: path.stat().st_size for path in staging.iterdir()}
        print(json.dumps({"artifacts": sizes}, indent=2), flush=True)
        publish_directory(staging, output)
        state = "Unverified candidate artifacts" if options.defer_verification else "Verified artifacts"
        print(f"{state}: {output}", flush=True)
    finally:
        cleanup(
            work,
            staging,
            container_name,
            config["build_image"],
            keep_work=options.keep_work,
            docker_platform=docker_platform,
        )


if __name__ == "__main__":
    main()
