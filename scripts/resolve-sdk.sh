#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# DNF resolves updates; builds consume only the reviewed URLs and hashes.
set -euo pipefail
# shellcheck source=scripts/download-rpm.sh
source /recipe/scripts/download-rpm.sh
dnf -y --setopt=timeout=30 --setopt=retries=2 install dnf-plugins-core
rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux /recipe/keys/RPM-GPG-KEY-EPEL-8
rm -f /etc/yum.repos.d/*.repo
for repo in BaseOS AppStream PowerTools; do
    cat >> /etc/yum.repos.d/sdk.repo <<EOF
[$repo]
name=$repo
baseurl=https://repo.almalinux.org/almalinux/$1/$repo/\$basearch/os/
enabled=1
gpgcheck=1

[$repo-source]
name=$repo source
baseurl=https://repo.almalinux.org/vault/$1/$repo/Source/
enabled=0
gpgcheck=1
EOF
done
cat >> /etc/yum.repos.d/sdk.repo <<'EOF'
[epel]
name=EPEL
baseurl=https://dl.fedoraproject.org/pub/epel/8/Everything/$basearch/
enabled=1
gpgcheck=1
EOF
for arch in x86_64 aarch64; do
    for stage in builder verifier source; do
        directory=/output/$arch/$stage
        mkdir -p "$directory"
        readarray -t packages < <(/usr/libexec/platform-python - "$stage" <<'PY'
import json, sys
print('\n'.join(json.load(open('/recipe/sdk-packages.json'))['requests'][sys.argv[1]]))
PY
)
        options=(--forcearch="$arch" --setopt=install_weak_deps=False --setopt=timeout=30 --setopt=retries=2)
        if [[ "$stage" == source ]]; then
            dnf "${options[@]}" download --source --url "${packages[@]}" > "$directory/query"
        else
            dnf "${options[@]}" download --resolve --alldeps --archlist="$arch,noarch" --url \
                "${packages[@]}" > "$directory/query"
        fi
        sed -n '/^https:\/\//p' "$directory/query" | sort -u > "$directory/urls"
        test -s "$directory/urls"
        while IFS= read -r url; do
            filename=${url##*/}
            [[ "$filename" =~ ^[A-Za-z0-9_.+-]+\.rpm$ ]] || exit 2
            download_rpm "$url" "$directory/$filename.incoming"
            rpmkeys --checksig --verbose "$directory/$filename.incoming" | tee "$directory/signature"
            grep -Eq 'Signature.*: OK$' "$directory/signature"
            # A package can appear in several repositories. Collapse identical
            # copies only; never pin one URL to another URL's different bytes.
            if [[ -f "$directory/$filename" ]]; then
                cmp "$directory/$filename" "$directory/$filename.incoming"
            fi
            mv "$directory/$filename.incoming" "$directory/$filename"
        done < "$directory/urls"
    done
done
