"""Legacy env/YAML/RSS-user migration with dry-run by default."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..manager import ConfigError, ConfigManager, UnsafeConfigError
from ..models import SeriesConfig
from ..repositories import LegacyYamlRepository
from ..schema import QUALITY_ALIASES, REMOVED_LEGACY_ENV


_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_OLD_ENV_PREFIX = ("BILI" + "POD") + "_"
_CURRENT_ENV_PREFIX = "BILIBILI_PODCAST_"


@dataclass(frozen=True)
class MigrationResult:
    output_root: Path
    files: tuple[Path, ...]
    series_count: int
    normalizations: tuple[str, ...]
    applied: bool


@dataclass(frozen=True)
class MigratedSeries:
    config: SeriesConfig
    schedules: tuple[str, ...]
    retry_schedules: tuple[str, ...]


def read_legacy_env(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"legacy env file does not exist: {source}")
    result: dict[str, str] = {}
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read legacy env file {source}: {type(exc).__name__}") from None
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.fullmatch(line)
        if not match:
            raise ConfigError(f"unsupported legacy env syntax {source}:{line_number}")
        key, value = match.groups()
        if key.startswith(_OLD_ENV_PREFIX):
            key = _CURRENT_ENV_PREFIX + key.removeprefix(_OLD_ENV_PREFIX)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if "$" in value or "`" in value:
            raise ConfigError(f"dynamic legacy env value is not supported {source}:{line_number}")
        result[key] = value
    return result


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise ConfigError(f"legacy migration is missing required field {key}")
    return value


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError("legacy migration contains an invalid boolean")


def _number(value: str, field: str, kind: type[int] | type[float]):
    try:
        return kind(value)
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"legacy migration contains an invalid number for {field}") from None


def _quote(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_quote(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{text}"'


def _toml(sections: list[tuple[str, Mapping[str, object]]]) -> str:
    lines: list[str] = []
    for section, values in sections:
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {_quote(value)}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def _parse_rss_users(path: str | Path | None) -> list[tuple[str, str, list[str]]]:
    if path is None:
        return []
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"legacy RSS users file does not exist: {source}")
    users = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read legacy RSS users file {source}: {type(exc).__name__}") from None
    for line_number, raw in enumerate(lines, 1):
        content = raw.split("#", 1)[0].strip()
        if not content:
            continue
        if ":" not in content:
            raise ConfigError(f"invalid legacy RSS user entry {source}:{line_number}")
        token, raw_series = content.split(":", 1)
        series = [item.strip() for item in raw_series.split(",") if item.strip()]
        if not token.strip() or not series:
            raise ConfigError(f"invalid legacy RSS user entry {source}:{line_number}")
        users.append((f"user_{len(users) + 1}", token.strip(), series))
    return users


def _rss_users_toml(users: list[tuple[str, str, list[str]]]) -> str:
    if not users:
        return "# No legacy RSS users were found.\n"
    chunks = []
    for name, token, series in users:
        chunks.append(_toml([(f"users.{name}", {"token": token, "series": series})]).strip())
    return "\n\n".join(chunks) + "\n"


def _normalize_legacy_series(root: Path) -> tuple[list[MigratedSeries], tuple[str, ...]]:
    repository = LegacyYamlRepository(root)
    configs = []
    normalizations: list[str] = []
    for path in repository.paths():
        try:
            raw = repository.read_raw(path)
            raw_sync = raw.get("sync") or {}
            config = SeriesConfig.from_yaml(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ConfigError(
                f"invalid legacy series configuration {path}: {type(exc).__name__}"
            ) from None
        quality = raw_sync.get("quality")
        if quality in QUALITY_ALIASES:
            normalized = QUALITY_ALIASES[quality]
            normalizations.append(f"{config.series}: sync.quality {quality} -> {normalized}")
        wait = raw_sync.get("browser_wait_seconds")
        if wait is not None:
            normalizations.append(f"{config.series}: sync.browser_wait_seconds -> min/max")
        cron = raw.get("cron") or {}
        schedules = tuple(cron.get("schedules", [])) if isinstance(cron, dict) else ()
        retry_schedules = tuple(cron.get("retry_schedules", [])) if isinstance(cron, dict) else ()
        configs.append(MigratedSeries(config, schedules, retry_schedules))
    return configs, tuple(normalizations)


def _write_migrated_series(path: Path, configs: list[MigratedSeries]) -> None:
    from ... import db
    from ...services.scheduler_service import replace_schedules_in_connection

    db.migrate(path)
    with db.transaction(path) as conn:
        for migrated in configs:
            config = migrated.config
            db.upsert_series(conn, config)
            db.upsert_source(conn, config)
            db.upsert_sync_policy(conn, config)
            db.upsert_filters(conn, config)
            db.upsert_paid_preview(conn, config)
            replace_schedules_in_connection(
                conn, config.series,
                list(migrated.schedules), list(migrated.retry_schedules),
            )


def migrate_legacy(
    *,
    legacy_env: str | Path | None,
    legacy_web_env: str | Path | None,
    legacy_series_dir: str | Path | None,
    legacy_rss_users: str | Path | None,
    output_root: str | Path,
    apply: bool = False,
) -> MigrationResult:
    env = read_legacy_env(legacy_env)
    web_env = read_legacy_env(legacy_web_env)
    merged = {**env, **web_env}
    app_dir = _require(merged, "BILIBILI_PODCAST_APP_DIR")
    state_root = _require(merged, "BILIBILI_PODCAST_STATE_ROOT")
    removed_rsync = sorted(set(merged) & REMOVED_LEGACY_ENV)
    generated = {
        "app.toml": _toml([
            ("database", {"path": merged.get("BILIBILI_PODCAST_CONFIG_DB", f"{state_root}/bilibili-podcast.db")}),
            ("paths", {
                "media_root": _require(merged, "BILIBILI_PODCAST_MEDIA_ROOT"),
                "json_root": _require(merged, "BILIBILI_PODCAST_JSON_ROOT"),
                "rss_root": _require(merged, "BILIBILI_PODCAST_RSS_ROOT"),
                "published_rss_root": _require(merged, "BILIBILI_PODCAST_PUBLISHED_RSS_ROOT"),
                "state_root": state_root,
                "log_dir": _require(merged, "BILIBILI_PODCAST_LOG_DIR"),
                "secrets_dir": _require(merged, "BILIBILI_PODCAST_SECRETS_DIR"),
            }),
            ("install", {"app_dir": app_dir, "venv_bin": _require(merged, "BILIBILI_PODCAST_VENV_BIN")}),
            ("executables", {"sync": _require(merged, "BILIBILI_PODCAST_SYNC_PATH"), "ffmpeg": "ffmpeg", "bilibili_podcast_config": "bilibili-podcast-config"}),
        ]),
        "sync.toml": _toml([
            ("downloads", {"max_per_run": 20, "scheduled_max_per_run": 1, "min_free_gb": _number(merged.get("BILIBILI_PODCAST_MIN_FREE_GB", "5"), "BILIBILI_PODCAST_MIN_FREE_GB", float)}),
            ("paths", {"cookie_file": _require(merged, "BILIBILI_PODCAST_COOKIE_FILE"), "lock_file": _require(merged, "BILIBILI_PODCAST_LOCK_FILE")}),
            ("browser", {"user_data_root": _require(merged, "BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT"), "playwright_browsers_path": _require(merged, "PLAYWRIGHT_BROWSERS_PATH"), "login_wait_seconds": 5.0}),
            ("timeouts", {"sync_seconds": 300, "preview_seconds": 120, "publish_seconds": 60}),
            ("logging", {"level": merged.get("BILIBILI_PODCAST_SYNC_LOG_LEVEL", "INFO"), "retention_days": 30, "max_bytes": 20971520, "backup_count": 10}),
        ]),
        "web.toml": _toml([
            ("server", {"enabled": bool(merged.get("BILIBILI_PODCAST_WEB_PASSWORD")), "host": "127.0.0.1", "port": 8000}),
            ("security", {"password": merged.get("BILIBILI_PODCAST_WEB_PASSWORD", ""), "https": _bool(merged.get("BILIBILI_PODCAST_HTTPS")), "cookie_name": "bilibili_podcast_session", "previous_cookie_names": [], "session_max_age_seconds": 86400}),
        ]),
        "scheduler.toml": _toml([
            ("runtime", {"user": "bilibili-podcast", "group": "bilibili-podcast"}),
            ("paths", {"systemd_dir": _require(merged, "BILIBILI_PODCAST_SYSTEMD_DIR"), "cron_script_dir": _require(merged, "BILIBILI_PODCAST_CRON_SCRIPT_DIR"), "wrapper_dir": merged.get("BILIBILI_PODCAST_CRON_SCRIPT_DIR", f"{app_dir}/auto")}),
            ("units", {"web": Path(merged.get("BILIBILI_PODCAST_WEB_UNIT", "bilibili-podcast-web.service")).name, "sync_glob": Path(merged.get("BILIBILI_PODCAST_SYNC_UNIT_GLOB", "bilibili-podcast-sync@*.service")).name}),
            ("timeouts", {"command_seconds": 30}),
        ]),
        "publish.toml": _toml([
            ("publish", {"enabled": bool(merged.get("BILIBILI_PODCAST_MEDIA_BASE_URL")), "media_base_url": merged.get("BILIBILI_PODCAST_MEDIA_BASE_URL", ""), "master_placeholder": "__MEDIA_PLACEHOLDER__", "gone_series": []}),
        ]),
        "manual-media.toml": _toml([("manual_media", {"enabled": bool(merged.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS")), "allowed_dirs": [item for item in merged.get("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS", "").split(":") if item], "follow_symlinks": False})]),
        "rss-users.toml": _rss_users_toml(_parse_rss_users(legacy_rss_users)),
    }
    configs, normalizations = ([], ())
    if legacy_series_dir is not None:
        configs, normalizations = _normalize_legacy_series(Path(legacy_series_dir))
    if removed_rsync:
        normalizations += ("legacy rsync configuration was removed and was not migrated",)
    output = Path(output_root).expanduser()
    files = tuple(output / name for name in generated)
    if not apply:
        with tempfile.TemporaryDirectory(prefix="bilibili-podcast-migrate-dry-run-") as temp_name:
            validation_root = Path(temp_name)
            for name, content in generated.items():
                staged = validation_root / name
                staged.write_text(content, encoding="utf-8")
                staged.chmod(0o600)
            ConfigManager(validation_root, environ={}).load()
            if configs:
                _write_migrated_series(validation_root / "series.db", configs)
        return MigrationResult(output, files, len(configs), normalizations, False)

    for target in files:
        if target.is_symlink():
            raise UnsafeConfigError(f"unsafe migration target {target}: symlink")
    output.mkdir(parents=True, exist_ok=True)
    temp_parent = output / ".temp"
    backup_parent = output / ".backups"
    temp_parent.mkdir(mode=0o700, exist_ok=True)
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="migrate-", dir=temp_parent))
    backup_root = Path(tempfile.mkdtemp(prefix="migrate-", dir=backup_parent))
    staged_db: Path | None = None
    db_path: Path | None = None
    db_existed = False
    db_backup: Path | None = None
    database_changed = False
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for name, content in generated.items():
            staged = temp_root / name
            staged.write_text(content, encoding="utf-8")
            staged.chmod(0o600)
        ConfigManager(temp_root, environ={}).load()
        if configs:
            db_path = Path(merged.get("BILIBILI_PODCAST_CONFIG_DB", f"{state_root}/bilibili-podcast.db"))
            if db_path.is_symlink():
                raise UnsafeConfigError(f"unsafe migration database {db_path}: symlink")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_existed = db_path.exists()
            fd, staged_name = tempfile.mkstemp(prefix=f".{db_path.name}.", dir=db_path.parent)
            os.close(fd)
            staged_db = Path(staged_name)
            if db_existed:
                with sqlite3.connect(db_path) as source_conn, sqlite3.connect(staged_db) as staged_conn:
                    source_conn.backup(staged_conn)
            _write_migrated_series(staged_db, configs)
            with sqlite3.connect(staged_db) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
        from .versioning import _application_lock, _online_database_backup, _write_manifest

        planned_configs: list[tuple[Path, Path, Path | None]] = []
        for name in generated:
            target = output / name
            backup: Path | None = None
            if target.exists():
                backup = backup_root / name
                shutil.copy2(target, backup)
            planned_configs.append((target, temp_root / name, backup))
        if db_existed and db_path is not None:
            db_backup = backup_root / db_path.name
            _online_database_backup(db_path, db_backup)
        _write_manifest(backup_root)

        # Only replace live files after every staged artifact and backup validates.
        with _application_lock(Path(_require(merged, "BILIBILI_PODCAST_LOCK_FILE"))):
            for target, staged, backup in planned_configs:
                staged.replace(target)
                replaced.append((target, backup))
            if staged_db is not None and db_path is not None:
                if db_existed:
                    database_changed = True
                    _write_migrated_series(db_path, configs)
                else:
                    staged_db.replace(db_path)
                    replaced.append((db_path, None))
                    staged_db = None
    except Exception:
        for target, backup in reversed(replaced):
            if backup is not None and backup.exists():
                shutil.copy2(backup, target)
            else:
                target.unlink(missing_ok=True)
        if database_changed and db_backup is not None and db_path is not None:
            with sqlite3.connect(db_backup) as source_conn, sqlite3.connect(db_path) as live_conn:
                source_conn.backup(live_conn)
        raise
    finally:
        if staged_db is not None:
            staged_db.unlink(missing_ok=True)
        shutil.rmtree(temp_root, ignore_errors=True)
    from .versioning import upgrade_installation

    try:
        upgrade_installation(output, apply=True)
    except Exception:
        for target, backup in reversed(replaced):
            if backup is not None and backup.exists():
                shutil.copy2(backup, target)
            else:
                target.unlink(missing_ok=True)
        if database_changed and db_backup is not None and db_path is not None:
            with sqlite3.connect(db_backup) as source_conn, sqlite3.connect(db_path) as live_conn:
                source_conn.backup(live_conn)
        raise
    return MigrationResult(output, files, len(configs), normalizations, True)


from .versioning import (  # noqa: E402  (legacy adapter is defined before the public facade)
    EARLIEST_UNIFIED_VERSION,
    LATEST_VERSION,
    PRE_VERSIONED_CURRENT,
    VERSION_FILE,
    VersionMigrationPlan,
    VersionMigrationResult,
    detect_version,
    plan_upgrade,
    upgrade_installation,
)


__all__ = (
    "EARLIEST_UNIFIED_VERSION",
    "LATEST_VERSION",
    "PRE_VERSIONED_CURRENT",
    "VERSION_FILE",
    "MigratedSeries",
    "MigrationResult",
    "VersionMigrationPlan",
    "VersionMigrationResult",
    "detect_version",
    "migrate_legacy",
    "plan_upgrade",
    "read_legacy_env",
    "upgrade_installation",
)
