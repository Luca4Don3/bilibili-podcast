from __future__ import annotations

import math
import sqlite3
from typing import Any

from ..config.schema import SERIES_SYNC_DEFAULTS


SYNC_POLICY_DEFAULTS: dict[str, Any] = dict(SERIES_SYNC_DEFAULTS)

# Column names (and order) that map to the INSERT/UPDATE for sync_policy.
SYNC_POLICY_COLUMNS = tuple(SYNC_POLICY_DEFAULTS.keys())

# BOOLEAN fields that need int() conversion for SQLite
BOOL_FIELDS = {
    "browser_fallback",
    "require_paid_state_confirmation",
}

ENUM_FIELDS = {
    "format": {"audio", "video"},
    "media_mode": {"auto", "manual"},
    "quality": {"64K", "132K", "192K"},
    "fetch_strategy": {"api_first", "browser_first"},
}
POSITIVE_FIELDS = {
    "page_size", "incremental_page_size", "max_pages", "max_requests_per_series",
}


def _validate_policy(sync: dict[str, Any]) -> None:
    unknown = sorted(set(sync) - set(SYNC_POLICY_COLUMNS))
    if unknown:
        raise ValueError(f"unknown sync policy field: {unknown[0]}")
    for key, default in SYNC_POLICY_DEFAULTS.items():
        value = sync.get(key, default)
        if key in BOOL_FIELDS:
            if not isinstance(value, (bool, int)) or value not in (0, 1, False, True):
                raise ValueError(f"invalid sync policy type: {key}")
        elif isinstance(default, int):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"invalid sync policy type: {key}")
        elif isinstance(default, float):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"invalid sync policy type: {key}")
        elif not isinstance(value, str):
            raise ValueError(f"invalid sync policy type: {key}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"invalid sync policy value: {key}")
    for key, allowed in ENUM_FIELDS.items():
        if sync.get(key, SYNC_POLICY_DEFAULTS[key]) not in allowed:
            raise ValueError(f"invalid sync policy value: {key}")
    for key in POSITIVE_FIELDS:
        if sync.get(key, SYNC_POLICY_DEFAULTS[key]) <= 0:
            raise ValueError(f"invalid sync policy value: {key}")
    if sync.get("page_size", SYNC_POLICY_DEFAULTS["page_size"]) > 50:
        raise ValueError("invalid sync policy value: page_size")
    if sync.get("browser_wait_min_seconds", 0) > sync.get("browser_wait_max_seconds", 0):
        raise ValueError("browser_wait_min_seconds exceeds browser_wait_max_seconds")
    minimum = sync.get("min_duration_seconds", 0)
    maximum = sync.get("max_duration_seconds", 0)
    if maximum and minimum > maximum:
        raise ValueError("min_duration_seconds exceeds max_duration_seconds")


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
        merged = {**SYNC_POLICY_DEFAULTS, **sync}
        _validate_policy(merged)
        self._validate_update_period(series, merged["update_period"])
        values = _sync_values(merged)
        self.conn.execute(SQL_INSERT, (series, *values))

    def update_fields(self, series: str, updates: dict[str, Any]) -> None:
        """Partially update specific fields. Falls back to current values
        for unspecified fields via full upsert (insert-then-update)."""
        if not updates:
            return
        existing = self.conn.execute(
            f"SELECT {', '.join(SYNC_POLICY_COLUMNS)} FROM sync_policy WHERE series=?",
            (series,),
        ).fetchone()
        if not existing:
            merged = dict(SYNC_POLICY_DEFAULTS)
        else:
            merged = dict(zip(SYNC_POLICY_COLUMNS, existing))
        merged.update(updates)
        _validate_policy(merged)
        self._validate_update_period(series, merged["update_period"])

        normalized = [
            (key, int(bool(value)) if key in BOOL_FIELDS else value)
            for key, value in updates.items()
        ]
        set_clause = ", ".join(f"{key}=?" for key, _ in normalized)
        values = [value for _, value in normalized]
        if not existing:
            self.conn.execute("INSERT INTO sync_policy (series) VALUES (?)", (series,))
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
