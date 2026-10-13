# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import pathlib
import shutil
import signal
import sys

args = sys.argv[1:]
with open(os.environ["GCC_TEST_DOCKER_CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
failure = os.environ.get("GCC_TEST_BUILD_FAIL")
mounts = {}
for index, value in enumerate(args):
    if value == "--mount":
        fields = dict(part.split("=", 1) for part in args[index + 1].split(",") if "=" in part)
        mounts[fields["dst"]] = pathlib.Path(fields["src"])
if args[0] == "info":
    print(os.environ["GCC_TEST_DOCKER_ARCH"])
elif args[0] == "build":
    if failure == "build":
        sys.exit(1)
elif args[:2] == ["container", "ls"]:
    pass
elif "/work" in mounts:
    work = mounts["/work"]
    (work / "downloads").mkdir()
    (work / "downloads/source.tar.xz").write_bytes(b"original archive")
    (work / "toolchain/share").mkdir(parents=True)
    (work / "packages.tsv").write_text("fixture-sdk\n")
    if failure == "compile":
        sys.exit(1)
    if failure == "interrupt":
        os.kill(os.getppid(), signal.SIGTERM)
elif "--archive" in args:
    report = pathlib.Path(args[args.index("--output") + 1]).name
    if failure == "verify" or failure == report:
        sys.exit(1)
    (mounts["/results"] / report).write_text("verified\n")
elif "--entrypoint" in args:
    entry = args[args.index("--entrypoint") + 1]
    if entry == "cp":
        work = mounts["/input"]
        shutil.copyfile(work / "provenance.json", work / "toolchain/share/provenance.json")
    elif entry == "bash":
        if failure == "pack":
            sys.exit(1)
        sys.stdout.buffer.write(b"fixture archive")
    elif entry == "sh":
        for path in mounts["/cleanup"].iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    else:
        raise RuntimeError("Unexpected entrypoint: " + entry)
else:
    raise RuntimeError("Unexpected command: " + repr(args))
