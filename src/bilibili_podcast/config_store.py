"""Unified config/state store — YAML+JSON files or SQLite backend."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .db import migrate as _migrate_db
from .db import load_series_configs as _db_load
from .db import read_state_file as _db_read_state
from .db import write_state_file as _db_write_state
from .utils.series_config import SeriesConfig, load_series_configs as _yaml_load


def from_args(config_dir: str, state_root: str, config_db: str | None = None) -> ConfigStore:
    """Select the appropriate store based on CLI args."""
    if config_db:
        return DbStore(Path(config_db))
    return YamlStore(Path(config_dir), Path(state_root))


class ConfigStore:
    """Abstracts config loading and state persistence."""

    def load_configs(self, series_filter: str | None = None) -> list[SeriesConfig]:
        """Load all enabled series configs, optionally filtered by comma-separated series names."""
        raise NotImplementedError

    def read_state(self, series: str) -> dict[str, Any]:
        raise NotImplementedError

    def write_state(self, series: str, state: dict[str, Any]) -> None:
        raise NotImplementedError


class YamlStore(ConfigStore):
    def __init__(self, config_dir: Path, state_root: Path) -> None:
        self.config_dir = config_dir
        self.state_root = state_root

    def load_configs(self, series_filter: str | None = None) -> list[SeriesConfig]:
        configs = _yaml_load(self.config_dir)
        return _select_configs(configs, series_filter)

    def read_state(self, series: str) -> dict[str, Any]:
        path = self.state_root / f"{series}.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [warn] failed to read state file {path}: {exc}", file=sys.stderr)
            return {}

    def write_state(self, series: str, state: dict[str, Any]) -> None:
        path = self.state_root / f"{series}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o644)


class DbStore(ConfigStore):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        _migrate_db(str(db_path))

    def load_configs(self, series_filter: str | None = None) -> list[SeriesConfig]:
        configs = _db_load(str(self.db_path))
        return _select_configs(configs, series_filter)

    def read_state(self, series: str) -> dict[str, Any]:
        return _db_read_state(str(self.db_path), series)

    def write_state(self, series: str, state: dict[str, Any]) -> None:
        _db_write_state(str(self.db_path), series, state)


def _select_configs(configs: list[SeriesConfig], series_filter: str | None) -> list[SeriesConfig]:
    selected = [c for c in configs if c.enabled]
    if series_filter:
        wanted = {item.strip() for item in series_filter.split(",") if item.strip()}
        selected = [c for c in selected if c.series in wanted]
    return selected
