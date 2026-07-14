"""Versioned installation upgrades from every registered historical format."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sqlite3
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..manager import ConfigError, ConfigManager, MIGRATION_LOCK_NAME, UnsafeConfigError


EARLIEST_UNIFIED_VERSION = 1
PRE_VERSIONED_CURRENT = 2
LATEST_VERSION = 3
VERSION_FILE = ".bilibili-podcast-version"
CONFIG_FILES = (
    "app.toml", "sync.toml", "web.toml", "scheduler.toml",
    "publish.toml", "manual-media.toml", "rss-users.toml",
)
_OLD_PRODUCT = "bili" + "pod"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class VersionMigrationPlan:
    root: Path
    source_version: int
    target_version: int
    steps: tuple[str, ...]


@dataclass(frozen=True)
class VersionMigrationResult:
    plan: VersionMigrationPlan
    applied: bool
    backup_root: Path | None = None


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot inspect migration source {path}: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise ConfigError(f"invalid migration source {path}")
    return value


def _quote(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not value.is_integer():
            return str(value)
        return f"{value:.1f}"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_quote(item) for item in value) + "]"
    raise ConfigError(f"unsupported migration value type: {type(value).__name__}")


def _quote_key(value: object) -> str:
    text = str(value)
    if text and all(character.isalnum() or character in "_-" for character in text):
        return text
    return _quote(text)


def _dump_toml(data: dict) -> str:
    lines: list[str] = []

    def emit(table: dict, prefix: tuple[str, ...]) -> None:
        scalars = [(key, value) for key, value in table.items() if not isinstance(value, dict)]
        children = [(key, value) for key, value in table.items() if isinstance(value, dict)]
        if prefix:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_quote_key(part) for part in prefix) + "]")
        lines.extend(f"{_quote_key(key)} = {_quote(value)}" for key, value in scalars)
        for key, child in children:
            emit(child, (*prefix, str(key)))

    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{_quote_key(key)} = {_quote(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            emit(value, (str(key),))
    return "\n".join(lines) + "\n"


def _version_rows(db_path: Path) -> tuple[int, ...]:
    if not db_path.exists():
        return ()
    try:
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                return ()
            return tuple(sorted(int(row[0]) for row in conn.execute("SELECT version FROM schema_version")))
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise ConfigError(f"cannot inspect migration database {db_path}: {type(exc).__name__}") from None


def _validate_database_path(db_path: Path) -> None:
    if not db_path.is_absolute():
        raise ConfigError("migration database path must be absolute")
    if db_path.is_symlink():
        raise UnsafeConfigError(f"unsafe migration database {db_path}: symlink")


def detect_version(root: str | Path) -> int:
    config_root = Path(root).expanduser().resolve()
    missing = [name for name in CONFIG_FILES if not (config_root / name).is_file()]
    if missing:
        raise ConfigError(f"incomplete migration source {config_root}: missing {missing[0]}")
    marker = config_root / VERSION_FILE
    if marker.is_symlink():
        raise UnsafeConfigError(f"unsafe migration version marker {marker}: symlink")
    if marker.exists():
        try:
            version = int(marker.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            raise ConfigError(f"invalid migration version marker {marker}") from None
        if version < EARLIEST_UNIFIED_VERSION or version > LATEST_VERSION:
            raise ConfigError(f"unsupported migration source version: {version}")
        app = _read_toml(config_root / "app.toml")
        db_path = Path(str(app.get("database", {}).get("path", "")))
        _validate_database_path(db_path)
        rows = _version_rows(db_path)
        if rows and rows != (version,):
            raise ConfigError("migration version mismatch between config and SQLite")
        if version == LATEST_VERSION and db_path.exists() and rows != (LATEST_VERSION,):
            raise ConfigError("migration version mismatch between config and SQLite")
        return version

    app = _read_toml(config_root / "app.toml")
    db_path = Path(str(app.get("database", {}).get("path", "")))
    _validate_database_path(db_path)
    web = _read_toml(config_root / "web.toml")
    publish = _read_toml(config_root / "publish.toml")
    executables = app.get("executables") or {}
    security = web.get("security") or {}
    settings = publish.get("publish") or {}
    old_key = f"{_OLD_PRODUCT}_config"
    if old_key in executables or security.get("cookie_name") == f"{_OLD_PRODUCT}_session" or "script" in settings:
        return EARLIEST_UNIFIED_VERSION
    if (
        "bilibili_podcast_config" in executables
        and "previous_cookie_names" in security
        and "gone_series" in settings
    ):
        return PRE_VERSIONED_CURRENT
    raise ConfigError("unrecognized unversioned migration source")


def plan_upgrade(root: str | Path, *, target_version: int = LATEST_VERSION) -> VersionMigrationPlan:
    source = detect_version(root)
    if target_version < source or target_version > LATEST_VERSION:
        raise ConfigError(f"unsupported migration target version: {target_version}")
    missing = [version for version in range(source + 1, target_version + 1) if version not in _STEPS]
    if missing:
        raise ConfigError(f"missing migration step for version {missing[0]}")
    return VersionMigrationPlan(
        Path(root).expanduser().resolve(), source, target_version,
        tuple(_STEPS[version][0] for version in range(source + 1, target_version + 1)),
    )


def _replace_command_name(value: object) -> object:
    if not isinstance(value, str):
        return value
    path = Path(value)
    replacement = path.name.replace(_OLD_PRODUCT, "bilibili-podcast")
    return str(path.with_name(replacement)) if replacement != path.name else value


def _upgrade_v1_to_v2(stage: Path) -> None:
    for name in CONFIG_FILES:
        path = stage / name
        data = _read_toml(path)
        if name == "app.toml":
            executables = data.setdefault("executables", {})
            old_key = f"{_OLD_PRODUCT}_config"
            old_value = executables.pop(old_key, None)
            if old_value is not None:
                executables["bilibili_podcast_config"] = _replace_command_name(old_value)
            if "sync" in executables:
                executables["sync"] = _replace_command_name(executables["sync"])
        elif name == "sync.toml":
            data.setdefault("timeouts", {}).setdefault("sync_seconds", 300)
        elif name == "web.toml":
            security = data.setdefault("security", {})
            previous = list(security.get("previous_cookie_names") or [])
            source_cookie = security.get("cookie_name")
            if isinstance(source_cookie, str) and source_cookie != "bilibili_podcast_session":
                if source_cookie not in previous:
                    previous.append(source_cookie)
            security["cookie_name"] = "bilibili_podcast_session"
            security["previous_cookie_names"] = previous
        elif name == "scheduler.toml":
            runtime = data.setdefault("runtime", {})
            for field in ("user", "group"):
                if runtime.get(field) == _OLD_PRODUCT:
                    runtime[field] = "bilibili-podcast"
            units = data.setdefault("units", {})
            for field in ("web", "sync_glob"):
                if field in units:
                    units[field] = _replace_command_name(units[field])
        elif name == "publish.toml":
            settings = data.setdefault("publish", {})
            settings.pop("script", None)
            settings["master_placeholder"] = "__MEDIA_PLACEHOLDER__"
            settings.setdefault("gone_series", [])
        path.write_text(_dump_toml(data), encoding="utf-8")
        path.chmod(0o600)


def _upgrade_v2_to_v3(stage: Path) -> None:
    (stage / VERSION_FILE).write_text("3\n", encoding="ascii")
    (stage / VERSION_FILE).chmod(0o600)


_STEPS: dict[int, tuple[str, Callable[[Path], None]]] = {
    2: ("unified-v1-to-current-names", _upgrade_v1_to_v2),
    3: ("initialize-versioned-installation", _upgrade_v2_to_v3),
}


def _copy_configs(source: Path, stage: Path) -> None:
    for name in CONFIG_FILES:
        source_path = source / name
        if source_path.is_symlink():
            raise UnsafeConfigError(f"unsafe migration source {source_path}: symlink")
        shutil.copy2(source_path, stage / name)


def _online_database_backup(source_db: Path, target: Path) -> None:
    try:
        with sqlite3.connect(source_db) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        with sqlite3.connect(target) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ConfigError("SQLite online backup quick_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ConfigError("SQLite online backup foreign key check failed")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.DatabaseError as exc:
        raise ConfigError(f"cannot back up migration database: {type(exc).__name__}") from None


def _restore_database_backup(backup: Path, target: Path) -> None:
    try:
        with sqlite3.connect(backup) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
    except sqlite3.DatabaseError as exc:
        raise ConfigError(f"cannot restore migration database: {type(exc).__name__}") from None


def _set_database_version(db_path: Path, version: int) -> None:
    from ... import db

    db.migrate(db_path, initialize_version=False)
    with db.transaction(db_path) as conn:
        rows = tuple(row[0] for row in conn.execute("SELECT version FROM schema_version"))
        if any(current > version for current in rows):
            raise ConfigError("SQLite belongs to a future migration version")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES(?)", (version,))
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConfigError("migrated SQLite quick_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ConfigError("migrated SQLite foreign key check failed")


def _validate_staged_database(stage: Path, source_db: Path, version: int) -> None:
    _validate_database_path(source_db)
    if not source_db.exists():
        return
    target = stage / source_db.name
    _online_database_backup(source_db, target)
    _set_database_version(target, version)


def _write_manifest(backup_root: Path) -> None:
    manifest_files = sorted(
        path for path in backup_root.iterdir()
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    )
    manifest = backup_root / "SHA256SUMS"
    manifest.write_text("".join(
        f"{_sha256_file(path)}  {path.name}\n"
        for path in manifest_files
    ), encoding="ascii")
    manifest.chmod(0o600)
    checksums = {
        name: digest
        for digest, name in (
            line.split("  ", 1)
            for line in manifest.read_text(encoding="ascii").splitlines()
        )
    }
    for path in manifest_files:
        if checksums.get(path.name) != _sha256_file(path):
            raise ConfigError(f"migration backup checksum verification failed: {path.name}")
        _fsync_file(path)
    _fsync_file(manifest)
    _fsync_dir(backup_root)


@contextmanager
def _migration_lock(root: Path):
    path = root / MIGRATION_LOCK_NAME
    if path.is_symlink():
        raise UnsafeConfigError(f"unsafe migration lock {path}: symlink")
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError(f"another installation migration holds {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _application_lock(path: Path):
    """Exclude active sync/admin writers while mutating the live database."""
    if path.is_symlink():
        raise UnsafeConfigError(f"unsafe application lock {path}: symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError(f"another application process holds {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _apply_steps(stage: Path, plan: VersionMigrationPlan) -> None:
    for version in range(plan.source_version + 1, plan.target_version + 1):
        _STEPS[version][1](stage)


def upgrade_installation(
    root: str | Path,
    *,
    apply: bool = False,
    target_version: int = LATEST_VERSION,
) -> VersionMigrationResult:
    initial_plan = plan_upgrade(root, target_version=target_version)
    if not initial_plan.steps:
        return VersionMigrationResult(initial_plan, apply, None)
    root_path = initial_plan.root

    if not apply:
        stage = Path(tempfile.mkdtemp(prefix="bilibili-podcast-version-migrate-"))
        try:
            _copy_configs(root_path, stage)
            _apply_steps(stage, initial_plan)
            ConfigManager(stage, environ={}).load()
            source_app = _read_toml(root_path / "app.toml")
            source_db = Path(str(source_app.get("database", {}).get("path", "")))
            _validate_staged_database(stage, source_db, initial_plan.target_version)
            return VersionMigrationResult(initial_plan, False, None)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    backup_root: Path | None = None
    replaced: list[tuple[Path, Path | None]] = []
    database_version_changed = False
    source_db: Path | None = None
    with _migration_lock(root_path):
        plan = plan_upgrade(root_path, target_version=target_version)
        if plan.source_version != initial_plan.source_version:
            raise ConfigError("migration source changed after planning")
        source_sync = _read_toml(root_path / "sync.toml")
        lock_path = Path(str(source_sync.get("paths", {}).get("lock_file", "")))
        if not lock_path.is_absolute():
            raise ConfigError("migration application lock path must be absolute")
        with _application_lock(lock_path):
            stage = Path(tempfile.mkdtemp(prefix="bilibili-podcast-version-migrate-"))
            try:
                _copy_configs(root_path, stage)
                _apply_steps(stage, plan)
                ConfigManager(stage, environ={}).load()
                for name in (*CONFIG_FILES, VERSION_FILE):
                    staged = stage / name
                    if staged.exists():
                        _fsync_file(staged)
                _fsync_dir(stage)
                source_app = _read_toml(root_path / "app.toml")
                source_db = Path(str(source_app.get("database", {}).get("path", "")))
                _validate_database_path(source_db)

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_root = root_path / ".backups" / f"version-{plan.source_version}-to-{plan.target_version}-{stamp}-{uuid.uuid4().hex[:8]}"
                backup_root.mkdir(mode=0o700, parents=True)
                config_replacements: list[tuple[Path, Path, Path | None]] = []
                for name in (*CONFIG_FILES, VERSION_FILE):
                    target = root_path / name
                    if target.is_symlink():
                        raise UnsafeConfigError(f"unsafe migration target {target}: symlink")
                    staged = stage / name
                    if not staged.exists():
                        continue
                    backup = None
                    if target.exists():
                        backup = backup_root / name
                        shutil.copy2(target, backup)
                    config_replacements.append((target, staged, backup))
                if source_db.exists():
                    db_backup = backup_root / source_db.name
                    if db_backup.exists():
                        raise ConfigError(f"migration backup name collision: {db_backup.name}")
                    _online_database_backup(source_db, db_backup)
                    previous_db_versions = _version_rows(source_db)
                    if any(version > plan.target_version for version in previous_db_versions):
                        raise ConfigError("SQLite belongs to a future migration version")
                _write_manifest(backup_root)

                if source_db.exists():
                    database_version_changed = True
                    _set_database_version(source_db, plan.target_version)
                for target, staged, backup in config_replacements:
                    staged.replace(target)
                    replaced.append((target, backup))
                _fsync_dir(root_path)
                return VersionMigrationResult(plan, True, backup_root)
            except Exception:
                for target, backup in reversed(replaced):
                    if backup is None:
                        target.unlink(missing_ok=True)
                    else:
                        shutil.copy2(backup, target)
                _fsync_dir(root_path)
                if database_version_changed and source_db is not None and source_db.exists():
                    db_backup = backup_root / source_db.name if backup_root is not None else None
                    if db_backup is not None and db_backup.exists():
                        _restore_database_backup(db_backup, source_db)
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)
