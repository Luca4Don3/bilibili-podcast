from __future__ import annotations

import sqlite3
from typing import Any


def list_filter_entries(filters: dict) -> list[tuple[str, str]]:
    """Convert filters dict to a list of (rule_type, value) tuples for DB storage."""
    entries: list[tuple[str, str]] = []
    entries.append(("exclude_paid", "true" if filters.get("exclude_paid", True) else "false"))
    for bvid in filters.get("exclude_bvids", []):
        entries.append(("exclude_bvid", bvid))
    for bvid in filters.get("advertisement_bvids", []):
        entries.append(("advertisement_bvid", bvid))
    for kw in filters.get("exclude_keywords", []):
        entries.append(("exclude_keyword", kw))
    for kw in filters.get("advertisement_keywords", []):
        entries.append(("advertisement_keyword", kw))
    for kw in filters.get("include_keywords", []):
        entries.append(("include_keyword", kw))
    return entries


def build_filters_from_rows(rows) -> dict[str, Any]:
    """Convert filter_rule DB rows into a structured filters dict.

    Expected row interface: row['rule_type'], row['value']
    Ignores enabled/disabled — caller should filter beforehand.
    """
    filters: dict[str, Any] = {
        "exclude_paid": True,
        "exclude_bvids": [],
        "advertisement_bvids": [],
        "exclude_keywords": [],
        "advertisement_keywords": [],
        "include_keywords": [],
    }
    for r in rows:
        rt = r["rule_type"]
        val = r["value"]
        if rt == "exclude_paid":
            filters["exclude_paid"] = val.lower() == "true"
        elif rt == "exclude_bvid":
            filters["exclude_bvids"].append(val)
        elif rt == "advertisement_bvid":
            filters["advertisement_bvids"].append(val)
        elif rt == "exclude_keyword":
            filters["exclude_keywords"].append(val)
        elif rt == "advertisement_keyword":
            filters["advertisement_keywords"].append(val)
        elif rt == "include_keyword":
            filters["include_keywords"].append(val)
    return filters


class FilterRuleService:
    """Centralized operations for filter_rule CRUD."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace_all(self, series: str, filters: dict[str, Any]) -> int:
        """Delete all filter rules for a series and insert from structured dict.

        Returns the number of rules inserted.
        """
        self.conn.execute("DELETE FROM filter_rule WHERE series=?", (series,))
        entries = list_filter_entries(filters)
        for pos, (rule_type, value) in enumerate(entries):
            self.conn.execute(
                "INSERT INTO filter_rule (series, rule_type, value, enabled, position) VALUES (?, ?, ?, 1, ?)",
                (series, rule_type, value, pos),
            )
        return len(entries)

    def set_exclude_paid(self, series: str, enabled: bool) -> None:
        """Set exclude_paid to a specific boolean value.

        exclude_paid is special: its default is True when no rule exists.
        This ensures exactly one enabled rule with the desired value.
        """
        value = "true" if enabled else "false"
        self.conn.execute(
            "DELETE FROM filter_rule WHERE series=? AND rule_type='exclude_paid'",
            (series,),
        )
        last_pos = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM filter_rule WHERE series=?",
            (series,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO filter_rule (series, rule_type, value, enabled, position) VALUES (?, 'exclude_paid', ?, 1, ?)",
            (series, value, last_pos + 1),
        )

    def add_rules(self, series: str, pairs: list[tuple[str, str]]) -> int:
        """Append filter rules at the end of the position order.

        Returns the number of rules added.
        """
        last_pos = self.conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM filter_rule WHERE series=?",
            (series,),
        ).fetchone()[0]
        for i, (rule_type, value) in enumerate(pairs):
            self.conn.execute(
                "INSERT INTO filter_rule (series, rule_type, value, enabled, position) VALUES (?, ?, ?, 1, ?)",
                (series, rule_type, value, last_pos + 1 + i),
            )
        return len(pairs)

    def remove_rules(self, series: str, conditions: list[tuple[str, str]], delete: bool = False) -> int:
        """Remove filter rules matching (rule_type, value) pairs.

        If delete=False, sets enabled=0. If delete=True, physically deletes.
        Returns the number of affected rows.
        """
        if not conditions:
            return 0
        clauses: list[str] = []
        params: list[str] = [series]
        for rule_type, value in conditions:
            clauses.append("(rule_type=? AND value=?)")
            params.extend([rule_type, value])
        where = " OR ".join(clauses)
        if delete:
            count = self.conn.execute(
                f"DELETE FROM filter_rule WHERE series=? AND ({where})",
                params,
            ).rowcount
        else:
            count = self.conn.execute(
                f"UPDATE filter_rule SET enabled=0 WHERE series=? AND ({where})",
                params,
            ).rowcount
        return count

    def toggle_rule(self, rule_id: int, series: str, enabled: bool) -> bool:
        """Enable or disable a filter rule by ID. Returns True if found."""
        row = self.conn.execute(
            "SELECT id, rule_type FROM filter_rule WHERE id=? AND series=?",
            (rule_id, series),
        ).fetchone()
        if not row:
            return False
        if row["rule_type"] == "exclude_paid":
            self.set_exclude_paid(series, enabled)
            return True
        self.conn.execute(
            f"UPDATE filter_rule SET enabled=? WHERE id=?",
            (int(enabled), rule_id),
        )
        return True
