#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
configure_gcc() {
    local gcc_version=$1 prefix=$2 configure=$3
    local -a fixincludes_options
    # GCC 8–12 still require the generated fixinc.sh build target. Their configure
    # scripts accept this switch but do not remove that dependency from GCC's makefile.
    fixincludes_options=()
    if (( ${gcc_version%%.*} >= 13 )); then
        fixincludes_options+=(--disable-fixincludes)
    fi
    "$configure" \
        --prefix="$prefix" --with-pkgversion="foss-mirror" \
        --enable-languages=c,c++ --disable-bootstrap --disable-multilib --enable-multiarch \
        --disable-nls --disable-werror "${fixincludes_options[@]}" \
        --enable-checking=release --enable-threads=posix --enable-shared \
        --enable-static --enable-plugin --enable-lto \
        --disable-libstdcxx-pch \
        --with-system-zlib --with-zstd-include=/usr/include \
        --with-zstd-lib=/usr/lib64 --with-isl=no
}

filter_fixed_headers() {
    local fixed
    # Old GCC needs fixincludes during its build. Keep GCC's limit wrappers,
    # but never let copied builder headers shadow the consumer's libc headers.
    for fixed in "$1"/lib/gcc/*/*/include-fixed; do
        [[ -d "$fixed" ]] || continue
        find "$fixed" -mindepth 1 -maxdepth 1 \
            ! -name limits.h ! -name syslimits.h -exec rm -rf -- {} +
    done
}

main() {
    set -euo pipefail
    umask 022
    export LC_ALL=C TZ=UTC
    : "${JOBS:?JOBS must be set}"
    [[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || exit 2

    readarray -t settings < <(python3.11 - <<'PY'
import json
from pathlib import Path
c = json.loads(Path('/recipe/build.json').read_text())
for value in (c['gcc']['version'], c['binutils']['version'],
              c['source_date_epoch']):
    print(value)
PY
    )
    gcc_version=${settings[0]}
    binutils_version=${settings[1]}
    export SOURCE_DATE_EPOCH=${settings[2]}
    prefix="/opt/gcc-builds/gcc-${gcc_version}"
    mkdir -p sources downloads build-binutils build-gcc install
    cp /opt/corresponding-source/* downloads/

    python3.11 /recipe/scripts/fetch.py gcc /work/downloads
    python3.11 /recipe/scripts/fetch.py binutils /work/downloads
    tar --no-same-owner -xf "downloads/gcc-${gcc_version}.tar.xz" -C sources
    tar --use-compress-program=unzstd --no-same-owner -xf "downloads/binutils-${binutils_version}.tar.zst" -C sources

    cd "/work/sources/gcc-${gcc_version}"
    # Delegate GCC's dependency selection and authenticated SHA-512 pins to its
    # own installer, which is covered by the verified GCC release signature.
    # Historical installers use FTP/HTTP; change only the transport to HTTPS.
    sed -i 's|\(ftp\|http\)://gcc.gnu.org/pub/gcc/infrastructure/|https://gcc.gnu.org/pub/gcc/infrastructure/|g' \
        contrib/download_prerequisites
    ./contrib/download_prerequisites --verify --sha512 --no-isl
    cp ./*.tar.* /work/downloads/

    target=$(./config.guess)
    case "$target" in
        x86_64-*) cpu_flags='-march=x86-64 -mtune=generic' ;;
        aarch64-*) cpu_flags='-march=armv8-a' ;;
        *) echo "Unsupported native architecture: $target" >&2; exit 2 ;;
    esac
    export CFLAGS="-O2 -g0 $cpu_flags"
    export CXXFLAGS="$CFLAGS"
    # Compiler support runtimes are static; glibc and the packaged zstd remain dynamic.
    export LDFLAGS='-static-libstdc++ -static-libgcc'

    cd /work/build-binutils
    "/work/sources/binutils-${binutils_version}/configure" \
        --prefix="$prefix" --with-pkgversion="foss-mirror" \
        --disable-nls --disable-werror --disable-gdb --disable-gdbserver \
        --disable-gprofng --disable-shared --enable-static --enable-plugins \
        --with-system-zlib --without-debuginfod --without-zstd
    make -j"$JOBS"
    make -j"$JOBS" install-strip DESTDIR=/work/install

    # Binutils is installed temporarily at its configured location for building.
    # It is removed before relocation verification, which runs in a fresh image.
    mkdir -p "$prefix"
    cp -a "/work/install${prefix}/." "$prefix/"
    export PATH="$prefix/bin:$PATH"
    cd /work/build-gcc
    configure_gcc "$gcc_version" "$prefix" "/work/sources/gcc-${gcc_version}/configure"
    make -j"$JOBS"
    make -j"$JOBS" install-strip DESTDIR=/work/install

    package="/work/install${prefix}"
    filter_fixed_headers "$package"
    # AlmaLinux supplies zstd as a shared library. Ship the locked, signed runtime
    # privately so compiler execution does not depend on the consumer's zstd ABI.
    cp -a /usr/lib64/libzstd.so.1* "$package/lib64/"
    licenses="$package/share/licenses"
    mkdir -p "$licenses/gcc" "$licenses/binutils" "$licenses/builder"
    cp /work/sources/gcc-"$gcc_version"/COPYING* "$licenses/gcc/"
    cp /work/sources/binutils-"$binutils_version"/COPYING* "$licenses/binutils/"
    cp /recipe/LICENSE "$licenses/builder/"
    mkdir -p "$licenses/zstd" "$licenses/zlib"
    cp -a /usr/share/licenses/libzstd/. "$licenses/zstd/"
    cp -a /usr/share/licenses/zlib/. "$licenses/zlib/"
    for dependency in gmp mpfr mpc gettext; do
        [[ -d "/work/sources/gcc-$gcc_version/$dependency" ]] || continue
        mkdir -p "$licenses/$dependency"
        find -L "/work/sources/gcc-$gcc_version/$dependency" -maxdepth 1 \
            -type f -name 'COPYING*' -exec cp {} "$licenses/$dependency/" \;
    done
    # Its README refers to the top-level COPYING3 and COPYING.RUNTIME copied above.
    cp /work/sources/gcc-"$gcc_version"/libstdc++-v3/README "$licenses/gcc/libstdc++-README"
    cp /recipe/build.json "$package/share/build.json"
    # Shared runtimes resolve sibling GCC libraries after relocation, without
    # requiring callers to set LD_LIBRARY_PATH or modifying the system loader.
    find "$package" -type f \( -perm /111 -o -name '*.so*' \) -print0 | while IFS= read -r -d '' library; do
        if readelf -h "$library" >/dev/null 2>&1; then
            runtime=$(realpath --relative-to="$(dirname "$library")" "$package/lib64")
            patchelf --set-rpath "\$ORIGIN:\$ORIGIN/$runtime" "$library"
        fi
    done
    # Runtime archives and headers stay; offline manuals and libtool metadata do not.
    rm -rf "$package/share/info" "$package/share/man"
    find "$package" -type f -name '*.la' -delete
    rpm -qa --qf '%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' | sort > /work/packages.tsv
    mv "$package" /work/toolchain
    echo "Toolchain staged at /work/toolchain"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
