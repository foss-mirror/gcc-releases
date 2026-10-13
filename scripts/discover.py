#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Discover stable GCC tags and prepare authenticated, unpublished release recipes."""

import argparse
import copy
import json
import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from distributions import distributions
from fetch import acquire
from github_api import releases
from release import package_name, version_tuple

ROOT = Path(__file__).resolve().parent.parent


def stable_tags(repository: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "git",
            "ls-remote",
            "--tags",
            repository,
            "refs/tags/releases/gcc-*",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=120,
    )
    tags = {}
    for line in result.stdout.splitlines():
        commit, tag = line.split()
        match = re.fullmatch(r"refs/tags/releases/gcc-(\d+\.\d+\.\d+)(\^\{\})?", tag)
        if match:
            version_tuple(match[1])
            if match[2] or match[1] not in tags:
                tags[match[1]] = commit
    return tags


def select_versions(tags: dict[str, str], policy: dict[str, Any], requested: str | None) -> list[str]:
    if requested:
        versions = requested.split(",")
    else:
        latest = {}
        for version in sorted(tags, key=version_tuple):
            major = version_tuple(version)[0]
            if major >= policy["minimum_major"]:
                latest[major] = version
        versions = list(latest.values())
        if not versions:
            raise RuntimeError("No stable tags at or above the configured minimum major")
    for version in versions:
        if version_tuple(version)[0] < policy["minimum_major"] or version not in tags:
            raise ValueError(f"Unavailable or out-of-policy GCC release: {version}")
    return sorted(set(versions), key=version_tuple)


def resolve(template: dict[str, Any], version: str, commit: str) -> dict[str, Any]:
    config = copy.deepcopy(template)
    if version == template["gcc"]["version"]:
        if commit != template["gcc"]["commit"]:
            raise RuntimeError(f"Pinned GCC tag moved: {version}")
        return config
    signers = template["discovery"]["signers"]
    item = {
        "url": f"https://gcc.gnu.org/pub/gcc/releases/gcc-{version}/gcc-{version}.tar.xz",
        "keys": sorted(set(signers.values())),
        "signers": list(signers),
    }
    with tempfile.TemporaryDirectory(prefix="gcc-discovery-", dir="/tmp") as directory:
        _, evidence = acquire(item, Path(directory), key_root=ROOT, discover=True)
    config["gcc"] = {
        "version": version,
        "tag": f"releases/gcc-{version}",
        "commit": commit,
        "url": item["url"],
        "sha256": evidence["sha256"],
        "signer": evidence["signer"],
        "key": signers[evidence["signer"]],
    }
    config["source_date_epoch"] = evidence["signature_epoch"]
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", help="comma-separated stable versions; default: latest point release per major")
    parser.add_argument("--output", type=Path, help="write resolved recipes and Actions matrices")
    parser.add_argument("--repository", help="skip published releases and report drafts requiring recovery")
    parser.add_argument("--configured", action="store_true", help="build only build.json's pinned version")
    args = parser.parse_args()
    if args.configured and args.versions:
        parser.error("--configured and --versions are mutually exclusive")
    config = json.loads((ROOT / "build.json").read_text())
    mirror = stable_tags("https://github.com/gcc-mirror/gcc.git")
    requested = config["gcc"]["version"] if args.configured else args.versions
    versions = select_versions(mirror, config["discovery"], requested)
    existing = list(releases(args.repository)) if args.repository else []
    published = {item["tag_name"] for item in existing if not item["draft"] and not item["prerelease"]}
    drafts = {item["tag_name"] for item in existing if item["draft"]}
    pending = [v for v in versions if f"releases/gcc-{v}" not in published | drafts]
    recovery = [v for v in versions if f"releases/gcc-{v}" in drafts]
    summary: dict[str, Any] = {
        "selected": versions,
        "pending": pending,
        "already_published": [v for v in versions if f"releases/gcc-{v}" in published],
        "needs_recovery": recovery,
    }
    if recovery:
        summary["recovery_instruction"] = (
            "Rerun the original publication job with its original artifacts; "
            "new builds cannot replace these drafts. See README.md for local recovery."
        )
    if args.output:
        # New executions require a supported builder. Existing binaries do not expire.
        if pending and date.today().isoformat() > config["almalinux"]["security_support_end"]:
            raise RuntimeError("Builder security support has ended; review and advance the AlmaLinux baseline")
        upstream = stable_tags("https://gcc.gnu.org/git/gcc.git") if pending else {}
        args.output.mkdir(parents=True, exist_ok=False)
        builds, publication = [], []
        for version in pending:
            if mirror[version] != upstream.get(version):
                raise RuntimeError(f"Canonical and mirror GCC tags disagree: {version}")
            resolved = resolve(config, version, mirror[version])
            release = package_name(resolved)
            (args.output / (release + ".json")).write_text(json.dumps(resolved, indent=2) + "\n")
            publication.append({"release": release})
            for arch, runner in (("x64", "ubuntu-24.04"), ("arm64", "ubuntu-24.04-arm")):
                builds.append({"release": release, "arch": arch, "runner": runner})
        targets = distributions(ROOT, config)
        plan = {
            **summary,
            "build": {"include": builds},
            "verify": {"include": [{**build, "distribution": target.name} for build in builds for target in targets]},
            "publish": {"include": publication},
        }
        (args.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                for key, value in (
                    ("has_releases", bool(pending)),
                    ("build", plan["build"]),
                    ("verify", plan["verify"]),
                    ("publish", plan["publish"]),
                ):
                    output.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
