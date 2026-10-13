# GCC releases

Portable, stripped GCC C/C++ packages for native Linux x64 and ARM64, published
from `foss-mirror/gcc-releases`. The GCC source mirror remains unchanged.

Initial verification: [Linux x64 packaging trial](docs/packaging-trial.md).

## Package contract

- GCC 8 and newer with Binutils 2.47, including headers, static/shared runtimes,
  sanitizer runtimes, coverage tools, and the LTO linker plugin.
- glibc 2.28 or newer and the zlib runtime (`libz.so.1`). Packages do not include
  glibc or a target sysroot; consumers need their distribution's libc development
  headers and startup objects.
- x64 uses the original x86-64 CPU baseline; ARM64 uses ARMv8-A. No emulation.
- The package supplies its private Zstandard runtime; consumers need no Zstandard installation.
- Parallel LTO (`-flto=N`) requires GNU Make on the consumer's `PATH`.
  Install the compiler in a path without spaces: GCC's generated parallel LTO
  Make recipes do not correctly quote its installation path.
- Compiler execution is independent of the installation directory. Applications
  using shared GCC runtimes must deploy or locate those runtimes themselves.
- The latest point releases in GCC 8–16 have passed native packaging and
  relocation verification on both architectures.
  GCC 16.2.0 also passed Workbench integration.
  These checks do not establish GCC's full upstream regression-suite coverage.

The compiler's execution baseline does not impose an application deployment
baseline by itself. Application compatibility depends on the entire target SDK,
including libc and GCC runtimes. The runtimes bundled here target glibc
2.28; targeting older systems requires compatible target runtimes and a sysroot.

## Local build

Requires Linux, Docker, Python 3.11 or newer, and Make. Run substantial work through your
installed scheduler where applicable:

```sh
benchctl hold -- make build JOBS=64
```

Without a scheduler, run `make build` directly. `JOBS` defaults to the host CPU
count. Sources, object files, and staging directories use `/tmp`; artifacts go to
`/tmp/gcc-builds-output/` by default. Set `OUTPUT=/some/path` to retain them elsewhere.
Existing output directories are never overwritten.
Concurrent builds targeting the same output are rejected. Verified results are
published with an atomic operation that also refuses newly created destinations.
Docker commands explicitly select the native platform, regardless of Docker's
default platform setting; emulated compiler builds remain unsupported.

Acquisition uses Docker's bridge network by default. Hosts whose firewall blocks
container DNS or bridge traffic may use `NETWORK=host`; verification still uses
`--network=none`. Build images remain in Docker's local image store for reuse.
Owned containers are stopped before cleanup on failure or interruption. Private
workspaces are removed after completion or failure; failed cleanup reports
retained paths, and uncertain container shutdown preserves its storage. For diagnosis,
`python3 scripts/build.py --keep-work` retains its printed workspace instead.

The recipe uses the same Dockerfile and scripts locally and in Actions. It freezes
a private recipe copy, builds a digest-pinned AlmaLinux environment with
hash-locked and signature-verified RPMs, compiles GCC and Binutils, strips installed binaries, and validates the
compressed package at an installation path different from its build prefix.
The relocated path has no spaces because of GCC's parallel LTO limitation.
`recipe-files.txt` explicitly lists the files retained in that copy and the
published source recipe. Update the list when adding recipe inputs or quality
checks. Missing files, symlinks, and nonregular files are rejected; unrelated
checkout files are excluded. The included keys are public upstream verification
keys used to authenticate source archives and SDK RPMs. The frozen recipe can
be rebuilt without Git metadata.

Before accepting a build, the same compressed archive must pass the full verifier
on AlmaLinux 8.10, Ubuntu 26.04 LTS, Debian 13, and Fedora 44. Each check runs in a
fresh native container without the build tree or network access. The AlmaLinux
baseline comes from `build.json` and the locked SDK; `portability.json` selects
the three consumer images by digest. `Dockerfile.portability` installs their
current libc development packages from signed distribution repositories, with
no system compiler. These consumer packages are recorded in each verification
report; they do not become build inputs or change the package's glibc baseline.
Refresh the selected consumer releases and image digests deliberately.
Local `make build` prepares all verifier images before compilation, so acquisition
failures stop early. SDK RPM transfers retry transient transport errors from the beginning;
each completed file must still pass its pinned hash and signature checks.

Repeat this gate on an existing trusted local package without rebuilding GCC:

```sh
benchctl hold -- make verify \
  ARCHIVE=/tmp/gcc-builds-output/gcc-16.2.0-linux-x64/gcc-16.2.0-linux-x64.tar.zst \
  OUTPUT=/tmp/gcc-portability-results
```

Use a new `OUTPUT` directory. It receives all four reports only after every check
passes. Add `DISTRIBUTION=debian-13`, for example, to run just that configured
distribution. Each report records the SHA-256 of the archive actually tested.
Local verification uses the host's native architecture; Actions checks
the same distributions on both x64 and ARM64. These containers share the host
kernel, so this gate checks distribution userspace compatibility, not a kernel
version matrix.

Validation covers C17 and C++20 (C++17 for GCC 8/9), mixed libc/Linux headers,
exceptions, threads, filesystem APIs, shared libraries, ASan/UBSan execution and error detection,
gcov, and C/C++ LTO (including an archive of slim LTO objects). Every packaged
ELF object's glibc requirements and dynamic dependencies are inspected.
Compiler headers, static libraries, plugins, and runtime libraries are retained;
offline manuals and libtool `.la` metadata are omitted. Older GCC builds still run
fixincludes, but packages retain only its `limits.h` and `syslimits.h` wrappers.
Copied builder headers are excluded so the consumer's libc headers are used.
GCC's [multiarch search support](https://gcc.gnu.org/install/configure.html)
is enabled explicitly so Ubuntu/Debian header and startup-object directories
are found as well as AlmaLinux's layout. This does not build 32-bit runtimes.
Binary archives use Zstandard level 9.
Sources use one architecture-independent `gcc-VERSION-source.tar` per release.
It contains the original compressed upstream archives, signatures, source RPMs,
and frozen recipe without unpacking or recompressing those inputs. Each native
builder produces the same source archive; publication compares their SHA-256
digests and retains one copy. Both architecture SDK locks are included in the recipe.
This initial recipe builds GCC in one stage; it does not perform a three-stage
bootstrap comparison or run the full upstream compiler regression suite.

## Code quality

`make check` runs Ruff linting and formatting, strict mypy, isolated regression
checks, ShellCheck, Bash syntax checks, and actionlint. Python check tools are
hash-locked in `requirements-dev.txt`; install them in a disposable virtual
environment with `pip install --require-hashes -r requirements-dev.txt`.
Use `make check PYTHON=/path/to/venv/bin/python` to run its linters and tests
without activating the environment or changing `PATH`.
`ACTIONLINT` and `SHELLCHECK` can select local tool paths. CI pins actionlint
1.7.12 and ShellCheck 0.10.0 by SHA-256, verifies actionlint's upstream attestation,
and uses the same Make target. ShellCheck publishes no detached signature or
artifact attestation for that [pinned release](https://github.com/koalaman/shellcheck/releases/tag/v0.10.0)
(checked 2026-10-12); its independently pinned hash and HTTPS are the available checks.
Regression tests install checked scripts from `tests/fixtures/` as disposable
processes to exercise failed downloads,
signature rejection, build cancellation, verification failures, and concurrent
writes without network access, GitHub writes, or full compiler builds.
Publication hashes archive bytes while copying them into private staging; checksum
sidecars and remote asset comparisons reuse those verified identities.

Commit messages use Conventional Commits, with subjects up to 50 characters and
body lines up to 72 characters. Review the complete change and run the quality
gate before committing. This repository is standalone and carries no Workbench
records or integration files.

## Source verification and updates

`build.json` owns the default release, source digests, signing identities,
discovery policy, build-image digest, and package snapshot. GitHub release tags
match upstream exactly, such as `releases/gcc-16.2.0`. Archive names use the
tag's basename and native platform, such as `gcc-16.2.0-linux-x64.tar.zst`.
The frozen recipe hash and packaging commit identify the build; names have no
packaging revision suffix. Update the recipe deliberately.

`make discover` reports stable release tags from `gcc-mirror/gcc`. The daily
workflow, scheduled for 00:00 UTC, selects the latest point release in each major
series at or above `discovery.minimum_major` (8). It compares candidate tags with
the canonical GCC repository, skips already published upstream tags, and freezes
one resolved configuration for both architectures. New major series are included
automatically; previously published point releases remain available. Older point
releases can be built through manual selection.
Only final release tags are eligible; release candidates and snapshots are excluded.
Historical availability does not imply upstream maintenance.

For a newly discovered release, discovery authenticates the GNU source archive
with a committed, explicitly allowed signing key before recording its SHA-256.
Builds then enforce both that pinned hash and the detached signature before
extraction. Discovery cannot authorize a new signing identity. The resolved
configuration and exact recipe are retained in the corresponding-source archive
and provenance; no automated commits to this repository are required. GCC's
prerequisites installer selects dependencies and verifies SHA-512 values supplied
inside that authenticated GCC release; it does not separately verify dependency
signatures. SDK RPMs have independently recorded SHA-256 hashes and must pass
RPM signature verification against AlmaLinux keys from the pinned base image or
the committed Fedora EPEL 8 key. EPEL supplies the builder-only patchelf tool.
Matching SDK GCC and Zstandard source RPMs accompany the statically linked support
runtimes and private Zstandard library in the source archive.

Pinned upstream identities:

| Material | Signing fingerprint |
| --- | --- |
| Selected GCC 9–13, 15/16 releases | `7F74F97C103468EE5D750B583AB00996FC26A641` (Richard Guenther signing subkey) |
| Selected GCC 8/14 releases | `D3A93CAD751C2AF4F8C7AD516C35B99309B5FA62` (Jakub Jelinek) |
| Binutils release | `3A24BC1E8FB409FA9F14371813FCEF89DD9E3C4F` (Nick Clifton) |

GCC's key comes from its published release-signing identity; Binutils' key comes
from GNU's keyring. Scoped public keys are retained in `keys/`. The Fedora EPEL 8
key fingerprint is `94E279EB8D8F25B21810ADF121EA45AB2F86D6A1`.

References: [GCC signing keys](https://gcc.gnu.org/mirrors.html),
[GNU keyring](https://ftp.gnu.org/gnu/gnu-keyring.gpg),
[Binutils 2.47 checksums](https://sourceware.org/pipermail/binutils/2026-July/150520.html),
[Fedora EPEL 8 key](https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-8).

## Builder lifecycle

Use the oldest AlmaLinux major release still receiving security updates, with
its latest maintained minor release. Pin the multi-architecture image digest in
`build.json` and the complete SDK RPM sets in `sdk-packages.json`. Local and
Actions builds consume these same inputs; ordinary builds never resolve new
builder package versions. The Dockerfile verifies the selected distribution and version.

| AlmaLinux major | Security support ends |
| --- | --- |
| 8 (current builder: 8.10) | 2029-05-31 |
| 9 | 2032-05-31 |
| 10 | 2035-05-31 |

Dates come from [AlmaLinux's lifecycle](https://wiki.almalinux.org/release-notes/).
`almalinux.version` and `almalinux.security_support_end` record the current
selection. New builds and release preparation stop after its security support
ends. Review and advance the baseline before that date, verifying the new glibc
contract on both native architectures.

Refresh the image digest and SDK locks deliberately to receive security fixes:
a pin does not update itself. `make sdk-lock` delegates dependency resolution to
DNF, downloads and authenticates packages for both architectures, and atomically
replaces the lock after all stages pass. Review the diff and run the build and
verification suite afterward. Set `NETWORK=host` only where needed locally.
Concurrent refreshes are rejected, and edits made to the lock during resolution
are preserved instead of replaced. Cleanup failures report retained resolver
files without obscuring the original error.
Upstream removal of a pinned RPM causes a build failure rather than substitution.

Existing published artifacts keep their original glibc contract and provenance.
Rebuilding a published GCC version requires explicit review of replacement
artifacts, checksums, attestations, and consumer pins; ordinary publication never
overwrites a release. Builder end of life does not expire existing GCC packages.

## Actions and publication

`build.yml` shows each stage in the top-level workflow graph. Discovery and
quality checks are followed by:

1. **Build** runs `make candidate` once per GCC version and native architecture
   (x64 or ARM64), uploading the binary, corresponding source, and checksums as
   unverified candidates.
2. **Verify** expands each candidate into four parallel distribution jobs, each
   running `make verify` on that same binary. Its matrix comes from the same
   configuration used locally. Each job has its own status and log, and receives
   only the binary archive.
3. **Complete** requires every distribution check for that candidate to pass, checks
   the candidate's checksums and all reports' archive hashes, distributions, and
   architectures, then attests and uploads the verified package.
4. **Publish** publishes releases with complete verified uploads from both
   architectures, in version order.

Rerun failed verification jobs to reuse the successful build's candidate instead
of recompiling GCC. Each stage waits for the preceding matrix to finish, while
completion and publication still require each candidate's own complete evidence.
Failed candidates do not block the successful release pairs.
Candidate uploads cannot be selected by publication. Local `make build` continues
to build and run all distribution checks in one invocation.

Archive uploads use no additional compression: binaries and the inputs inside
the source tar are already compressed. No compiler build outputs are saved to
Actions cache.

Successful builds from `main` automatically publish a GitHub Release after both
native architectures pass for that version. Failed versions are reported and
skipped without blocking other complete native pairs. Pull requests build the
pinned default release without publication.

Manual dispatch supports two modes:

- `publish` (default): build missing releases and publish from `main`. Blank
  `versions` discovers the latest point release in each eligible major series.
- `validate`: build and verify selected versions even if already published,
  retaining Actions artifacts without attestation or publication. Blank
  `versions` selects only the pinned version in `build.json`.

For example, `gh workflow run build.yml -f mode=validate -f versions=8.5.0,16.2.0`
checks both ends of the supported range on both native architectures.

Each release contains two binary archives and one shared source archive, each
with `<archive>.sha256` and `<archive>.jsonl` verification sidecars.
Detailed provenance and verification reports remain in Actions artifacts; the
binary packages also retain their build metadata. The JSONL bundles use GitHub's
standard Sigstore format and may authenticate multiple archives, as LLVM's do.
GitHub's automatic **Source code (zip)** and **Source code (tar.gz)** links contain
this packaging repository. Download `gcc-VERSION-source.tar` for the compiler and
dependency sources with their frozen build recipe.
The publisher verifies the bundles against `build.yml`, the branch, and source commit.
Published release tags and assets are never overwritten. Historical backfills do
not replace a newer version's Latest designation. Uploads are checked by size and
SHA-256 before publication. If an upload response is lost, an exact matching asset
is accepted without replacing it. A failed upload can leave a draft; explicit
`python3 scripts/publish.py INPUTS --config PLAN/gcc-VERSION.json --resume`
recovery requires the original run's verified artifacts and build commit in
`GITHUB_SHA`, together with
`GITHUB_REPOSITORY`, `GITHUB_REF=refs/heads/main`, and an authenticated `GH_TOKEN`.
Recovery checks the actual tag target when present, rejects unexpected or changed
assets, and uploads missing draft assets. An already-published release is skipped
only after verifying its tag, build identity, attestations, and complete asset set
against the original artifacts. Without `--resume`, existing releases and drafts
are rejected.
No releases or assets are deleted automatically.

For local recovery, download the named `release-plan` artifact into `PLAN`.
Download each native artifact into the same `INPUTS` directory using
`gh run download RUN_ID --name gcc-VERSION-linux-x64 --dir INPUTS` and the
corresponding ARM64 command. Specifying one artifact with `--name` gives the
same directory layout as the workflow's merged downloads.

The publication job uses `--resume`, so retrying only that job reuses the run's
uploaded packages without recompiling. Discovery reports existing drafts as
`needs_recovery` and excludes them from new builds, while other versions proceed.
The discovery job needs `contents: write` because GitHub exposes drafts only to
callers with push access; discovery itself performs no release or repository writes.
Rerun the original publication job or use its saved artifacts for local recovery;
a new discovery run does not search previous runs or replace a draft.
Draft creation, asset uploads, and final publication use the release's numeric
ID; a newly created draft need not appear in the release listing immediately.
Drafts from another build commit remain
protected from replacement.

To recover a partially completed batch, use
`python3 scripts/publish.py INPUTS --plan PLAN --resume` with the same
original artifacts and build commit. Batch publication processes versions oldest
first, reports per-version failures, and continues attempting the remaining
versions. It returns a failing status if any version failed. Cancellation stops
the batch immediately; no published assets are overwritten.

Actions artifacts retain the resolved plans, original packages, verification
reports, and attestation bundles for thirty days. The publication job uses them
to transfer build results and to resume interrupted publication without rebuilding.
GitHub expires these temporary artifacts; retain a separate copy if recovery may
take longer. Validation-only runs also use them to provide downloadable results.
GitHub Release assets provide the durable public download URLs used by Workbench;
consumers do not need Actions credentials. GitHub assigns publication/upload dates. Upstream
source dates and normalized archive timestamps are retained separately.

Before consuming a package, verify its SHA-256 and GitHub attestation against this
repository and `.github/workflows/build.yml`. A checksum alone does not
authenticate a download. Local trial packages have upstream source verification
but do not have a GitHub build attestation.

Each batch publishes in numeric order, oldest first and newest last. A failed
version recovered in a later run receives a later publication date, so GitHub's
display order can differ from version order across runs. The highest GCC version
is explicitly designated Latest; publishing an older version preserves that
designation. Publication uses one job with an ordered loop.

## License

Repository code is **GPL-3.0-or-later**; see [LICENSE](LICENSE). Upstream components
retain their own licenses and exceptions, including the GCC Runtime Library
Exception. Packages include notices and a corresponding-source archive with
dependency sources and the exact build recipe. Binary releases are distributed
with their matching source archives.
