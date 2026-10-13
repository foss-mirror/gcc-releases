# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure stable release naming and version rules."""

import re
from typing import Any

VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+\.[0-9]+")


def version_tuple(version: str) -> tuple[int, ...]:
    if not VERSION.fullmatch(version):
        raise ValueError(f"Not a stable GCC version: {version}")
    return tuple(map(int, version.split(".")))


def release_tag(config: dict[str, Any]) -> str:
    version = config["gcc"]["version"]
    version_tuple(version)
    tag = f"releases/gcc-{version}"
    if config["gcc"]["tag"] != tag:
        raise ValueError("Release tag must match the upstream stable GCC tag")
    return tag


def package_name(config: dict[str, Any]) -> str:
    return release_tag(config).removeprefix("releases/")
