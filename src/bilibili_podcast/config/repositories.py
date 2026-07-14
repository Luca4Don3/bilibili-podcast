"""Storage adapters for TOML application config, SQLite, and legacy YAML."""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path
from typing import Any

import yaml

from ..sqlite_connection import connect as sqlite_connect


class TomlRepository:
    def read(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            raise ValueError(f"configuration file is missing: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid TOML in {path}: {exc}") from None
        except OSError as exc:
            raise ValueError(f"cannot read configuration file {path}: {type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise ValueError(f"configuration root must be a table: {path}")
        return data


class SQLiteSeriesRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self):
        from ..db import load_series_configs, migrate

        migrate(self.path)
        return load_series_configs(self.path)

    def access_rule_count(self) -> int:
        if not self.path.exists():
            return 0
        try:
            with sqlite_connect(self.path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM access_rule").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise ValueError(
                f"cannot inspect SQLite legacy state {self.path}: {type(exc).__name__}"
            ) from None
        return int(row[0]) if row else 0


class LegacyYamlRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def paths(self) -> list[Path]:
        return [path for path in sorted(self.root.glob("*.yaml")) if not path.name.startswith("_")]

    def read_raw(self, path: Path) -> dict[str, Any]:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid legacy YAML {path}: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError(f"legacy YAML root must be a mapping: {path}")
        return data

    def load(self):
        from ..utils.series_config import load_series_configs

        return load_series_configs(self.root)
