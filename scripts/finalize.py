#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Join an unchanged candidate with all required distribution reports before attestation."""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from distributions import distributions
from hashing import sha256
from release import package_name

ROOT = Path(__file__).resolve().parent.parent


def finalize(package: Path, reports: Path, config: dict[str, Any], architecture: str) -> None:
    release = package_name(config)
    binary = f"{release}-linux-{architecture}.tar.zst"
    expected_files = {binary, f"{release}-source.tar", "provenance.json"}
    if {path.name for path in package.iterdir()} != expected_files | {"SHA256SUMS"}:
        raise ValueError("Candidate contains missing or unexpected files")
    digests = {}
    for line in (package / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if name not in expected_files or name in digests or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError(f"Invalid candidate checksum: {name}")
        path = package / name
        if not path.is_file() or path.is_symlink() or sha256(path) != digest:
            raise ValueError(f"Candidate checksum mismatch: {name}")
        digests[name] = digest
    if digests.keys() != expected_files:
        raise ValueError("Incomplete candidate checksums")
    provenance = json.loads((package / "provenance.json").read_text())
    if (
        provenance["configuration"] != config
        or provenance["release"] != release
        or provenance["architecture"] != architecture
    ):
        raise ValueError("Candidate does not match the resolved release and architecture")

    targets = distributions(ROOT, config)
    if {path.name for path in reports.iterdir()} != {target.report for target in targets}:
        raise ValueError("Missing or unexpected distribution reports")
    accepted = {}
    for target in targets:
        path = reports / target.report
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Report must be a regular file: {target.report}")
        data = path.read_bytes()
        report = json.loads(data)
        environment = report["environment"]
        if (
            report["archive_sha256"] != digests[binary]
            or environment["architecture"] != {"x64": "x86_64", "arm64": "aarch64"}[architecture]
            or environment["distribution"]["ID"] != target.distribution
            or environment["distribution"]["VERSION_ID"] != target.version
            or not report["checks"]
        ):
            raise ValueError(f"Report does not verify this archive on {target.name}")
        accepted[target.report] = data

    # All evidence is checked before changing the candidate. Attestation and
    # the verified upload run only after this command succeeds.
    for name, data in accepted.items():
        (package / name).write_bytes(data)
        digests[name] = sha256(package / name)
    (package / "SHA256SUMS").write_text("".join(f"{digests[name]}  {name}\n" for name in sorted(digests)))
    print(f"Accepted {release} / {architecture} on {len(targets)} distributions", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="downloaded candidate directory; completed in place")
    parser.add_argument("--reports", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=("x64", "arm64"))
    options = parser.parse_args()
    finalize(options.package, options.reports, json.loads(options.config.read_text()), options.architecture)


if __name__ == "__main__":
    main()
