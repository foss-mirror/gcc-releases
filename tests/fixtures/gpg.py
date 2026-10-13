# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys

if "--verify" in sys.argv:
    print("[GNUPG:] VALIDSIG " + os.environ["GCC_TEST_SIGNER"] + " 2026-01-01 123 0")
    sys.exit(int(os.environ.get("GCC_TEST_SIGNATURE_EXIT", "0")))
