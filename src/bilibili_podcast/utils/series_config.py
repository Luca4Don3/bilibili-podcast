import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


SERIES_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class SeriesConfig:
    series: str
    enabled: bool
    title: str
    description: str
    author: str
    cover_art: str
    category: str
    subcategories: list[str]
    explicit: bool
    lang: str
    source: dict
    sync: dict
    filters: dict
    paid_preview: dict
    keep_last: int

    @property
    def uid(self) -> int:
        uid = self.source.get("uid")
        if uid:
            return int(uid)
        space_url = self.source.get("space_url", "")
        path = urlparse(space_url).path.strip("/")
        candidate = path.split("/", 1)[0] if path else ""
        if candidate.isdigit():
            return int(candidate)
        raise ValueError("source.uid or source.space_url with UID is required")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SeriesConfig":
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sync = data.get("sync") or {}
        filters = data.get("filters") or {}
        keep_last = sync.get("keep_last", 100)

        series = data.get("series")
        title = data.get("title")
        author = data.get("author")
        source = data.get("source") or {}
        if not series:
            raise ValueError("series is required")
        if not isinstance(series, str) or not SERIES_SLUG_RE.fullmatch(series):
            raise ValueError("series must use lowercase letters, numbers, hyphens, or underscores")
        if path.stem != series:
            raise ValueError(f"series must match config file name: {path.name}")
        if not title:
            raise ValueError("title is required")
        if not author:
            raise ValueError("author must be the Bilibili UP name")
        if not source.get("space_url") and not source.get("uid"):
            raise ValueError("source.space_url or source.uid is required")
        if not isinstance(keep_last, int) or keep_last < 0:
            raise ValueError("sync.keep_last must be 0 or a positive integer")
        try:
            exclude_season_ids = [int(value) for value in filters.get("exclude_season_ids", [])]
        except (TypeError, ValueError) as exc:
            raise ValueError("filters.exclude_season_ids must contain positive integers") from exc
        if any(value <= 0 for value in exclude_season_ids):
            raise ValueError("filters.exclude_season_ids must contain positive integers")
        filters["exclude_season_ids"] = exclude_season_ids

        return cls(
            series=series,
            enabled=data.get("enabled", True),
            title=title,
            description=data.get("description") or "",
            author=author,
            cover_art=data.get("cover_art") or "",
            category=data.get("category") or "",
            subcategories=data.get("subcategories") or [],
            explicit=data.get("explicit", False),
            lang=data.get("lang") or "zh-CN",
            source=source,
            sync=sync,
            filters=filters,
            paid_preview=data.get("paid_preview") or {"enabled": False},
            keep_last=keep_last,
        )


def load_series_configs(config_dir: str | Path) -> list[SeriesConfig]:
    config_path = Path(config_dir)
    configs = []
    for path in sorted(config_path.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        configs.append(SeriesConfig.from_yaml(path))
    return configs
