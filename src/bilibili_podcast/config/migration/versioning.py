"""Persistent, crash-resumable installation upgrade state machine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from ...locks import LockBusyError, LockKind, ordered_lock
from ...secure_files import (
    UnsafeFileError,
    atomic_write_bytes,
    ensure_directory,
    fsync_directory,
)
from ..manager import ConfigError, ConfigManager, MIGRATION_LOCK_NAME, UnsafeConfigError


EARLIEST_UNIFIED_VERSION = 1
PRE_VERSIONED_CURRENT = 2
LATEST_VERSION = 4
VERSION_FILE = ".bilibili-podcast-version"
ACTIVE_UPGRADE_SENTINEL = ".active-upgrade"
UPGRADE_STATE_DIR = ".upgrade"
PLAN_STATE_DIR = "plans"
CONFIG_FILES = (
    "app.toml",
    "sync.toml",
    "web.toml",
    "scheduler.toml",
    "publish.toml",
    "manual-media.toml",
    "rss-users.toml",
)
PLAN_STATES = (
    "prepared",
    "data_applied",
    "permissions_applied",
    "system_applied",
    "finalizing",
    "finalized",
    "failed",
    "rolled_back",
)
_ACTIVE_PLAN_STATES = set(PLAN_STATES) - {"finalized", "failed", "rolled_back"}
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{48}$")
_OLD_PRODUCT = "bili" + "pod"


@dataclass(frozen=True)
class VersionMigrationPlan:
    root: Path
    source_version: int
    target_version: int
    steps: tuple[str, ...]
    plan_id: str | None = None
    state: str | None = None


@dataclass(frozen=True)
class VersionMigrationResult:
    plan: VersionMigrationPlan
    applied: bool
    backup_root: Path | None = None


def _read_toml(path: Path) -> dict:
    if path.is_symlink():
        raise UnsafeConfigError(f"unsafe migration source {path}: symlink")
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot inspect migration source {path}: {type(exc).__name__}"
        ) from None
    if not isinstance(value, dict):
        raise ConfigError(f"invalid migration source {path}")
    return value


def _quote(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if not value.is_integer() else f"{value:.1f}"
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fsync_directory(path)


def _version_rows(db_path: Path) -> tuple[int, ...]:
    if not db_path.exists():
        return ()
    if db_path.is_symlink() or not db_path.is_file():
        raise UnsafeConfigError("unsafe migration database")
    try:
        with sqlite3.connect(db_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if not exists:
                return ()
            return tuple(
                sorted(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM schema_version"
                    )
                )
            )
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        raise ConfigError(
            f"cannot inspect migration database: {type(exc).__name__}"
        ) from None


def _validate_database_path(path: Path) -> None:
    if not path.is_absolute():
        raise ConfigError("migration database path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise UnsafeConfigError("migration database path contains a symlink")


def _configured_database(root: Path) -> Path:
    app = _read_toml(root / "app.toml")
    value = app.get("database", {}).get("path", "")
    if not isinstance(value, str) or not value:
        raise ConfigError("migration database path is missing")
    path = Path(value)
    _validate_database_path(path)
    return path


def detect_version(root: str | Path) -> int:
    config_root = Path(root).expanduser().resolve()
    missing = [name for name in CONFIG_FILES if not (config_root / name).is_file()]
    if missing:
        raise ConfigError(f"incomplete migration source: missing {missing[0]}")
    marker = config_root / VERSION_FILE
    database = _configured_database(config_root)
    rows = _version_rows(database)
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink():
            raise UnsafeConfigError("unsafe migration version marker: symlink")
        try:
            version = int(marker.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            raise ConfigError("invalid migration version marker") from None
        if not EARLIEST_UNIFIED_VERSION <= version <= LATEST_VERSION:
            raise ConfigError(f"unsupported migration source version: {version}")
        if database.exists() and rows != (version,):
            raise ConfigError("migration version mismatch between config and SQLite")
        return version

    app = _read_toml(config_root / "app.toml")
    web = _read_toml(config_root / "web.toml")
    publish = _read_toml(config_root / "publish.toml")
    executables = app.get("executables") or {}
    security = web.get("security") or {}
    settings = publish.get("publish") or {}
    old_key = f"{_OLD_PRODUCT}_config"
    if (
        old_key in executables
        or security.get("cookie_name") == f"{_OLD_PRODUCT}_session"
        or "script" in settings
    ):
        source = EARLIEST_UNIFIED_VERSION
    elif (
        "bilibili_podcast_config" in executables
        and "previous_cookie_names" in security
        and "gone_series" in settings
    ):
        source = PRE_VERSIONED_CURRENT
    else:
        raise ConfigError("unrecognized unversioned migration source")
    if database.exists() and rows and rows != (source,):
        raise ConfigError("migration version mismatch between config and SQLite")
    return source


def plan_upgrade(
    root: str | Path,
    *,
    target_version: int = LATEST_VERSION,
    plan_id: str | None = None,
) -> VersionMigrationPlan:
    if plan_id is not None:
        state = load_plan(root, plan_id)
        return _plan_from_state(Path(root).expanduser().resolve(), state)
    source = detect_version(root)
    if target_version < source or target_version > LATEST_VERSION:
        raise ConfigError(f"unsupported migration target version: {target_version}")
    missing = [
        version
        for version in range(source + 1, target_version + 1)
        if version not in _STEPS
    ]
    if missing:
        raise ConfigError(f"missing migration step for version {missing[0]}")
    return VersionMigrationPlan(
        Path(root).expanduser().resolve(),
        source,
        target_version,
        tuple(
            _STEPS[version][0]
            for version in range(source + 1, target_version + 1)
        ),
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
            if (
                isinstance(source_cookie, str)
                and source_cookie != "bilibili_podcast_session"
                and source_cookie not in previous
            ):
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
        atomic_write_bytes(path, _dump_toml(data).encode("utf-8"), mode=0o600)


def _upgrade_v2_to_v3(stage: Path) -> None:
    # v3 introduced the explicit marker at finalize; its TOML layout is
    # intentionally preserved as an independent historical snapshot.
    ConfigManager(stage, environ={}).load()


def _upgrade_v3_to_v4(stage: Path) -> None:
    app_path = stage / "app.toml"
    app = _read_toml(app_path)
    app.setdefault("paths", {}).setdefault(
        "fallback_log_dir",
        "/tmp/bilibili-podcast-logs",
    )
    app.setdefault("executables", {}).setdefault("ffprobe", "ffprobe")
    atomic_write_bytes(
        app_path,
        _dump_toml(app).encode("utf-8"),
        mode=0o600,
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if column not in columns:
        connection.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )


def _db_v1_to_v2(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection,
        "sync_policy",
        "update_period_grace_seconds",
        "INTEGER NOT NULL DEFAULT 120",
    )


def _db_v2_to_v3(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection,
        "cron_schedule",
        "kind",
        "TEXT NOT NULL DEFAULT 'primary'",
    )
    _ensure_column(
        connection,
        "sync_state",
        "retry_pending",
        "INTEGER NOT NULL DEFAULT 0",
    )


def _db_v3_to_v4(connection: sqlite3.Connection) -> None:
    _ensure_column(
        connection,
        "sync_policy",
        "media_mode",
        "TEXT NOT NULL DEFAULT 'auto'",
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_backend (
            series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
            backend TEXT NOT NULL DEFAULT 'cron'
                CHECK(backend IN ('cron','systemd'))
        )
        """
    )


_STEPS: dict[
    int,
    tuple[str, Callable[[Path], None], Callable[[sqlite3.Connection], None]],
] = {
    2: ("unified-v1-to-current-names", _upgrade_v1_to_v2, _db_v1_to_v2),
    3: ("initialize-versioned-installation", _upgrade_v2_to_v3, _db_v2_to_v3),
    4: ("apply-v4-security-contract", _upgrade_v3_to_v4, _db_v3_to_v4),
}


def schema_snapshot_path(version: int) -> Path:
    if not EARLIEST_UNIFIED_VERSION <= version <= LATEST_VERSION:
        raise ConfigError(f"unsupported schema snapshot: {version}")
    return Path(__file__).with_name("schemas") / f"v{version}.sql"


def _copy_configs(source: Path, stage: Path) -> None:
    for name in CONFIG_FILES:
        source_path = source / name
        if source_path.is_symlink() or source_path.stat().st_nlink != 1:
            raise UnsafeConfigError(f"unsafe migration source {name}")
        shutil.copy2(source_path, stage / name)


def _apply_config_steps(stage: Path, plan: VersionMigrationPlan) -> None:
    for version in range(plan.source_version + 1, plan.target_version + 1):
        _STEPS[version][1](stage)


def _apply_database_steps(
    database: Path,
    source_version: int,
    target_version: int,
) -> None:
    if not database.exists():
        return
    inode = database.stat().st_ino
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_version"
                )
            )
            if rows == () and source_version <= PRE_VERSIONED_CURRENT:
                pass
            elif rows != (source_version,):
                raise ConfigError("database version changed after prepare")
            for version in range(source_version + 1, target_version + 1):
                _STEPS[version][2](connection)
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ConfigError("migrated SQLite quick_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ConfigError("migrated SQLite foreign key check failed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    if database.stat().st_ino != inode:
        raise ConfigError("active database inode changed during migration")


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
        raise ConfigError(
            f"cannot back up migration database: {type(exc).__name__}"
        ) from None


def _restore_database_backup(backup: Path, target: Path) -> None:
    inode = target.stat().st_ino
    try:
        with sqlite3.connect(backup) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
    except sqlite3.DatabaseError as exc:
        raise ConfigError(
            f"cannot restore migration database: {type(exc).__name__}"
        ) from None
    if target.stat().st_ino != inode:
        raise ConfigError("database inode changed during restore")


def _write_manifest(backup_root: Path) -> None:
    files = sorted(
        path
        for path in backup_root.iterdir()
        if path.is_file()
        and path.name != "SHA256SUMS"
        and not path.name.endswith(("-wal", "-shm"))
    )
    payload = "".join(
        f"{_sha256_file(path)}  {path.name}\n"
        for path in files
    ).encode("ascii")
    manifest = backup_root / "SHA256SUMS"
    atomic_write_bytes(manifest, payload, mode=0o600)
    for path in files:
        _fsync_file(path)
    _fsync_file(manifest)
    _fsync_dir(backup_root)


def _stable_value(value):
    if isinstance(value, bytes):
        return {"blob_sha256": hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return repr(value)


def database_fingerprint(path: Path) -> dict:
    def sidecars() -> dict:
        result = {}
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.exists():
                metadata = sidecar.stat()
                result[suffix] = {
                    "exists": True,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "mode": metadata.st_mode,
                }
            else:
                result[suffix] = {"exists": False}
        return result

    if not path.exists():
        return {
            "exists": False,
            "device": None,
            "inode": None,
            "schema_digest": None,
            "table_digest": None,
            "data_digest": None,
            "table_counts": {},
            "sidecars": sidecars(),
        }
    _validate_database_path(path)
    metadata = path.stat()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ConfigError("SQLite quick_check failed")
            schema_rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            schema_payload = json.dumps(
                schema_rows,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            )
            counts: dict[str, int] = {}
            digest = hashlib.sha256()
            data_digest = hashlib.sha256()
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
                normalized = sorted(
                    json.dumps(
                        [_stable_value(value) for value in row],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    for row in rows
                )
                counts[table] = len(rows)
                digest.update(table.encode("utf-8"))
                if table != "schema_version":
                    data_digest.update(table.encode("utf-8"))
                for row in normalized:
                    digest.update(b"\0")
                    digest.update(row.encode("utf-8"))
                    if table != "schema_version":
                        data_digest.update(b"\0")
                        data_digest.update(row.encode("utf-8"))
    except sqlite3.DatabaseError as exc:
        raise ConfigError(
            f"cannot fingerprint migration database: {type(exc).__name__}"
        ) from None
    return {
        "exists": True,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "schema_digest": hashlib.sha256(schema_payload).hexdigest(),
        "table_digest": digest.hexdigest(),
        "data_digest": data_digest.hexdigest(),
        "table_counts": counts,
        "sidecars": sidecars(),
    }


def _config_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in CONFIG_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise UnsafeConfigError(f"unsafe configuration input {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _system_digest(root: Path) -> str | None:
    path = root / "system.toml"
    if not path.exists():
        return None
    ConfigManager(root, environ={}).load_system()
    return _sha256_file(path)


def _plan_root(root: Path) -> Path:
    return root / UPGRADE_STATE_DIR / PLAN_STATE_DIR


def _plan_path(root: Path, plan_id: str) -> Path:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise ConfigError("invalid migration plan id")
    return _plan_root(root) / f"{plan_id}.json"


def _write_plan(root: Path, state: dict) -> None:
    path = _plan_path(root, state["plan_id"])
    atomic_write_bytes(
        path,
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        mode=0o600,
    )


def load_plan(root: str | Path, plan_id: str) -> dict:
    resolved = Path(root).expanduser().resolve()
    path = _plan_path(resolved, plan_id)
    if path.is_symlink():
        raise UnsafeConfigError("unsafe migration plan state")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"cannot read migration plan: {type(exc).__name__}"
        ) from None
    if (
        not isinstance(state, dict)
        or state.get("format_version") != 1
        or state.get("plan_id") != plan_id
        or state.get("state") not in PLAN_STATES
        or state.get("target_version") != LATEST_VERSION
    ):
        raise ConfigError("invalid migration plan state")
    return state


def _active_plan_states(root: Path) -> Iterator[dict]:
    directory = _plan_root(root)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not _PLAN_ID_RE.fullmatch(path.stem):
            raise UnsafeConfigError("unsafe migration plan inventory")
        state = load_plan(root, path.stem)
        if state["state"] in _ACTIVE_PLAN_STATES:
            yield state


def _plan_from_state(root: Path, state: dict) -> VersionMigrationPlan:
    return VersionMigrationPlan(
        root,
        int(state["source_version"]),
        int(state["target_version"]),
        tuple(state["steps"]),
        state["plan_id"],
        state["state"],
    )


def prepare_upgrade(
    root: str | Path,
    *,
    system_manifest: str | Path | None = None,
) -> VersionMigrationPlan:
    resolved = Path(root).expanduser().resolve()
    with _migration_lock(resolved):
        active = next(_active_plan_states(resolved), None)
        if active is not None:
            raise ConfigError("another upgrade plan is active")
        if system_manifest is not None:
            ConfigManager(resolved, environ={}).import_system_manifest(system_manifest)
        preview = plan_upgrade(resolved)
        database = _configured_database(resolved)
        plan_id = secrets.token_hex(24)
        state = {
            "format_version": 1,
            "plan_id": plan_id,
            "state": "prepared",
            "source_version": preview.source_version,
            "target_version": preview.target_version,
            "steps": list(preview.steps),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "config_digest": _config_digest(resolved),
            "system_digest": _system_digest(resolved),
            "database": database_fingerprint(database),
            "backup_id": None,
            "post_data": None,
            "permissions_backup_id": None,
            "system_backup_id": None,
            "failure_category": None,
        }
        _write_plan(resolved, state)
        return _plan_from_state(resolved, state)


def _verify_prepared_inputs(root: Path, state: dict) -> Path:
    if _config_digest(root) != state["config_digest"]:
        raise ConfigError("configuration changed after upgrade prepare")
    if _system_digest(root) != state["system_digest"]:
        raise ConfigError("system manifest changed after upgrade prepare")
    database = _configured_database(root)
    if database_fingerprint(database) != state["database"]:
        raise ConfigError("database changed after upgrade prepare")
    if detect_version(root) != state["source_version"]:
        raise ConfigError("installation version changed after upgrade prepare")
    return database


@contextmanager
def _migration_lock(root: Path):
    try:
        with ordered_lock(root / MIGRATION_LOCK_NAME, LockKind.MIGRATION):
            yield
    except LockBusyError:
        raise ConfigError("another installation migration holds the migration lock") from None
    except UnsafeFileError:
        raise UnsafeConfigError(
            "unsafe migration lock: symlink or unsupported object"
        ) from None


@contextmanager
def _application_lock(path: Path):
    try:
        with ordered_lock(path, LockKind.SYNC):
            yield
    except LockBusyError:
        raise ConfigError("another application process holds the sync lock") from None
    except UnsafeFileError:
        raise UnsafeConfigError("unsafe application lock") from None


def _make_backup(
    root: Path,
    database: Path,
    state: dict,
) -> tuple[Path, str]:
    backup_id = (
        f"upgrade-{state['source_version']}-to-{state['target_version']}-"
        f"{uuid.uuid4().hex}"
    )
    backup = root / ".backups" / backup_id
    backup.mkdir(mode=0o700, parents=True)
    for name in (*CONFIG_FILES, VERSION_FILE):
        source = root / name
        if source.exists():
            shutil.copy2(source, backup / name)
    if database.exists():
        _online_database_backup(database, backup / "database.sqlite3")
    _write_manifest(backup)
    return backup, backup_id


def _restore_configs(root: Path, backup: Path) -> None:
    for name in (*CONFIG_FILES, VERSION_FILE):
        source = backup / name
        target = root / name
        if source.exists():
            temporary = root / ".temp" / f"restore-{uuid.uuid4().hex}-{name}"
            shutil.copy2(source, temporary)
            _fsync_file(temporary)
            os.replace(temporary, target)
        elif target.exists():
            target.unlink()
    _fsync_dir(root)


def apply_data_upgrade(root: str | Path, plan_id: str) -> VersionMigrationResult:
    resolved = Path(root).expanduser().resolve()
    state = load_plan(resolved, plan_id)
    if state["state"] == "data_applied":
        return VersionMigrationResult(
            _plan_from_state(resolved, state),
            True,
            resolved / ".backups" / state["backup_id"],
        )
    if state["state"] != "prepared":
        raise ConfigError("upgrade data apply requires a prepared plan")
    sync = _read_toml(resolved / "sync.toml")
    lock_path = Path(str(sync.get("paths", {}).get("lock_file", "")))
    if not lock_path.is_absolute():
        raise ConfigError("migration sync lock path must be absolute")
    with _migration_lock(resolved):
        with _application_lock(lock_path):
            state = load_plan(resolved, plan_id)
            database = _verify_prepared_inputs(resolved, state)
            temp_root = resolved / ".temp"
            ensure_directory(temp_root, mode=0o700)
            stage = Path(
                tempfile.mkdtemp(
                    prefix=f"upgrade-{plan_id}-",
                    dir=temp_root,
                )
            )
            backup: Path | None = None
            database_changed = False
            replaced: list[str] = []
            try:
                _copy_configs(resolved, stage)
                plan = _plan_from_state(resolved, state)
                _apply_config_steps(stage, plan)
                ConfigManager(stage, environ={}).load()
                backup, backup_id = _make_backup(resolved, database, state)
                if database.exists():
                    _apply_database_steps(
                        database,
                        plan.source_version,
                        plan.target_version,
                    )
                    database_changed = True
                for name in CONFIG_FILES:
                    staged = stage / name
                    _fsync_file(staged)
                    os.replace(staged, resolved / name)
                    replaced.append(name)
                _fsync_dir(resolved)
                sentinel = resolved / ACTIVE_UPGRADE_SENTINEL
                atomic_write_bytes(
                    sentinel,
                    f"{plan_id}\n".encode("ascii"),
                    mode=0o600,
                )
                state["state"] = "data_applied"
                state["backup_id"] = backup_id
                state["post_data"] = {
                    "config_digest": _config_digest(resolved),
                    "database": database_fingerprint(database),
                }
                _write_plan(resolved, state)
                return VersionMigrationResult(
                    _plan_from_state(resolved, state),
                    True,
                    backup,
                )
            except Exception:
                if backup is not None:
                    _restore_configs(resolved, backup)
                    database_backup = backup / "database.sqlite3"
                    if (
                        database_changed
                        and database.exists()
                        and database_backup.exists()
                    ):
                        _restore_database_backup(database_backup, database)
                (resolved / ACTIVE_UPGRADE_SENTINEL).unlink(missing_ok=True)
                raise
            finally:
                shutil.rmtree(stage, ignore_errors=True)


def update_plan_state(
    root: str | Path,
    plan_id: str,
    *,
    expected: str,
    new_state: str,
    **updates,
) -> dict:
    if new_state not in PLAN_STATES:
        raise ConfigError("invalid migration plan transition")
    resolved = Path(root).expanduser().resolve()
    with _migration_lock(resolved):
        state = load_plan(resolved, plan_id)
        if state["state"] == new_state:
            return state
        if state["state"] != expected:
            raise ConfigError(
                f"migration plan state is {state['state']}, expected {expected}"
            )
        return _transition_loaded_state(
            resolved,
            state,
            expected=expected,
            new_state=new_state,
            **updates,
        )


def _transition_loaded_state(
    root: Path,
    state: dict,
    *,
    expected: str,
    new_state: str,
    **updates,
) -> dict:
    """Persist one transition while the caller already holds migration lock."""
    if new_state not in PLAN_STATES:
        raise ConfigError("invalid migration plan transition")
    if state["state"] == new_state:
        return state
    if state["state"] != expected:
        raise ConfigError(
            f"migration plan state is {state['state']}, expected {expected}"
        )
    state.update(updates)
    state["state"] = new_state
    _write_plan(root, state)
    return state


def _verify_post_data(root: Path, state: dict) -> Path:
    post = state.get("post_data")
    if not isinstance(post, dict):
        raise ConfigError("upgrade post-data fingerprint is missing")
    if _config_digest(root) != post.get("config_digest"):
        raise ConfigError("configuration changed after data apply")
    database = _configured_database(root)
    current_database = database_fingerprint(database)
    expected_database = post.get("database")
    if state["state"] == "finalizing":
        stable_keys = (
            "exists",
            "device",
            "inode",
            "schema_digest",
            "data_digest",
        )
        if (
            not isinstance(expected_database, dict)
            or any(
                current_database.get(key) != expected_database.get(key)
                for key in stable_keys
            )
        ):
            raise ConfigError("database changed after data apply")
    elif current_database != expected_database:
        raise ConfigError("database changed after data apply")
    sentinel = root / ACTIVE_UPGRADE_SENTINEL
    if sentinel.exists():
        if (
            not sentinel.is_file()
            or sentinel.read_text(encoding="ascii").strip() != state["plan_id"]
        ):
            raise ConfigError("active upgrade sentinel is invalid")
    elif state["state"] != "finalizing":
        raise ConfigError("active upgrade sentinel is missing")
    return database


def verify_plan_post_data(root: str | Path, state: dict) -> Path:
    """Verify immutable post-data inputs for privileged plan steps."""
    return _verify_post_data(Path(root).expanduser().resolve(), state)


def finalize_upgrade(root: str | Path, plan_id: str, *, apply: bool) -> VersionMigrationPlan:
    resolved = Path(root).expanduser().resolve()
    state = load_plan(resolved, plan_id)
    if not apply:
        if state["state"] not in {"system_applied", "finalizing", "finalized"}:
            raise ConfigError("finalize has pending upgrade steps")
        return _plan_from_state(resolved, state)
    sync = _read_toml(resolved / "sync.toml")
    lock_path = Path(str(sync.get("paths", {}).get("lock_file", "")))
    with _migration_lock(resolved):
        with _application_lock(lock_path):
            state = load_plan(resolved, plan_id)
            if state["state"] == "finalized":
                return _plan_from_state(resolved, state)
            if state["state"] not in {"system_applied", "finalizing"}:
                raise ConfigError("finalize has pending upgrade steps")
            database = _verify_post_data(resolved, state)
            permissions_backup_id = state.get("permissions_backup_id")
            system_backup_id = state.get("system_backup_id")
            if not isinstance(permissions_backup_id, str):
                raise ConfigError("finalize permission backup reference is missing")
            if not isinstance(system_backup_id, str):
                raise ConfigError("finalize system backup reference is missing")
            from .runtime_permissions import verify_permissions_applied
            from .system_upgrade import verify_system_applied

            verify_permissions_applied(
                resolved,
                permissions_backup_id,
                migration_locked=True,
            )
            verify_system_applied(
                resolved,
                system_backup_id,
                migration_locked=True,
            )
            if state["state"] != "finalizing":
                state["state"] = "finalizing"
                _write_plan(resolved, state)
            if database.exists():
                with sqlite3.connect(database) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM schema_version")
                    connection.execute(
                        "INSERT INTO schema_version(version) VALUES(?)",
                        (LATEST_VERSION,),
                    )
                    connection.commit()
            atomic_write_bytes(
                resolved / VERSION_FILE,
                f"{LATEST_VERSION}\n".encode("ascii"),
                mode=0o600,
            )
            if detect_version(resolved) != LATEST_VERSION:
                raise ConfigError("finalized installation version verification failed")
            (resolved / ACTIVE_UPGRADE_SENTINEL).unlink()
            _fsync_dir(resolved)
            state["state"] = "finalized"
            state["finalized_at"] = datetime.now(timezone.utc).isoformat()
            _write_plan(resolved, state)
            return _plan_from_state(resolved, state)


def rollback_upgrade(root: str | Path, plan_id: str, *, apply: bool) -> VersionMigrationPlan:
    resolved = Path(root).expanduser().resolve()
    state = load_plan(resolved, plan_id)
    if state["state"] == "finalized":
        raise ConfigError("finalized upgrade cannot be rolled back")
    if state["state"] == "rolled_back":
        return _plan_from_state(resolved, state)
    if not apply:
        return _plan_from_state(resolved, state)
    sync = _read_toml(resolved / "sync.toml")
    lock_path = Path(str(sync.get("paths", {}).get("lock_file", "")))
    with _migration_lock(resolved):
        with _application_lock(lock_path):
            state = load_plan(resolved, plan_id)
            try:
                system_backup_id = state.get("system_backup_id")
                if system_backup_id:
                    from .system_upgrade import restore_system_backup

                    restore_system_backup(
                        resolved,
                        system_backup_id,
                        migration_locked=True,
                        sync_locked=True,
                    )
                permissions_backup_id = state.get("permissions_backup_id")
                if permissions_backup_id:
                    from .runtime_permissions import restore_permission_backup

                    restore_permission_backup(
                        resolved,
                        permissions_backup_id,
                        migration_locked=True,
                        sync_locked=True,
                    )
                backup_id = state.get("backup_id")
                if backup_id:
                    backup = resolved / ".backups" / backup_id
                    manifest = backup / "SHA256SUMS"
                    if not manifest.is_file():
                        raise ConfigError("upgrade rollback backup is incomplete")
                    database = _configured_database(resolved)
                    database_backup = backup / "database.sqlite3"
                    if database.exists() and database_backup.exists():
                        _restore_database_backup(database_backup, database)
                    _restore_configs(resolved, backup)
                (resolved / ACTIVE_UPGRADE_SENTINEL).unlink(missing_ok=True)
                state["state"] = "rolled_back"
                state["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
                _write_plan(resolved, state)
                return _plan_from_state(resolved, state)
            except Exception:
                state["state"] = "failed"
                state["failure_category"] = "rollback"
                _write_plan(resolved, state)
                raise


def status_upgrade(root: str | Path, plan_id: str | None = None) -> VersionMigrationPlan:
    resolved = Path(root).expanduser().resolve()
    if plan_id is not None:
        return _plan_from_state(resolved, load_plan(resolved, plan_id))
    active = tuple(_active_plan_states(resolved))
    if len(active) > 1:
        raise ConfigError("multiple active upgrade plans were found")
    if active:
        return _plan_from_state(resolved, active[0])
    plans = sorted(_plan_root(resolved).glob("*.json")) if _plan_root(resolved).exists() else []
    if not plans:
        return plan_upgrade(resolved)
    return _plan_from_state(resolved, load_plan(resolved, plans[-1].stem))


def upgrade_installation(
    root: str | Path,
    *,
    apply: bool = False,
    target_version: int = LATEST_VERSION,
    plan_id: str | None = None,
) -> VersionMigrationResult:
    """Compatibility facade for preview and prepared data apply."""
    if not apply:
        plan = (
            plan_upgrade(root, target_version=target_version)
            if plan_id is None
            else _plan_from_state(
                Path(root).expanduser().resolve(),
                load_plan(root, plan_id),
            )
        )
        stage = Path(tempfile.mkdtemp(prefix="bilibili-podcast-upgrade-preview-"))
        try:
            _copy_configs(plan.root, stage)
            _apply_config_steps(stage, plan)
            ConfigManager(stage, environ={}).load()
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return VersionMigrationResult(plan, False, None)
    if plan_id is None:
        raise ConfigError("upgrade --apply requires a prepared --plan-id")
    return apply_data_upgrade(root, plan_id)
