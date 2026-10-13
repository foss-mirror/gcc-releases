# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib

prefix = pathlib.Path(__file__).parent.parent
print(json.loads((prefix / "share/fixture.json").read_text()).get("loaded", "libstdc++.so.6 => not found"))
