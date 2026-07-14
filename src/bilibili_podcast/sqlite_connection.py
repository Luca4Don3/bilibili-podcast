"""Shared SQLite connection policy for blue/green compatible processes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


def connect(path: str | Path, **kwargs) -> sqlite3.Connection:
    kwargs.setdefault("timeout", BUSY_TIMEOUT_MS / 1000)
    connection = sqlite3.connect(str(path), **kwargs)
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection
