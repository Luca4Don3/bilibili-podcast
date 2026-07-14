import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils.series_config import SeriesConfig
from .services.filter_service import list_filter_entries
from .services.sync_policy_service import SyncPolicyService
from .config.schema import QUALITY_ALIASES, SERIES_SYNC_DEFAULTS
from .sqlite_connection import connect


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


def migrate(db_path: str | Path, *, initialize_version: bool = True) -> None:
    """Create or migrate the SQLite schema."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _schema_statements(SCHEMA_SQL):
            conn.execute(statement)
        _ensure_column(conn, "cron_schedule", "kind", "TEXT NOT NULL DEFAULT 'primary'")
        _ensure_column(conn, "sync_state", "retry_pending", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "sync_policy", "update_period_grace_seconds", "INTEGER NOT NULL DEFAULT 120")
        _ensure_column(conn, "sync_policy", "media_mode", "TEXT NOT NULL DEFAULT 'auto'")
        if initialize_version and conn.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone() is None:
            from .config.migration.versioning import LATEST_VERSION

            conn.execute("INSERT INTO schema_version(version) VALUES(?)", (LATEST_VERSION,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _schema_statements(script: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise sqlite3.OperationalError("incomplete SQLite schema statement")
    return tuple(statements)


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
    update_period_grace_seconds INTEGER NOT NULL DEFAULT 120,
    format TEXT NOT NULL DEFAULT 'audio',
    media_mode TEXT NOT NULL DEFAULT 'auto',
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
    conn = connect(db_path)
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
    sync = dict(config.sync)
    sync["keep_last"] = config.keep_last
    sync["quality"] = QUALITY_ALIASES.get(sync.get("quality"), sync.get("quality", "64K"))
    SyncPolicyService(conn).upsert(config.series, sync)


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
    conn.execute("DELETE FROM cron_schedule WHERE series=? AND kind='primary'", (series,))
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
        key: _row_value(sp, key, default)
        for key, default in SERIES_SYNC_DEFAULTS.items()
    }
    sync["quality"] = QUALITY_ALIASES.get(sync["quality"], sync["quality"])
    sync["browser_fallback"] = bool(sync["browser_fallback"])
    sync["require_paid_state_confirmation"] = bool(sync["require_paid_state_confirmation"])

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
