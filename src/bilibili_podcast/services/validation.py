import re

SERIES_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
CRON_EXPR_RE = re.compile(r"^(\S+\s+){4}\S+$")

SOURCE_TYPE_VALUES = {"space", "season", "series"}
QUALITY_VALUES = {"64K", "132K", "192K", "low", "medium", "high"}
FETCH_STRATEGY_VALUES = {"api_first", "browser_first"}
FORMAT_VALUES = {"audio", "video"}
RULE_TYPE_VALUES = {
    "exclude_paid", "exclude_bvid", "advertisement_bvid",
    "exclude_keyword", "advertisement_keyword", "include_keyword",
    "exclude_season_id",
}


def validate_slug(slug: str) -> bool:
    return bool(SERIES_SLUG_RE.fullmatch(slug))


def validate_bvid(bvid: str) -> bool:
    return bool(BVID_RE.fullmatch(bvid))


def validate_source_type(t: str) -> bool:
    return t in SOURCE_TYPE_VALUES
