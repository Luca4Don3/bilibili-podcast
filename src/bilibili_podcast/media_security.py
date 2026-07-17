"""Media integrity, isolated downloader staging, and private Cookie copies."""

from __future__ import annotations

import math
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .locks import LockBusyError, LockKind, ordered_lock
from .secure_files import (
    UnsafeFileError,
    ensure_directory,
    fsync_directory,
    open_directory,
    open_regular,
    safe_unlink,
    validate_regular_fd,
)


class MediaIntegrityError(ValueError):
    """Existing or staged media does not satisfy the integrity contract."""


class MediaDownloadError(RuntimeError):
    """An external media command failed without exposing sensitive details."""


@contextmanager
def media_update_lock(path: str | Path | None):
    """Acquire the shared sync/update lock used by Admin, Web, and sync."""
    if path is None:
        yield
        return
    try:
        with ordered_lock(path, LockKind.SYNC):
            yield
    except (UnsafeFileError, LockBusyError) as exc:
        message = (
            "another media update is in progress"
            if isinstance(exc, LockBusyError)
            else "unsafe media update lock"
        )
        raise MediaIntegrityError(message) from None


def _media_metadata(descriptor: int) -> os.stat_result:
    try:
        return validate_regular_fd(
            descriptor,
            require_single_link=True,
            require_nonempty=True,
        )
    except UnsafeFileError as exc:
        raise MediaIntegrityError(str(exc)) from None


def _probe_media_fd(descriptor: int, *, ffprobe_bin: str) -> None:
    before = _media_metadata(descriptor)
    inherited = os.dup(descriptor)
    try:
        try:
            result = subprocess.run(
                [
                    ffprobe_bin,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    f"/dev/fd/{inherited}",
                ],
                check=False,
                capture_output=True,
                text=True,
                pass_fds=(inherited,),
            )
        except OSError as exc:
            raise MediaIntegrityError(
                f"ffprobe unavailable: {type(exc).__name__}"
            ) from None
        if result.returncode != 0:
            raise MediaIntegrityError("ffprobe rejected media")
        try:
            duration = float(result.stdout.strip())
        except (TypeError, ValueError):
            duration = math.nan
        if not math.isfinite(duration) or duration <= 0:
            raise MediaIntegrityError("media duration is invalid")
        after = _media_metadata(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MediaIntegrityError("media changed during validation")
    finally:
        os.close(inherited)


def validate_media(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> Path:
    """Validate a media file through the same no-follow file descriptor."""
    target = Path(path)
    try:
        descriptor = open_regular(
            target,
            os.O_RDONLY,
            require_single_link=True,
            require_nonempty=True,
        )
    except (OSError, UnsafeFileError) as exc:
        raise MediaIntegrityError(
            f"media cannot be inspected: {type(exc).__name__}"
        ) from None
    try:
        _probe_media_fd(descriptor, ffprobe_bin=ffprobe_bin)
    finally:
        os.close(descriptor)
    return target


def media_is_valid(path: str | Path, *, ffprobe_bin: str = "ffprobe") -> bool:
    """Return False only for an absent file; corrupt existing files raise."""
    try:
        validate_media(path, ffprobe_bin=ffprobe_bin)
    except MediaIntegrityError:
        if not os.path.lexists(path):
            return False
        raise
    return True


def _copy_descriptors(source: int, destination: int) -> None:
    os.lseek(source, 0, os.SEEK_SET)
    while True:
        block = os.read(source, 1024 * 1024)
        if not block:
            break
        view = memoryview(block)
        while view:
            written = os.write(destination, view)
            view = view[written:]


def atomic_media_copy(
    source: str | Path,
    destination: str | Path,
    *,
    replace: bool = False,
    ffprobe_bin: str = "ffprobe",
    mode: int = 0o440,
) -> Path:
    """Copy and validate media before an atomic same-filesystem activation."""
    source_path = Path(source)
    destination_path = Path(destination)
    try:
        source_fd = open_regular(
            source_path,
            os.O_RDONLY,
            require_single_link=True,
            require_nonempty=True,
        )
    except UnsafeFileError as exc:
        raise MediaIntegrityError(str(exc)) from None
    parent_fd = -1
    operation_fd = -1
    staging_fd = -1
    operation_name = f".media-operation-{uuid.uuid4().hex}"
    staging_name = "payload"
    activated = False
    try:
        source_before = _media_metadata(source_fd)
        ensure_directory(destination_path.parent)
        parent_fd = open_directory(destination_path.parent)
        try:
            os.stat(destination_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            validate_media(destination_path, ffprobe_bin=ffprobe_bin)
            if not replace:
                raise FileExistsError(str(destination_path))

        os.mkdir(operation_name, 0o700, dir_fd=parent_fd)
        operation_fd = os.open(
            operation_name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        staging_fd = os.open(
            staging_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=operation_fd,
        )
        _copy_descriptors(source_fd, staging_fd)
        os.fsync(staging_fd)
        source_after = _media_metadata(source_fd)
        if (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
        ) != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
        ):
            raise MediaIntegrityError("media changed while copying")
        if os.fstat(staging_fd).st_size != source_before.st_size:
            raise MediaIntegrityError("media copy size mismatch")
        _probe_media_fd(staging_fd, ffprobe_bin=ffprobe_bin)
        os.fchmod(staging_fd, mode)
        staged = _media_metadata(staging_fd)
        if stat.S_IMODE(staged.st_mode) != mode:
            raise MediaIntegrityError("media mode verification failed")
        os.fsync(staging_fd)
        os.replace(
            staging_name,
            destination_path.name,
            src_dir_fd=operation_fd,
            dst_dir_fd=parent_fd,
        )
        activated = True
        fsync_directory(destination_path.parent)
        final_fd = open_regular(
            destination_path,
            os.O_RDONLY,
            require_single_link=True,
            require_nonempty=True,
        )
        try:
            final = _media_metadata(final_fd)
            if final.st_ino != staged.st_ino or stat.S_IMODE(final.st_mode) != mode:
                raise MediaIntegrityError("activated media verification failed")
        finally:
            os.close(final_fd)
        return destination_path
    finally:
        if staging_fd != -1:
            os.close(staging_fd)
        if operation_fd != -1:
            if not activated:
                try:
                    os.unlink(staging_name, dir_fd=operation_fd)
                except FileNotFoundError:
                    pass
            os.close(operation_fd)
        if parent_fd != -1:
            try:
                os.rmdir(operation_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        os.close(source_fd)


@contextmanager
def private_cookie_copy(source: str | Path) -> Iterator[Path]:
    """Expose a production Cookie only through a private disposable copy."""
    try:
        source_fd = open_regular(
            source,
            os.O_RDONLY,
            require_single_link=True,
        )
    except UnsafeFileError as exc:
        raise PermissionError("cookie source is unsafe") from None
    directory = Path(tempfile.mkdtemp(prefix="bilibili-podcast-cookie-"))
    directory.chmod(0o700)
    target = directory / "cookies.txt"
    target_fd = -1
    try:
        source_before = validate_regular_fd(source_fd, require_single_link=True)
        target_fd = open_regular(
            target,
            os.O_WRONLY,
            mode=0o600,
            exclusive=True,
            require_single_link=True,
        )
        _copy_descriptors(source_fd, target_fd)
        os.fsync(target_fd)
        os.fchmod(target_fd, 0o600)
        copied = validate_regular_fd(target_fd, require_single_link=True)
        source_after = validate_regular_fd(source_fd, require_single_link=True)
        if source_before.st_size != copied.st_size or (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_mtime_ns,
        ) != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_mtime_ns,
        ):
            raise PermissionError("cookie source changed while copying")
        os.close(target_fd)
        target_fd = -1
        yield target
    finally:
        if target_fd != -1:
            os.close(target_fd)
        os.close(source_fd)
        try:
            safe_unlink(target, missing_ok=True)
        finally:
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass


def run_download(
    command: Sequence[str],
    *,
    cookie_file: str | Path | None = None,
) -> None:
    """Run an external downloader without exposing output or Cookie paths."""
    if cookie_file is None:
        raise MediaDownloadError("download requires a cookie file")
    with private_cookie_copy(cookie_file) as private_cookie:
        safe_command = list(command)
        try:
            index = safe_command.index("--cookies")
            safe_command[index + 1] = str(private_cookie)
        except (ValueError, IndexError):
            safe_command.extend(("--cookies", str(private_cookie)))
        try:
            result = subprocess.run(
                safe_command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MediaDownloadError(
                f"download failed: {type(exc).__name__}"
            ) from None
        if result.returncode:
            raise MediaDownloadError("download command failed")


def download_media(
    command: Sequence[str],
    destination: str | Path,
    *,
    cookie_file: str | Path,
    ffprobe_bin: str = "ffprobe",
    mode: int = 0o440,
) -> Path:
    """Run one downloader in an isolated operation directory and activate it."""
    target = Path(destination)
    if os.path.lexists(target):
        validate_media(target, ffprobe_bin=ffprobe_bin)
        return target
    ensure_directory(target.parent)
    operation = target.parent / f".download-operation-{uuid.uuid4().hex}"
    operation.mkdir(mode=0o700)
    output_pattern = operation / "media.%(ext)s"
    safe_command = list(command)
    try:
        try:
            output_index = safe_command.index("-o") + 1
            safe_command[output_index] = str(output_pattern)
        except (ValueError, IndexError):
            safe_command.extend(("-o", str(output_pattern)))
        run_download(safe_command, cookie_file=cookie_file)
        candidates = tuple(operation.iterdir())
        if (
            len(candidates) != 1
            or candidates[0].is_symlink()
            or not candidates[0].is_file()
        ):
            raise MediaDownloadError("download did not produce one staging file")
        atomic_media_copy(
            candidates[0],
            target,
            ffprobe_bin=ffprobe_bin,
            mode=mode,
        )
        return target
    finally:
        shutil.rmtree(operation, ignore_errors=True)
