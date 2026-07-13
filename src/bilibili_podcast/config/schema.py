"""Single source of truth for application and series configuration fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MISSING = object()


@dataclass(frozen=True)
class FieldSpec:
    path: str
    value_type: type | tuple[type, ...]
    default: Any = MISSING
    required: bool = False
    minimum: int | float | None = None
    maximum: int | float | None = None
    sensitive: bool = False
    owner: str = ""
    sqlite_column: str | None = None
    legacy_aliases: tuple[str, ...] = ()


APP_FIELDS = (
    FieldSpec("database.path", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_CONFIG_DB",)),
    FieldSpec("paths.media_root", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_MEDIA_ROOT",)),
    FieldSpec("paths.json_root", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_JSON_ROOT",)),
    FieldSpec("paths.rss_root", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_RSS_ROOT",)),
    FieldSpec("paths.published_rss_root", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_PUBLISHED_RSS_ROOT",)),
    FieldSpec("paths.state_root", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_STATE_ROOT",)),
    FieldSpec("paths.log_dir", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_LOG_DIR",)),
    FieldSpec("paths.secrets_dir", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_SECRETS_DIR",)),
    FieldSpec("install.app_dir", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_APP_DIR",)),
    FieldSpec("install.venv_bin", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_VENV_BIN",)),
    FieldSpec("executables.sync", str, required=True, owner="app", legacy_aliases=("BILIBILI_PODCAST_SYNC_PATH",)),
    FieldSpec("executables.ffmpeg", str, "ffmpeg", owner="app"),
    FieldSpec("executables.bilibili_podcast_config", str, "bilibili-podcast-config", owner="app"),
)

SYNC_FIELDS = (
    FieldSpec("downloads.max_per_run", int, 20, minimum=0, owner="sync"),
    FieldSpec("downloads.scheduled_max_per_run", int, 1, minimum=0, owner="sync"),
    FieldSpec("downloads.min_free_gb", (int, float), 5.0, minimum=0, owner="sync", legacy_aliases=("BILIBILI_PODCAST_MIN_FREE_GB",)),
    FieldSpec("paths.cookie_file", str, required=True, sensitive=True, owner="sync", legacy_aliases=("BILIBILI_PODCAST_COOKIE_FILE",)),
    FieldSpec("paths.lock_file", str, required=True, owner="sync", legacy_aliases=("BILIBILI_PODCAST_LOCK_FILE",)),
    FieldSpec("browser.user_data_root", str, required=True, owner="sync", legacy_aliases=("BILIBILI_PODCAST_BROWSER_USER_DATA_ROOT",)),
    FieldSpec("browser.playwright_browsers_path", str, required=True, owner="sync", legacy_aliases=("PLAYWRIGHT_BROWSERS_PATH",)),
    FieldSpec("browser.login_wait_seconds", (int, float), 5.0, minimum=0, owner="sync"),
    FieldSpec("timeouts.sync_seconds", int, 300, minimum=1, owner="sync"),
    FieldSpec("timeouts.preview_seconds", int, 120, minimum=1, owner="sync"),
    FieldSpec("timeouts.publish_seconds", int, 60, minimum=1, owner="sync"),
    FieldSpec("logging.level", str, "INFO", owner="sync", legacy_aliases=("BILIBILI_PODCAST_SYNC_LOG_LEVEL",)),
    FieldSpec("logging.retention_days", int, 30, minimum=0, owner="sync"),
    FieldSpec("logging.max_bytes", int, 20971520, minimum=1, owner="sync"),
    FieldSpec("logging.backup_count", int, 10, minimum=0, owner="sync"),
)

WEB_FIELDS = (
    FieldSpec("server.enabled", bool, False, owner="web"),
    FieldSpec("server.host", str, "127.0.0.1", owner="web"),
    FieldSpec("server.port", int, 8000, minimum=1, maximum=65535, owner="web"),
    FieldSpec("security.password", str, "", sensitive=True, owner="web", legacy_aliases=("BILIBILI_PODCAST_WEB_PASSWORD",)),
    FieldSpec("security.https", bool, False, owner="web", legacy_aliases=("BILIBILI_PODCAST_HTTPS",)),
    FieldSpec("security.cookie_name", str, "bilibili_podcast_session", owner="web"),
    FieldSpec("security.session_max_age_seconds", int, 86400, minimum=1, owner="web"),
)

SCHEDULER_FIELDS = (
    FieldSpec("runtime.user", str, "bilibili-podcast", owner="scheduler"),
    FieldSpec("runtime.group", str, "bilibili-podcast", owner="scheduler"),
    FieldSpec("paths.systemd_dir", str, required=True, owner="scheduler", legacy_aliases=("BILIBILI_PODCAST_SYSTEMD_DIR",)),
    FieldSpec("paths.cron_script_dir", str, required=True, owner="scheduler", legacy_aliases=("BILIBILI_PODCAST_CRON_SCRIPT_DIR",)),
    FieldSpec("paths.wrapper_dir", str, required=True, owner="scheduler"),
    FieldSpec("units.web", str, "bilibili-podcast-web.service", owner="scheduler"),
    FieldSpec("units.sync_glob", str, "bilibili-podcast-sync@*.service", owner="scheduler"),
    FieldSpec("timeouts.command_seconds", int, 30, minimum=1, owner="scheduler"),
)

PUBLISH_FIELDS = (
    FieldSpec("publish.enabled", bool, False, owner="publish"),
    FieldSpec("publish.media_base_url", str, "", owner="publish", legacy_aliases=("BILIBILI_PODCAST_MEDIA_BASE_URL",)),
    FieldSpec("publish.script", str, "", owner="publish", legacy_aliases=("BILIBILI_PODCAST_RSS_PUBLISH", "BILIBILI_PODCAST_RSS_PUBLISH_SCRIPT")),
    FieldSpec("publish.master_placeholder", str, "__MEDIA_PLACEHOLDER__", owner="publish"),
)

MANUAL_MEDIA_FIELDS = (
    FieldSpec("manual_media.enabled", bool, False, owner="manual-media"),
    FieldSpec("manual_media.allowed_dirs", list, [], owner="manual-media", legacy_aliases=("BILIBILI_PODCAST_MANUAL_MEDIA_DIRS",)),
    FieldSpec("manual_media.follow_symlinks", bool, False, owner="manual-media"),
)

RSS_USERS_FIELDS = (
    FieldSpec("users", dict, {}, sensitive=True, owner="rss-users", legacy_aliases=("RSS_USERS_CONF",)),
)

FILE_SCHEMAS = {
    "app": APP_FIELDS,
    "sync": SYNC_FIELDS,
    "web": WEB_FIELDS,
    "scheduler": SCHEDULER_FIELDS,
    "publish": PUBLISH_FIELDS,
    "manual-media": MANUAL_MEDIA_FIELDS,
    "rss-users": RSS_USERS_FIELDS,
}

ALL_FIELDS = tuple(spec for specs in FILE_SCHEMAS.values() for spec in specs)
LEGACY_ENV_MAP = {
    alias: f"{spec.owner}.{spec.path}"
    for spec in ALL_FIELDS
    for alias in spec.legacy_aliases
}
LEGACY_INPUT_ONLY = {
    "BILIBILI_PODCAST_ENV_FILE", "BILIBILI_PODCAST_WEB_ENV_FILE", "BILIBILI_PODCAST_WEB_UNIT",
    "BILIBILI_PODCAST_SYNC_UNIT_GLOB",
}
REMOVED_LEGACY_ENV = {
    "BILIBILI_PODCAST_RSYNC_HOST", "BILIBILI_PODCAST_RSYNC_PORT", "BILIBILI_PODCAST_RSYNC_USER",
    "BILIBILI_PODCAST_RSYNC_SECRET", "BILIBILI_PODCAST_RSYNC_RSS_SRC", "RSYNC_PASSWORD",
}
RUN_CONTROL_ENV = {"FORCE", "DEBUG", "SMOKE_SYNC", "PATH", "BILIBILI_PODCAST_CONFIG_ROOT"}
CRON_COMPAT_ENV = {"MAX_DOWNLOADS_PER_RUN", "LOG_LEVEL"}


SERIES_SYNC_DEFAULTS: dict[str, Any] = {
    "page_size": 20,
    "incremental_page_size": 5,
    "max_pages": 10,
    "max_requests_per_series": 8,
    "request_interval_seconds": 2.0,
    "request_jitter_seconds": 0.5,
    "rate_limit_cooldown_seconds": 21600,
    "update_period": "12h",
    "update_period_grace_seconds": 120,
    "format": "audio",
    "media_mode": "auto",
    "quality": "64K",
    "fetch_strategy": "api_first",
    "keep_last": 100,
    "browser_fallback": False,
    "browser_wait_min_seconds": 4.0,
    "browser_wait_max_seconds": 8.0,
    "browser_fallback_cooldown_seconds": 3600,
    "require_paid_state_confirmation": False,
    "min_duration_seconds": 0,
    "max_duration_seconds": 0,
}

QUALITY_ALIASES = {"low": "64K", "medium": "132K", "high": "192K"}

SERIES_TOP_FIELDS = {
    "series", "enabled", "title", "description", "author", "cover_art",
    "category", "subcategories", "explicit", "lang", "source", "sync",
    "filters", "paid_preview", "cron",
}
SERIES_SOURCE_FIELDS = {"space_url", "uid", "type", "sid"}
SERIES_FILTER_FIELDS = {
    "exclude_paid", "exclude_bvids", "advertisement_bvids",
    "exclude_season_ids", "exclude_keywords", "advertisement_keywords",
    "include_keywords",
}
SERIES_PAID_PREVIEW_FIELDS = {"enabled", "retry_after_days"}
SERIES_CRON_FIELDS = {"schedules", "retry_schedules"}
