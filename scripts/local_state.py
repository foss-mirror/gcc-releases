# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinate local writers and publish results without replacing user data."""

import ctypes
import errno
import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    # Keep the lock inode after releasing it: unlinking permits two owners to
    # lock different inodes under the same name. Locks are local coordination.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError(f"Lock is not a regular file: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Another invocation owns {path}") from error
        yield
    finally:
        os.close(descriptor)


def publish_directory(staging: Path, output: Path) -> None:
    """Atomically move a completed Linux build without replacing any entry."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # AT_FDCWD and RENAME_NOREPLACE; staging is on the destination filesystem.
    if rename(-100, os.fsencode(staging), -100, os.fsencode(output), 1) != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            raise FileExistsError(number, "Refusing to replace existing output", str(output))
        raise OSError(number, os.strerror(number), str(output))


def replace_lock(path: Path, original: bytes, replacement: bytes) -> None:
    """Detect edits during resolution and use a private, same-filesystem file."""
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".new", dir=path.parent, delete=False) as stream:
        incoming = Path(stream.name)
        try:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
            incoming.chmod(stat.S_IMODE(path.stat().st_mode))
            if path.read_bytes() != original:
                raise RuntimeError(f"Lock changed during resolution; preserving edits: {path}")
            incoming.replace(path)
        finally:
            incoming.unlink(missing_ok=True)
