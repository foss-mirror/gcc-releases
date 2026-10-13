#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Small GitHub read client; authentication and server failures remain errors."""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any


def get(repository: str, endpoint: str, *, absent_ok: bool = False) -> Any:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    request = urllib.request.Request(f"https://api.github.com/repos/{repository}/{endpoint}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 — fixed HTTPS GitHub origin
            return json.load(response)
    except urllib.error.HTTPError as error:
        if absent_ok and error.code == 404:
            return None
        raise


def releases(repository: str) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        batch = get(repository, f"releases?per_page=100&page={page}")
        yield from batch
        if len(batch) < 100:
            return
        page += 1
