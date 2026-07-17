"""Plan, apply, verify, and restore the exact v4 runtime ACL contract."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from ...locks import LockBusyError, LockKind, ordered_lock
from ...secure_files import UnsafeFileError, ensure_directory
from ..manager import ConfigError, ConfigManager, MIGRATION_LOCK_NAME, UnsafeConfigError
from ..models import ConfigSnapshot, SystemConfigSnapshot


_SERIES_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_GENERATION_RE = re.compile(r"[0-9]+-[0-9a-f]{12}\Z")
_REQUIRED_TOOLS = ("getfacl", "setfacl", "systemctl")
_TIMER_GUARD_SECONDS = 300


@dataclass(frozen=True)
class AclPolicy:
    owner: str
    service: str = ""
    nginx: str = ""
    inherit: bool = False
    create: bool = False


@dataclass(frozen=True)
class PermissionTarget:
    kind: str
    series: str | None
    path: Path
    compliant: bool
    existed: bool = True
    required: str = ""
    category: str = "runtime"
    policy: AclPolicy | None = None


@dataclass(frozen=True)
class PermissionPlan:
    root: Path
    service_user: str
    media_root: Path
    json_root: Path
    lock_file: Path
    series: tuple[str, ...]
    targets: tuple[PermissionTarget, ...]
    nginx_user: str | None = None

    @property
    def directory_count(self) -> int:
        return sum(target.kind == "directory" for target in self.targets)

    @property
    def file_count(self) -> int:
        return sum(target.kind == "file" for target in self.targets)

    @property
    def noncompliant_directory_count(self) -> int:
        return sum(
            target.kind == "directory" and not target.compliant
            for target in self.targets
        )

    @property
    def noncompliant_file_count(self) -> int:
        return sum(
            target.kind == "file" and not target.compliant
            for target in self.targets
        )


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


def _require_account(name: str, label: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise ConfigError(f"configured {label} does not exist") from None


def _require_service_user(name: str) -> None:
    _require_account(name, "service user")


def _safe_path(path: Path, label: str, *, directory: bool, may_be_missing: bool) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"configured {label} must be absolute")
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if may_be_missing:
                return path
            raise ConfigError(f"configured {label} does not exist") from None
        except OSError as exc:
            raise ConfigError(
                f"cannot inspect configured {label}: {type(exc).__name__}"
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafeConfigError(f"configured {label} contains a symlink")
        if index < len(path.parts[1:]) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeConfigError(f"configured {label} has a non-directory parent")
    if not path.exists():
        if may_be_missing:
            return path
        raise ConfigError(f"configured {label} does not exist")
    metadata = path.stat(follow_symlinks=False)
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        raise UnsafeConfigError(f"configured {label} has an unsafe type")
    if not directory and metadata.st_nlink != 1:
        raise UnsafeConfigError(f"configured {label} is hard-linked")
    return path


def _series_from_database(path: Path) -> tuple[str, ...]:
    _safe_path(path, "database", directory=False, may_be_missing=False)
    sources = (
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    )

    def source_state() -> tuple[tuple[int, int, int, int] | None, ...]:
        result = []
        for source in sources:
            try:
                metadata = source.stat(follow_symlinks=False)
            except FileNotFoundError:
                result.append(None)
            else:
                result.append(
                    (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                    )
                )
        return tuple(result)

    before = source_state()
    try:
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory(
            prefix="bilibili-podcast-permissions-db-"
        ) as temporary:
            temporary_root = Path(temporary)
            for source in sources:
                if source.exists():
                    shutil.copy2(source, temporary_root / source.name)
            snapshot = temporary_root / path.name
            if source_state() != before:
                raise ConfigError(
                    "configured database changed while creating a read-only snapshot"
                )
            with sqlite3.connect(snapshot) as connection:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(
                    "SELECT series FROM series ORDER BY series"
                ).fetchall()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ConfigError(
            f"cannot read configured database: {type(exc).__name__}"
        ) from None
    if source_state() != before:
        raise ConfigError("configured database changed while creating a read-only snapshot")
    series = tuple(str(row[0]) for row in rows)
    if not series:
        raise ConfigError("configured database contains no series")
    if any(_SERIES_RE.fullmatch(name) is None for name in series):
        raise UnsafeConfigError("configured database contains an unsafe series name")
    return series


def _normalize_permissions(value: str) -> str:
    return "".join(character for character in "rwx" if character in value)


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
            entries[f"{parts[0]}:{parts[1]}"] = _normalize_permissions(parts[2])
        elif (
            len(parts) == 4
            and parts[0] == "default"
            and parts[1] in {"user", "group", "mask", "other"}
        ):
            entries[f"default:{parts[1]}:{parts[2]}"] = _normalize_permissions(
                parts[3]
            )
    return entries


def _union_permissions(*values: str) -> str:
    return "".join(
        permission
        for permission in "rwx"
        if any(permission in value for value in values)
    )


def _expected_acl(
    path: Path,
    policy: AclPolicy,
    service_user: str,
    nginx_user: str | None,
) -> dict[str, str]:
    metadata = path.stat(follow_symlinks=False)
    service = _require_account(service_user, "service user")
    nginx = (
        _require_account(nginx_user, "Nginx user")
        if nginx_user is not None
        else None
    )
    named: dict[str, str] = {}
    owner_access = policy.owner
    if policy.service:
        if metadata.st_uid == service.pw_uid:
            owner_access = _union_permissions(owner_access, policy.service)
        else:
            named[f"user:{service_user}"] = policy.service
    if policy.nginx:
        if nginx is None:
            raise ConfigError("Nginx identity is required by the permission policy")
        if metadata.st_uid == nginx.pw_uid:
            owner_access = _union_permissions(owner_access, policy.nginx)
        elif nginx_user == service_user and f"user:{service_user}" in named:
            named[f"user:{service_user}"] = _union_permissions(
                named[f"user:{service_user}"], policy.nginx
            )
        else:
            named[f"user:{nginx_user}"] = policy.nginx
    expected = {
        "user:": owner_access,
        "group:": "",
        "other:": "",
        **named,
    }
    if named:
        expected["mask:"] = _union_permissions(*named.values())
    if policy.inherit:
        defaults = {
            "default:user:": owner_access,
            "default:group:": "",
            "default:other:": "",
            **{
                f"default:{key}": value
                for key, value in named.items()
            },
        }
        if named:
            defaults["default:mask:"] = _union_permissions(*named.values())
        expected.update(defaults)
    return expected


def _policy_is_compliant(
    path: Path,
    service_user: str,
    nginx_user: str | None,
    policy: AclPolicy,
) -> bool:
    if not path.exists():
        return False
    return _acl_entries(path) == _expected_acl(
        path, policy, service_user, nginx_user
    )


def _acl_is_compliant(
    path: Path,
    user: str,
    *,
    directory: bool,
    required_access: str | None = None,
) -> bool:
    """Compatibility helper that now enforces an exact, no-extra ACL."""
    required = required_access or ("rwx" if directory else "rw")
    return _policy_is_compliant(
        path,
        user,
        None,
        AclPolicy(
            owner="rwx" if directory else "rw",
            service=required,
            inherit=directory,
        ),
    )


def _target(
    path: Path,
    *,
    kind: str,
    category: str,
    service_user: str,
    nginx_user: str | None,
    policy: AclPolicy,
    series: str | None = None,
) -> PermissionTarget:
    existed = path.exists()
    if existed:
        metadata = path.stat(follow_symlinks=False)
        if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
            raise UnsafeConfigError("permission target is not a directory")
        if kind == "file":
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeConfigError("permission target is not a regular file")
            if metadata.st_nlink != 1:
                raise UnsafeConfigError("permission target is hard-linked")
    compliant = existed and _policy_is_compliant(
        path, service_user, nginx_user, policy
    )
    required = policy.service or policy.nginx or policy.owner
    return PermissionTarget(
        kind,
        series,
        path,
        compliant,
        existed,
        required,
        category,
        policy,
    )


def _walk_tree(
    root: Path,
    *,
    category: str,
    service_user: str,
    nginx_user: str | None,
    directory_policy: AclPolicy,
    file_policy: AclPolicy,
    series: str | None = None,
    allow_current_link: bool = False,
) -> Iterator[PermissionTarget]:
    yield _target(
        root,
        kind="directory",
        category=category,
        service_user=service_user,
        nginx_user=nginx_user,
        policy=directory_policy,
        series=series,
    )
    if not root.exists():
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name,
                reverse=True,
            )
        except OSError as exc:
            raise ConfigError(
                f"cannot inspect permission tree: {type(exc).__name__}"
            ) from None
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ConfigError(
                    f"cannot inspect permission object: {type(exc).__name__}"
                ) from None
            path = Path(entry.path)
            if stat.S_ISLNK(metadata.st_mode):
                if allow_current_link and directory == root and entry.name == "current":
                    target = os.readlink(path)
                    value = Path(target)
                    if (
                        value.is_absolute()
                        or len(value.parts) != 2
                        or value.parts[0] != ".generations"
                        or _GENERATION_RE.fullmatch(value.parts[1]) is None
                    ):
                        raise UnsafeConfigError("published current symlink is unsafe")
                    continue
                raise UnsafeConfigError("permission tree contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                yield _target(
                    path,
                    kind="directory",
                    category=category,
                    service_user=service_user,
                    nginx_user=nginx_user,
                    policy=directory_policy,
                    series=series,
                )
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise UnsafeConfigError(
                        "permission tree contains a hard-linked file"
                    )
                yield _target(
                    path,
                    kind="file",
                    category=category,
                    service_user=service_user,
                    nginx_user=nginx_user,
                    policy=file_policy,
                    series=series,
                )
            else:
                raise UnsafeConfigError(
                    "permission tree contains an unsupported object"
                )


def _load_snapshots(
    root: str | Path,
    *,
    require_system: bool,
    migration_locked: bool = False,
) -> tuple[ConfigSnapshot, SystemConfigSnapshot | None]:
    manager = ConfigManager(root, environ={})
    # Permission planning is read-only; apply re-reads under the exclusive
    # migration lock before it trusts or mutates any target.
    snapshot = manager.load_for_migration()
    system_path = manager.root / "system.toml"
    if not system_path.exists():
        if require_system:
            raise UnsafeConfigError("system.toml is required for this permission step")
        system = None
    else:
        # An explicitly present system scope is never silently downgraded to
        # a partial plan when it is malformed or owned incorrectly.
        system = manager.load_system()
    return snapshot, system


def _build_permission_plan(
    snapshot: ConfigSnapshot,
    system: SystemConfigSnapshot | None,
) -> PermissionPlan:
    service_user = snapshot.scheduler.runtime.user
    _require_service_user(service_user)
    nginx_user = system.nginx.user if system is not None else None
    if nginx_user is not None:
        _require_account(nginx_user, "Nginx user")
        if nginx_user == service_user:
            raise ConfigError("service and Nginx identities must be distinct")

    paths = snapshot.app.paths
    media_root = _safe_path(
        paths.media_root, "media root", directory=True, may_be_missing=True
    )
    json_root = _safe_path(
        paths.json_root, "JSON root", directory=True, may_be_missing=True
    )
    database = _safe_path(
        snapshot.app.database.path,
        "database",
        directory=False,
        may_be_missing=False,
    )
    series = _series_from_database(database)

    targets: list[PermissionTarget] = []

    def add_file(path: Path, category: str, policy: AclPolicy) -> None:
        targets.append(
            _target(
                path,
                kind="file",
                category=category,
                service_user=service_user,
                nginx_user=nginx_user,
                policy=policy,
            )
        )

    def add_directory(path: Path, category: str, policy: AclPolicy) -> None:
        targets.append(
            _target(
                path,
                kind="directory",
                category=category,
                service_user=service_user,
                nginx_user=nginx_user,
                policy=policy,
            )
        )

    add_directory(
        snapshot.root,
        "config",
        AclPolicy("rwx", service="x"),
    )
    for path in sorted(set(snapshot.sources.values())):
        add_file(path, "config", AclPolicy("rw", service="r"))
    system_path = snapshot.root / "system.toml"
    if system is not None:
        add_file(system_path, "system-config", AclPolicy("rw"))
    marker = snapshot.root / ".bilibili-podcast-version"
    if marker.exists():
        add_file(marker, "config", AclPolicy("rw", service="r"))

    add_file(
        snapshot.root / MIGRATION_LOCK_NAME,
        "lock",
        AclPolicy("rw", service="rw"),
    )
    state_root = paths.state_root
    add_directory(
        state_root,
        "state",
        AclPolicy("rwx", service="rwx", inherit=True, create=True),
    )
    add_file(database, "database", AclPolicy("rw", service="rw"))
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            add_file(sidecar, "database", AclPolicy("rw", service="rw"))
    add_file(
        snapshot.sync.paths.lock_file,
        "lock",
        AclPolicy("rw", service="rw"),
    )

    media_directory = AclPolicy(
        "rwx", service="rwx", nginx="x" if nginx_user else "", inherit=True, create=True
    )
    media_file = AclPolicy(
        "r", service="r", nginx="r" if nginx_user else ""
    )
    targets.extend(
        _walk_tree(
            media_root,
            category="media",
            service_user=service_user,
            nginx_user=nginx_user,
            directory_policy=media_directory,
            file_policy=media_file,
        )
    )

    private_directory = AclPolicy(
        "rwx", service="rwx", inherit=True, create=True
    )
    private_file = AclPolicy("rw", service="rw")
    for root, category in (
        (json_root, "json"),
        (paths.rss_root, "master-rss"),
        (snapshot.sync.browser.user_data_root, "browser-profile"),
        (paths.log_dir, "log"),
        (paths.fallback_log_dir, "fallback-log"),
    ):
        targets.extend(
            _walk_tree(
                root,
                category=category,
                service_user=service_user,
                nginx_user=nginx_user,
                directory_policy=private_directory,
                file_policy=private_file,
            )
        )

    published_directory = AclPolicy(
        "rwx", service="rwx", nginx="x" if nginx_user else "", inherit=True, create=True
    )
    published_file = AclPolicy(
        "rw", service="rw", nginx="r" if nginx_user else ""
    )
    targets.extend(
        _walk_tree(
            paths.published_rss_root,
            category="published-rss",
            service_user=service_user,
            nginx_user=nginx_user,
            directory_policy=published_directory,
            file_policy=published_file,
            allow_current_link=True,
        )
    )

    add_directory(
        paths.secrets_dir,
        "secrets",
        AclPolicy("rwx", service="x", create=True),
    )
    add_file(
        snapshot.sync.paths.cookie_file,
        "cookie",
        AclPolicy("rw", service="r"),
    )

    if system is not None:
        for log_path in (
            system.nginx.access_log_path,
            system.nginx.error_log_path,
        ):
            if log_path.exists():
                add_file(log_path, "nginx-log", AclPolicy("rw", nginx="rw"))
        if system.nginx.config_path.exists():
            add_file(system.nginx.config_path, "system-file", AclPolicy("rw"))

    identities: set[Path] = set()
    for target in targets:
        if target.path in identities:
            raise ConfigError("permission inventory contains a duplicate target")
        identities.add(target.path)
    return PermissionPlan(
        snapshot.root,
        service_user,
        media_root,
        json_root,
        snapshot.sync.paths.lock_file,
        series,
        tuple(targets),
        nginx_user,
    )


def plan_runtime_permissions(
    root: str | Path,
    *,
    system_manifest: str | Path | None = None,
    require_system: bool = False,
) -> PermissionPlan:
    if system_manifest is not None:
        raise ConfigError(
            "--system-manifest is valid only with upgrade --prepare"
        )
    _require_tools()
    snapshot, system = _load_snapshots(root, require_system=require_system)
    return _build_permission_plan(snapshot, system)


@contextmanager
def _exclusive_lock(path: Path, occupied_message: str):
    kind = (
        LockKind.MIGRATION
        if path.name == MIGRATION_LOCK_NAME
        else LockKind.SYNC
    )
    try:
        with ordered_lock(path, kind):
            yield
    except LockBusyError:
        raise ConfigError(occupied_message) from None
    except UnsafeFileError:
        raise UnsafeConfigError("runtime lock path is unsafe") from None


def _timer_window_is_safe() -> bool:
    result = _run(
        ("systemctl", "list-timers", "--all", "--no-pager", "--output=json")
    )
    if result.returncode:
        raise ConfigError("cannot inspect scheduler timer window")
    try:
        timers = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        raise ConfigError("cannot parse scheduler timer window") from None
    now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    for timer in timers:
        unit = str(timer.get("unit", ""))
        if not unit.startswith("bilibili-podcast-sync@") or not unit.endswith(
            ".timer"
        ):
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
    result: list[dict[str, int | str | bool]] = []
    for index, target in enumerate(plan.targets):
        item: dict[str, int | str | bool] = {
            "index": index,
            "path_digest": hashlib.sha256(
                os.fsencode(target.path)
            ).hexdigest(),
            "kind": target.kind,
            "category": target.category,
            "existed": target.existed,
        }
        if target.existed:
            metadata = target.path.stat(follow_symlinks=False)
            item.update(
                {
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "uid": metadata.st_uid,
                    "gid": metadata.st_gid,
                    "nlink": metadata.st_nlink,
                    "type": (
                        "directory"
                        if stat.S_ISDIR(metadata.st_mode)
                        else "file"
                    ),
                }
            )
        result.append(item)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(backup: Path) -> None:
    files = sorted(
        path
        for path in backup.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    )
    manifest = backup / "SHA256SUMS"
    manifest.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in files),
        encoding="ascii",
    )
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
    if (
        backup.is_symlink()
        or not backup.is_dir()
        or stat.S_IMODE(backup.stat().st_mode) != 0o700
    ):
        raise UnsafeConfigError("permission backup directory is unsafe")
    manifest = backup / "SHA256SUMS"
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or stat.S_IMODE(manifest.stat().st_mode) != 0o600
    ):
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
        if (
            name not in expected_names
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or name in seen
        ):
            raise ConfigError("invalid permission backup manifest")
        path = backup / name
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise ConfigError("permission backup checksum verification failed")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise UnsafeConfigError("permission backup file mode is unsafe")
        seen.add(name)
    if seen != expected_names:
        raise ConfigError("permission backup manifest is incomplete")


def _create_backup(plan: PermissionPlan) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = (
        plan.root
        / ".backups"
        / f"permissions-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    backup.mkdir(mode=0o700, parents=True)
    backup.chmod(0o700)
    inventory = backup / "inventory.json"
    inventory.write_text(
        json.dumps(_inventory(plan), separators=(",", ":")),
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    acl = backup / "acl.restore"
    with acl.open("w", encoding="utf-8") as handle:
        acl.chmod(0o600)
        for target in plan.targets:
            if not target.existed:
                continue
            result = subprocess.run(
                ("getfacl", "-P", "-p", "--", str(target.path)),
                text=True,
                stdout=handle,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                raise ConfigError("cannot create permission ACL backup")
    _write_manifest(backup)
    _verify_manifest(backup)
    return backup


def _permission_spec(entries: dict[str, str], *, default: bool) -> str:
    prefix = "default:" if default else ""
    selected = {
        key.removeprefix("default:"): value
        for key, value in entries.items()
        if key.startswith("default:") == default
    }
    order = (
        "user:",
        *sorted(key for key in selected if key.startswith("user:") and key != "user:"),
        "group:",
        *sorted(key for key in selected if key.startswith("group:") and key != "group:"),
        "mask:",
        "other:",
    )
    values = []
    for key in order:
        if key not in selected:
            continue
        qualifier = key.split(":", 1)[1]
        values.append(
            f"{prefix}{key.split(':', 1)[0]}:{qualifier}:"
            f"{''.join(ch if ch in selected[key] else '-' for ch in 'rwx')}"
        )
    return ",".join(values)


def _apply_target_acl(plan: PermissionPlan, target: PermissionTarget) -> None:
    if target.policy is None:
        raise ConfigError("permission target policy is missing")
    if not target.path.exists():
        if target.kind == "directory" and target.policy.create:
            ensure_directory(target.path, mode=0o700)
        else:
            raise ConfigError(f"required permission target is missing: {target.category}")
    expected = _expected_acl(
        target.path,
        target.policy,
        plan.service_user,
        plan.nginx_user,
    )
    clear = _run(("setfacl", "-P", "-b", "--", str(target.path)))
    if clear.returncode:
        raise ConfigError("cannot clear runtime ACL")
    if target.kind == "directory":
        clear_default = _run(("setfacl", "-P", "-k", "--", str(target.path)))
        if clear_default.returncode:
            raise ConfigError("cannot clear runtime default ACL")
    access_spec = _permission_spec(expected, default=False)
    result = _run(
        ("setfacl", "-P", "-n", "--set", access_spec, "--", str(target.path))
    )
    if result.returncode:
        raise ConfigError("cannot apply exact runtime ACL")
    if target.policy.inherit:
        default_spec = _permission_spec(expected, default=True)
        result = _run(
            (
                "setfacl",
                "-P",
                "-n",
                "-d",
                "--set",
                default_spec,
                "--",
                str(target.path),
            )
        )
        if result.returncode:
            raise ConfigError("cannot apply exact runtime default ACL")


def _apply_acl(plan: PermissionPlan) -> None:
    for target in plan.targets:
        if not target.compliant:
            _apply_target_acl(plan, target)


def _load_inventory(backup: Path) -> list[dict]:
    try:
        value = json.loads(
            (backup / "inventory.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("cannot read permission backup inventory") from None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ConfigError("invalid permission backup inventory")
    return value


def _inventory_path(plan: PermissionPlan, item: dict) -> Path:
    index = item.get("index")
    if not isinstance(index, int) or not 0 <= index < len(plan.targets):
        raise ConfigError("invalid permission backup inventory")
    path = plan.targets[index].path
    if hashlib.sha256(os.fsencode(path)).hexdigest() != item.get("path_digest"):
        raise UnsafeConfigError("permission backup inventory target changed")
    return path


def _verify_metadata(
    plan: PermissionPlan,
    inventory: list[dict],
    *,
    acl_may_change: bool = False,
) -> None:
    for item in inventory:
        path = _inventory_path(plan, item)
        if not item.get("existed"):
            continue
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            raise ConfigError(
                "runtime object changed during permission operation"
            ) from None
        stable_expected = (
            item.get("device"),
            item.get("inode"),
            item.get("size"),
            item.get("mtime_ns"),
            item.get("uid"),
            item.get("gid"),
            item.get("nlink"),
            item.get("type"),
        )
        stable_actual = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            "directory" if stat.S_ISDIR(metadata.st_mode) else "file",
        )
        if stable_actual != stable_expected:
            raise ConfigError(
                "runtime object metadata changed during permission operation"
            )
        if (
            not acl_may_change
            and stat.S_IMODE(metadata.st_mode) != item.get("mode")
        ):
            raise ConfigError(
                "runtime object mode changed during permission operation"
            )


def _verify_restore_scope(plan: PermissionPlan, backup: Path) -> None:
    try:
        lines = (backup / "acl.restore").read_text(
            encoding="utf-8"
        ).splitlines()
    except (OSError, UnicodeError):
        raise ConfigError("cannot read permission ACL backup") from None
    allowed = {target.path for target in plan.targets if target.existed}
    paths = []
    for line in lines:
        if line.startswith("# file: "):
            path = Path(line.removeprefix("# file: "))
            if not path.is_absolute():
                raise UnsafeConfigError(
                    "permission ACL backup contains a relative path"
                )
            paths.append(path)
    if allowed and not paths:
        raise ConfigError("permission ACL backup contains no runtime paths")
    if any(path not in allowed for path in paths):
        raise UnsafeConfigError("permission ACL backup escapes inventory scope")


def _restore_backup(
    plan: PermissionPlan,
    backup: Path,
    *,
    remove_created: bool,
) -> None:
    _verify_manifest(backup)
    _verify_restore_scope(plan, backup)
    inventory = _load_inventory(backup)
    result = _run(("setfacl", "--restore", str(backup / "acl.restore")))
    if result.returncode:
        raise ConfigError("cannot restore runtime ACL backup")
    if remove_created:
        for item in reversed(inventory):
            if item.get("existed"):
                continue
            path = _inventory_path(plan, item)
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                raise ConfigError(
                    "cannot remove directory created by failed ACL operation"
                ) from None
    _verify_metadata(plan, inventory)


def _verify_applied(
    plan: PermissionPlan,
    inventory: list[dict],
    *,
    migration_locked: bool = False,
) -> PermissionPlan:
    snapshot, system = _load_snapshots(
        plan.root,
        require_system=plan.nginx_user is not None,
        migration_locked=migration_locked,
    )
    verified = _build_permission_plan(snapshot, system)
    if (
        verified.noncompliant_directory_count
        or verified.noncompliant_file_count
    ):
        raise ConfigError("runtime ACL verification failed")
    _verify_metadata(verified, inventory, acl_may_change=True)
    return verified


def _backup_path(plan: PermissionPlan, value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    backups = (plan.root / ".backups").resolve()
    if path.parent != backups or not path.name.startswith("permissions-"):
        raise UnsafeConfigError(
            "permission backup is outside the private backup directory"
        )
    return path


def restore_permission_backup(
    root: str | Path,
    backup_id: str,
    *,
    migration_locked: bool = False,
    sync_locked: bool = False,
) -> PermissionPlan:
    """Restore one verified permission backup, optionally inside plan locks."""
    if migration_locked:
        initial_snapshot, initial_system = _load_snapshots(
            root,
            require_system=True,
            migration_locked=True,
        )
        initial = _build_permission_plan(initial_snapshot, initial_system)
    else:
        initial = plan_runtime_permissions(root, require_system=True)
    backup = _backup_path(initial, initial.root / ".backups" / backup_id)

    def restore() -> PermissionPlan:
        snapshot, system = _load_snapshots(
            initial.root,
            require_system=True,
            migration_locked=True,
        )
        plan = _build_permission_plan(snapshot, system)
        _restore_backup(plan, backup, remove_created=True)
        return _build_permission_plan(snapshot, system)

    if migration_locked and sync_locked:
        return restore()
    with _exclusive_lock(
        initial.root / MIGRATION_LOCK_NAME,
        "another installation migration holds the migration lock",
    ):
        with _exclusive_lock(
            initial.lock_file,
            "another sync process holds the shared sync lock",
        ):
            return restore()


def verify_permissions_applied(
    root: str | Path,
    backup_id: str,
    *,
    migration_locked: bool = False,
) -> PermissionPlan:
    """Revalidate the exact ACL set and its immutable backup reference."""
    snapshot, system = _load_snapshots(
        root,
        require_system=True,
        migration_locked=migration_locked,
    )
    plan = _build_permission_plan(snapshot, system)
    backup = _backup_path(plan, plan.root / ".backups" / backup_id)
    inventory = _load_inventory(backup)
    return _verify_applied(
        plan,
        inventory,
        migration_locked=migration_locked,
    )


def run_runtime_permissions(
    root: str | Path,
    *,
    apply: bool = False,
    restore: str | Path | None = None,
    system_manifest: str | Path | None = None,
    plan_id: str | None = None,
) -> PermissionResult:
    if apply and restore is not None:
        raise ConfigError("--apply and --restore are mutually exclusive")
    if system_manifest is not None:
        raise ConfigError(
            "--system-manifest is valid only with upgrade --prepare"
        )
    if apply and not plan_id:
        raise ConfigError("permissions --apply requires --plan-id")
    if restore is not None and plan_id is not None:
        raise ConfigError("--restore and --plan-id are mutually exclusive")
    _require_tools(restore=restore is not None)
    initial = plan_runtime_permissions(
        root,
        require_system=apply or restore is not None,
    )
    if not apply and restore is None:
        return PermissionResult(initial, False)

    from .versioning import (
        _transition_loaded_state,
        _write_plan,
        load_plan,
        verify_plan_post_data,
    )

    with _exclusive_lock(
        initial.root / MIGRATION_LOCK_NAME,
        "another installation migration holds the migration lock",
    ):
        with _exclusive_lock(
            initial.lock_file,
            "another sync process holds the shared sync lock",
        ):
            if not _timer_window_is_safe():
                raise ConfigError(
                    "next scheduler timer is less than five minutes away"
                )
            snapshot, system = _load_snapshots(
                initial.root,
                require_system=True,
                migration_locked=True,
            )
            plan = _build_permission_plan(snapshot, system)
            if restore is not None:
                backup = _backup_path(plan, restore)
                _restore_backup(plan, backup, remove_created=True)
                return PermissionResult(
                    _build_permission_plan(snapshot, system),
                    False,
                    True,
                    backup.name,
                )

            state = load_plan(initial.root, str(plan_id))
            if state["state"] == "permissions_applied":
                backup_id = state.get("permissions_backup_id")
                if not isinstance(backup_id, str):
                    raise ConfigError(
                        "permission plan backup reference is missing"
                    )
                verified = _verify_applied(
                    plan,
                    _load_inventory(initial.root / ".backups" / backup_id),
                    migration_locked=True,
                )
                return PermissionResult(
                    verified, True, False, backup_id
                )
            if state["state"] != "data_applied":
                raise ConfigError(
                    "permissions apply requires a data_applied plan"
                )
            verify_plan_post_data(initial.root, state)
            backup = _create_backup(plan)
            inventory = _load_inventory(backup)
            try:
                _apply_acl(plan)
                verified = _verify_applied(
                    plan,
                    inventory,
                    migration_locked=True,
                )
            except Exception as original:
                try:
                    _restore_backup(plan, backup, remove_created=True)
                except Exception as rollback:
                    state["state"] = "failed"
                    state["failure_category"] = "permissions-rollback"
                    _write_plan(initial.root, state)
                    raise ConfigError(
                        "runtime ACL operation failed and automatic rollback "
                        f"failed: {type(rollback).__name__}"
                    ) from original
                raise
            state = _transition_loaded_state(
                initial.root,
                state,
                expected="data_applied",
                new_state="permissions_applied",
                permissions_backup_id=backup.name,
            )
            return PermissionResult(
                verified,
                True,
                False,
                str(state["permissions_backup_id"]),
            )
