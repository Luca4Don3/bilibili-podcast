"""Ordered, link-safe process locks.

The global order is migration -> sync -> publish.  Acquiring a lower-ranked
lock while holding a higher-ranked lock is an explicit programming error.
"""

from __future__ import annotations

import contextvars
import fcntl
import os
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path
from typing import Iterator

from .secure_files import open_regular


class LockOrderError(RuntimeError):
    """A caller attempted to invert the global lock order."""


class LockBusyError(RuntimeError):
    """A non-blocking ordered lock is held by another process."""


class LockKind(IntEnum):
    MIGRATION = 10
    SYNC = 20
    PUBLISH = 30


_HELD_LOCKS: contextvars.ContextVar[tuple[LockKind, ...]] = contextvars.ContextVar(
    "bilibili_podcast_held_locks",
    default=(),
)


@contextmanager
def ordered_lock(
    path: str | Path,
    kind: LockKind,
    *,
    blocking: bool = False,
) -> Iterator[int]:
    held = _HELD_LOCKS.get()
    if held and kind < held[-1]:
        raise LockOrderError(
            f"lock order violation: requested {kind.name.lower()} after "
            f"{held[-1].name.lower()}"
        )
    descriptor = open_regular(
        path,
        os.O_RDWR | os.O_CREAT,
        mode=0o600,
        create_parents=True,
        require_single_link=True,
    )
    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            raise LockBusyError(f"{kind.name.lower()} lock is busy") from None
        token = _HELD_LOCKS.set((*held, kind))
        try:
            yield descriptor
        finally:
            _HELD_LOCKS.reset(token)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
