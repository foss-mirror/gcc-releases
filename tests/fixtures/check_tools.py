# SPDX-License-Identifier: GPL-3.0-or-later
"""Serve linter archives and record network tool calls without using the network."""

import json
import os
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
with Path(os.environ["GCC_TEST_LINTER_LOG"]).open("a") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\n")
if name == "curl":
    status = int(os.environ.get("GCC_TEST_LINTER_CURL_EXIT", "0"))
    if status == 0:
        target = Path(sys.argv[sys.argv.index("--output") + 1])
        shutil.copyfile(Path(os.environ["GCC_TEST_LINTER_UPSTREAM"]) / target.name, target)
else:
    status = int(os.environ.get("GCC_TEST_LINTER_GH_EXIT", "0"))
sys.exit(status)
