#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Publish one complete verified Actions run without overwriting releases."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from github_api import get, releases
from hashing import sha256
from release import package_name, release_tag, version_tuple


def require_absent(repository: str, endpoint: str) -> None:
    if get(repository, endpoint, absent_ok=True) is not None:
        raise RuntimeError(f"Refusing to overwrite existing {endpoint}")


def find_release(repository: str, tag: str) -> dict[str, Any] | None:
    # The by-tag endpoint excludes drafts, so enumerate authenticated releases.
    matches = [item for item in releases(repository) if item["tag_name"] == tag]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple releases have tag {tag}")
    return matches[0] if matches else None


def verify_build_tag(repository: str, tag: str, *, required: bool) -> None:
    """Check the actual tag target, including annotated tags, before recovery."""
    reference = get(repository, "git/ref/tags/" + quote(tag, safe=""), absent_ok=True)
    if reference is None:
        if required:
            raise RuntimeError(f"Published release has no tag: {tag}")
        return
    target = reference["object"]
    seen: set[str] = set()
    while target["type"] == "tag":
        if target["sha"] in seen or len(seen) >= 16:
            raise RuntimeError(f"Invalid annotated tag chain: {tag}")
        seen.add(target["sha"])
        target = get(repository, "git/tags/" + target["sha"])["object"]
    if target["type"] != "commit" or target["sha"] != os.environ["GITHUB_SHA"]:
        raise RuntimeError(f"Release tag differs from the exact build commit: {tag}")


def report(message: str) -> None:
    print(message, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(message + "\n\n")


@dataclass(frozen=True)
class Asset:
    """Expected identity of a file in this invocation's private staging directory."""

    path: Path
    size: int
    sha256: str

    @classmethod
    def inspect(cls, path: Path) -> "Asset":
        return cls(path, path.stat().st_size, sha256(path))


@dataclass(frozen=True)
class NativeBuild:
    architecture: str
    root: Path
    bundle: Path


def locate_builds(inputs: Path, release: str) -> dict[str, NativeBuild | None]:
    """Distinguish absent uploads from incomplete or ambiguous uploads."""
    builds: dict[str, NativeBuild | None] = {}
    for arch in ("x64", "arm64"):
        roots = list(inputs.rglob(f"{release}-linux-{arch}"))
        bundles = list(inputs.rglob(f"attestation-{release}-{arch}.jsonl"))
        if not roots and not bundles:
            builds[arch] = None
        elif len(roots) != 1 or not roots[0].is_dir() or len(bundles) != 1 or not bundles[0].is_file():
            raise RuntimeError(f"Incomplete or ambiguous uploaded {release} / {arch} build or attestation")
        else:
            builds[arch] = NativeBuild(arch, roots[0], bundles[0])
    return builds


def copy_asset(source: Path, destination: Path) -> Asset:
    """Hash the bytes being staged, rather than trusting an input manifest."""
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            outgoing.write(block)
            digest.update(block)
            size += len(block)
    return Asset(destination, size, digest.hexdigest())


def verify_attestation(asset: Asset, bundle: Path, repository: str) -> None:
    subprocess.run(
        [
            "gh",
            "attestation",
            "verify",
            str(asset.path),
            "--bundle",
            str(bundle),
            "--repo",
            repository,
            "--signer-workflow",
            repository + "/.github/workflows/build.yml",
            "--source-ref",
            "refs/heads/main",
            "--source-digest",
            os.environ["GITHUB_SHA"],
            "--deny-self-hosted-runners",
        ],
        check=True,
    )


def validate_build(
    build: NativeBuild, config: dict[str, Any], directory: Path, repository: str
) -> tuple[dict[str, Asset], str]:
    directory.mkdir()
    assets = {}
    for line in (build.root / "SHA256SUMS").read_text().splitlines():
        expected, filename = line.split("  ", 1)
        if Path(filename).name != filename or filename in assets:
            raise RuntimeError(f"Invalid {build.architecture} checksum: {filename}")
        asset = copy_asset(build.root / filename, directory / filename)
        if asset.sha256 != expected:
            raise RuntimeError(f"Invalid {build.architecture} checksum: {filename}")
        assets[filename] = asset
    if assets.keys() != {path.name for path in build.root.iterdir() if path.name != "SHA256SUMS"}:
        raise RuntimeError(f"Incomplete {build.architecture} checksum manifest")
    provenance = json.loads(assets["provenance.json"].path.read_text())
    release = package_name(config)
    if (
        provenance["configuration"] != config
        or provenance["release"] != release
        or provenance["architecture"] != build.architecture
    ):
        raise RuntimeError(f"Unexpected {build.architecture} provenance")
    bundle = copy_asset(build.bundle, directory / build.bundle.name)
    if not bundle.size:
        raise RuntimeError(f"Missing {build.architecture} attestation")
    for filename in (f"{release}-linux-{build.architecture}.tar.zst", release + "-source.tar"):
        verify_attestation(assets[filename], bundle.path, repository)
    assets[bundle.path.name] = bundle
    return assets, provenance["recipe_sha256"]


def prepare_assets(
    inputs: Path,
    config: dict[str, Any],
    staging: Path,
    repository: str,
    builds: dict[str, NativeBuild | None] | None = None,
) -> list[Asset]:
    """Validate private copies and assemble the exact public asset set."""
    release = package_name(config)
    builds = locate_builds(inputs, release) if builds is None else builds
    recipes = set()
    files = []
    source_asset = None
    for arch in ("x64", "arm64"):
        build = builds[arch]
        if build is None:
            raise RuntimeError(f"Expected exactly one {arch} build")
        assets, recipe = validate_build(build, config, staging / arch, repository)
        recipes.add(recipe)
        binary = assets[f"{release}-linux-{arch}.tar.zst"]
        bundle = assets[build.bundle.name]
        files.append(binary)
        files.append(copy_asset(bundle.path, binary.path.with_name(binary.path.name + ".jsonl")))
        source = assets[release + "-source.tar"]
        if source_asset is None:
            source_asset = source
            files.append(source)
            files.append(copy_asset(bundle.path, source.path.with_name(source.path.name + ".jsonl")))
        else:
            if source_asset.sha256 != source.sha256:
                raise RuntimeError("Architectures have different corresponding-source archives")
            source.path.unlink()
    if len(recipes) != 1:
        raise RuntimeError("Architectures were built with different recipes")
    for asset in list(files):
        if asset.path.name.endswith((".tar", ".tar.zst")):
            sidecar = asset.path.with_name(asset.path.name + ".sha256")
            sidecar.write_text(f"{asset.sha256}  {asset.path.name}\n")
            files.append(Asset.inspect(sidecar))
    return sorted(files, key=lambda asset: asset.path.name)


def missing_assets(repository: str, release_id: int, files: list[Asset]) -> list[Asset]:
    expected = {asset.path.name: asset for asset in files}
    seen = set()
    page = 1
    while True:
        assets = get(repository, f"releases/{release_id}/assets?per_page=100&page={page}")
        for asset in assets:
            name = asset["name"]
            asset_file = expected.get(name)
            if (
                asset_file is None
                or name in seen
                or asset["state"] != "uploaded"
                or asset["size"] != asset_file.size
                or asset.get("digest") != "sha256:" + asset_file.sha256
            ):
                raise RuntimeError(f"Unexpected or mismatched release asset: {name}")
            seen.add(name)
        if len(assets) < 100:
            return [asset for asset in files if asset.path.name not in seen]
        page += 1


def upload_assets(repository: str, release_id: int, files: list[Asset]) -> None:
    for asset in missing_assets(repository, release_id, files):
        print(f"Uploading {asset.path.name}", flush=True)
        try:
            subprocess.run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets"
                    + "?name="
                    + quote(asset.path.name, safe=""),
                    "--header",
                    "Content-Type: application/octet-stream",
                    "--input",
                    str(asset.path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            # A response can be lost after the server accepts an upload. Never
            # clobber it: accept only the exact bytes authenticated above.
            if asset in missing_assets(repository, release_id, files):
                raise
            print(f"Verified accepted upload after client failure: {asset.path.name}", flush=True)
    if missing_assets(repository, release_id, files):
        raise RuntimeError("Release upload is incomplete; leaving draft unpublished")


def check_existing_release(repository: str, tag: str, *, resume: bool) -> dict[str, Any] | None:
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Publication requires main")
    existing = find_release(repository, tag)
    if existing is not None and (
        not resume or existing["prerelease"] or existing["target_commitish"] != os.environ["GITHUB_SHA"]
    ):
        raise RuntimeError("Refusing to overwrite release; recovery requires the exact build commit")
    if existing is None:
        require_absent(repository, "git/ref/tags/" + quote(tag, safe=""))
    else:
        verify_build_tag(repository, tag, required=not existing["draft"])
    return existing


def release_notes(config: dict[str, Any], repository: str) -> str:
    release = package_name(config)
    return (
        f"GCC {config['gcc']['version']} with Binutils {config['binutils']['version']} "
        "for **Linux x64 and ARM64**, including C/C++ runtimes, LTO, sanitizers, and coverage tools.\n\n"
        f"**Requirements:** glibc {config['glibc_minimum']}+ and zlib. Compiling also requires system libc "
        "development headers and startup objects. Install into a path without spaces; "
        "parallel LTO requires GNU Make.\n\n"
        f"- **Binaries:** `{release}-linux-x64.tar.zst` or `{release}-linux-arm64.tar.zst`.\n"
        f"- **Sources:** `{release}-source.tar`, containing GCC, Binutils, dependency sources, "
        "and the build recipe.\n\n"
        "Each archive has a `.sha256` checksum and a `.jsonl` GitHub attestation. "
        f"[Verification details](https://github.com/{repository}/blob/main/README.md#actions-and-publication).\n\n"
        "GitHub's automatic **Source code (zip)** and **Source code (tar.gz)** downloads "
        "contain this packaging repository only.\n"
    )


def publish_assets(
    repository: str, config: dict[str, Any], files: list[Asset], existing: dict[str, Any] | None
) -> None:
    release = package_name(config)
    tag = release_tag(config)
    # Backfills must not displace a newer version's Latest designation.
    published = []
    for item in releases(repository):
        if item["draft"] or item["prerelease"]:
            continue
        if item["tag_name"].startswith("releases/gcc-"):
            published.append(version_tuple(item["tag_name"].removeprefix("releases/gcc-")))
    current = version_tuple(config["gcc"]["version"])
    latest = "true" if current >= max(published, default=(0, 0, 0)) else "false"
    if existing is None:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repository}/releases",
                "--input",
                "-",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            input=json.dumps(
                {
                    "tag_name": tag,
                    "target_commitish": os.environ["GITHUB_SHA"],
                    "draft": True,
                    "name": release,
                    "body": release_notes(config, repository),
                }
            ),
        )
        # The creation response owns the draft identity; release listings may lag.
        existing = json.loads(result.stdout)
    if (
        existing["tag_name"] != tag
        or not existing["draft"]
        or existing["prerelease"]
        or existing["target_commitish"] != os.environ["GITHUB_SHA"]
    ):
        raise RuntimeError("Expected a draft from the exact build commit")
    upload_assets(repository, existing["id"], files)
    subprocess.run(
        ["gh", "api", "--method", "PATCH", f"repos/{repository}/releases/{existing['id']}", "--input", "-"],
        input=json.dumps({"draft": False, "make_latest": latest}),
        text=True,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    report(f"Published: {release}")


def publish(
    inputs: Path,
    config: dict[str, Any],
    *,
    resume: bool = False,
    builds: dict[str, NativeBuild | None] | None = None,
) -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    tag = release_tag(config)
    existing = check_existing_release(repository, tag, resume=resume)
    with tempfile.TemporaryDirectory(prefix="gcc-release-", dir="/tmp") as temporary:
        staging = Path(temporary)
        files = prepare_assets(inputs.resolve(), config, staging, repository, builds)
        if existing is not None and not existing["draft"]:
            if missing_assets(repository, existing["id"], files):
                raise RuntimeError(f"Published release has missing assets: {tag}")
            report(f"Already published and verified: {package_name(config)}")
            return
        publish_assets(repository, config, files, existing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="recover a matching draft or verify and skip an already-published release",
    )
    parser.add_argument(
        "--available", action="store_true", help="publish only plan entries with both uploaded native builds"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, help="publish one resolved release")
    source.add_argument("--plan", type=Path, help="publish a resolved plan in ascending version order")
    args = parser.parse_args()
    if args.available and not args.plan:
        parser.error("--available requires --plan")
    if args.config:
        configurations = [json.loads(args.config.read_text())]
    else:
        plan = json.loads((args.plan / "plan.json").read_text())
        configurations = []
        for entry in plan["publish"]["include"]:
            name = entry["release"]
            version_tuple(name.removeprefix("gcc-"))
            config = json.loads((args.plan / (name + ".json")).read_text())
            if package_name(config) != name:
                raise RuntimeError("Release plan and recipe disagree")
            configurations.append(config)
    names = [release_tag(config) for config in configurations]
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate releases in publication plan")
    if args.config:
        publish(args.inputs, configurations[0], resume=args.resume)
        return
    # An explicit loop guarantees order; a one-at-a-time matrix does not.
    # Publish oldest first so the newest version is published last.
    failures = []
    for config in sorted(configurations, key=lambda item: version_tuple(item["gcc"]["version"])):
        release = package_name(config)
        try:
            builds = locate_builds(args.inputs, release) if args.available else None
            if builds is not None:
                missing = [arch for arch, build in builds.items() if build is None]
                if missing:
                    report(f"Skipped {release}: no completed upload for {', '.join(missing)}")
                    continue
            publish(args.inputs, config, resume=args.resume, builds=builds)
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            failure = f"{release}: {error}"
            failures.append(failure)
            report("Failed " + failure)
    if failures:
        raise RuntimeError("Publication failed for " + "; ".join(failures))


if __name__ == "__main__":
    main()
