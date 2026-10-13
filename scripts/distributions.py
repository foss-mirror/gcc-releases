# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured native distributions shared by discovery, verification, and finalization."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Distribution:
    distribution: str
    version: str
    image: str
    baseline: bool = False

    @property
    def name(self) -> str:
        return f"{self.distribution}-{self.version}"

    @property
    def report(self) -> str:
        return "verification.json" if self.baseline else f"verification-{self.name}.json"


def distributions(recipe_root: Path, config: dict[str, Any]) -> list[Distribution]:
    consumers = json.loads((recipe_root / "portability.json").read_text())
    return [
        Distribution("almalinux", config["almalinux"]["version"], config["build_image"], baseline=True),
        *(Distribution(**consumer) for consumer in consumers),
    ]
