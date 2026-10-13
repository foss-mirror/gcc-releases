#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Acquire pinned source archives and authenticate before extraction."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def download(url: str, archive: Path) -> str:
    """Retry whole transfers, never append retried bodies to a streaming hash."""
    command = [
        "curl",
        "--fail",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-time",
        "600",
        url,
    ]
    retryable = {5, 6, 7, 18, 22, 28, 35, 52, 55, 56, 92}
    for attempt in range(4):
        digest = hashlib.sha256()
        with archive.open("wb") as output, subprocess.Popen(command, stdout=subprocess.PIPE) as transfer:
            stream = transfer.stdout
            if stream is None:
                raise RuntimeError("Download stdout pipe is unavailable")
            try:
                count = 0
                while block := stream.read(1024 * 1024):
                    count += len(block)
                    if count > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("Source archive exceeds 256 MiB")
                    digest.update(block)
                    output.write(block)
            except BaseException:
                # Do not wait for a producer blocked on stdout after a write failure.
                transfer.kill()
                raise
            status = transfer.wait()
        if status == 0:
            return digest.hexdigest()
        if status not in retryable or attempt == 3:
            raise RuntimeError(f"Download failed: {archive.name} (curl exit {status})")
        time.sleep(2**attempt)
    raise AssertionError("Unreachable download attempt")


def acquire(
    item: dict[str, Any],
    destination: Path,
    *,
    key_root: Path = Path("/recipe"),
    discover: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Authenticate a source; discovery may learn its hash from a trusted signature."""
    if not discover and not item.get("sha256"):
        raise ValueError("Build downloads require a pinned SHA-256")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / item["url"].rsplit("/", 1)[1]
    digest = download(item["url"], archive)
    if item.get("sha256") and digest != item["sha256"]:
        raise RuntimeError(f"SHA-256 mismatch: {archive.name}")
    signature = archive.with_name(archive.name + ".sig")
    subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--retry",
            "3",
            "--max-time",
            "60",
            "--max-filesize",
            "131072",
            "--output",
            str(signature),
            item["url"] + ".sig",
        ],
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="gnupg-", dir=destination) as gnupg_home:
        os.chmod(gnupg_home, 0o700)
        common = ["gpg", "--batch", "--no-options", "--homedir", gnupg_home]
        keys = item.get("keys", [item.get("key")])
        subprocess.run([*common, "--import", *[str(key_root / key) for key in keys]], check=True, timeout=30)
        result = subprocess.run(
            [*common, "--status-fd", "1", "--verify", str(signature), str(archive)],
            text=True,
            stdout=subprocess.PIPE,
            check=True,
            timeout=30,
        )
        valid = [line.split() for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        allowed = item.get("signers", [item.get("signer")])
        if len(valid) != 1 or valid[0][2] not in allowed:
            raise RuntimeError(f"Unexpected signer: {valid}")
    return archive, {"sha256": digest, "signer": valid[0][2], "signature_epoch": int(valid[0][4])}


def fetch(item: dict[str, Any], destination: Path) -> Path:
    return acquire(item, destination)[0]


if __name__ == "__main__":
    configuration = json.loads(Path("/recipe/build.json").read_text())
    print(fetch(configuration[sys.argv[1]], Path(sys.argv[2])), flush=True)
