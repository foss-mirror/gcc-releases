#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later

download_rpm() {
    local url=$1 target=$2 attempt status
    # AlmaLinux 8's curl predates --retry-all-errors. Retry whole transfers for
    # transport failures, including a connection reset after a partial response.
    for attempt in 0 1 2 3; do
        if curl --fail --location --proto '=https' --proto-redir '=https' --max-time 180 \
            --max-filesize 268435456 "$url" --output "$target"; then
            return 0
        else
            status=$?
        fi
        case "$status" in
            5|6|7|18|22|28|35|52|55|56|92) ;;
            *) return "$status" ;;
        esac
        if (( attempt == 3 )); then return "$status"; fi
        sleep "$((1 << attempt))"
    done
}
