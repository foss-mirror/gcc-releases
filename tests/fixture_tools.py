# SPDX-License-Identifier: GPL-3.0-or-later
"""Install checked process fixtures under the names exercised by the tests."""

import sys
from pathlib import Path


def install_tool(directory: Path, name: str, fixture: str) -> Path:
    path = directory / name
    source = Path(__file__).resolve().parent / "fixtures" / fixture
    path.write_text(f"#!{sys.executable}\n" + source.read_text())
    path.chmod(0o755)
    return path
