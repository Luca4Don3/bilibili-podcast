import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils.series_config import SeriesConfig
from .services.filter_service import list_filter_entries


SERIES_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
CRON_EXPR_RE = re.compile(r"^(\S+\s+){4}\S+$")
QUALITY_VALUES = {"64K", "132K", "192K", "low", "medium", "high"}
SOURCE_TYPE_VALUES = {"space", "season", "series"}
RULE_TYPE_VALUES = {
    "exclude_paid", "exclude_bvid", "advertisement_bvid",
    "exclude_keyword", "advertisement_keyword", "include_keyword",
    "exclude_season_id",
}
SCHEDULER_BACKEND_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_backend (
    series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
    backend TEXT NOT NULL DEFAULT 'cron' CHECK(backend IN ('cron','systemd'))
)
"""


def migrate(db_path: str | Path) -> None:
    """Create or migrate the SQLite schema."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    _ensure_column(conn, "cron_schedule", "kind", "TEXT NOT NULL DEFAULT 'primary'")
    _ensure_column(conn, "sync_state", "retry_pending", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS series (
    series TEXT PRIMARY KEY CHECK(series GLOB '[a-z0-9][a-z0-9_-]*'),
    enabled INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL,
    cover_art TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    subcategories TEXT NOT NULL DEFAULT '[]',
    explicit INTEGER NOT NULL DEFAULT 0,
    lang TEXT NOT NULL DEFAULT 'zh-CN'
);

CREATE TABLE IF NOT EXISTS series_source (
    series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
    space_url TEXT NOT NULL DEFAULT '',
    uid INTEGER,
    type TEXT NOT NULL DEFAULT 'space' CHECK(type IN ('space','season','series')),
    sid INTEGER
);

CREATE TABLE IF NOT EXISTS sync_policy (
    series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
    page_size INTEGER NOT NULL DEFAULT 20 CHECK(page_size > 0 AND page_size <= 50),
    incremental_page_size INTEGER NOT NULL DEFAULT 5 CHECK(incremental_page_size > 0),
    max_pages INTEGER NOT NULL DEFAULT 10 CHECK(max_pages > 0),
    max_requests_per_series INTEGER NOT NULL DEFAULT 8 CHECK(max_requests_per_series > 0),
    request_interval_seconds REAL NOT NULL DEFAULT 2.0,
    request_jitter_seconds REAL NOT NULL DEFAULT 0.5,
    rate_limit_cooldown_seconds INTEGER NOT NULL DEFAULT 21600,
    update_period TEXT NOT NULL DEFAULT '12h',
    format TEXT NOT NULL DEFAULT 'audio',
    quality TEXT NOT NULL DEFAULT '64K',
    fetch_strategy TEXT NOT NULL DEFAULT 'api_first',
    keep_last INTEGER NOT NULL DEFAULT 100 CHECK(keep_last >= 0),
    browser_fallback INTEGER NOT NULL DEFAULT 0,
    browser_wait_min_seconds REAL NOT NULL DEFAULT 4.0,
    browser_wait_max_seconds REAL NOT NULL DEFAULT 8.0,
    browser_fallback_cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    require_paid_state_confirmation INTEGER NOT NULL DEFAULT 0,
    min_duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(min_duration_seconds >= 0),
    max_duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(max_duration_seconds >= 0)
);

CREATE TABLE IF NOT EXISTS filter_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    value TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paid_preview_policy (
    series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    retry_after_days INTEGER NOT NULL DEFAULT 4
);

CREATE TABLE IF NOT EXISTS cron_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'primary' CHECK(kind IN ('primary','retry'))
);

{scheduler_backend_sql};

CREATE TABLE IF NOT EXISTS access_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL REFERENCES series(series) ON DELETE CASCADE,
    allowed_user TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    series TEXT PRIMARY KEY REFERENCES series(series) ON DELETE CASCADE,
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER NOT NULL DEFAULT 0,
    last_browser_fallback_at INTEGER NOT NULL DEFAULT 0,
    rate_limited_until INTEGER NOT NULL DEFAULT 0,
    retry_pending INTEGER NOT NULL DEFAULT 0
);
""".format(scheduler_backend_sql=SCHEDULER_BACKEND_SQL.strip())


@contextmanager
def transaction(db_path: str | Path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── CRUD: series ──────────────────────────────────────────────────────

def upsert_series(conn, config: SeriesConfig) -> None:
    import json
    conn.execute(
        """INSERT INTO series (series, enabled, title, description,
                              author, cover_art, category, subcategories, explicit, lang)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(series) DO UPDATE SET
               enabled=excluded.enabled, title=excluded.title,
               description=excluded.description, author=excluded.author,
               cover_art=excluded.cover_art, category=excluded.category,
               subcategories=excluded.subcategories, explicit=excluded.explicit,
               lang=excluded.lang""",
        (config.series, int(config.enabled), config.title, config.description,
         config.author, config.cover_art, config.category,
         json.dumps(config.subcategories, ensure_ascii=False), int(config.explicit), config.lang),
    )


def upsert_source(conn, config: SeriesConfig) -> None:
    src = config.source
    conn.execute(
        """INSERT INTO series_source (series, space_url, uid, type, sid)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(series) DO UPDATE SET
               space_url=excluded.space_url, uid=excluded.uid,
               type=excluded.type, sid=excluded.sid""",
        (config.series, src.get("space_url", ""),
         src.get("uid"), src.get("type", "space"), src.get("sid")),
    )


def upsert_sync_policy(conn, config: SeriesConfig) -> None:
    s = config.sync
    conn.execute(
        """INSERT INTO sync_policy (
               series, page_size, incremental_page_size, max_pages,
               max_requests_per_series, request_interval_seconds,
               request_jitter_seconds, rate_limit_cooldown_seconds,
               update_period, format, quality, fetch_strategy, keep_last,
               browser_fallback, browser_wait_min_seconds,
               browser_wait_max_seconds, browser_fallback_cooldown_seconds,
               require_paid_state_confirmation,
               min_duration_seconds, max_duration_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(series) DO UPDATE SET
               page_size=excluded.page_size,
               incremental_page_size=excluded.incremental_page_size,
               max_pages=excluded.max_pages,
               max_requests_per_series=excluded.max_requests_per_series,
               request_interval_seconds=excluded.request_interval_seconds,
               request_jitter_seconds=excluded.request_jitter_seconds,
               rate_limit_cooldown_seconds=excluded.rate_limit_cooldown_seconds,
               update_period=excluded.update_period,
               format=excluded.format, quality=excluded.quality,
               fetch_strategy=excluded.fetch_strategy,
               keep_last=excluded.keep_last,
               browser_fallback=excluded.browser_fallback,
               browser_wait_min_seconds=excluded.browser_wait_min_seconds,
               browser_wait_max_seconds=excluded.browser_wait_max_seconds,
               browser_fallback_cooldown_seconds=excluded.browser_fallback_cooldown_seconds,
               require_paid_state_confirmation=excluded.require_paid_state_confirmation,
               min_duration_seconds=excluded.min_duration_seconds,
               max_duration_seconds=excluded.max_duration_seconds""",
        (config.series,
         s.get("page_size", 20), s.get("incremental_page_size", 5),
         s.get("max_pages", 10), s.get("max_requests_per_series", 8),
         s.get("request_interval_seconds", 2.0),
         s.get("request_jitter_seconds", 0.5),
         s.get("rate_limit_cooldown_seconds", 21600),
         s.get("update_period", "12h"), s.get("format", "audio"),
         s.get("quality", "64K"), s.get("fetch_strategy", "api_first"),
         config.keep_last, int(bool(s.get("browser_fallback", False))),
         s.get("browser_wait_min_seconds", 4.0),
         s.get("browser_wait_max_seconds", 8.0),
         s.get("browser_fallback_cooldown_seconds", 3600),
         int(bool(s.get("require_paid_state_confirmation", False))),
         s.get("min_duration_seconds", 0),
         s.get("max_duration_seconds", 0)),
    )


def upsert_filters(conn, config: SeriesConfig) -> None:
    conn.execute("DELETE FROM filter_rule WHERE series=?", (config.series,))
    pos = 0
    for rule_type, value in list_filter_entries(config.filters):
        conn.execute(
            "INSERT INTO filter_rule (series, rule_type, value, position) VALUES (?, ?, ?, ?)",
            (config.series, rule_type, value, pos),
        )
        pos += 1


def upsert_paid_preview(conn, config: SeriesConfig) -> None:
    pp = config.paid_preview
    conn.execute(
        """INSERT INTO paid_preview_policy (series, enabled, retry_after_days)
           VALUES (?, ?, ?)
           ON CONFLICT(series) DO UPDATE SET
               enabled=excluded.enabled, retry_after_days=excluded.retry_after_days""",
        (config.series, int(pp.get("enabled", False)),
         pp.get("retry_after_days", 4)),
    )


def upsert_cron(conn, series: str, schedules: list[str]) -> None:
    conn.execute("DELETE FROM cron_schedule WHERE series=?", (series,))
    for pos, sched in enumerate(schedules):
        conn.execute(
            "INSERT INTO cron_schedule (series, enabled, schedule, position) VALUES (?, 1, ?, ?)",
            (series, sched, pos),
        )


def get_scheduler_backend(conn, series: str) -> str:
    conn.execute(SCHEDULER_BACKEND_SQL)
    row = conn.execute(
        "SELECT backend FROM scheduler_backend WHERE series=?",
        (series,),
    ).fetchone()
    return row[0] if row is not None else "cron"


def set_scheduler_backend(conn, series: str, backend: str) -> None:
    if backend not in {"cron", "systemd"}:
        raise ValueError(f"unsupported scheduler backend: {backend}")
    conn.execute(SCHEDULER_BACKEND_SQL)
    conn.execute(
        """INSERT INTO scheduler_backend(series, backend) VALUES(?, ?)
           ON CONFLICT(series) DO UPDATE SET backend=excluded.backend""",
        (series, backend),
    )


def upsert_sync_state(conn, series: str, state: dict) -> None:
    conn.execute(
        """INSERT INTO sync_state (series, last_attempt_at, last_success_at,
                                   last_browser_fallback_at, rate_limited_until,
                                   retry_pending)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(series) DO UPDATE SET
               last_attempt_at=excluded.last_attempt_at,
               last_success_at=excluded.last_success_at,
               last_browser_fallback_at=excluded.last_browser_fallback_at,
               rate_limited_until=excluded.rate_limited_until,
               retry_pending=excluded.retry_pending""",
        (series,
         int(state.get("last_attempt_at", 0)),
         int(state.get("last_success_at", 0)),
         int(state.get("last_browser_fallback_at", 0)),
         int(state.get("rate_limited_until", 0)),
         int(bool(state.get("retry_pending", False)))),
    )


# ── READ ──────────────────────────────────────────────────────────────

def _parse_subcategories(raw: str | None) -> list[str]:
    """Parse subcategories from DB, handling both JSON and old Python repr."""
    if not raw:
        return []
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    import ast
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return []


def _row_value(row, key: str, default: Any = None) -> Any:
    try:
        v = row[key]
        return v if v is not None else default
    except (KeyError, IndexError):
        return default


def load_series_configs(db_path: str | Path) -> list[SeriesConfig]:
    """Load all enabled series configs from SQLite, returning SeriesConfig list."""
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT * FROM series WHERE enabled=1 ORDER BY series").fetchall()
        return [_row_to_config(conn, row) for row in rows]


def _row_to_config(conn: sqlite3.Connection, row) -> SeriesConfig:
    series = row["series"]
    src = conn.execute("SELECT * FROM series_source WHERE series=?", (series,)).fetchone() or {}
    sp = conn.execute("SELECT * FROM sync_policy WHERE series=?", (series,)).fetchone() or {}
    fr = conn.execute(
        "SELECT rule_type, value FROM filter_rule WHERE series=? AND enabled=1 ORDER BY position", (series,),
    ).fetchall()
    pp = conn.execute("SELECT * FROM paid_preview_policy WHERE series=?", (series,)).fetchone() or {}

    source = {
        "space_url": _row_value(src, "space_url", ""),
        "uid": _row_value(src, "uid"),
        "type": _row_value(src, "type", "space"),
        "sid": _row_value(src, "sid"),
    }

    sync = {
        "page_size": _row_value(sp, "page_size", 20),
        "incremental_page_size": _row_value(sp, "incremental_page_size", 5),
        "max_pages": _row_value(sp, "max_pages", 10),
        "max_requests_per_series": _row_value(sp, "max_requests_per_series", 8),
        "request_interval_seconds": _row_value(sp, "request_interval_seconds", 2.0),
        "request_jitter_seconds": _row_value(sp, "request_jitter_seconds", 0.5),
        "rate_limit_cooldown_seconds": _row_value(sp, "rate_limit_cooldown_seconds", 21600),
        "update_period": _row_value(sp, "update_period", "12h"),
        "format": _row_value(sp, "format", "audio"),
        "quality": _row_value(sp, "quality", "64K"),
        "fetch_strategy": _row_value(sp, "fetch_strategy", "api_first"),
        "keep_last": _row_value(sp, "keep_last", 100),
        "browser_fallback": bool(_row_value(sp, "browser_fallback", 0)),
        "browser_wait_min_seconds": _row_value(sp, "browser_wait_min_seconds", 4.0),
        "browser_wait_max_seconds": _row_value(sp, "browser_wait_max_seconds", 8.0),
        "browser_fallback_cooldown_seconds": _row_value(sp, "browser_fallback_cooldown_seconds", 3600),
        "require_paid_state_confirmation": bool(_row_value(sp, "require_paid_state_confirmation", 0)),
        "min_duration_seconds": _row_value(sp, "min_duration_seconds", 0),
        "max_duration_seconds": _row_value(sp, "max_duration_seconds", 0),
    }

    filters: dict[str, Any] = {
        "exclude_paid": True,
        "exclude_bvids": [],
        "advertisement_bvids": [],
        "exclude_season_ids": [],
        "exclude_keywords": [],
        "advertisement_keywords": [],
        "include_keywords": [],
    }
    for fr_row in fr:
        rt = fr_row["rule_type"]
        val = fr_row["value"]
        if rt == "exclude_paid":
            filters["exclude_paid"] = val.lower() == "true"
        elif rt == "exclude_bvid":
            filters["exclude_bvids"].append(val)
        elif rt == "advertisement_bvid":
            filters["advertisement_bvids"].append(val)
        elif rt == "exclude_season_id":
            filters["exclude_season_ids"].append(int(val))
        elif rt == "exclude_keyword":
            filters["exclude_keywords"].append(val)
        elif rt == "advertisement_keyword":
            filters["advertisement_keywords"].append(val)
        elif rt == "include_keyword":
            filters["include_keywords"].append(val)

    paid_preview = {
        "enabled": bool(_row_value(pp, "enabled", 0)),
        "retry_after_days": _row_value(pp, "retry_after_days", 4),
    }

    return SeriesConfig(
        series=series,
        enabled=True,
        title=_row_value(row, "title", ""),
        description=_row_value(row, "description", ""),
        author=_row_value(row, "author", ""),
        cover_art=_row_value(row, "cover_art", ""),
        category=_row_value(row, "category", ""),
        subcategories=_parse_subcategories(_row_value(row, "subcategories", "[]")),
        explicit=bool(_row_value(row, "explicit", 0)),
        lang=_row_value(row, "lang", "zh-CN"),
        source=source,
        sync=sync,
        filters=filters,
        paid_preview=paid_preview,
        keep_last=_row_value(sp, "keep_last", 100),
    )


def read_state_file(db_path: str | Path, series: str) -> dict:
    """Read sync state from DB (matching read_json_file interface)."""
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sync_state WHERE series=?", (series,),
        ).fetchone()
    if row is None:
        return {}
    return {
        "last_attempt_at": _row_value(row, "last_attempt_at", 0),
        "last_success_at": _row_value(row, "last_success_at", 0),
        "last_browser_fallback_at": _row_value(row, "last_browser_fallback_at", 0),
        "rate_limited_until": _row_value(row, "rate_limited_until", 0),
        "retry_pending": bool(_row_value(row, "retry_pending", 0)),
    }


def write_state_file(db_path: str | Path, series: str, state: dict) -> None:
    """Write sync state to DB (matching write_json_file interface)."""
    with transaction(db_path) as conn:
        upsert_sync_state(conn, series, state)
