# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("--output") + 1]) if "--output" in sys.argv else None
if output is not None and output.suffix == ".sig":
    output.write_text("signature fixture")
else:
    state = pathlib.Path(os.environ["GCC_TEST_STATE"])
    count = int(state.read_text()) if state.exists() else 0
    state.write_text(str(count + 1))
    responses = json.loads(pathlib.Path(os.environ["GCC_TEST_RESPONSES"]).read_text())
    response = responses[min(count, len(responses) - 1)]
    if output is None:
        sys.stdout.buffer.write(response["body"].encode())
    else:
        output.write_bytes(response["body"].encode())
    sys.exit(response["exit"])
