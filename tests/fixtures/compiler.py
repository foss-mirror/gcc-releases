# SPDX-License-Identifier: GPL-3.0-or-later
import json
import pathlib
import sys

prefix = pathlib.Path(__file__).parent.parent
config = json.loads((prefix / "share/build.json").read_text())
fixture = json.loads((prefix / "share/fixture.json").read_text())
args = sys.argv[1:]
if args == ["-dumpfullversion"]:
    print(config["gcc"]["version"])
elif args == ["--version"]:
    print(config["binutils"]["version"])
elif args[0].startswith("-print-prog-name="):
    print(prefix / "bin" / args[0].split("=")[1])
elif args[0].startswith("-print-file-name="):
    print(prefix / "lib64" / args[0].split("=")[1])
elif args[0] == "--version-info":
    print(fixture.get("version", "GLIBC_2.28"))
elif args[0] == "-d":
    print(fixture.get("dynamic", "(NEEDED) Shared library: [libstdc++.so.6]"))
else:
    raise RuntimeError("Unexpected fixture tool arguments: " + repr(args))
