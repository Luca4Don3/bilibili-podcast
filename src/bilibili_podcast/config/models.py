"""Immutable configuration models used by every application entry point."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse
import re


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class AppPathsConfig:
    media_root: Path
    json_root: Path
    rss_root: Path
    published_rss_root: Path
    state_root: Path
    log_dir: Path
    fallback_log_dir: Path
    secrets_dir: Path


@dataclass(frozen=True)
class InstallConfig:
    app_dir: Path
    venv_bin: Path


@dataclass(frozen=True)
class ExecutablesConfig:
    sync: Path
    ffmpeg: str
    ffprobe: str
    bilibili_podcast_config: str


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig
    paths: AppPathsConfig
    install: InstallConfig
    executables: ExecutablesConfig


@dataclass(frozen=True)
class DownloadConfig:
    max_per_run: int
    scheduled_max_per_run: int
    min_free_gb: float


@dataclass(frozen=True)
class SyncPathsConfig:
    cookie_file: Path
    lock_file: Path


@dataclass(frozen=True)
class BrowserConfig:
    user_data_root: Path
    playwright_browsers_path: Path
    login_wait_seconds: float


@dataclass(frozen=True)
class TimeoutConfig:
    sync_seconds: int
    preview_seconds: int
    publish_seconds: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    retention_days: int
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class SyncConfig:
    downloads: DownloadConfig
    paths: SyncPathsConfig
    browser: BrowserConfig
    timeouts: TimeoutConfig
    logging: LoggingConfig


@dataclass(frozen=True)
class WebServerConfig:
    enabled: bool
    host: str
    port: int


@dataclass(frozen=True)
class WebSecurityConfig:
    password: str
    https: bool
    cookie_name: str
    previous_cookie_names: tuple[str, ...]
    session_max_age_seconds: int


@dataclass(frozen=True)
class WebConfig:
    server: WebServerConfig
    security: WebSecurityConfig


@dataclass(frozen=True)
class SchedulerRuntimeConfig:
    user: str
    group: str


@dataclass(frozen=True)
class SchedulerPathsConfig:
    systemd_dir: Path
    cron_script_dir: Path
    wrapper_dir: Path


@dataclass(frozen=True)
class SchedulerUnitsConfig:
    web: str
    sync_glob: str


@dataclass(frozen=True)
class SchedulerConfig:
    runtime: SchedulerRuntimeConfig
    paths: SchedulerPathsConfig
    units: SchedulerUnitsConfig
    command_timeout_seconds: int


@dataclass(frozen=True)
class PublishSettings:
    enabled: bool
    media_base_url: str
    master_placeholder: str
    gone_series: tuple[str, ...]


@dataclass(frozen=True)
class PublishConfig:
    publish: PublishSettings


@dataclass(frozen=True)
class ManualMediaConfig:
    enabled: bool
    allowed_dirs: tuple[Path, ...]
    follow_symlinks: bool


@dataclass(frozen=True)
class RssUser:
    token: str
    series: tuple[str, ...]


@dataclass(frozen=True)
class RssUsersConfig:
    users: Mapping[str, RssUser]


@dataclass(frozen=True)
class ConfigSnapshot:
    root: Path
    app: AppConfig
    sync: SyncConfig
    web: WebConfig
    scheduler: SchedulerConfig
    publish: PublishConfig
    manual_media: ManualMediaConfig
    rss_users: RssUsersConfig
    sources: Mapping[str, Path] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class OperatorConfig:
    user: str


@dataclass(frozen=True)
class NginxSystemConfig:
    user: str
    group: str
    config_path: Path
    access_log_path: Path
    error_log_path: Path


@dataclass(frozen=True)
class SystemConfigSnapshot:
    root: Path
    operator: OperatorConfig
    nginx: NginxSystemConfig


@dataclass
class SeriesConfig:
    series: str
    enabled: bool
    title: str
    description: str
    author: str
    cover_art: str
    category: str
    subcategories: tuple[str, ...] | list[str]
    explicit: bool
    lang: str
    source: Mapping[str, Any] | dict[str, Any]
    sync: Mapping[str, Any] | dict[str, Any]
    filters: Mapping[str, Any] | dict[str, Any]
    paid_preview: Mapping[str, Any] | dict[str, Any]
    keep_last: int
    api_backend: str = "bilibili-api"

    @property
    def uid(self) -> int:
        uid = self.source.get("uid")
        if uid:
            return int(uid)
        space_url = str(self.source.get("space_url", ""))
        path = urlparse(space_url).path.strip("/")
        candidate = path.split("/", 1)[0] if path else ""
        if candidate.isdigit():
            return int(candidate)
        raise ValueError("source.uid or source.space_url with UID is required")

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        legacy: bool = False,
    ) -> "SeriesConfig":
        import yaml

        from .schema import (
            QUALITY_ALIASES, SERIES_CRON_FIELDS, SERIES_FILTER_FIELDS,
            SERIES_PAID_PREVIEW_FIELDS, SERIES_SOURCE_FIELDS,
            SERIES_SYNC_DEFAULTS, SERIES_TOP_FIELDS,
        )

        source_path = Path(path)
        data = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("series config root must be a mapping")
        if legacy:
            data = dict(data)
            data.pop("access", None)
            legacy_source = data.get("source")
            if isinstance(legacy_source, dict) and legacy_source.get("sid") is None:
                data["source"] = dict(legacy_source)
                data["source"].pop("sid", None)
            legacy_cron = data.get("cron")
            if isinstance(legacy_cron, dict):
                data["cron"] = dict(legacy_cron)
                data["cron"].pop("enabled", None)
        unknown_top = sorted(set(data) - SERIES_TOP_FIELDS)
        if unknown_top:
            raise ValueError(f"unknown series field: {unknown_top[0]}")
        from ..api_backends import BACKEND_NAMES, parse_backend_spec

        api_backend = data.get("api_backend", "bilibili-api")
        if isinstance(api_backend, str):
            try:
                backend_names = parse_backend_spec(api_backend)
            except ValueError as exc:
                raise ValueError(f"api_backend must be one of: {', '.join(BACKEND_NAMES)}") from exc
        else:
            backend_names = list(api_backend)
        invalid = [name for name in backend_names if name not in BACKEND_NAMES]
        if invalid:
            raise ValueError(f"api_backend must be one of: {', '.join(BACKEND_NAMES)}")
        series = data.get("series")
        if not series:
            raise ValueError("series is required")
        if not isinstance(series, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", series):
            raise ValueError("series must use lowercase letters, numbers, hyphens, or underscores")
        if source_path.stem != series:
            raise ValueError(f"series must match config file name: {source_path.name}")
        if not data.get("title"):
            raise ValueError("title is required")
        if not data.get("author"):
            raise ValueError("author must be the Bilibili UP name")
        for field_name in ("enabled", "explicit"):
            if field_name in data and not isinstance(data[field_name], bool):
                raise ValueError(f"{field_name} must be a boolean")
        for field_name in ("title", "description", "author", "cover_art", "category", "lang"):
            if field_name in data and not isinstance(data[field_name], str):
                raise ValueError(f"{field_name} must be a string")
        if "subcategories" in data and (
            not isinstance(data["subcategories"], list)
            or not all(isinstance(item, str) for item in data["subcategories"])
        ):
            raise ValueError("subcategories must be a list of strings")
        raw_source = data.get("source") or {}
        if not isinstance(raw_source, dict):
            raise ValueError("source must be a mapping")
        source = dict(raw_source)
        unknown_source = sorted(set(source) - SERIES_SOURCE_FIELDS)
        if unknown_source:
            raise ValueError(f"unknown source field: {unknown_source[0]}")
        if not source.get("space_url") and not source.get("uid"):
            raise ValueError("source.space_url or source.uid is required")
        if "uid" in source and (
            not isinstance(source["uid"], int) or isinstance(source["uid"], bool) or source["uid"] <= 0
        ):
            raise ValueError("source.uid must be a positive integer")
        if "sid" in source and (
            not isinstance(source["sid"], int) or isinstance(source["sid"], bool) or source["sid"] < 0
        ):
            raise ValueError("source.sid must be a non-negative integer")
        for key in ("space_url", "type"):
            if key in source and not isinstance(source[key], str):
                raise ValueError(f"source.{key} must be a string")
        raw_sync = data.get("sync") or {}
        if not isinstance(raw_sync, dict):
            raise ValueError("sync must be a mapping")
        sync = dict(raw_sync)
        unknown_sync = sorted(set(sync) - set(SERIES_SYNC_DEFAULTS) - {"browser_wait_seconds", "cron"})
        if unknown_sync:
            raise ValueError(f"unknown sync field: {unknown_sync[0]}")
        wait = sync.pop("browser_wait_seconds", None)
        if wait is not None:
            sync.setdefault("browser_wait_min_seconds", wait)
            sync.setdefault("browser_wait_max_seconds", wait)
        sync["quality"] = QUALITY_ALIASES.get(sync.get("quality"), sync.get("quality", "64K"))
        if sync["quality"] not in {"64K", "132K", "192K"}:
            raise ValueError("sync.quality must be 64K, 132K, or 192K")
        for key, default in SERIES_SYNC_DEFAULTS.items():
            if key not in sync:
                continue
            value = sync[key]
            if isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, int):
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif isinstance(default, float):
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                valid = isinstance(value, type(default))
            if not valid:
                raise ValueError(f"sync.{key} has invalid type")
        keep_last = sync.get("keep_last", SERIES_SYNC_DEFAULTS["keep_last"])
        if not isinstance(keep_last, int) or isinstance(keep_last, bool) or keep_last < 0:
            raise ValueError("sync.keep_last must be 0 or a positive integer")
        raw_filters = data.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise ValueError("filters must be a mapping")
        filters = dict(raw_filters)
        unknown_filters = sorted(set(filters) - SERIES_FILTER_FIELDS)
        if unknown_filters:
            raise ValueError(f"unknown filters field: {unknown_filters[0]}")
        for key, value in filters.items():
            if key == "exclude_paid":
                if not isinstance(value, bool):
                    raise ValueError("filters.exclude_paid must be a boolean")
            elif not isinstance(value, list):
                raise ValueError(f"filters.{key} must be a list")
            elif key != "exclude_season_ids" and not all(isinstance(item, str) for item in value):
                raise ValueError(f"filters.{key} must contain strings")
        raw_paid = data.get("paid_preview") or {"enabled": False}
        if not isinstance(raw_paid, dict):
            raise ValueError("paid_preview must be a mapping")
        paid_preview = dict(raw_paid)
        unknown_paid = sorted(set(paid_preview) - SERIES_PAID_PREVIEW_FIELDS)
        if unknown_paid:
            raise ValueError(f"unknown paid_preview field: {unknown_paid[0]}")
        if "enabled" in paid_preview and not isinstance(paid_preview["enabled"], bool):
            raise ValueError("paid_preview.enabled must be a boolean")
        if "retry_after_days" in paid_preview and (
            not isinstance(paid_preview["retry_after_days"], int)
            or isinstance(paid_preview["retry_after_days"], bool)
            or paid_preview["retry_after_days"] < 0
        ):
            raise ValueError("paid_preview.retry_after_days must be a non-negative integer")
        cron = data.get("cron") or {}
        if not isinstance(cron, dict) or set(cron) - SERIES_CRON_FIELDS:
            raise ValueError("unknown cron field")
        for key in SERIES_CRON_FIELDS:
            if key in cron and (
                not isinstance(cron[key], list)
                or not all(isinstance(item, str) and item.strip() for item in cron[key])
            ):
                raise ValueError(f"cron.{key} must be a list of non-empty strings")
        try:
            exclude_season_ids = [int(value) for value in filters.get("exclude_season_ids", [])]
        except (TypeError, ValueError) as exc:
            raise ValueError("filters.exclude_season_ids must contain positive integers") from exc
        if any(value <= 0 for value in exclude_season_ids):
            raise ValueError("filters.exclude_season_ids must contain positive integers")
        filters["exclude_season_ids"] = exclude_season_ids
        return cls(
            series=series,
            enabled=data.get("enabled", True),
            title=data["title"],
            description=data.get("description") or "",
            author=data["author"],
            cover_art=data.get("cover_art") or "",
            category=data.get("category") or "",
            subcategories=data.get("subcategories") or [],
            explicit=data.get("explicit", False),
            lang=data.get("lang") or "zh-CN",
            source=source,
            sync=sync,
            filters=filters,
            paid_preview=paid_preview,
            keep_last=keep_last,
            api_backend=api_backend,
        )
