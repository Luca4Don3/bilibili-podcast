"""Legacy env/YAML/RSS-user migration with dry-run by default."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import tempfile
import tomllib
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..manager import ConfigError, ConfigManager, UnsafeConfigError
from ..models import SeriesConfig
from ..repositories import LegacyYamlRepository
from ..schema import (
    LEGACY_ENV_MAP,
    LEGACY_INPUT_ONLY,
    QUALITY_ALIASES,
    REMOVED_LEGACY_ENV,
)

from .runtime_permissions import (
    PermissionPlan,
    PermissionResult,
    plan_runtime_permissions,
    verify_permissions_applied,
    run_runtime_permissions,
)
from .system_upgrade import (
    SystemFile,
    SystemUpgradePlan,
    SystemUpgradeResult,
    plan_system_upgrade,
    restore_system_backup,
    run_system_upgrade,
    verify_system_applied,
)


_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_CURRENT_ENV_PREFIX = "BILIBILI_PODCAST_"
LEGACY_UNVERSIONED_PROFILE = "legacy-unversioned"
LEGACY_V0_PROFILE = "legacy-v0"
LEGACY_PROFILES = (LEGACY_UNVERSIONED_PROFILE, LEGACY_V0_PROFILE)

_LAYOUT_FIELDS = {
    "app_dir": "BILIBILI_PODCAST_APP_DIR",
    "venv_bin": "BILIBILI_PODCAST_VENV_BIN",
    "sync_path": "BILIBILI_PODCAST_SYNC_PATH",
    "database_path": "BILIBILI_PODCAST_CONFIG_DB",
    "media_root": "BILIBILI_PODCAST_MEDIA_ROOT",
    "json_root": "BILIBILI_PODCAST_JSON_ROOT",
    "rss_root": "BILIBILI_PODCAST_RSS_ROOT",
    "published_rss_root": "BILIBILI_PODCAST_PUBLISHED_RSS_ROOT",
    "state_root": "BILIBILI_PODCAST_STATE_ROOT",
    "log_dir": "BILIBILI_PODCAST_LOG_DIR",
    "secrets_dir": "BILIBILI_PODCAST_SECRETS_DIR",
    "cookie_file": "BILIBILI_PODCAST_COOKIE_FILE",
    "lock_file": "BILIBILI_PODCAST_LOCK_FILE",
    "browser_user_data_root": "BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT",
    "playwright_browsers_path": "PLAYWRIGHT_BROWSERS_PATH",
    "systemd_dir": "BILIBILI_PODCAST_SYSTEMD_DIR",
    "cron_script_dir": "BILIBILI_PODCAST_CRON_SCRIPT_DIR",
    "wrapper_dir": "BILIBILI_PODCAST_WRAPPER_DIR",
}
_KNOWN_ENV_NAMES = frozenset(
    set(_LAYOUT_FIELDS.values())
    | set(LEGACY_ENV_MAP)
    | set(LEGACY_INPUT_ONLY)
    | set(REMOVED_LEGACY_ENV)
)


def _normalize_legacy_key(key: str) -> tuple[str, str | None]:
    if key in _KNOWN_ENV_NAMES:
        return key, None
    candidates = []
    for current in _KNOWN_ENV_NAMES:
        if not current.startswith(_CURRENT_ENV_PREFIX):
            continue
        suffix = current.removeprefix(_CURRENT_ENV_PREFIX)
        marker = f"_{suffix}"
        if key.endswith(marker) and len(key) > len(marker):
            candidates.append((len(marker), current, key[:-len(marker)]))
    if not candidates:
        return key, None
    _, current, prefix = max(candidates)
    return current, prefix


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
    cron_enabled: bool = True
    allowed_users: tuple[str, ...] = ()


def _read_legacy_env(path: str | Path | None) -> tuple[dict[str, str], frozenset[str]]:
    if path is None:
        return {}, frozenset()
    source = Path(path)
    if not source.exists():
        raise ConfigError(f"legacy env file does not exist: {source}")
    result: dict[str, str] = {}
    prefixes: set[str] = set()
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
        key, prefix = _normalize_legacy_key(key)
        if prefix is not None:
            prefixes.add(prefix)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if "$" in value or "`" in value:
            raise ConfigError(f"dynamic legacy env value is not supported {source}:{line_number}")
        result[key] = value
    return result, frozenset(prefixes)


def read_legacy_env(path: str | Path | None) -> dict[str, str]:
    values, _ = _read_legacy_env(path)
    return values


def read_legacy_layout(path: str | Path) -> dict[str, str]:
    """Read the explicit, non-secret path map for the oldest production layout."""
    source = Path(path)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect legacy layout manifest: {type(exc).__name__}") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafeConfigError(f"unsafe legacy layout manifest {source}: symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeConfigError(f"unsafe legacy layout manifest {source}: not a regular file")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise UnsafeConfigError("unsafe legacy layout manifest changed while opening")
            if opened.st_mode & 0o077:
                raise UnsafeConfigError("unsafe legacy layout manifest permissions")
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read legacy layout manifest: {type(exc).__name__}") from None
    if set(raw) != {"layout"} or not isinstance(raw["layout"], dict):
        raise ConfigError("legacy layout manifest must contain only [layout]")
    layout = raw["layout"]
    unknown = sorted(set(layout) - set(_LAYOUT_FIELDS))
    if unknown:
        raise ConfigError(f"unknown legacy layout field: {unknown[0]}")
    missing = sorted(set(_LAYOUT_FIELDS) - set(layout))
    if missing:
        raise ConfigError(f"missing legacy layout field: {missing[0]}")
    result: dict[str, str] = {}
    for field, environment_name in _LAYOUT_FIELDS.items():
        value = layout[field]
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ConfigError(f"invalid legacy layout path: {field}")
        if any(ord(character) < 32 for character in value):
            raise ConfigError(f"invalid legacy layout path: {field}")
        result[environment_name] = value
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


def _normalize_legacy_series_paths(
    paths: list[Path] | tuple[Path, ...],
) -> tuple[list[MigratedSeries], tuple[str, ...]]:
    configs = []
    normalizations: list[str] = []
    for path in paths:
        try:
            raw = LegacyYamlRepository(path.parent).read_raw(path)
            raw_sync = raw.get("sync") or {}
            access_value = raw.get("access")
            if isinstance(access_value, dict):
                unknown_access = set(access_value) - {"allowed_users", "users"}
                if unknown_access:
                    raise ValueError(
                        f"unknown access field: {sorted(unknown_access)[0]}"
                    )
            config = SeriesConfig.from_yaml(path, legacy=True)
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
        cron_enabled = _bool(str(cron.get("enabled", True)), True) if isinstance(cron, dict) else True
        access = raw.get("access") or {}
        allowed = access.get("allowed_users", access.get("users", [])) if isinstance(access, dict) else []
        if isinstance(allowed, str):
            allowed = [allowed]
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ConfigError(f"invalid legacy access users {path}")
        configs.append(MigratedSeries(config, schedules, retry_schedules, cron_enabled, tuple(allowed)))
    return configs, tuple(normalizations)


def _normalize_legacy_series(root: Path) -> tuple[list[MigratedSeries], tuple[str, ...]]:
    repository = LegacyYamlRepository(root)
    return _normalize_legacy_series_paths(list(repository.paths()))


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
            conn.execute("UPDATE cron_schedule SET enabled=? WHERE series=?", (int(migrated.cron_enabled), config.series))
            conn.execute("DELETE FROM access_rule WHERE series=?", (config.series,))
            conn.executemany(
                "INSERT INTO access_rule(series, allowed_user) VALUES(?, ?)",
                [(config.series, user) for user in migrated.allowed_users],
            )


def migrate_legacy(
    *,
    legacy_env: str | Path | None,
    legacy_web_env: str | Path | None,
    legacy_series_dir: str | Path | list[str | Path] | tuple[str | Path, ...] | None,
    legacy_rss_users: str | Path | None,
    output_root: str | Path,
    apply: bool = False,
    profile: str = LEGACY_UNVERSIONED_PROFILE,
    layout_manifest: str | Path | None = None,
    series_source: str | None = None,
) -> MigrationResult:
    if profile not in LEGACY_PROFILES:
        raise ConfigError(f"unknown legacy migration profile: {profile}")
    if profile == LEGACY_V0_PROFILE and layout_manifest is None:
        raise ConfigError("legacy-v0 migration requires a layout manifest")
    if profile != LEGACY_V0_PROFILE and layout_manifest is not None:
        raise ConfigError("layout manifest is only valid with legacy-v0")
    if series_source not in {None, "yaml", "db-authoritative"}:
        raise ConfigError("unknown legacy series source")
    env, env_prefixes = _read_legacy_env(legacy_env)
    web_env, web_prefixes = _read_legacy_env(legacy_web_env)
    legacy_prefixes = env_prefixes | web_prefixes
    if len(legacy_prefixes) > 1:
        raise ConfigError("legacy migration inputs use inconsistent environment prefixes")
    legacy_cookie_names = (
        [f"{next(iter(legacy_prefixes)).lower()}_session"]
        if profile == LEGACY_V0_PROFILE and legacy_prefixes
        else []
    )
    merged = {**env, **web_env}
    if layout_manifest is not None:
        # The explicit manifest is authoritative for paths. Historical shell
        # files were incomplete and frequently retained pre-release paths.
        merged.update(read_legacy_layout(layout_manifest))
    candidate_state_root = merged.get("BILIBILI_PODCAST_STATE_ROOT", "")
    candidate_database = Path(
        merged.get(
            "BILIBILI_PODCAST_CONFIG_DB",
            f"{candidate_state_root}/bilibili-podcast.db",
        )
    )
    if series_source is None:
        active_rows = False
        if candidate_database.exists():
            from .versioning import database_fingerprint

            counts = database_fingerprint(candidate_database)["table_counts"]
            active_rows = any(
                count
                for table, count in counts.items()
                if table != "schema_version"
            )
        if active_rows:
            raise ConfigError(
                "active database contains data; explicitly select --series-source"
            )
        series_source = "yaml"
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
                "fallback_log_dir": "/tmp/bilibili-podcast-logs",
                "secrets_dir": _require(merged, "BILIBILI_PODCAST_SECRETS_DIR"),
            }),
            ("install", {"app_dir": app_dir, "venv_bin": _require(merged, "BILIBILI_PODCAST_VENV_BIN")}),
            ("executables", {"sync": _require(merged, "BILIBILI_PODCAST_SYNC_PATH"), "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "bilibili_podcast_config": "bilibili-podcast-config"}),
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
            ("security", {"password": merged.get("BILIBILI_PODCAST_WEB_PASSWORD", ""), "https": _bool(merged.get("BILIBILI_PODCAST_HTTPS")), "cookie_name": "bilibili_podcast_session", "previous_cookie_names": legacy_cookie_names, "session_max_age_seconds": 86400}),
        ]),
        "scheduler.toml": _toml([
            ("runtime", {"user": "bilibili-podcast", "group": "bilibili-podcast"}),
            ("paths", {"systemd_dir": _require(merged, "BILIBILI_PODCAST_SYSTEMD_DIR"), "cron_script_dir": _require(merged, "BILIBILI_PODCAST_CRON_SCRIPT_DIR"), "wrapper_dir": merged.get("BILIBILI_PODCAST_WRAPPER_DIR", merged.get("BILIBILI_PODCAST_CRON_SCRIPT_DIR", f"{app_dir}/auto"))}),
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
    series_dirs = [] if legacy_series_dir is None else (
        list(legacy_series_dir) if isinstance(legacy_series_dir, (list, tuple))
        else [legacy_series_dir]
    )
    seen_files: dict[str, str] = {}
    unique_paths: list[Path] = []
    for series_dir in series_dirs:
        repository = LegacyYamlRepository(Path(series_dir))
        for path in repository.paths():
            raw_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            previous = seen_files.get(path.name)
            if previous is not None and previous != raw_digest:
                raise ConfigError(
                    f"conflicting legacy series file: {path.name}"
                )
            if previous is None:
                seen_files[path.name] = raw_digest
                unique_paths.append(path)
    found, notes = _normalize_legacy_series_paths(unique_paths)
    seen_series: dict[str, str] = {}
    for migrated in found:
        semantic_digest = hashlib.sha256(
            repr(migrated).encode("utf-8")
        ).hexdigest()
        previous = seen_series.get(migrated.config.series)
        if previous is not None and previous != semantic_digest:
            raise ConfigError(
                f"conflicting legacy series configuration: {migrated.config.series}"
            )
        if previous is None:
            seen_series[migrated.config.series] = semantic_digest
            configs.append(migrated)
    normalizations += notes
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
            if configs and series_source == "yaml":
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
    authoritative_before: dict | None = None
    database_changed = False
    replaced: list[tuple[Path, Path | None]] = []
    try:
        for name, content in generated.items():
            staged = temp_root / name
            staged.write_text(content, encoding="utf-8")
            staged.chmod(0o600)
        ConfigManager(temp_root, environ={}).load()
        db_path = Path(merged.get("BILIBILI_PODCAST_CONFIG_DB", f"{state_root}/bilibili-podcast.db"))
        db_existed = db_path.exists()
        if series_source == "db-authoritative":
            from .versioning import database_fingerprint

            authoritative_before = database_fingerprint(db_path)
        if configs and series_source == "yaml":
            if db_path.is_symlink():
                raise UnsafeConfigError(f"unsafe migration database {db_path}: symlink")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            fd, staged_name = tempfile.mkstemp(prefix=f".{db_path.name}.", dir=db_path.parent)
            os.close(fd)
            staged_db = Path(staged_name)
            if db_existed:
                with sqlite3.connect(db_path) as source_conn, sqlite3.connect(staged_db) as staged_conn:
                    source_conn.backup(staged_conn)
            else:
                staged_db.unlink()
            _write_migrated_series(staged_db, configs)
            with sqlite3.connect(staged_db) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA journal_mode=DELETE")
        from .versioning import _application_lock, _online_database_backup, _write_manifest

        from .versioning import LATEST_VERSION, VERSION_FILE, _version_rows

        database_versions = _version_rows(db_path) if db_existed else ()
        if db_existed and (
            len(database_versions) != 1
            or not 1 <= database_versions[0] <= LATEST_VERSION
        ):
            raise ConfigError(
                "active database must have one supported version before legacy migration"
            )
        install_version = database_versions[0] if database_versions else LATEST_VERSION
        marker = temp_root / VERSION_FILE
        marker.write_text(f"{install_version}\n", encoding="ascii")
        marker.chmod(0o600)

        planned_configs: list[tuple[Path, Path, Path | None]] = []
        for name in (*generated, VERSION_FILE):
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
            if authoritative_before is not None:
                from .versioning import database_fingerprint

                if database_fingerprint(db_path) != authoritative_before:
                    raise ConfigError(
                        "db-authoritative migration changed the active database"
                    )
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
    return MigrationResult(output, files, len(configs), normalizations, True)


from .versioning import (  # noqa: E402  (legacy adapter is defined before the public facade)
    ACTIVE_UPGRADE_SENTINEL,
    EARLIEST_UNIFIED_VERSION,
    LATEST_VERSION,
    PRE_VERSIONED_CURRENT,
    VERSION_FILE,
    VersionMigrationPlan,
    VersionMigrationResult,
    apply_data_upgrade,
    detect_version,
    finalize_upgrade,
    load_plan,
    plan_upgrade,
    prepare_upgrade,
    rollback_upgrade,
    status_upgrade,
    update_plan_state,
    upgrade_installation,
)


__all__ = (
    "ACTIVE_UPGRADE_SENTINEL",
    "EARLIEST_UNIFIED_VERSION",
    "LATEST_VERSION",
    "LEGACY_PROFILES",
    "LEGACY_UNVERSIONED_PROFILE",
    "LEGACY_V0_PROFILE",
    "PRE_VERSIONED_CURRENT",
    "VERSION_FILE",
    "MigratedSeries",
    "MigrationResult",
    "PermissionPlan",
    "PermissionResult",
    "VersionMigrationPlan",
    "VersionMigrationResult",
    "apply_data_upgrade",
    "detect_version",
    "finalize_upgrade",
    "load_plan",
    "migrate_legacy",
    "plan_upgrade",
    "prepare_upgrade",
    "plan_runtime_permissions",
    "verify_permissions_applied",
    "read_legacy_env",
    "read_legacy_layout",
    "run_runtime_permissions",
    "SystemFile",
    "SystemUpgradePlan",
    "SystemUpgradeResult",
    "plan_system_upgrade",
    "restore_system_backup",
    "run_system_upgrade",
    "verify_system_applied",
    "rollback_upgrade",
    "status_upgrade",
    "update_plan_state",
    "upgrade_installation",
)
