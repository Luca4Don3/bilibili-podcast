"""Small POSIX file primitives used by security-sensitive writers.

The helpers in this module deliberately operate through directory file
descriptors.  Callers keep business policy (which path, mode, or content is
appropriate); this module only prevents link traversal and validates the same
file descriptor that is subsequently used.
"""

from __future__ import annotations

import os
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class UnsafeFileError(OSError):
    """A path resolved through a link or to an unsupported object."""


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_OPEN_LOCK = threading.RLock()


def _path_parts(path: Path) -> tuple[str, tuple[str, ...]]:
    expanded = path.expanduser()
    if expanded.is_absolute():
        anchor = expanded.anchor or os.sep
        parts = expanded.parts[1:]
    else:
        anchor = "."
        parts = expanded.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeFileError("unsafe path component")
    return anchor, tuple(parts)


def open_directory(path: str | Path, *, create: bool = False, mode: int = 0o750) -> int:
    """Open *path* without following a symlink in any component."""
    anchor, parts = _path_parts(Path(path))
    descriptor = os.open(anchor, _DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode, dir_fd=descriptor)
                except FileExistsError:
                    # Another process/thread may have created the same
                    # directory after the failed open.  The verified
                    # O_NOFOLLOW open below remains the authority.
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise UnsafeFileError(
                    f"unsafe directory component: {type(exc).__name__}"
                ) from None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def validate_regular_fd(
    descriptor: int,
    *,
    require_single_link: bool = True,
    require_nonempty: bool = False,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeFileError("file must be regular")
    if require_single_link and metadata.st_nlink != 1:
        raise UnsafeFileError("file must have one hard link")
    if require_nonempty and metadata.st_size <= 0:
        raise UnsafeFileError("file must be non-empty")
    return metadata


def _open_regular_unlocked(
    path: str | Path,
    flags: int = os.O_RDONLY,
    *,
    mode: int = 0o600,
    create_parents: bool = False,
    exclusive: bool = False,
    require_single_link: bool = True,
    require_nonempty: bool = False,
) -> int:
    """Open a regular file through a verified parent directory descriptor."""
    target = Path(path)
    if not target.name or target.name in {".", ".."}:
        raise UnsafeFileError("unsafe file name")
    parent = open_directory(target.parent, create=create_parents)
    try:
        open_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        if exclusive:
            open_flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(target.name, open_flags, mode, dir_fd=parent)
        try:
            metadata = validate_regular_fd(
                descriptor,
                require_single_link=require_single_link,
                require_nonempty=require_nonempty,
            )
            current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            if (
                current.st_dev != metadata.st_dev
                or current.st_ino != metadata.st_ino
                or not stat.S_ISREG(current.st_mode)
            ):
                raise UnsafeFileError("file changed while opening")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    except OSError as exc:
        if isinstance(exc, UnsafeFileError):
            raise
        raise UnsafeFileError(f"unsafe file open: {type(exc).__name__}") from None
    finally:
        os.close(parent)


def open_regular(
    path: str | Path,
    flags: int = os.O_RDONLY,
    *,
    mode: int = 0o600,
    create_parents: bool = False,
    exclusive: bool = False,
    require_single_link: bool = True,
    require_nonempty: bool = False,
) -> int:
    # macOS implements relative dir_fd opens in a way that can race with
    # concurrent descriptor churn in another thread.  Cross-process safety is
    # still provided by O_EXCL/flock; this lock only stabilizes one process's
    # open-and-fstat sequence.
    with _OPEN_LOCK:
        return _open_regular_unlocked(
            path,
            flags,
            mode=mode,
            create_parents=create_parents,
            exclusive=exclusive,
            require_single_link=require_single_link,
            require_nonempty=require_nonempty,
        )


@contextmanager
def regular_file(
    path: str | Path,
    flags: int = os.O_RDONLY,
    **kwargs,
) -> Iterator[int]:
    descriptor = open_regular(path, flags, **kwargs)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def fsync_directory(path: str | Path) -> None:
    descriptor = open_directory(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path: str | Path, *, mode: int = 0o750) -> None:
    descriptor = open_directory(path, create=True, mode=mode)
    os.close(descriptor)


def safe_unlink(path: str | Path, *, missing_ok: bool = False) -> None:
    target = Path(path)
    parent = open_directory(target.parent)
    try:
        try:
            metadata = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafeFileError("refusing to unlink unsupported object")
        os.unlink(target.name, dir_fd=parent)
    finally:
        os.close(parent)


@contextmanager
def staged_replace(
    destination: str | Path,
    *,
    mode: int,
    require_nonempty: bool = True,
) -> Iterator[Path]:
    """Yield one private same-filesystem path and atomically activate it."""
    target = Path(destination)
    ensure_directory(target.parent)
    parent = open_directory(target.parent)
    operation_name = f".file-operation-{uuid.uuid4().hex}"
    operation = target.parent / operation_name
    temporary = operation / "payload"
    operation_fd = -1
    activated = False
    try:
        os.mkdir(operation_name, 0o700, dir_fd=parent)
        operation_fd = os.open(
            operation_name,
            _DIRECTORY_FLAGS,
            dir_fd=parent,
        )
        yield temporary
        descriptor = open_regular(
            temporary,
            os.O_RDWR,
            require_single_link=True,
            require_nonempty=require_nonempty,
        )
        try:
            os.fchmod(descriptor, mode)
            metadata = validate_regular_fd(
                descriptor,
                require_single_link=True,
                require_nonempty=require_nonempty,
            )
            if stat.S_IMODE(metadata.st_mode) != mode:
                raise UnsafeFileError("staged file mode verification failed")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            "payload",
            target.name,
            src_dir_fd=operation_fd,
            dst_dir_fd=parent,
        )
        activated = True
        os.fsync(parent)
    finally:
        if operation_fd != -1:
            if not activated:
                try:
                    metadata = os.stat(
                        "payload",
                        dir_fd=operation_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        os.unlink("payload", dir_fd=operation_fd)
            os.close(operation_fd)
        try:
            os.rmdir(operation_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def atomic_write_bytes(
    destination: str | Path,
    payload: bytes,
    *,
    mode: int,
) -> Path:
    target = Path(destination)
    with staged_replace(target, mode=mode, require_nonempty=False) as temporary:
        descriptor = open_regular(
            temporary,
            os.O_WRONLY,
            mode=0o600,
            exclusive=True,
            require_single_link=True,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return target
