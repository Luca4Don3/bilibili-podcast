from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .filter_service import build_filters_from_rows
from .sync_policy_service import SYNC_POLICY_DEFAULTS


def _row_value(row, key: str, default: Any = None) -> Any:
    try:
        v = row[key]
        return v if v is not None else default
    except (KeyError, IndexError):
        return default


class ConfigService:
    """Shared config loading for CLI and Web."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def load_series(self, series: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM series WHERE series=?", (series,)).fetchone()
        return dict(row) if row else None

    def load_source(self, series: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM series_source WHERE series=?", (series,)).fetchone()
        return dict(row) if row else {}

    def load_sync_policy(self, series: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sync_policy WHERE series=?", (series,)).fetchone()
        return dict(row) if row else {}

    def load_paid_preview(self, series: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM paid_preview_policy WHERE series=?", (series,)).fetchone()
        return dict(row) if row else {}

    def load_cron_schedules(self, series: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT schedule FROM cron_schedule WHERE series=? AND enabled=1 ORDER BY position",
            (series,),
        ).fetchall()
        return [s["schedule"] for s in rows]

    def load_sync_state(self, series: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM sync_state WHERE series=?", (series,)).fetchone()
        return dict(row) if row else {}

    def load_filter_rules(self, series: str, only_enabled: bool = True) -> list[dict[str, Any]]:
        if only_enabled:
            rows = self.conn.execute(
                "SELECT rule_type, value FROM filter_rule WHERE series=? AND enabled=1 ORDER BY position",
                (series,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, rule_type, value, enabled, position FROM filter_rule WHERE series=? ORDER BY position",
                (series,),
            ).fetchall()
        return [dict(r) for r in rows]

    def load_filter_dict(self, series: str) -> dict[str, Any]:
        """Build a structured filters dict from DB rows (same as build_filters_from_rows)."""
        rows = self.conn.execute(
            "SELECT rule_type, value FROM filter_rule WHERE series=? AND enabled=1 ORDER BY position",
            (series,),
        ).fetchall()
        return build_filters_from_rows(rows)

    def series_exists(self, series: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM series WHERE series=?", (series,)).fetchone()
        return row is not None

    def load_full_config(self, series: str) -> dict[str, Any] | None:
        """Load all config for a series into a single nested dict (CLI-style)."""
        s = self.load_series(series)
        if s is None:
            return None
        return {
            "series": s,
            "source": self.load_source(series),
            "sync": self.load_sync_policy(series),
            "filters": self.load_filter_rules(series, only_enabled=False),
            "paid_preview": self.load_paid_preview(series),
            "cron": self.load_cron_schedules(series),
            "state": self.load_sync_state(series),
        }

    def load_full_config_tuple(self, series: str) -> tuple:
        """Load all config as a 7-tuple (Web-style: data, source, sync, filters, pp, state, scheds)."""
        s = self.load_series(series)
        if s is None:
            return (None,) * 7
        source = self.load_source(series)
        sync = self.load_sync_policy(series)
        filters = self.load_filter_dict(series)
        pp = self.load_paid_preview(series)
        state = self.load_sync_state(series)
        scheds = self.load_cron_schedules(series)
        return (s, source, sync, filters, pp, state, scheds)
