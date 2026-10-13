# Linux x64 packaging trial

Completed locally on 2026-10-12. GCC 16.2.0 and Binutils 2.47 build, compress,
relocate, and pass the packaging smoke suite. Subsequent native Actions builds
and publication also passed; see the release verification below.

This is a historical log, including the Ubuntu trials that preceded the current
AlmaLinux baseline. Run and release identifiers refer to the retired `gcc-builds`
repository; its hosted evidence is no longer available there. The current package
contract is described in [README.md](../README.md).

## Distribution gate verification

On 2026-10-13, GCC 8.5.0 and 16.2.0 were rebuilt on AlmaLinux 8.10 with
64 CPUs, filtered fixed headers, and explicit multiarch search support.
Each exact binary archive passed the shared verifier on native Linux x64 in
AlmaLinux 8.10, Ubuntu 26.04, Debian 13, and Fedora 44 containers. Each container
had consumer libc headers and no distribution compiler; verification was offline.

| GCC | Checks per distribution | Retained paths | ELF objects | Highest glibc requirement |
| --- | --- | --- | --- | --- |
| 8.5.0 | 466 | 1,556 | 79 | 2.27 |
| 16.2.0 | 480 | 1,914 | 81 | 2.27 |

These checks include mixed libc/Linux headers, C/C++ execution, parallel LTO,
sanitizer execution and error detection, coverage, and every ELF dependency.
The documented baseline remains glibc 2.28. Native ARM64 and intermediate GCC
versions are checked by the same gate in Actions.

## Results

| Measurement | Result |
| --- | --- |
| Native build allocation | 48 CPUs |
| Clean build with prepared Docker images | 7 min 24 sec |
| Installed regular-file contents, counting hard links once | 274.8 MiB |
| Binary archive, Zstandard level 9 | 82.1 MiB |
| Separate corresponding-source archive | 161.2 MiB |
| Retained file and symlink paths | 1,911 |
| ELF objects audited | 80 |
| Required glibc baseline | 2.35 |

Both extractions passed 367 checks at independent paths containing spaces. Tests
covered C17, C++20, exceptions, threads, filesystem APIs, shared libraries, C/C++
LTO, slim LTO archives, sanitizer execution and error detection, and gcov JSON.
The two verification reports have identical contents. Actionlint, ShellCheck,
Python syntax inspection, and Bash syntax inspection also passed.
Publication preflight passed with mocked GitHub operations, including rejection
of corrupt archives and requests from branches other than `main`.

Compiler programs require symbols through glibc 2.34, but bundled
`lib64/libgcc_s.so.1` requires 2.35. Other external runtime dependencies are glibc
components and `libz.so.1`; GCC's C++ and support runtimes are supplied by the
package. The host uses glibc 2.34, so behavioral verification ran inside the
pinned Ubuntu 22.04 container.

Stripping retains compiler functionality, headers, static/shared runtimes, and
the LTO plugin. Offline manuals and libtool metadata are omitted. Unshipped
precompiled standard-library headers are disabled during the build.

## Artifacts and scope

Artifacts, provenance, checksums, and complete verification reports are retained
under `/tmp/gcc-builds-output/gcc-16.2.0-r1-linux-x64/` for review. Corresponding
source includes dependency archives and the frozen build recipe. These local
artifacts do not have a GitHub build attestation.

Trial recipe SHA-256 (before release-discovery and lifecycle automation):
`2ee59fb79a89555707fca273468bbebe5ef675464c8065087da9b17db2b50a65`.

Binary archive SHA-256:
`80a3863b6afbfaf71a4f664bd0bcc7b5bfb78478520659b93f227df7eee8850d`.

At the time of this local trial, ARM64 execution, Actions, and publication had
not been verified. This trial uses a single-stage GCC build and does not claim
upstream regression-suite coverage or a three-stage bootstrap comparison.

## Release automation review

A second clean Linux x64 run passed on 2026-10-12 with the configured Ubuntu
suite and resolved-release support: 10 min 35 sec including Docker preparation,
82.1 MiB binary archive, and 161.5 MiB corresponding-source archive. Both archive
extractions passed the packaging smoke suite. These repeat-run artifacts are at
`/tmp/gcc-builds-reviewed-output/gcc-16.2.0-r1-linux-x64/`.

Pre-commit review covers discovery, signature verification, release planning,
artifact layout, attestation enforcement, publication, cancellation, and cleanup.
The final quality gate includes 20 focused regression checks, strict mypy, Ruff,
ShellCheck, Bash syntax, and actionlint. Live discovery selects only GCC 16.2.0
for the initial run.

## Published release verification

Actions run 38242349435
passed code quality, discovery, both native builds, attestation verification, and
publication from reviewed commit `d95506efa4fb8d22b0078f21128c7eb340576885`.
The GCC 16.2.0 r1 release
retained binary and corresponding-source archives, attestations, checksums,
provenance, and both relocation reports for each architecture.

| Architecture | Binary bytes | Retained paths | ELF objects |
| --- | --- | --- | --- |
| x64 | 86,091,606 | 1,911 | 80 |
| ARM64 | 85,889,984 | 2,071 | 76 |

Both architectures ran the same offline smoke suite at two installation paths.
Workbench independently verified the ARM64 bundle using its committed Sigstore
trust roots before integration. ARM64 execution was native, without emulation.

## GCC 14 and 15 glibc comparison

On 2026-10-12, GCC 14.4.0 and 15.3.0 were built concurrently on Linux x64,
each allowed 64 CPUs, using the same pinned Ubuntu 22.04 SDK, package snapshot,
and Binutils 2.47 as GCC 16.2.0. Both builds completed in approximately eight
minutes, including packaging and offline relocation verification.

| GCC | Compiler glibc requirement | Bundled libgcc_s requirement | Binary bytes |
| --- | --- | --- | --- |
| 14.4.0 | 2.34 | 2.35 | 77,072,156 |
| 15.3.0 | 2.34 | 2.35 | 82,823,127 |
| 16.2.0, published trial | 2.34 | 2.35 | 86,091,606 |

These are required imports, not exported glibc symbols. For all three versions,
`lib64/libgcc_s.so.1` imports `_dl_find_object@GLIBC_2.35`. Compiler drivers import
`__libc_start_main@GLIBC_2.34`. GCC 14 and 15 each passed 366 checks at both
relocated paths, with 80 ELF objects audited; their paired reports were identical.
No other shipped ELF object required a glibc symbol newer than 2.34.

The complete packages therefore require glibc 2.35. This is a measured property
of these builds, not an intrinsic GCC source requirement. Supporting older glibc
requires a separately verified older SDK or sysroot rather than selecting an
older GCC major with the current recipe.

Local archives, provenance, checksums, and reports are retained under
`/tmp/gcc-builds-glibc-comparison/gcc-{14.4.0,15.3.0}-linux-x64/`.
They use archive names without a packaging revision suffix and have no GitHub
build attestations. These trials froze the earlier two-path verification recipe;
subsequent builds use one relocated path containing a space and one report.

## AlmaLinux baseline verification

On 2026-10-12, GCC 16.2.0 and Binutils 2.47 were rebuilt on the digest-pinned
AlmaLinux 8.10 SDK, using 64 CPUs. The build, packaging, offline relocation,
and cleanup completed in 9 min 5 sec, including Docker image preparation.
All 372 packaging checks passed at `/opt/relocated toolchain`, an unrelated
prefix containing a space. The verifier had no network or build workspace.

| Measurement | Result |
| --- | --- |
| Binary archive, Zstandard level 9 | 85,643,509 bytes (81.7 MiB) |
| Corresponding-source archive | 169,681,425 bytes |
| Retained file and symlink paths | 1,914 |
| ELF objects audited | 81 |
| Compiler driver glibc requirement | 2.15 |
| Bundled libgcc_s glibc requirement | 2.14 |
| Highest glibc requirement in the package | 2.27, in the Binutils linker |
| Documented and tested baseline | glibc 2.28 |

The package supplies its locked Zstandard runtime privately. Compiler programs,
plugins, and shared runtimes use relative loader paths so this dependency remains
available after relocation. Both architecture SDK locks were downloaded and
signature-verified; this local build establishes execution only for Linux x64.
At that point, native AlmaLinux ARM64 execution and Actions publication remained pending.

Artifacts and the single `verification.json` report are retained under
`/tmp/gcc-builds-alma-output/gcc-16.2.0-linux-x64/`. Binary archive SHA-256:
`81484b7c874516a0979642862051debde6cd5a48685176215889044246507fa5`.
Frozen trial recipe SHA-256:
`de3c2acb7ed7ef9bb4d19d56b2a4795f358fc2d70f1334378e56686787453a60`.

The local package has authenticated upstream source inputs but no GitHub build
attestation. Historical backfill, repository rename, and publication have not
been performed as part of this baseline trial.

### Workbench integration on the host

Workbench installed this archive into an isolated `/tmp` cache through its normal
HTTPS downloader and streaming filter, checking the recorded SHA-256 and all
1,914 retained entries. A second installation checked the integrity manifest.
An isolated distribution selected the local artifact without changing production
download pins or treating this local build as GitHub-attested.

The host's native glibc is 2.34. Its normal Workbench CLI passed C and C++
`test`, `lto`, and `sanitizers` commands with GCC 16.2.0, including vcpkg-managed
yyjson and fmt dependencies. Native AVX2 cases passed; portable-only sanitizer
variants intentionally skipped that optional backend. Test artifacts and the
integration result are retained under `/tmp/workbench-alma-integration/`.

Post-trial review added the exact SDK GCC source RPM for statically linked support
runtimes, alongside the Zstandard source RPM. Both architectures' refreshed locks
passed signature checks, and a fresh Linux x64 SDK image passed the source-version
checks. The frozen trial source archive above predates this addition; publication
must use a new build from the reviewed recipe. Review also fixed attestation-name
collisions between release versions and SDK resolver cancellation cleanup.
The final code quality gate passed 25 regression checks, strict mypy, Ruff,
ShellCheck (including the resolver shell), Bash syntax, and actionlint.

### Hosted AlmaLinux packages

Actions run 38276663303
built and verified both native architectures from commit
`436a68b1d1f2daa7306f1550e9fac3f048b0f6b5`, without emulation.

| Architecture | Binary bytes | Checks passed | ELF objects | Highest glibc requirement |
| --- | --- | --- | --- | --- |
| x64 | 85,666,869 | 372 | 81 | 2.27 |
| ARM64 | 84,887,111 | 357 | 77 | 2.27 |

The publisher failed during concurrent asset upload with HTTP 422, reporting
an existing provenance filename despite unique input names. Nine assets remained
in an unpublished draft. Recovery reverified all four archive attestations against
the original build commit, matched existing asset sizes and SHA-256 digests, and
uploaded only the missing x64 binary and checksum manifest. It then published
the canonical release
without deleting or replacing any asset. The original Actions run recorded a
failed publication; recovery ran locally against those same hosted artifacts.

Workbench independently verified the binary and source attestations for both
architectures using its pinned Sigstore roots. Both provenance records bind
the same recipe SHA-256:
`a286f6cbdc048fb972764318a4ad5c58df4ad699fa22e87a37c06922ac92aed0`.

### Shared source packaging

The original source bundle measured 238,366,720 bytes before outer compression
and 235,396,933 bytes afterward: only 1.2% savings. Subsequent packaging therefore
uses one uncompressed `gcc-VERSION-source.tar`, retaining the original compressed
archives, signatures, and source RPMs inside it.

Local verification reconstructed the shared bundle independently from both
published architectures' authenticated source archives, using the revised source
staging helper and the AlmaLinux tar implementation. Both outputs were identical
at 238,376,960 bytes, with all ten original download files preserved byte for byte.
Architecture-specific package reports remain with their binaries and CI evidence;
the shared source recipe includes both SDK locks. This checked source packaging
locally, without rebuilding either compiler or replacing published release assets.

### Parallel LTO verifier correction

The initial backfill exposed a missing verifier dependency: parallel LTO needs
GNU Make. GCC 8–10 failed the C LTO link on both native architectures. A local
GCC 8 probe reproduced that failure without Make and passed with Make restored.
The verifier now includes the builder's existing pinned Make RPM and reports
captured command diagnostics on failure.

With Make available, the existing GCC 16.2.0 x64 package exposed a separate
limitation: parallel LTO generated an unquoted Make command for the compiler
installation path containing a space. The same package passed all 397 checks
and 81 ELF inspections at `/opt/relocated-toolchain`. Verification now uses that
unrelated path, and the consumer contract requires an installation path without
spaces. Earlier successes at a path containing a space do not establish support
for parallel LTO at such paths.
