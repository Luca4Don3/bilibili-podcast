"""Plan, apply, and restore runtime media/JSON ACLs safely."""

from __future__ import annotations

import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from ..manager import ConfigError, MIGRATION_LOCK_NAME, UnsafeConfigError


_SERIES_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_REQUIRED_TOOLS = ("getfacl", "setfacl", "systemctl")
_TIMER_GUARD_SECONDS = 300


@dataclass(frozen=True)
class PermissionTarget:
    kind: str
    series: str | None
    path: Path
    compliant: bool
    existed: bool = True
    required: str = ""


@dataclass(frozen=True)
class PermissionPlan:
    root: Path
    service_user: str
    media_root: Path
    json_root: Path
    lock_file: Path
    series: tuple[str, ...]
    targets: tuple[PermissionTarget, ...]

    @property
    def directory_count(self) -> int:
        return sum(target.kind == "directory" for target in self.targets)

    @property
    def file_count(self) -> int:
        return sum(target.kind == "file" for target in self.targets)

    @property
    def noncompliant_directory_count(self) -> int:
        return sum(target.kind == "directory" and not target.compliant for target in self.targets)

    @property
    def noncompliant_file_count(self) -> int:
        return sum(target.kind == "file" and not target.compliant for target in self.targets)


@dataclass(frozen=True)
class PermissionResult:
    plan: PermissionPlan
    applied: bool
    restored: bool = False
    backup_id: str | None = None


def _run(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def _require_tools(*, restore: bool = False) -> None:
    required = _REQUIRED_TOOLS[:2] if restore else _REQUIRED_TOOLS
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise ConfigError(f"required ACL tool is unavailable: {missing[0]}")


def _require_service_user(name: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        raise ConfigError("configured service user does not exist") from None


def _safe_absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"configured {label} must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise ConfigError(f"configured {label} does not exist") from None
        except OSError as exc:
            raise ConfigError(f"cannot inspect configured {label}: {type(exc).__name__}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeConfigError(f"configured {label} contains a symlink")
    if not path.is_dir():
        raise UnsafeConfigError(f"configured {label} is not a directory")
    return path.resolve()


def _series_from_database(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        raise ConfigError("configured database path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise UnsafeConfigError("configured database is missing or unsafe")
    candidates = [path]
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink():
            raise UnsafeConfigError("configured database has unsafe WAL state")
        if sidecar.exists():
            if not sidecar.is_file():
                raise UnsafeConfigError("configured database has unsafe WAL state")
        candidates.append(sidecar)

    def source_state() -> dict[str, tuple[int, int, int] | None]:
        result: dict[str, tuple[int, int, int] | None] = {}
        for source in candidates:
            try:
                metadata = source.stat(follow_symlinks=False)
            except FileNotFoundError:
                result[source.name] = None
            else:
                result[source.name] = (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        return result

    try:
        before = source_state()
        with tempfile.TemporaryDirectory(prefix="bilibili-podcast-permissions-db-") as temporary:
            temporary_root = Path(temporary)
            for source in candidates:
                if before[source.name] is not None:
                    shutil.copy2(source, temporary_root / source.name)
            after = source_state()
            if after != before:
                raise ConfigError("configured database changed while creating a read-only snapshot")
            snapshot = temporary_root / path.name
            with sqlite3.connect(snapshot) as connection:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute("SELECT series FROM series ORDER BY series").fetchall()
    except OSError as exc:
        raise ConfigError(f"cannot snapshot configured database: {type(exc).__name__}") from None
    except sqlite3.DatabaseError as exc:
        raise ConfigError(f"cannot read configured database: {type(exc).__name__}") from None
    series = tuple(str(row[0]) for row in rows)
    if not series:
        raise ConfigError("configured database contains no series")
    if any(_SERIES_RE.fullmatch(name) is None for name in series):
        raise UnsafeConfigError("configured database contains an unsafe series name")
    return series


def _runtime_config(root: str | Path) -> tuple[Path, Path, Path, Path, Path, str]:
    config_root = Path(root).expanduser()
    if config_root.is_symlink() or not config_root.is_dir():
        raise UnsafeConfigError("configuration root is missing or unsafe")
    config_root = config_root.resolve()
    values: dict[str, dict] = {}
    for name in ("app", "sync", "scheduler"):
        path = config_root / f"{name}.toml"
        if path.is_symlink() or not path.is_file():
            raise UnsafeConfigError(f"runtime {name} configuration is missing or unsafe")
        try:
            with path.open("rb") as handle:
                value = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read runtime {name} configuration: {type(exc).__name__}") from None
        if not isinstance(value, dict):
            raise ConfigError(f"invalid runtime {name} configuration")
        values[name] = value
    try:
        media_root = Path(values["app"]["paths"]["media_root"])
        json_root = Path(values["app"]["paths"]["json_root"])
        database = Path(values["app"]["database"]["path"])
        lock_file = Path(values["sync"]["paths"]["lock_file"])
        service_user = values["scheduler"]["runtime"]["user"]
    except (KeyError, TypeError):
        raise ConfigError("runtime configuration is missing a permissions field") from None
    if not isinstance(service_user, str) or not service_user:
        raise ConfigError("runtime service user is invalid")
    return config_root, media_root, json_root, database, lock_file, service_user


def _effective_permissions(entries: dict[str, str], key: str, mask_key: str) -> set[str]:
    permissions = set(entries.get(key, ""))
    mask = set(entries.get(mask_key, ""))
    return permissions & mask


def _acl_entries(path: Path) -> dict[str, str]:
    result = _run(("getfacl", "-cp", "--", str(path)))
    if result.returncode:
        raise ConfigError("cannot inspect runtime ACL")
    entries: dict[str, str] = {}
    for raw in result.stdout.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) == 3 and parts[0] in {"user", "group", "mask", "other"}:
            entries[f"{parts[0]}:{parts[1]}"] = parts[2]
        elif len(parts) == 4 and parts[0] == "default" and parts[1] in {"user", "group", "mask", "other"}:
            entries[f"default:{parts[1]}:{parts[2]}"] = parts[3]
    return entries


def _group_id(name: str) -> int | None:
    if name.isdigit():
        return int(name)
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def _acl_is_compliant(
    path: Path,
    user: str,
    *,
    directory: bool,
    required_access: str | None = None,
) -> bool:
    entries = _acl_entries(path)
    required = set(required_access or ("rwx" if directory else "rw"))
    account = pwd.getpwnam(user)
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_uid == account.pw_uid:
        access = set(entries.get("user:", ""))
    elif f"user:{user}" in entries:
        access = _effective_permissions(entries, f"user:{user}", "mask:")
    else:
        group_ids = set(os.getgrouplist(user, account.pw_gid))
        group_access: set[str] = set()
        if metadata.st_gid in group_ids:
            group_access.update(entries.get("group:", ""))
        for key, value in entries.items():
            if not key.startswith("group:") or key == "group:":
                continue
            group_id = _group_id(key.removeprefix("group:"))
            if group_id in group_ids:
                group_access.update(value)
        access = group_access & set(entries.get("mask:", "")) if group_access else set(entries.get("other:", ""))
    if not required <= access:
        return False
    if directory:
        default = _effective_permissions(
            entries, f"default:user:{user}", "default:mask:"
        )
        return required <= default
    return True


def _walk(root: Path, series: str, user: str, file_access: str) -> Iterator[PermissionTarget]:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        yield PermissionTarget("directory", series, root, False, False, "rwx")
        return
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeConfigError("runtime series path is unsafe")
    pending = [root]
    while pending:
        directory = pending.pop()
        yield PermissionTarget(
            "directory", series, directory,
            _acl_is_compliant(directory, user, directory=True),
            True, "rwx",
        )
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            raise ConfigError(f"cannot inspect runtime directory: {type(exc).__name__}") from None
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ConfigError(f"cannot inspect runtime object: {type(exc).__name__}") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise UnsafeConfigError("runtime tree contains a symlink")
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise UnsafeConfigError("runtime tree contains a hard-linked file")
                yield PermissionTarget(
                    "file", series, path,
                    _acl_is_compliant(
                        path, user, directory=False, required_access=file_access,
                    ),
                    True, file_access,
                )
            else:
                raise UnsafeConfigError("runtime tree contains an unsupported object")


def plan_runtime_permissions(root: str | Path) -> PermissionPlan:
    _require_tools()
    config_root, media_value, json_value, database, lock_file, service_user = _runtime_config(root)
    _require_service_user(service_user)
    media_root = _safe_absolute_directory(media_value, "media root")
    json_root = _safe_absolute_directory(json_value, "JSON root")
    if media_root == json_root or media_root in json_root.parents or json_root in media_root.parents:
        raise UnsafeConfigError("configured runtime roots overlap")
    series = _series_from_database(database)
    targets: list[PermissionTarget] = []
    for runtime_root, file_access in ((media_root, "r"), (json_root, "rw")):
        targets.append(PermissionTarget(
            "directory", None, runtime_root,
            _acl_is_compliant(runtime_root, service_user, directory=True),
            True, "rwx",
        ))
        for name in series:
            target = runtime_root / name
            if target.parent != runtime_root:
                raise UnsafeConfigError("runtime series path escapes configured root")
            targets.extend(_walk(target, name, service_user, file_access))
    return PermissionPlan(
        config_root, service_user, media_root, json_root,
        lock_file, series, tuple(targets),
    )


@contextmanager
def _exclusive_lock(path: Path, occupied_message: str):
    if not path.is_absolute():
        raise ConfigError("runtime lock path must be absolute")
    if path.is_symlink():
        raise UnsafeConfigError("runtime lock path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError(occupied_message) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _timer_window_is_safe() -> bool:
    result = _run(("systemctl", "list-timers", "--all", "--no-pager", "--output=json"))
    if result.returncode:
        raise ConfigError("cannot inspect scheduler timer window")
    try:
        timers = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        raise ConfigError("cannot parse scheduler timer window") from None
    now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    for timer in timers:
        unit = str(timer.get("unit", ""))
        if not unit.startswith("bilibili-podcast-sync@") or not unit.endswith(".timer"):
            continue
        value = timer.get("next")
        if isinstance(value, dict):
            value = value.get("timestamp") or value.get("usec")
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, (int, float)):
            next_us = int(value)
            if next_us < 10_000_000_000:
                next_us *= 1_000_000
            if 0 <= next_us - now_us < _TIMER_GUARD_SECONDS * 1_000_000:
                return False
        elif value is not None:
            raise ConfigError("cannot interpret scheduler timer window")
    return True


def _inventory(plan: PermissionPlan) -> list[dict[str, int | str | bool]]:
    roots = (("media", plan.media_root), ("json", plan.json_root))
    result: list[dict[str, int | str | bool]] = []
    for target in plan.targets:
        root_name, runtime_root = next(
            (name, path) for name, path in roots
            if target.path == path or path in target.path.parents
        )
        relative = "." if target.path == runtime_root else target.path.relative_to(runtime_root).as_posix()
        if not target.existed:
            result.append({
                "root": root_name, "relative": relative,
                "kind": target.kind, "existed": False,
            })
            continue
        metadata = target.path.stat(follow_symlinks=False)
        result.append({
            "root": root_name, "relative": relative,
            "kind": target.kind, "existed": True,
            "inode": metadata.st_ino, "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns, "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid, "gid": metadata.st_gid,
        })
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(backup: Path) -> None:
    files = sorted(path for path in backup.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    manifest = backup / "SHA256SUMS"
    manifest.write_text("".join(f"{_sha256(path)}  {path.name}\n" for path in files), encoding="ascii")
    for path in (*files, manifest):
        path.chmod(0o600)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    descriptor = os.open(backup, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_manifest(backup: Path) -> None:
    if backup.is_symlink() or not backup.is_dir() or stat.S_IMODE(backup.stat().st_mode) != 0o700:
        raise UnsafeConfigError("permission backup directory is unsafe")
    manifest = backup / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file() or stat.S_IMODE(manifest.stat().st_mode) != 0o600:
        raise UnsafeConfigError("permission backup manifest mode is unsafe")
    try:
        lines = manifest.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise ConfigError("cannot read permission backup manifest") from None
    expected_names = {"acl.restore", "inventory.json"}
    seen: set[str] = set()
    for line in lines:
        try:
            digest, name = line.split("  ", 1)
        except ValueError:
            raise ConfigError("invalid permission backup manifest") from None
        if name not in expected_names or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ConfigError("invalid permission backup manifest")
        if name in seen:
            raise ConfigError("permission backup manifest contains a duplicate entry")
        path = backup / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise ConfigError("permission backup checksum verification failed")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise UnsafeConfigError("permission backup file mode is unsafe")
        seen.add(name)
    if seen != expected_names:
        raise ConfigError("permission backup manifest is incomplete")


def _verify_restore_scope(plan: PermissionPlan, backup: Path) -> None:
    try:
        lines = (backup / "acl.restore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise ConfigError("cannot read permission ACL backup") from None
    roots = (plan.media_root, plan.json_root)
    paths = []
    for line in lines:
        if line.startswith("# file: "):
            value = line.removeprefix("# file: ")
            path = Path(value)
            if not path.is_absolute():
                raise UnsafeConfigError("permission ACL backup contains a relative path")
            paths.append(path)
    if not paths:
        raise ConfigError("permission ACL backup contains no runtime paths")
    if any(not any(path == root or root in path.parents for root in roots) for path in paths):
        raise UnsafeConfigError("permission ACL backup escapes runtime roots")


def _create_backup(plan: PermissionPlan) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = plan.root / ".backups" / f"permissions-{stamp}-{uuid.uuid4().hex[:8]}"
    backup.mkdir(mode=0o700, parents=True)
    backup.chmod(0o700)
    inventory = backup / "inventory.json"
    inventory.write_text(json.dumps(_inventory(plan), separators=(",", ":")), encoding="utf-8")
    inventory.chmod(0o600)
    acl = backup / "acl.restore"
    with acl.open("w", encoding="utf-8") as handle:
        acl.chmod(0o600)
        for runtime_root in (plan.media_root, plan.json_root):
            result = subprocess.run(
                ("getfacl", "-R", "-P", "-p", "--", str(runtime_root)),
                text=True, stdout=handle, stderr=subprocess.PIPE,
            )
            if result.returncode:
                raise ConfigError("cannot create permission ACL backup")
    _write_manifest(backup)
    _verify_manifest(backup)
    return backup


def _batch(items: Sequence[Path], size: int = 100) -> Iterator[Sequence[Path]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _apply_acl(plan: PermissionPlan) -> None:
    missing = [target.path for target in plan.targets if target.kind == "directory" and not target.existed]
    for path in missing:
        path.mkdir(mode=0o755)
    directories = [
        target.path for target in plan.targets
        if target.kind == "directory" and not target.compliant
    ]
    readable_files = [
        target.path for target in plan.targets
        if target.kind == "file" and target.required == "r" and not target.compliant
    ]
    writable_files = [
        target.path for target in plan.targets
        if target.kind == "file" and target.required == "rw" and not target.compliant
    ]
    for chunk in _batch(directories):
        result = _run(("setfacl", "-P", "-n", "-m", f"u:{plan.service_user}:rwx,d:u:{plan.service_user}:rwx", "--", *(str(path) for path in chunk)))
        if result.returncode:
            raise ConfigError("cannot apply runtime directory ACL")
    for chunk in _batch(readable_files):
        result = _run(("setfacl", "-P", "-n", "-m", f"u:{plan.service_user}:r", "--", *(str(path) for path in chunk)))
        if result.returncode:
            raise ConfigError("cannot apply runtime media file ACL")
    writable_by_mask: dict[str, list[Path]] = {}
    for path in writable_files:
        entries = _acl_entries(path)
        mask = set(entries.get("mask:", entries.get("group:", "")))
        for key, value in entries.items():
            if key in {"user:", f"user:{plan.service_user}"} or key.startswith("default:"):
                continue
            if key.startswith(("user:", "group:")) and "w" in value and "w" not in mask:
                raise ConfigError("JSON ACL mask expansion would widen another ACL entry")
        target_mask = "".join(permission if permission in mask | {"r", "w"} else "-" for permission in "rwx")
        writable_by_mask.setdefault(target_mask, []).append(path)
    for target_mask, paths in writable_by_mask.items():
        for chunk in _batch(paths):
            result = _run((
                "setfacl", "-P", "-n", "-m",
                f"u:{plan.service_user}:rw,m::{target_mask}", "--",
                *(str(path) for path in chunk),
            ))
            if result.returncode:
                raise ConfigError("cannot apply runtime JSON file ACL")


def _load_inventory(backup: Path) -> list[dict]:
    try:
        value = json.loads((backup / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("cannot read permission backup inventory") from None
    if not isinstance(value, list):
        raise ConfigError("invalid permission backup inventory")
    return value


def _inventory_path(plan: PermissionPlan, item: dict) -> Path:
    roots = {"media": plan.media_root, "json": plan.json_root}
    root = roots.get(item.get("root"))
    relative = item.get("relative")
    if root is None or not isinstance(relative, str):
        raise ConfigError("invalid permission backup inventory")
    path = root if relative == "." else root / relative
    if path != root and root not in path.parents:
        raise UnsafeConfigError("permission backup inventory escapes runtime root")
    return path


def _verify_metadata(
    plan: PermissionPlan,
    inventory: list[dict],
    *,
    allow_json_mask_change: bool = False,
) -> None:
    for item in inventory:
        path = _inventory_path(plan, item)
        if not item.get("existed"):
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            raise ConfigError("runtime object changed during permission operation") from None
        expected = (
            item.get("inode"), item.get("size"), item.get("mtime_ns"),
            item.get("mode"), item.get("uid"), item.get("gid"),
        )
        actual = (
            metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
            stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid,
        )
        if (
            actual != expected
            and allow_json_mask_change
            and item.get("root") == "json"
            and item.get("kind") == "file"
        ):
            expected_mode = item.get("mode")
            if isinstance(expected_mode, int):
                expected = (*expected[:3], expected_mode | stat.S_IWGRP, *expected[4:])
        if actual != expected:
            raise ConfigError("runtime object metadata changed during permission operation")


def _restore_backup(plan: PermissionPlan, backup: Path, *, remove_created: bool) -> None:
    _verify_manifest(backup)
    _verify_restore_scope(plan, backup)
    inventory = _load_inventory(backup)
    result = _run(("setfacl", "--restore", str(backup / "acl.restore")))
    if result.returncode:
        raise ConfigError("cannot restore runtime ACL backup")
    if remove_created:
        for item in reversed(inventory):
            if not item.get("existed"):
                path = _inventory_path(plan, item)
                try:
                    path.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise ConfigError("cannot remove directory created by failed ACL operation") from None
    _verify_metadata(plan, inventory)


def _verify_applied(plan: PermissionPlan, inventory: list[dict]) -> PermissionPlan:
    verified = plan_runtime_permissions(plan.root)
    if verified.noncompliant_directory_count or verified.noncompliant_file_count:
        raise ConfigError("runtime ACL verification failed")
    _verify_metadata(verified, inventory, allow_json_mask_change=True)
    return verified


def _backup_path(plan: PermissionPlan, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    backups = (plan.root / ".backups").resolve()
    if path.parent != backups or not path.name.startswith("permissions-"):
        raise UnsafeConfigError("permission backup is outside the private backup directory")
    return path


def run_runtime_permissions(
    root: str | Path,
    *,
    apply: bool = False,
    restore: str | Path | None = None,
) -> PermissionResult:
    if apply and restore is not None:
        raise ConfigError("--apply and --restore are mutually exclusive")
    _require_tools(restore=restore is not None)
    initial = plan_runtime_permissions(root)
    if not apply and restore is None:
        return PermissionResult(initial, False)

    with ExitStack() as stack:
        stack.enter_context(_exclusive_lock(
            initial.root / MIGRATION_LOCK_NAME,
            "another installation migration holds the migration lock",
        ))
        stack.enter_context(_exclusive_lock(
            initial.lock_file,
            "another sync process holds the shared sync lock",
        ))
        if not _timer_window_is_safe():
            raise ConfigError("next scheduler timer is less than five minutes away")
        plan = plan_runtime_permissions(initial.root)
        initial_targets = tuple(
            (target.kind, target.required, target.path, target.existed)
            for target in initial.targets
        )
        current_targets = tuple(
            (target.kind, target.required, target.path, target.existed)
            for target in plan.targets
        )
        if plan.series != initial.series or current_targets != initial_targets:
            raise ConfigError("runtime permission source changed after planning")
        if restore is not None:
            backup = _backup_path(plan, restore)
            _restore_backup(plan, backup, remove_created=True)
            return PermissionResult(plan_runtime_permissions(plan.root), False, True, backup.name)

        backup = _create_backup(plan)
        inventory = _load_inventory(backup)
        try:
            _apply_acl(plan)
            verified = _verify_applied(plan, inventory)
        except Exception as original:
            try:
                _restore_backup(plan, backup, remove_created=True)
            except Exception as rollback:
                raise ConfigError(
                    f"runtime ACL operation failed and automatic rollback failed: {type(rollback).__name__}"
                ) from original
            raise
        return PermissionResult(verified, True, False, backup.name)
