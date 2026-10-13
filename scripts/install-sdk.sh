#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
# shellcheck source=scripts/download-rpm.sh
source /recipe/scripts/download-rpm.sh
stage=${1:?builder or verifier stage required}
case "$stage" in builder|verifier) ;; *) exit 2 ;; esac
rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux /recipe/keys/RPM-GPG-KEY-EPEL-8
directory=$(mktemp -d /tmp/gcc-sdk.XXXXXXXX)
trap 'rm -rf "$directory"' EXIT
# The pinned base image's platform Python is sufficient for reading the lock;
# the locked SDK supplies Python 3.11 for build and verification scripts.
/usr/libexec/platform-python - "$stage" "$(uname -m)" > "$directory/inputs" <<'PY'
import json, sys
locks = json.load(open('/recipe/sdk-packages.json'))['locks'][sys.argv[2]]
for stage in [sys.argv[1]] + (['source'] if sys.argv[1] == 'builder' else []):
    for item in locks[stage]:
        print('{}\t{}\t{}'.format(stage, item['sha256'], item['url']))
PY
mkdir -p /opt/corresponding-source
while IFS=$'\t' read -r kind expected url; do
    filename=${url##*/}
    [[ "$filename" =~ ^[A-Za-z0-9_.+-]+\.rpm$ ]] || exit 2
    target="$directory/$filename"
    download_rpm "$url" "$target"
    printf '%s  %s\n' "$expected" "$target" | sha256sum --check --strict
    rpmkeys --checksig --verbose "$target" > "$directory/signature"
    grep -Eq 'Signature.*: OK$' "$directory/signature"
    if [[ "$kind" == source ]]; then mv "$target" /opt/corresponding-source/; fi
done < "$directory/inputs"
# Resolve against these exact local RPMs and the digest-pinned base image only.
dnf -y --disablerepo='*' --setopt=localpkg_gpgcheck=1 --setopt=install_weak_deps=False \
    --allowerasing install "$directory/"*.rpm
if [[ "$stage" == builder ]]; then
    # Retain exact source RPMs for the private zstd library and the SDK support
    # runtimes linked into compiler programs, rather than merely a newer source.
    for package in gcc libstdc++-static libzstd; do
        source=$(rpm -q --qf '%{SOURCERPM}' "$package")
        test -f "/opt/corresponding-source/$source"
    done
fi
dnf clean all
