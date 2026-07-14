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

from ..manager import ConfigError, ConfigManager, UnsafeConfigError


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


def detect_version(root: str | Path) -> int:
    config_root = Path(root).expanduser().resolve()
    missing = [name for name in CONFIG_FILES if not (config_root / name).is_file()]
    if missing:
        raise ConfigError(f"incomplete migration source {config_root}: missing {missing[0]}")
    marker = config_root / VERSION_FILE
    if marker.exists():
        if marker.is_symlink():
            raise UnsafeConfigError(f"unsafe migration version marker {marker}: symlink")
        try:
            version = int(marker.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            raise ConfigError(f"invalid migration version marker {marker}") from None
        if version < EARLIEST_UNIFIED_VERSION or version > LATEST_VERSION:
            raise ConfigError(f"unsupported migration source version: {version}")
        app = _read_toml(config_root / "app.toml")
        db_path = Path(str(app.get("database", {}).get("path", "")))
        rows = _version_rows(db_path)
        if rows and rows != (version,):
            raise ConfigError("migration version mismatch between config and SQLite")
        if version == LATEST_VERSION and db_path.exists() and rows != (LATEST_VERSION,):
            raise ConfigError("migration version mismatch between config and SQLite")
        return version

    app = _read_toml(config_root / "app.toml")
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


def _replace_product(value: object) -> object:
    if isinstance(value, str):
        return value.replace(_OLD_PRODUCT, "bilibili-podcast").replace(
            _OLD_PRODUCT.upper(), "BILIBILI_PODCAST"
        )
    if isinstance(value, list):
        return [_replace_product(item) for item in value]
    if isinstance(value, dict):
        return {str(_replace_product(key)): _replace_product(item) for key, item in value.items()}
    return value


def _upgrade_v1_to_v2(stage: Path) -> None:
    for name in CONFIG_FILES:
        path = stage / name
        source = _read_toml(path)
        data = _replace_product(source)
        if name == "app.toml":
            executables = data.setdefault("executables", {})
            old_key = f"{_OLD_PRODUCT}_config"
            replaced_key = "bilibili-podcast_config"
            old_value = executables.pop(old_key, executables.pop(replaced_key, None))
            if old_value is not None:
                executables["bilibili_podcast_config"] = str(old_value).replace(
                    _OLD_PRODUCT, "bilibili-podcast"
                )
        elif name == "sync.toml":
            data.setdefault("timeouts", {}).setdefault("sync_seconds", 300)
        elif name == "web.toml":
            security = data.setdefault("security", {})
            old_cookie = f"{_OLD_PRODUCT}_session"
            previous = list(security.get("previous_cookie_names") or [])
            source_cookie = (source.get("security") or {}).get("cookie_name")
            if source_cookie == old_cookie and old_cookie not in previous:
                previous.append(old_cookie)
            security["cookie_name"] = "bilibili_podcast_session"
            security["previous_cookie_names"] = previous
        elif name == "publish.toml":
            settings = data.setdefault("publish", {})
            settings.pop("script", None)
            settings["master_placeholder"] = "__MEDIA_PLACEHOLDER__"
            settings.setdefault("gone_series", [])
        path.write_text(_dump_toml(data), encoding="utf-8")
        path.chmod(0o600)


def _upgrade_v2_to_v3(stage: Path) -> None:
    (stage / VERSION_FILE).write_text(f"{LATEST_VERSION}\n", encoding="ascii")
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


def _migrate_database(
    stage: Path,
    *,
    source_db: Path,
    schema_version: int,
    staged_db: Path | None = None,
) -> Path | None:
    from ... import db

    app = _read_toml(stage / "app.toml")
    target_db = Path(str(app.get("database", {}).get("path", "")))
    if not source_db.is_absolute() or not target_db.is_absolute():
        raise ConfigError("migration database path must be absolute")
    if not source_db.exists():
        return None
    target = staged_db or stage / target_db.name
    with sqlite3.connect(source_db) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    db.migrate(target)
    with db.transaction(target) as conn:
        rows = tuple(row[0] for row in conn.execute("SELECT version FROM schema_version"))
        if any(version > schema_version for version in rows):
            raise ConfigError("SQLite belongs to a future migration version")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES(?)", (schema_version,))
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConfigError("migrated SQLite quick_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ConfigError("migrated SQLite foreign key check failed")
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    return target


@contextmanager
def _migration_lock(root: Path):
    path = root / ".migration.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError(f"another installation migration holds {path}") from None
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
    plan = plan_upgrade(root, target_version=target_version)
    if not plan.steps:
        return VersionMigrationResult(plan, apply, None)
    root_path = plan.root
    source_app = _read_toml(root_path / "app.toml")
    source_db = Path(str(source_app.get("database", {}).get("path", "")))
    stage = Path(tempfile.mkdtemp(prefix="bilibili-podcast-version-migrate-"))
    backup_root: Path | None = None
    staged_db: Path | None = None
    live_db: Path | None = None
    replaced: list[tuple[Path, Path | None]] = []
    try:
        _copy_configs(root_path, stage)
        _apply_steps(stage, plan)
        ConfigManager(stage, environ={}).load()
        if not apply:
            _migrate_database(stage, source_db=source_db, schema_version=plan.target_version)
            return VersionMigrationResult(plan, False, None)

        with _migration_lock(root_path):
            if detect_version(root_path) != plan.source_version:
                raise ConfigError("migration source changed after planning")
            app = _read_toml(stage / "app.toml")
            live_db = Path(str(app.get("database", {}).get("path", "")))
            if source_db.exists():
                if live_db != source_db and live_db.exists():
                    raise ConfigError(f"migration database target already exists: {live_db}")
                live_db.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=f".{live_db.name}.migration-", dir=live_db.parent)
                os.close(fd)
                staged_db = Path(name)
                _migrate_database(
                    stage,
                    source_db=source_db,
                    schema_version=plan.target_version,
                    staged_db=staged_db,
                )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_root = root_path / ".backups" / f"version-{plan.source_version}-to-{plan.target_version}-{stamp}-{uuid.uuid4().hex[:8]}"
            backup_root.mkdir(mode=0o700, parents=True)
            config_replacements: list[tuple[Path, Path, Path | None]] = []
            for name in (*CONFIG_FILES, VERSION_FILE):
                target = root_path / name
                source = stage / name
                if not source.exists():
                    continue
                backup = None
                if target.exists():
                    backup = backup_root / name
                    shutil.copy2(target, backup)
                config_replacements.append((target, source, backup))
            db_backup: Path | None = None
            if staged_db is not None and live_db is not None:
                backup_source = live_db if live_db.exists() else source_db
                db_backup = backup_root / backup_source.name
                if db_backup.exists():
                    raise ConfigError(f"migration backup name collision: {db_backup.name}")
                shutil.copy2(backup_source, db_backup)
            manifest_files = sorted(path for path in backup_root.iterdir() if path.is_file())
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
            for target, source, backup in config_replacements:
                source.replace(target)
                replaced.append((target, backup))
            if staged_db is not None and live_db is not None and db_backup is not None:
                staged_db.replace(live_db)
                replaced.append((live_db, db_backup if live_db == source_db else None))
                staged_db = None
        return VersionMigrationResult(plan, True, backup_root)
    except Exception:
        for target, backup in reversed(replaced):
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(backup, target)
        raise
    finally:
        if staged_db is not None:
            staged_db.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
