"""Exclusive process ownership for an autonomous host's durable state."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive_host(database: Path) -> Iterator[None]:
    # Keep the lock inode after release: unlinking lets a second owner lock a new inode.
    lock = database.resolve().with_suffix(database.suffix + ".host-lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This agent host database is already running") from None
        yield
    finally:
        os.close(descriptor)
