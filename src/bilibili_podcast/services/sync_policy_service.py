from __future__ import annotations

import sqlite3
from typing import Any


SYNC_POLICY_DEFAULTS: dict[str, Any] = {
    "page_size": 20,
    "incremental_page_size": 5,
    "max_pages": 10,
    "max_requests_per_series": 8,
    "request_interval_seconds": 2.0,
    "request_jitter_seconds": 0.5,
    "rate_limit_cooldown_seconds": 21600,
    "update_period": "12h",
    "format": "audio",
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

# Column names (and order) that map to the INSERT/UPDATE for sync_policy.
SYNC_POLICY_COLUMNS = tuple(SYNC_POLICY_DEFAULTS.keys())

# BOOLEAN fields that need int() conversion for SQLite
BOOL_FIELDS = {
    "browser_fallback",
    "require_paid_state_confirmation",
}


def _sync_values(sync: dict[str, Any]) -> list[Any]:
    """Convert a sync dict to a list matching SYNC_POLICY_COLUMNS order,
    applying boolean->int and default fallback."""
    values: list[Any] = []
    for col in SYNC_POLICY_COLUMNS:
        val = sync.get(col)
        if val is None:
            val = SYNC_POLICY_DEFAULTS[col]
        if col in BOOL_FIELDS:
            val = int(bool(val))
        values.append(val)
    return values


SQL_INSERT = f"""INSERT INTO sync_policy (
    series, {', '.join(SYNC_POLICY_COLUMNS)}
) VALUES (
    ?, {', '.join('?' for _ in SYNC_POLICY_COLUMNS)}
) ON CONFLICT(series) DO UPDATE SET
    {', '.join(f'{col}=excluded.{col}' for col in SYNC_POLICY_COLUMNS)}
"""

SQL_UPDATE = f"""UPDATE sync_policy SET
    {', '.join(f'{col}=?' for col in SYNC_POLICY_COLUMNS)}
WHERE series=?
"""


class SyncPolicyService:
    """Centralized operations for sync_policy CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert(self, series: str, sync: dict[str, Any]) -> None:
        """Insert or update the full sync_policy row."""
        self._validate_update_period(series, sync.get("update_period", "12h"))
        values = _sync_values(sync)
        self.conn.execute(SQL_INSERT, (series, *values))

    def update_fields(self, series: str, updates: dict[str, Any]) -> None:
        """Partially update specific fields. Falls back to current values
        for unspecified fields via full upsert (insert-then-update)."""
        if not updates:
            return
        if "update_period" in updates:
            self._validate_update_period(series, updates["update_period"])
        # Ensure row exists
        existing = self.conn.execute(
            "SELECT 1 FROM sync_policy WHERE series=?", (series,),
        ).fetchone()
        if not existing:
            self.conn.execute("INSERT INTO sync_policy (series) VALUES (?)", (series,))

        normalized = [
            (key, int(bool(value)) if key in BOOL_FIELDS else value)
            for key, value in updates.items()
        ]
        set_clause = ", ".join(f"{key}=?" for key, _ in normalized)
        values = [value for _, value in normalized]
        self.conn.execute(
            f"UPDATE sync_policy SET {set_clause} WHERE series=?",
            (*values, series),
        )

    def _validate_update_period(self, series: str, update_period: object) -> None:
        from .scheduler_service import ScheduleEntry, validate_schedules

        rows = self.conn.execute(
            "SELECT id, schedule, enabled, position, kind "
            "FROM cron_schedule WHERE series=? AND enabled=1 ORDER BY position",
            (series,),
        ).fetchall()
        validate_schedules([
            ScheduleEntry(
                id=row[0], series=series, schedule=row[1],
                enabled=bool(row[2]), position=row[3], kind=row[4],
            )
            for row in rows
        ], update_period)
