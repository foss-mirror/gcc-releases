#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

archive_dir=${1:?archive directory required}
install_dir=${2:?installation directory required}
: "${ACTIONLINT_SHA256:?pinned actionlint hash required}"
: "${SHELLCHECK_SHA256:?pinned ShellCheck hash required}"
cache_hit=${LINTER_CACHE_HIT:-false}

download() {
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 5 --retry-delay 15 \
        --retry-max-time 300 --max-time 120 "$1" --output "$2"
}

mkdir -p "$archive_dir" "$install_dir"
if [[ "$cache_hit" != true ]]; then
    download https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz \
        "$archive_dir/actionlint.tar.gz"
    download https://github.com/koalaman/shellcheck/releases/download/v0.10.0/shellcheck-v0.10.0.linux.x86_64.tar.xz \
        "$archive_dir/shellcheck.tar.xz"
fi

# Check pinned hashes on every run, including cache hits.
printf '%s  %s\n' "$ACTIONLINT_SHA256" "$archive_dir/actionlint.tar.gz" | sha256sum --check --strict
printf '%s  %s\n' "$SHELLCHECK_SHA256" "$archive_dir/shellcheck.tar.xz" | sha256sum --check --strict
# Authenticate new downloads before they can be saved to the cache.
if [[ "$cache_hit" != true ]]; then
    gh attestation verify "$archive_dir/actionlint.tar.gz" --repo rhysd/actionlint \
        --signer-workflow rhysd/actionlint/.github/workflows/release.yaml --source-ref refs/tags/v1.7.12
fi

tar -xzf "$archive_dir/actionlint.tar.gz" -C "$install_dir" actionlint
tar -xJf "$archive_dir/shellcheck.tar.xz" -C "$install_dir" --strip-components=1 \
    shellcheck-v0.10.0/shellcheck
