# SPDX-License-Identifier: GPL-3.0-or-later
"""Freeze only the files selected for the published source recipe."""

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

FILE_LIST = "recipe-files.txt"


@contextmanager
def recipe_input(root: Path, relative: Path) -> Iterator[BinaryIO]:
    """Open a regular recipe file without following any directory or file symlink."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Recipe input must be a regular file: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def snapshot(root: Path, destination: Path, config: dict[str, Any]) -> str:
    """Copy the explicit output file list, resolve the configuration, and hash it."""
    with recipe_input(root, Path(FILE_LIST)) as stream:
        listing = stream.read()
        listing_mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o777
    lines = listing.decode("utf-8").splitlines()
    names = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    paths = [Path(name) for name in names]
    if len(names) != len(set(names)) or any(
        path.is_absolute() or ".." in path.parts or path.as_posix() != name or not path.name
        for name, path in zip(names, paths, strict=True)
    ):
        raise ValueError("Recipe file list requires unique, normalized relative file paths")
    if not {FILE_LIST, "build.json"}.issubset(names):
        raise ValueError("Recipe file list must retain itself and build.json")
    destination.mkdir()
    for relative in sorted(paths):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == Path(FILE_LIST):
            # Freeze the list that selected these inputs even if it is edited
            # while the remaining files are copied.
            target.write_bytes(listing)
            target.chmod(listing_mode)
            continue
        with recipe_input(root, relative) as source, target.open("wb") as output:
            if relative == Path("build.json"):
                # Both architectures freeze this same resolved plan.
                output.write((json.dumps(config, indent=2) + "\n").encode())
            else:
                shutil.copyfileobj(source, output)
            target.chmod(stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o777)
    digest = hashlib.sha256()
    for relative in sorted(paths):
        data = (destination / relative).read_bytes()
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
