#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Verify a relocated compiler against the consumer's native Linux environment."""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from hashing import sha256

# The consumer contract permits glibc components and zlib outside the package.
# A library installed incidentally in the verifier SDK must not hide a new
# consumer dependency. All other dynamic dependencies must be shipped.
SYSTEM_LIBRARIES = {
    "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2",
    "libc.so.6",
    "libdl.so.2",
    "libm.so.6",
    "libpthread.so.0",
    "librt.so.1",
    "libz.so.1",
}


def uses_packaged_library(loaded: str, soname: str, prefix: Path) -> bool:
    """Require the named dependency to resolve inside the relocated package."""
    for line in loaded.splitlines():
        name, separator, target = line.strip().partition(" => ")
        if separator and name == soname:
            path = Path(target.rsplit(" (", 1)[0]).resolve()
            return path.is_file() and path.is_relative_to(prefix.resolve())
    return False


class Verifier:
    """Shared execution environment and ordered check results for one package."""

    def __init__(self, prefix: Path, config: dict[str, Any]) -> None:
        self.prefix = prefix
        self.config = config
        self.environment = {"PATH": f"{prefix}/bin:/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}
        self.checks: list[str] = []

    def run(self, *command: object, quiet: bool = False, **kwargs: Any) -> str:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            list(map(str, command)),
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            **kwargs,
        )
        if not quiet or result.returncode:
            print("+", " ".join(map(str, command)), "\n" + result.stdout, flush=True)
        result.check_returncode()
        return result.stdout

    def check(self, name: str, condition: bool) -> None:
        if not condition:
            raise RuntimeError(name)
        self.checks.append(name)


@dataclass
class PackageInspection:
    contents: list[dict[str, Any]]
    requirements: dict[str, str]
    dependencies: dict[str, list[str]]


def inspect_tools(verifier: Verifier) -> None:
    prefix, config = verifier.prefix, verifier.config
    run, check = verifier.run, verifier.check
    gcc, gxx = prefix / "bin/gcc", prefix / "bin/g++"
    check("compiler version", run(gcc, "-dumpfullversion").strip() == config["gcc"]["version"])
    for tool in ("as", "ld", "ar", "ranlib", "readelf", "objcopy", "strip"):
        check(tool + " version", config["binutils"]["version"] in run(prefix / "bin" / tool, "--version"))
    for tool in ("gcov", "gcc-ar", "gcc-nm", "gcc-ranlib"):
        check(tool + " present", (prefix / "bin" / tool).is_file())
    for name in ("cc1", "cc1plus", "collect2", "lto1", "liblto_plugin.so"):
        path = Path(run(gcc, "-print-prog-name=" + name).strip()).resolve()
        check(name + " relocates", path.is_file() and prefix in path.parents)
    for name in ("as", "ld"):
        path = Path(run(gcc, "-print-prog-name=" + name).strip()).resolve()
        check(name + " relocates", path.is_file() and prefix in path.parents)

    for name in ("libstdc++.so.6", "libgcc_s.so.1"):
        path = Path(run(gxx, "-print-file-name=" + name).strip()).resolve()
        check(name + " relocates", path.is_file() and prefix in path.parents)
    check("private Zstandard runtime", (prefix / "lib64/libzstd.so.1").is_file())


def inspect_contents(verifier: Verifier) -> PackageInspection:
    prefix, config = verifier.prefix, verifier.config
    run, check = verifier.run, verifier.check
    packaged_libraries = {path.name for path in prefix.rglob("*.so*") if path.is_file()}

    floor = tuple(map(int, config["glibc_minimum"].split(".")))
    requirements: dict[str, str] = {}
    dependencies: dict[str, list[str]] = {}
    contents: list[dict[str, Any]] = []
    for path in sorted(prefix.rglob("*")):
        relative = path.relative_to(prefix).as_posix()
        if path.is_symlink():
            check("contained symlink " + relative, path.resolve().is_relative_to(prefix))
            contents.append({"path": relative, "link": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            magic = stream.read(4)
            digest.update(magic)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        contents.append(
            {
                "path": relative,
                "sha256": digest.hexdigest(),
                "bytes": path.stat().st_size,
                "mode": oct(path.stat().st_mode & 0o777),
            }
        )
        if magic != b"\x7fELF":
            continue
        version_text = run(prefix / "bin/readelf", "--version-info", path, quiet=True)
        check("no private glibc ABI " + relative, "GLIBC_PRIVATE" not in version_text)
        versions = [
            tuple(map(int, match.split("."))) for match in re.findall(r"\bGLIBC_([0-9]+(?:\.[0-9]+)+)", version_text)
        ]
        highest = max(versions, default=(0,))
        check("glibc baseline " + relative, highest <= floor)
        requirements[relative] = ".".join(map(str, highest))
        dynamic = run(prefix / "bin/readelf", "-d", path, quiet=True)
        dependencies[relative] = re.findall(r"\(NEEDED\).*Shared library: \[([^\]]+)\]", dynamic)
        unexpected = set(dependencies[relative]) - packaged_libraries - SYSTEM_LIBRARIES
        check("declared runtime dependencies " + relative + f": {sorted(unexpected)}", not unexpected)
        check(
            "no build-path runpath " + relative,
            not re.search(r"\((?:RPATH|RUNPATH)\).*\[(?:[^\]]*/work|[^\]]*/opt/gcc-builds)", dynamic),
        )
        if "(NEEDED)" in dynamic:
            loaded = run("ldd", path, quiet=True)
            check("resolved dependencies " + relative, "not found" not in loaded)
            for dependency in dependencies[relative]:
                if dependency in packaged_libraries:
                    check(
                        "packaged dependency " + dependency + " for " + relative,
                        uses_packaged_library(loaded, dependency, prefix),
                    )

    return PackageInspection(contents, requirements, dependencies)


def check_gcov_output(verifier: Verifier, obj: Path, work: Path) -> None:
    major = int(verifier.config["gcc"]["version"].split(".")[0])
    if major >= 9:
        # GCC 9/10 use -i for JSON and -j for human-readable counts.
        verifier.run(verifier.prefix / "bin/gcov", "--json-format", obj, cwd=work)
        verifier.check("gcov JSON output", any(work.glob("*.gcov.json.gz")))
    else:
        # JSON output was introduced in GCC 9; still verify coverage on GCC 8.
        verifier.run(verifier.prefix / "bin/gcov", obj, cwd=work)
        verifier.check("gcov annotated source output", (work / "answer.c.gcov").is_file())


def run_probes(verifier: Verifier) -> None:
    prefix, config = verifier.prefix, verifier.config
    environment = verifier.environment
    check = verifier.check
    gcc, gxx = prefix / "bin/gcc", prefix / "bin/g++"
    major = int(config["gcc"]["version"].split(".")[0])
    cpp_standard = "c++20" if major >= 10 else "c++17"
    # GCC 8 ships std::filesystem in a separate library. GCC 9 merged it.
    cpp_libraries = ["-lstdc++fs"] if major == 8 else []
    with tempfile.TemporaryDirectory(prefix="gcc-probes-") as directory:
        work = Path(directory)
        run = partial(verifier.run, cwd=work)
        environment["HOME"] = directory
        headers = work / "system-headers.c"
        headers.write_text("""#include <limits.h>
#include <sys/stat.h>
#include <linux/stat.h>
int main(void) { return INT_MAX <= 0; }
""")
        for compiler, language, standard in ((gcc, "c", "c17"), (gxx, "c++", cpp_standard)):
            run(
                compiler,
                "-std=" + standard,
                "-D_GNU_SOURCE",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-x",
                language,
                "-fsyntax-only",
                headers,
            )
            check(standard + " libc and Linux headers", True)

        c = work / "main.c"
        c.write_text("""#include <stdio.h>
#include <stdlib.h>
int answer(void);
int main(void) { if (answer() != 42) return EXIT_FAILURE; puts("ok"); return EXIT_SUCCESS; }
""")
        library = work / "answer.c"
        library.write_text("int answer(void); int answer(void) { return 42; }\n")
        flags = ["-std=c17", "-Wall", "-Wextra", "-Werror", "-pedantic", "-O2"]
        runtime_flags = [f"-Wl,-rpath,{prefix}/lib64", f"-Wl,-rpath,{prefix}/lib"]
        executable = work / "c-program"
        run(gcc, *flags, library, c, *runtime_flags, "-o", executable)
        check("C17 execution", run(executable).strip() == "ok")

        cpp = work / "main.cpp"
        cpp.write_text("""#include <array>
#include <filesystem>
#include <iostream>
#if __cplusplus >= 202002L
#include <span>
#endif
#include <stdexcept>
#include <thread>
int main() {
    std::array values{20, 22};
#if __cplusplus >= 202002L
    std::span view(values);
#else
    auto& view = values;
#endif
    int result = 0;
    std::thread worker([&] { result = view[0] + view[1]; });
    worker.join();
    if (result != 42 || !std::filesystem::exists("/tmp")) return 1;
    try { throw std::runtime_error("expected"); }
    catch (const std::runtime_error&) { std::cout << "ok\\n"; }
}
""")
        executable = work / "cpp-program"
        run(
            gxx,
            "-std=" + cpp_standard,
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-O2",
            "-pthread",
            cpp,
            *cpp_libraries,
            *runtime_flags,
            "-o",
            executable,
        )
        check(cpp_standard + " execution, threads, exceptions, filesystem", run(executable).strip() == "ok")
        check("uses packaged C++ runtime", uses_packaged_library(run("ldd", executable), "libstdc++.so.6", prefix))

        # Parallel LTO invokes GNU Make, even for these small probes on older GCC.
        check("GNU Make for parallel LTO", run("make", "--version").startswith("GNU Make"))
        # Require the linker plugin: slim LTO objects cannot fall back to native code.
        obj = work / "answer.o"
        archive = work / "libanswer.a"
        run(gcc, *flags, "-flto", "-fno-fat-lto-objects", "-c", library, "-o", obj)
        run(prefix / "bin/gcc-ar", "rcs", archive, obj)
        run(prefix / "bin/gcc-ranlib", archive)
        executable = work / "c-lto"
        run(gcc, *flags, "-flto=2", c, archive, *runtime_flags, "-o", executable)
        check("C LTO archive execution", run(executable).strip() == "ok")
        executable = work / "cpp-lto"
        run(
            gxx,
            "-std=" + cpp_standard,
            "-O2",
            "-flto=2",
            "-fno-fat-lto-objects",
            "-pthread",
            cpp,
            *cpp_libraries,
            *runtime_flags,
            "-o",
            executable,
        )
        check("C++ LTO execution", run(executable).strip() == "ok")

        for compiler, source, standard in ((gcc, c, "c17"), (gxx, cpp, cpp_standard)):
            executable = work / (standard + "-sanitized")
            sources = [library, source] if compiler == gcc else [source]
            run(
                compiler,
                "-std=" + standard,
                "-O1",
                "-g",
                "-pthread",
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
                *sources,
                *(cpp_libraries if compiler == gxx else []),
                *runtime_flags,
                "-o",
                executable,
            )
            check(standard + " ASan/UBSan execution", run(executable).strip() == "ok")

        # Confirm that instrumentation diagnoses errors, as well as loading.
        for sanitizer, body, diagnostic in (
            (
                "address",
                "#include <stdlib.h>\nint main(void) { volatile char *p = malloc(1); "
                "p[2] = 1; free((void*)p); return 0; }",
                "AddressSanitizer: heap-buffer-overflow",
            ),
            (
                "undefined",
                "#include <limits.h>\nint main(void) { volatile int n = INT_MAX; return n + 1; }",
                "runtime error: signed integer overflow",
            ),
        ):
            bad = work / (sanitizer + ".c")
            bad.write_text(body + "\n")
            executable = work / (sanitizer + "-error")
            run(
                gcc,
                "-O1",
                "-g",
                "-fsanitize=" + sanitizer,
                "-fno-sanitize-recover=all",
                bad,
                *runtime_flags,
                "-o",
                executable,
            )
            result = subprocess.run(
                [str(executable)],
                cwd=work,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            check(sanitizer + " diagnoses invalid code", result.returncode != 0 and diagnostic in result.stdout)

        shared = work / "libanswer.so"
        run(gcc, *flags, "-fPIC", "-shared", library, "-o", shared)
        executable = work / "c-shared"
        run(gcc, *flags, c, shared, "-Wl,-rpath," + directory, "-o", executable)
        check("shared library execution", run(executable).strip() == "ok")

        run(gcc, *flags, "--coverage", "-c", library, "-o", obj)
        executable = work / "c-coverage"
        run(gcc, *flags, "--coverage", c, obj, *runtime_flags, "-o", executable)
        check("coverage execution", run(executable).strip() == "ok")
        check_gcov_output(verifier, obj, work)


def write_report(
    output: Path, verifier: Verifier, inspection: PackageInspection, archive_sha256: str | None = None
) -> None:
    output.write_text(
        json.dumps(
            {
                "archive_sha256": archive_sha256,
                "environment": {
                    "distribution": platform.freedesktop_os_release(),
                    "architecture": platform.machine(),
                    "packages": (
                        Path("/portability-packages.tsv").read_text().splitlines()
                        if Path("/portability-packages.tsv").is_file()
                        else []
                    ),
                },
                "checks": verifier.checks,
                "elf_glibc_requirements": inspection.requirements,
                "elf_dependencies": inspection.dependencies,
                "contents": inspection.contents,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Verified {len(inspection.contents)} files/links and {len(inspection.requirements)} ELF objects", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="extract a locally built archive before verification")
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    archive_sha256 = sha256(args.archive) if args.archive else None
    if args.archive:
        prefix.mkdir(parents=True)
        subprocess.run(
            [
                "tar",
                "--use-compress-program=unzstd",
                "--no-same-owner",
                "--strip-components=1",
                "-xf",
                str(args.archive),
                "-C",
                str(prefix),
            ],
            check=True,
        )
    config = json.loads((prefix / "share" / "build.json").read_text())
    verifier = Verifier(prefix, config)
    inspect_tools(verifier)
    inspection = inspect_contents(verifier)
    run_probes(verifier)
    write_report(args.output, verifier, inspection, archive_sha256)


if __name__ == "__main__":
    main()
