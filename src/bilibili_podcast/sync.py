import argparse
import asyncio
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from .utils.series_config import SeriesConfig
from .config_store import from_args as make_store
from .utils.paid_content import has_paid_state, is_paid_content
from .config import ConfigError, ConfigManager, ConfigSnapshot


QUALITY_TO_AUDIO = {
    "64K": "64K",
    "132K": "132K",
    "192K": "192K",
    "low": "64K",
    "medium": "132K",
    "high": "192K",
}
DEFAULT_REQUEST_INTERVAL_SECONDS = 2.0
LOG_RETENTION_DAYS = 30
LOG_BACKUP_NAMES = ("sync.log", "sync.error.log", "playwright.log")
DEFAULT_REQUEST_JITTER_SECONDS = 0.5
DEFAULT_INCREMENTAL_PAGE_SIZE = 5
DEFAULT_MAX_REQUESTS_PER_SERIES = 8
DEFAULT_BROWSER_WAIT_SECONDS = 5.0
DEFAULT_BROWSER_FALLBACK_COOLDOWN_SECONDS = 3600
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 21600
DEFAULT_UPDATE_PERIOD_GRACE_SECONDS = 120
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
EXIT_SYNC_ERROR = 1
EXIT_PUBLISH_ERROR = 3
MAX_PUBLISH_ERROR_CHARS = 4000

LOGGER = logging.getLogger("bilibili_podcast.sync")
PLAYWRIGHT_LOGGER = logging.getLogger("bilibili_podcast.sync.playwright")


def sanitize_external_output(text: str) -> str:
    """Redact common credentials and bound external command output for logs."""
    sanitized = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", text or "")
    sanitized = re.sub(
        r"(?i)(token|secret|password|authorization)\s*[=:]\s*\S+",
        r"\1=***",
        sanitized,
    )
    return sanitized[-MAX_PUBLISH_ERROR_CHARS:]


def parse_log_level(value: str) -> str:
    level = value.upper()
    if level not in LOG_LEVELS:
        choices = ", ".join(LOG_LEVELS)
        raise argparse.ArgumentTypeError(f"invalid log level {value!r}; choose one of: {choices}")
    return level


def cleanup_old_log_backups(
    log_root: Path,
    retention_days: int = LOG_RETENTION_DAYS,
    *,
    now: float | None = None,
) -> int:
    """Delete recognized rotated log backups older than the retention window."""
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    removed = 0
    for base_name in LOG_BACKUP_NAMES:
        for candidate in log_root.glob(f"{base_name}.*"):
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                if candidate.stat().st_mtime >= cutoff:
                    continue
                candidate.unlink()
                removed += 1
            except OSError as exc:
                LOGGER.warning("log cleanup failed path=%s error=%s", candidate, exc)
    if removed:
        LOGGER.info(
            "log cleanup complete log_dir=%s retention_days=%s removed=%s",
            log_root,
            retention_days,
            removed,
        )
    return removed


def setup_logging(
    log_dir: str,
    log_level: str = "INFO",
    debug: bool = False,
    *,
    retention_days: int = LOG_RETENTION_DAYS,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
) -> Path:
    if isinstance(log_level, bool):
        debug = log_level
        log_level = "INFO"
    requested_log_root = Path(log_dir)
    effective_level_name = "DEBUG" if debug else parse_log_level(log_level)
    effective_level = LOG_LEVELS[effective_level_name]

    def configure_handlers(log_root: Path) -> None:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        for logger, filename in (
            (LOGGER, "sync.log"),
            (PLAYWRIGHT_LOGGER, "playwright.log"),
        ):
            logger.setLevel(effective_level)
            logger.propagate = False
            logger.handlers.clear()
            handler = RotatingFileHandler(
                log_root / filename,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        # Separate error-only log for quick monitoring
        error_handler = RotatingFileHandler(
            log_root / "sync.error.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        LOGGER.addHandler(error_handler)

    log_root = requested_log_root
    try:
        log_root.mkdir(parents=True, exist_ok=True)
        configure_handlers(log_root)
        cleanup_old_log_backups(log_root, retention_days)
    except OSError as exc:
        fallback = Path("/tmp/bilibili-podcast-logs")
        fallback.mkdir(parents=True, exist_ok=True)
        print(
            f"cannot write bilibili-podcast logs under {requested_log_root}: {exc}; using {fallback}",
            file=sys.stderr,
        )
        log_root = fallback
        configure_handlers(log_root)
        cleanup_old_log_backups(log_root, retention_days)

    LOGGER.info(
        "logging initialized log_dir=%s pid=%s log_level=%s debug=%s",
        log_root,
        os.getpid(),
        effective_level_name,
        debug,
    )
    return log_root


def log_result(result: dict) -> None:
    level = logging.ERROR if result.get("error") else logging.INFO
    LOGGER.log(level, "result %s", json.dumps(result, ensure_ascii=False, sort_keys=True))


@contextmanager
def process_lock(lock_file: str):
    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.warning("another bilibili-podcast process is already running lock_file=%s", lock_file)
            print(f"another bilibili-podcast process is already running: {lock_file}", file=sys.stderr)
            raise SystemExit(2)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        LOGGER.info("acquired process lock lock_file=%s", lock_file)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            LOGGER.info("released process lock lock_file=%s", lock_file)


@dataclass
class SyncPaths:
    media_root: Path
    json_root: Path
    rss_root: Path
    media_base_url: str


def now_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def parse_duration_seconds(value, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    text = str(value).strip().lower()
    if not text:
        return default
    try:
        return max(int(float(text)), 0)
    except ValueError:
        pass
    unit = text[-1]
    number = text[:-1]
    try:
        amount = float(number)
    except ValueError:
        return default
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }
    return max(int(amount * multipliers.get(unit, 1)), 0)


def episode_duration_seconds(value) -> int:
    """Convert episode duration (from B站 API) to integer seconds.

    Handles integer seconds, "mm:ss", "hh:mm:ss", and suffixed formats.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    text = str(value).strip()
    if not text:
        return 0
    # Try "mm:ss" or "hh:mm:ss"
    if ":" in text:
        parts = text.split(":")
        try:
            parts = [int(p) for p in parts]
        except ValueError:
            pass
        else:
            if len(parts) == 2:
                return max(parts[0] * 60 + parts[1], 0)
            if len(parts) == 3:
                return max(parts[0] * 3600 + parts[1] * 60 + parts[2], 0)
    # Try pure int
    try:
        return max(int(float(text)), 0)
    except ValueError:
        pass
    return parse_duration_seconds(value, 0)


def update_period_seconds(config: SeriesConfig) -> int:
    return parse_duration_seconds(config.sync.get("update_period"), 12 * 3600)


def rate_limit_cooldown_seconds(config: SeriesConfig) -> int:
    return parse_duration_seconds(
        config.sync.get("rate_limit_cooldown_seconds"),
        DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    )


def update_period_grace_seconds(config: SeriesConfig) -> int:
    return parse_duration_seconds(
        config.sync.get("update_period_grace_seconds"),
        DEFAULT_UPDATE_PERIOD_GRACE_SECONDS,
    )


def next_allowed_run_at(config: SeriesConfig, state: dict) -> int:
    last_success_at = int(state.get("last_success_at", 0) or 0)
    rate_limited_until = int(state.get("rate_limited_until", 0) or 0)
    scheduled_at = last_success_at + update_period_seconds(config) if last_success_at else 0
    return max(scheduled_at, rate_limited_until)


def should_skip_series(config: SeriesConfig, state: dict, force: bool) -> tuple[bool, str, int]:
    if force:
        return False, "", 0
    allowed_at = next_allowed_run_at(config, state)
    now = now_timestamp()
    if allowed_at > now:
        rate_limited_until = int(state.get("rate_limited_until", 0) or 0)
        if rate_limited_until >= allowed_at:
            return True, "rate_limit_cooldown", allowed_at
        if allowed_at - now <= update_period_grace_seconds(config):
            return False, "", allowed_at
        reason = "rate_limit_cooldown" if int(state.get("rate_limited_until", 0) or 0) >= allowed_at else "update_period"
        return True, reason, allowed_at
    return False, "", allowed_at


def load_cookie_file(cookie_file: Optional[str]):
    if not cookie_file:
        return None
    from bilibili_api import Credential

    values = {}
    for parts in iter_netscape_cookie_parts(cookie_file):
        if len(parts) >= 7:
            values[parts[5]] = parts[6]

    sessdata = values.get("SESSDATA")
    bili_jct = values.get("bili_jct")
    dedeuserid = values.get("DedeUserID") or values.get("DedeUserID__ckMd5")
    buvid3 = values.get("buvid3")
    if not all([sessdata, bili_jct, dedeuserid, buvid3]):
        raise ValueError("cookie file must contain SESSDATA, bili_jct, DedeUserID, and buvid3")

    return Credential(
        sessdata=sessdata,
        bili_jct=bili_jct,
        dedeuserid=dedeuserid,
        buvid3=buvid3,
        buvid4=values.get("buvid4", ""),
        ac_time_value=values.get("ac_time_value", ""),
    )


def load_browser_cookies(cookie_file: Optional[str]) -> list[dict]:
    if not cookie_file:
        return []
    cookies = []
    for parts in iter_netscape_cookie_parts(cookie_file):
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            expires_int = int(expires)
        except ValueError:
            expires_int = 0
        if expires_int > 0:
            cookie["expires"] = expires_int
        cookies.append(cookie)
    return cookies


def iter_netscape_cookie_parts(cookie_file: str):
    for line in Path(cookie_file).read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        yield line.split("\t")


def audio_quality(config: SeriesConfig) -> str:
    return QUALITY_TO_AUDIO.get(str(config.sync.get("quality", "64K")), "64K")


def media_path(config: SeriesConfig, paths: SyncPaths, bvid: str) -> Path:
    return paths.media_root / config.series / f"{bvid}_{audio_quality(config)}.mp3"


def json_path(config: SeriesConfig, paths: SyncPaths, bvid: str) -> Path:
    return paths.json_root / config.series / f"{bvid}_{audio_quality(config)}.info.json"


def media_url(config: SeriesConfig, paths: SyncPaths, bvid: str, token: Optional[str]) -> str:
    if not token:
        raise ValueError(
            f"media token is required for series={config.series} bvid={bvid} — "
            "ensure --token is passed to avoid publishing tokenless media URLs"
        )
    base = paths.media_base_url.rstrip("/")
    url = f"{base}/media/{config.series}/{bvid}_{audio_quality(config)}.mp3"
    if token:
        url = f"{url}?token={token}"
    return url


def needs_paid_state(config: SeriesConfig) -> bool:
    return bool(config.filters.get("exclude_paid", True) or config.paid_preview.get("enabled", False))


def must_confirm_paid_state(config: SeriesConfig) -> bool:
    return bool(config.paid_preview.get("enabled", False) or config.sync.get("require_paid_state_confirmation", False))


def text_matches(keywords: list[str], episode: dict) -> bool:
    title = str(episode.get("title", "")).lower()
    description = str(episode.get("description", "")).lower()
    return any(keyword.lower() in title or keyword.lower() in description for keyword in keywords)


def media_item_to_episode(item: dict) -> dict:
    bvid = item.get("bv_id") or item.get("bvid")
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": item.get("intro") or item.get("description", ""),
        "duration": item.get("duration") or item.get("length", 0),
        "image": item.get("cover") or item.get("pic", ""),
        "pubdate": item.get("pubtime") or item.get("created", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": item,
    }


def browser_item_to_episode(item: dict) -> dict:
    bvid = item["bvid"]
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": "",
        "duration": 0,
        "image": "",
        "pubdate": 0,
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": {"source": "playwright", "browser_text": item.get("text", ""), **item},
    }


def is_safe_enclosure_url(url: str) -> bool:
    """Return True if *url* contains a token or placeholder.

    Rejects tokenless URLs that would bypass Nginx auth and cause 403/fail2ban.
    """
    return "?token=" in url or "__MEDIA_PLACEHOLDER__" in url


def sync_number(config: SeriesConfig, key: str, default: float) -> float:
    value = config.sync.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sync_int(config: SeriesConfig, key: str, default: int) -> int:
    value = config.sync.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def browser_fallback_cooldown_seconds(config: SeriesConfig) -> int:
    return max(
        sync_int(
            config,
            "browser_fallback_cooldown_seconds",
            DEFAULT_BROWSER_FALLBACK_COOLDOWN_SECONDS,
        ),
        0,
    )


def browser_fallback_allowed(config: SeriesConfig, state: dict) -> bool:
    last_browser_at = int(state.get("last_browser_fallback_at", 0) or 0)
    cooldown = browser_fallback_cooldown_seconds(config)
    now = now_timestamp()
    if last_browser_at and now - last_browser_at < cooldown:
        LOGGER.debug(
            "browser fallback blocked series=%s cooldown=%ss remaining=%ss",
            config.series, cooldown, cooldown - (now - last_browser_at),
        )
        return False
    return True


def fetch_strategy(config: SeriesConfig) -> str:
    strategy = str(config.sync.get("fetch_strategy", "api_first")).strip().lower()
    if strategy not in {"api_first", "browser_first"}:
        return "api_first"
    return strategy


def browser_wait_seconds(config: SeriesConfig) -> float:
    minimum = sync_number(
        config,
        "browser_wait_min_seconds",
        sync_number(config, "browser_wait_seconds", DEFAULT_BROWSER_WAIT_SECONDS),
    )
    maximum = sync_number(config, "browser_wait_max_seconds", minimum)
    minimum = max(minimum, 1.0)
    maximum = max(maximum, minimum)
    return random.uniform(minimum, maximum)


def browser_user_data_dir(root: str, config: SeriesConfig) -> Path:
    safe_series = "".join(ch for ch in config.series if ch.isalnum() or ch in "-_")
    return Path(root) / safe_series


async def polite_sleep(config: SeriesConfig, request_count: int) -> None:
    if request_count <= 0:
        return
    interval = max(sync_number(config, "request_interval_seconds", DEFAULT_REQUEST_INTERVAL_SECONDS), 0.0)
    jitter = max(sync_number(config, "request_jitter_seconds", DEFAULT_REQUEST_JITTER_SECONDS), 0.0)
    if interval <= 0 and jitter <= 0:
        return
    await asyncio.sleep(interval + random.uniform(0, jitter))


def is_bilibili_rate_limited(error: Exception) -> bool:
    text = str(error)
    return "-799" in text or "请求过于频繁" in text or "rate limit" in text.lower()


async def fetch_space_episodes(config: SeriesConfig, credential) -> tuple[dict, list[dict], int]:
    from bilibili_api import request_settings, user

    request_settings.set("impersonate", "chrome131")
    user_obj = user.User(uid=config.uid, credential=credential)
    LOGGER.debug("api fetch user info series=%s uid=%s", config.series, config.uid)
    try:
        info = await user_obj.get_user_info()
    except Exception as exc:
        LOGGER.warning("api user info failed series=%s uid=%s error=%s", config.series, config.uid, exc)
        info = {
            "name": config.author,
            "face": config.cover_art,
            "sign": config.description,
        }

    page_size = min(max(sync_int(config, "page_size", 20), 1), 50)
    incremental_page_size = min(
        max(sync_int(config, "incremental_page_size", DEFAULT_INCREMENTAL_PAGE_SIZE), 1),
        page_size,
    )
    max_pages = max(sync_int(config, "max_pages", 10), 1)
    max_items = max_pages * page_size
    target = config.keep_last if config.keep_last > 0 else max_items
    max_requests = max(sync_int(config, "max_requests_per_series", DEFAULT_MAX_REQUESTS_PER_SERIES), 1)
    request_count = 0
    stopped_by_rate_limit = False
    episodes: list[dict] = []
    seen_bvids = set()

    async def fetch_page(page_number: int, size: int) -> int:
        nonlocal request_count, stopped_by_rate_limit
        if request_count >= max_requests:
            return 0
        await polite_sleep(config, request_count)
        LOGGER.debug(
            "api fetch videos series=%s uid=%s page=%s size=%s request=%s/%s",
            config.series,
            config.uid,
            page_number,
            size,
            request_count + 1,
            max_requests,
        )
        video_list = await user_obj.get_videos(
            pn=page_number,
            ps=size,
            order=user.VideoOrder.PUBDATE,
        )
        request_count += 1
        items = video_list.get("list", {}).get("vlist", [])
        added = 0
        for item in items:
            bvid = item.get("bvid") or item.get("bv_id")
            if not bvid or bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)
            episodes.append(media_item_to_episode(item))
            added += 1
        return added

    await fetch_page(1, page_size)
    while len(limit_episodes(config, apply_filters(config, episodes)[0])) < target:
        if len(episodes) >= max_items or request_count >= max_requests or stopped_by_rate_limit:
            break
        # Page-number pagination depends on ps. Use small follow-up batches so
        # filtered feeds can top up without scanning like a crawler.
        page_number = (len(episodes) // incremental_page_size) + 1
        try:
            added = await fetch_page(page_number, incremental_page_size)
        except Exception as exc:
            if is_bilibili_rate_limited(exc):
                LOGGER.warning(
                    "api rate limited series=%s uid=%s page=%s error=%s",
                    config.series, config.uid, page_number, exc,
                )
                stopped_by_rate_limit = True
                break
            raise
        if added == 0:
            break

    info["_bilibili_podcast_request_count"] = request_count
    info["_bilibili_podcast_stopped_by_rate_limit"] = stopped_by_rate_limit
    LOGGER.info(
        "api fetch complete series=%s uid=%s fetched=%s requests=%s rate_limited=%s",
        config.series,
        config.uid,
        len(episodes),
        request_count,
        stopped_by_rate_limit,
    )
    return info, episodes, len(episodes)


async def fetch_series_episodes(config: SeriesConfig, credential) -> tuple[dict, list[dict], int]:
    from bilibili_api import channel_series, request_settings

    request_settings.set("impersonate", "chrome131")

    sid = config.source.get("sid")
    if not sid:
        raise ValueError("source.sid is required for series fetch mode")

    playlist_type = str(config.source.get("type", "season")).strip().lower()
    if playlist_type == "series":
        series_type = channel_series.ChannelSeriesType.SERIES
    else:
        series_type = channel_series.ChannelSeriesType.SEASON

    series = channel_series.ChannelSeries(
        id_=sid,
        type_=series_type,
        credential=credential,
    )

    LOGGER.info(
        "api fetch series series=%s sid=%s playlist_type=%s",
        config.series, sid, playlist_type,
    )

    try:
        meta = await series.get_meta()
    except Exception as exc:
        LOGGER.warning("api series meta failed series=%s sid=%s error=%s", config.series, sid, exc)
        meta = {}

    if playlist_type == "season":
        info = {
            "name": meta.get("upper", {}).get("name", config.author),
            "face": config.cover_art or meta.get("cover", ""),
            "sign": meta.get("intro", config.description),
        }
        LOGGER.debug(
            "api series meta series=%s name=%s total=%s",
            config.series, info["name"], meta.get("total", "?"),
        )
    else:
        info = {
            "name": config.author,
            "face": config.cover_art,
            "sign": config.description,
        }

    page_size = 100
    max_pages = max(sync_int(config, "max_pages", 10), 1)
    max_items = max_pages * page_size
    max_requests = max(sync_int(config, "max_requests_per_series", DEFAULT_MAX_REQUESTS_PER_SERIES), 1)
    request_count = 0
    stopped_by_rate_limit = False
    episodes: list[dict] = []
    seen_bvids = set()

    async def fetch_page(page_number: int) -> int:
        nonlocal request_count, stopped_by_rate_limit
        if request_count >= max_requests:
            return 0
        await polite_sleep(config, request_count)
        LOGGER.debug(
            "api fetch series videos series=%s sid=%s page=%s ps=%s request=%s/%s",
            config.series, sid, page_number, page_size,
            request_count + 1, max_requests,
        )
        video_list = await series.get_videos(
            pn=page_number,
            ps=page_size,
            sort=channel_series.ChannelOrder.DEFAULT,
        )
        request_count += 1
        items = video_list.get("archives", [])
        added = 0
        for item in items:
            bvid = item.get("bvid")
            if not bvid or bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)
            episodes.append({
                "bvid": bvid,
                "title": item.get("title", ""),
                "description": "",
                "duration": item.get("duration", 0),
                "image": item.get("pic", ""),
                "pubdate": item.get("pubdate", 0),
                "link": f"https://www.bilibili.com/video/{bvid}",
                "raw": item,
            })
            added += 1
        return added

    await fetch_page(1)
    while len(episodes) < max_items and request_count < max_requests and not stopped_by_rate_limit:
        page_number = (len(episodes) // page_size) + 1
        try:
            added = await fetch_page(page_number)
        except Exception as exc:
            if is_bilibili_rate_limited(exc):
                LOGGER.warning(
                    "api rate limited series=%s sid=%s page=%s error=%s",
                    config.series, sid, page_number, exc,
                )
                stopped_by_rate_limit = True
                break
            raise
        if added == 0:
            break

    info["_bilibili_podcast_request_count"] = request_count
    info["_bilibili_podcast_stopped_by_rate_limit"] = stopped_by_rate_limit
    LOGGER.info(
        "api fetch series complete series=%s sid=%s fetched=%s requests=%s rate_limited=%s",
        config.series, sid, len(episodes), request_count, stopped_by_rate_limit,
    )
    return info, episodes, len(episodes)


async def fetch_space_episodes_with_playwright(
    config: SeriesConfig,
    cookie_file: Optional[str],
    browser_user_data_root: str,
) -> tuple[dict, list[dict], int]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright fallback requires the 'playwright' package and browser install") from exc

    wait_seconds = browser_wait_seconds(config)
    url = config.source.get("space_url") or f"https://space.bilibili.com/{config.uid}"
    info = {"name": config.author, "face": config.cover_art, "sign": config.description}
    episodes: list[dict] = []
    seen = set()
    PLAYWRIGHT_LOGGER.info(
        "fallback start series=%s uid=%s url=%s wait_seconds=%.2f",
        config.series,
        config.uid,
        url,
        wait_seconds,
    )

    async with async_playwright() as playwright:
        user_data_dir = browser_user_data_dir(browser_user_data_root, config)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        PLAYWRIGHT_LOGGER.info("launch persistent context series=%s user_data_dir=%s", config.series, user_data_dir)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            cookies = load_browser_cookies(cookie_file)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            page.on(
                "console",
                lambda msg: PLAYWRIGHT_LOGGER.info(
                    "console series=%s type=%s text=%s",
                    config.series,
                    msg.type,
                    msg.text[:500],
                ),
            )
            page.on(
                "pageerror",
                lambda exc: PLAYWRIGHT_LOGGER.warning("pageerror series=%s error=%s", config.series, exc),
            )
            page.on(
                "requestfailed",
                lambda request: PLAYWRIGHT_LOGGER.warning(
                    "requestfailed series=%s method=%s url=%s failure=%s",
                    config.series,
                    request.method,
                    request.url,
                    request.failure,
                ),
            )
            PLAYWRIGHT_LOGGER.info("goto series=%s url=%s", config.series, url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            PLAYWRIGHT_LOGGER.info("wait series=%s milliseconds=%s", config.series, int(wait_seconds * 1000))
            await page.wait_for_timeout(int(wait_seconds * 1000))
            title = await page.title()
            PLAYWRIGHT_LOGGER.info("page loaded series=%s title=%s", config.series, title)
            if title:
                info["name"] = title.split("的个人空间", 1)[0].strip() or info["name"]

            anchors = await page.locator("a[href*='/video/BV']").evaluate_all(
                """nodes => nodes.map(node => ({
                    href: node.href || node.getAttribute('href') || '',
                    title: (node.innerText || node.textContent || node.title || '').trim(),
                    text: ((node.closest('.bili-video-card') || node.closest('.small-item') || node.parentElement || node).innerText || '').trim()
                }))"""
            )
            for anchor in anchors:
                match = re.search(r"(BV[0-9A-Za-z]+)", anchor.get("href", ""))
                if not match:
                    continue
                bvid = match.group(1)
                if bvid in seen:
                    continue
                seen.add(bvid)
                episodes.append(browser_item_to_episode({
                    "bvid": bvid,
                    "title": anchor.get("title", ""),
                    "text": anchor.get("text", ""),
                }))
        finally:
            PLAYWRIGHT_LOGGER.info("close context series=%s collected=%s", config.series, len(episodes))
            await context.close()

    info["_bilibili_podcast_request_count"] = 1
    info["_bilibili_podcast_stopped_by_rate_limit"] = False
    info["_bilibili_podcast_source"] = "playwright"
    PLAYWRIGHT_LOGGER.info("fallback complete series=%s collected=%s", config.series, len(episodes))
    return info, episodes, len(episodes)


async def check_playwright_login(
    cookie_file: Optional[str],
    wait_seconds: float,
    browser_user_data_root: str,
) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright login check requires the 'playwright' package and browser install") from exc

    async with async_playwright() as playwright:
        user_data_dir = Path(browser_user_data_root) / "login-check"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        PLAYWRIGHT_LOGGER.info("login check start user_data_dir=%s wait_seconds=%.2f", user_data_dir, wait_seconds)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            cookies = load_browser_cookies(cookie_file)
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            page.on(
                "pageerror",
                lambda exc: PLAYWRIGHT_LOGGER.warning("login-check pageerror error=%s", exc),
            )
            page.on(
                "requestfailed",
                lambda request: PLAYWRIGHT_LOGGER.warning(
                    "login-check requestfailed method=%s url=%s failure=%s",
                    request.method,
                    request.url,
                    request.failure,
                ),
            )
            PLAYWRIGHT_LOGGER.info("login check goto https://www.bilibili.com")
            await page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(int(max(wait_seconds, 1.0) * 1000))
            title = await page.title()
            login_text = await page.locator("body").evaluate(
                """body => {
                    const text = body.innerText || '';
                    return {
                        hasLoginButton: text.includes('登录'),
                        hasLogoutText: text.includes('退出登录'),
                        hasAccountHints: text.includes('个人中心') || text.includes('消息') || text.includes('动态')
                    };
                }"""
            )
        finally:
            PLAYWRIGHT_LOGGER.info("login check close context")
            await context.close()

    PLAYWRIGHT_LOGGER.info("login check complete title=%s indicators=%s", title, login_text)
    return {
        "ok": True,
        "title": title,
        "cookies_loaded": bool(cookie_file),
        "login_indicators": login_text,
    }


def paid_state_incomplete(config: SeriesConfig, episodes: list[dict]) -> bool:
    return needs_paid_state(config) and bool(episodes) and not all(has_paid_state(episode) for episode in episodes)


def merge_browser_hints(api_episodes: list[dict], browser_episodes: list[dict]) -> list[dict]:
    browser_by_bvid = {episode["bvid"]: episode for episode in browser_episodes}
    merged = []
    for episode in api_episodes:
        browser_episode = browser_by_bvid.get(episode["bvid"])
        if browser_episode:
            episode = {**episode, "raw": {**episode.get("raw", {})}}
            episode["raw"]["browser_text"] = browser_episode.get("raw", {}).get("browser_text", "")
            if not episode.get("title") and browser_episode.get("title"):
                episode["title"] = browser_episode["title"]
        merged.append(episode)
    return merged


def apply_filters(config: SeriesConfig, episodes: list[dict]) -> tuple[list[dict], dict[str, set]]:
    filters = config.filters
    exclude_bvids = {
        *filters.get("exclude_bvids", []),
        *filters.get("advertisement_bvids", []),
    }
    exclude_keywords = filters.get("exclude_keywords", [])
    include_keywords = filters.get("include_keywords", [])
    advertisement_keywords = filters.get("advertisement_keywords", [])
    exclude_season_ids = {int(value) for value in filters.get("exclude_season_ids", [])}
    exclude_paid = filters.get("exclude_paid", True)

    # Duration filter config
    min_dur = int(config.sync.get("min_duration_seconds", 0))
    max_dur = int(config.sync.get("max_duration_seconds", 0))
    skip_duration_filter = False
    if min_dur > 0 and max_dur > 0 and max_dur <= min_dur:
        LOGGER.warning(
            "duration config invalid series=%s min=%s max=%s (max <= min, skipping duration filter)",
            config.series, min_dur, max_dur,
        )
        skip_duration_filter = True

    counts = {
        "total": len(episodes),
        "duration_skip": 0,
        "paid_confirmed": 0,
        "paid_unconfirmed": 0,
        "exclude_bvid": 0,
        "exclude_season": 0,
        "exclude_keyword": 0,
        "advertisement_keyword": 0,
        "not_in_include": 0,
        "kept": 0,
    }
    excluded = {
        "duration": set(),
        "paid_confirmed": set(),
        "paid_unconfirmed": set(),
        "bvid": set(),
        "season": set(),
        "keyword": set(),
        "ad_keyword": set(),
        "not_in_include": set(),
    }
    filtered = []
    for episode in episodes:
        bvid = episode["bvid"]
        if not skip_duration_filter and (min_dur > 0 or max_dur > 0):
            dur = episode_duration_seconds(episode.get("duration"))
            if (min_dur > 0 and dur < min_dur) or (max_dur > 0 and dur > max_dur):
                counts["duration_skip"] += 1
                excluded["duration"].add(bvid)
                continue
        if exclude_paid and is_paid_content(episode):
            counts["paid_confirmed"] += 1
            excluded["paid_confirmed"].add(bvid)
            continue
        if must_confirm_paid_state(config) and not has_paid_state(episode):
            counts["paid_unconfirmed"] += 1
            excluded["paid_unconfirmed"].add(bvid)
            continue
        if bvid in exclude_bvids:
            counts["exclude_bvid"] += 1
            excluded["bvid"].add(bvid)
            continue
        raw_season_id = episode.get("raw", {}).get("season_id", 0)
        try:
            season_id = int(raw_season_id or 0)
        except (TypeError, ValueError):
            season_id = 0
        if season_id in exclude_season_ids:
            counts["exclude_season"] += 1
            excluded["season"].add(bvid)
            continue
        if exclude_keywords and text_matches(exclude_keywords, episode):
            counts["exclude_keyword"] += 1
            excluded["keyword"].add(bvid)
            continue
        if advertisement_keywords and text_matches(advertisement_keywords, episode):
            counts["advertisement_keyword"] += 1
            excluded["ad_keyword"].add(bvid)
            continue
        if include_keywords and not text_matches(include_keywords, episode):
            counts["not_in_include"] += 1
            excluded["not_in_include"].add(bvid)
            continue
        filtered.append(episode)
    counts["kept"] = len(filtered)
    LOGGER.info(
        "filters series=%s total=%s kept=%s duration_skip=%s paid_confirmed=%s paid_unconfirmed=%s by_bvid=%s by_season=%s by_keyword=%s ad_keyword=%s not_in_include=%s",
        config.series,
        counts["total"],
        counts["kept"],
        counts["duration_skip"],
        counts["paid_confirmed"],
        counts["paid_unconfirmed"],
        counts["exclude_bvid"],
        counts["exclude_season"],
        counts["exclude_keyword"],
        counts["advertisement_keyword"],
        counts["not_in_include"],
    )
    return filtered, excluded


def limit_episodes(config: SeriesConfig, episodes: list[dict]) -> list[dict]:
    episodes = sorted(episodes, key=lambda item: item.get("pubdate", 0), reverse=True)
    if config.keep_last > 0 and len(episodes) > config.keep_last:
        LOGGER.debug(
            "limit kept series=%s total=%s keep_last=%s",
            config.series, len(episodes), config.keep_last,
        )
        return episodes[: config.keep_last]
    LOGGER.debug("limit kept series=%s total=%s (unlimited)", config.series, len(episodes))
    return episodes


def write_metadata(config: SeriesConfig, paths: SyncPaths, episode: dict, dry_run: bool) -> None:
    path = json_path(config, paths, episode["bvid"])
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(episode, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o644)
    LOGGER.debug("metadata written series=%s bvid=%s path=%s", config.series, episode["bvid"], path)


def ensure_free_space(path: Path, min_free_gb: float) -> None:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_free_gb:
            LOGGER.error(
                "disk full path=%s free_gb=%.1f min_free_gb=%.1f",
                path, free_gb, min_free_gb,
            )
            raise OSError(
                f"only {free_gb:.1f} GB free on {path}, "
                f"need {min_free_gb:.1f} GB"
            )
        LOGGER.debug(
            "disk space ok path=%s free_gb=%.1f min_free_gb=%.1f",
            path, free_gb, min_free_gb,
        )
    except FileNotFoundError:
        LOGGER.warning("disk usage check skipped path=%s (not mounted yet)", path)


def download_episode(config: SeriesConfig, paths: SyncPaths, episode: dict, cookie_file: str, dry_run: bool) -> None:
    out = media_path(config, paths, episode["bvid"])
    if out.exists():
        LOGGER.info("download skipped existing series=%s bvid=%s path=%s", config.series, episode["bvid"], out)
        return
    if dry_run:
        LOGGER.info("download planned dry-run series=%s bvid=%s path=%s", config.series, episode["bvid"], out)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    quality = audio_quality(config)
    LOGGER.info("download start series=%s bvid=%s quality=%s path=%s", config.series, episode["bvid"], quality, out)
    yt_dlp_bin = Path(sys.executable).with_name("yt-dlp")
    yt_dlp_command = str(yt_dlp_bin) if yt_dlp_bin.exists() else "yt-dlp"
    try:
        subprocess.run(
            [
                yt_dlp_command,
                "--cookies",
                cookie_file,
                "--extract-audio",
                "--audio-format",
                "mp3",
                "--audio-quality",
                quality,
                "-o",
                str(out.with_suffix(".%(ext)s")),
                episode["link"],
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.error("download failed series=%s bvid=%s error=%s", config.series, episode["bvid"], exc)
        return
    out.chmod(0o644)
    LOGGER.info("download complete series=%s bvid=%s bytes=%s", config.series, episode["bvid"], out.stat().st_size)


def generate_rss(
    config: SeriesConfig,
    paths: SyncPaths,
    up_info: dict,
    episodes: list[dict],
    token: Optional[str],
    dry_run: bool,
) -> Path:
    from feedgen.feed import FeedGenerator

    rss_path = paths.rss_root / f"{config.series}.xml"
    if dry_run:
        return rss_path

    paths.rss_root.mkdir(parents=True, exist_ok=True)
    fg = FeedGenerator()
    fg.load_extension("podcast", atom=False, rss=True)
    fg.title(config.title)
    fg.description(config.description or config.title)
    fg.link({"href": config.source.get("space_url", f"https://space.bilibili.com/{config.uid}")})
    image = config.cover_art or up_info.get("face")
    if image:
        fg.image(url=image, title=config.title, link=config.source.get("space_url"))
        fg.podcast.itunes_image(image)
    if config.category:
        fg.podcast.itunes_category(config.category)
    fg.podcast.itunes_author(config.author)

    output_items = 0
    for episode in episodes:
        path = media_path(config, paths, episode["bvid"])
        enclosure_url = ""
        enclosure_length = 0
        if path.exists():
            enclosure_url = media_url(config, paths, episode["bvid"], token)
            enclosure_length = path.stat().st_size
        elif episode.get("_existing_enclosure_url") and is_safe_enclosure_url(episode["_existing_enclosure_url"]):
            enclosure_url = episode["_existing_enclosure_url"]
            enclosure_length = episode.get("_existing_enclosure_length", 0)
        else:
            continue
        output_items += 1
        entry = fg.add_entry()
        entry.title(episode["title"])
        entry.link({"href": episode["link"], "rel": "alternate"})
        entry.description(episode.get("description", ""))
        entry.guid(episode["bvid"], permalink=False)
        entry.pubDate(datetime.fromtimestamp(int(episode.get("pubdate", 0))).astimezone())
        entry.enclosure(
            url=enclosure_url,
            length=enclosure_length,
            type="audio/mpeg",
        )
        if episode.get("duration"):
            entry.podcast.itunes_duration(str(episode["duration"]))
        if episode.get("image"):
            entry.podcast.itunes_image(episode["image"])

    item_count = len(episodes)
    fg.rss_file(str(rss_path), pretty=True)
    rss_path.chmod(0o644)
    LOGGER.info(
        "rss written series=%s path=%s input_items=%s output_items=%s",
        config.series, rss_path, item_count, output_items,
    )
    return rss_path


def bvid_from_text(text: str) -> str:
    match = re.search(r"(BV[0-9A-Za-z]+)", text or "")
    return match.group(1) if match else ""


def timestamp_from_rss_pubdate(value: str) -> int:
    if not value:
        return 0
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def existing_rss_items(rss_path: Path) -> list[dict]:
    if not rss_path.exists():
        LOGGER.debug("rss file not found path=%s (skipping merge)", rss_path)
        return []
    try:
        root = ET.parse(rss_path).getroot()
    except ET.ParseError:
        LOGGER.warning("rss parse error path=%s", rss_path)
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    items = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        enclosure_url = enclosure.get("url", "") if enclosure is not None else ""
        guid_text = item.findtext("guid") or ""
        link = item.findtext("link") or ""
        guid = bvid_from_text(guid_text) or bvid_from_text(link) or bvid_from_text(enclosure_url)
        title = item.findtext("title") or ""
        if not guid or enclosure is None:
            continue
        items.append(
            {
                "bvid": guid,
                "title": title,
                "description": item.findtext("description") or "",
                "duration": "",
                "image": "",
                "pubdate": timestamp_from_rss_pubdate(item.findtext("pubDate") or ""),
                "link": link or f"https://www.bilibili.com/video/{guid}",
                "raw": {"source": "existing_rss"},
                "_existing_enclosure_url": enclosure_url,
                "_existing_enclosure_length": int(enclosure.get("length", "0") or 0),
            }
        )
    return items


def merge_existing_rss_items(
    config: SeriesConfig, paths: SyncPaths, episodes: list[dict],
    excluded_bvids: set | None = None,
    *, apply_limit: bool = True,
) -> list[dict]:
    existing = existing_rss_items(paths.rss_root / f"{config.series}.xml")
    # Static blacklists from config — catches BVIDs that weren't re-fetched
    filters = config.filters
    static_exclude = set()
    static_exclude.update(filters.get("exclude_bvids", []))
    static_exclude.update(filters.get("advertisement_bvids", []))
    excluded_bvid = set(excluded_bvids or ()) | static_exclude
    removed_count = 0
    paid_removed = 0
    surviving = []
    for item in existing:
        if item["bvid"] in excluded_bvid:
            removed_count += 1
            continue
        if is_paid_content(item):
            paid_removed += 1
            continue
        meta_path = json_path(config, paths, item["bvid"])
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if is_paid_content(meta):
                    paid_removed += 1
                    continue
            except (json.JSONDecodeError, OSError):
                pass
        surviving.append(item)
    merged = {episode["bvid"]: episode for episode in surviving}
    for episode in episodes:
        old = merged.get(episode["bvid"])
        if old and old.get("_existing_enclosure_url") and is_safe_enclosure_url(old["_existing_enclosure_url"]):
            episode["_existing_enclosure_url"] = old["_existing_enclosure_url"]
            episode["_existing_enclosure_length"] = old.get("_existing_enclosure_length", 0)
        merged[episode["bvid"]] = episode
    total_removed = removed_count + paid_removed
    LOGGER.debug(
        "merge rss series=%s existing=%s removed=%s paid_removed=%s new=%s merged=%s",
        config.series, len(existing), removed_count, paid_removed, len(episodes), len(merged),
    )
    result = limit_episodes(config, list(merged.values())) if apply_limit else list(merged.values())
    if total_removed:
        LOGGER.info(
            "rss cleanup series=%s removed_from_rss=%s paid_removed=%s",
            config.series, removed_count, paid_removed,
        )
    return result


def pad_with_existing_rss_items(
    config: SeriesConfig,
    paths: SyncPaths,
    playable_episodes: list[dict],
    excluded_bvids: set,
) -> list[dict]:
    """Pad a sparse feed from old RSS items without restoring exclusions."""
    if config.keep_last <= 0 or len(playable_episodes) >= config.keep_last:
        return playable_episodes
    existing = existing_rss_items(paths.rss_root / f"{config.series}.xml")
    existing_bvids = {episode["bvid"] for episode in playable_episodes}
    padded = list(playable_episodes)
    for item in existing:
        if item["bvid"] in excluded_bvids or item["bvid"] in existing_bvids:
            continue
        enclosure_url = item.get("_existing_enclosure_url", "")
        if enclosure_url and is_safe_enclosure_url(enclosure_url):
            padded.append(item)
            existing_bvids.add(item["bvid"])
        if len(padded) >= config.keep_last:
            break
    if len(padded) > len(playable_episodes):
        LOGGER.info(
            "rss padding series=%s playable_before=%s after_padding=%s keep_last=%s",
            config.series, len(playable_episodes), len(padded), config.keep_last,
        )
    return padded


def cleanup_paid_media(
    config: SeriesConfig, paths: SyncPaths, episodes: list[dict], filtered: list[dict],
    paid_confirmed_bvids: set,
) -> set:
    """Remove JSON and media files for paid content.

    Pass 1 scans the JSON directory for paid content on disk (catches old
    paid content that was synced before filtering was active).  Pass 2 cleans
    current-fetch paid-confirmed BVIDs not already handled by pass 1.
    """
    filtered_bvids = {ep["bvid"] for ep in filtered}
    cleaned_bvids: set = set()

    # Pass 1: scan JSON directory for paid content on disk
    json_dir = paths.json_root / config.series
    if json_dir.exists():
        for json_file in sorted(json_dir.glob("*.json")):
            bvid = json_file.stem.split("_")[0]
            if bvid in filtered_bvids:
                continue
            try:
                meta = json.loads(json_file.read_text(encoding="utf-8"))
                if not is_paid_content(meta):
                    continue
            except (json.JSONDecodeError, OSError):
                continue
            med = media_path(config, paths, bvid)
            json_file.unlink()
            if med.exists():
                med.unlink()
            cleaned_bvids.add(bvid)
            LOGGER.debug("paid cleanup removed series=%s bvid=%s", config.series, bvid)

    # Pass 2: clean current-fetch paid-confirmed BVIDs not on disk
    for bvid in paid_confirmed_bvids - cleaned_bvids:
        meta_path = json_path(config, paths, bvid)
        med = media_path(config, paths, bvid)
        if meta_path.exists():
            meta_path.unlink()
            cleaned_bvids.add(bvid)
        if med.exists():
            med.unlink()
            cleaned_bvids.add(bvid)
        if bvid in cleaned_bvids:
            LOGGER.debug("paid cleanup removed series=%s bvid=%s", config.series, bvid)

    if cleaned_bvids:
        LOGGER.info("paid cleanup complete series=%s removed=%s", config.series, len(cleaned_bvids))
    return paid_confirmed_bvids | cleaned_bvids


def cleanup_duration_media(
    config: SeriesConfig, paths: SyncPaths, episodes: list[dict], filtered: list[dict],
    duration_bvids: set,
) -> set:
    """Remove JSON and media files for episodes outside the configured duration range.

    Pass 1 scans the JSON directory for out-of-range content on disk.
    Pass 2 cleans current-fetch duration-excluded BVIDs not already handled.
    """
    min_dur = int(config.sync.get("min_duration_seconds", 0))
    max_dur = int(config.sync.get("max_duration_seconds", 0))
    if min_dur <= 0 and max_dur <= 0:
        return set()
    if min_dur > 0 and max_dur > 0 and max_dur <= min_dur:
        LOGGER.warning(
            "duration cleanup config invalid series=%s min=%s max=%s (max <= min, skipped)",
            config.series, min_dur, max_dur,
        )
        return set()

    def out_of_range(dur: int) -> bool:
        if min_dur > 0 and dur < min_dur:
            return True
        if max_dur > 0 and dur > max_dur:
            return True
        return False

    filtered_bvids = {ep["bvid"] for ep in filtered}
    cleaned_bvids: set = set()

    # Pass 1: scan JSON directory for out-of-duration media on disk
    json_dir = paths.json_root / config.series
    if json_dir.exists():
        for json_file in sorted(json_dir.glob("*.json")):
            bvid = json_file.stem.split("_")[0]
            if bvid in filtered_bvids:
                continue
            try:
                meta = json.loads(json_file.read_text(encoding="utf-8"))
                dur = episode_duration_seconds(meta.get("duration"))
                if not out_of_range(dur):
                    continue
            except (json.JSONDecodeError, OSError, ValueError):
                continue
            med = media_path(config, paths, bvid)
            json_file.unlink()
            if med.exists():
                med.unlink()
            cleaned_bvids.add(bvid)
            LOGGER.debug("duration cleanup removed series=%s bvid=%s duration=%s", config.series, bvid, dur)

    # Pass 2: clean current-fetch duration-excluded BVIDs not on disk
    for bvid in duration_bvids - cleaned_bvids:
        meta_path = json_path(config, paths, bvid)
        med = media_path(config, paths, bvid)
        if meta_path.exists():
            meta_path.unlink()
            cleaned_bvids.add(bvid)
        if med.exists():
            med.unlink()
            cleaned_bvids.add(bvid)
        if bvid in cleaned_bvids:
            LOGGER.debug("duration cleanup removed series=%s bvid=%s", config.series, bvid)

    if cleaned_bvids:
        LOGGER.info("duration cleanup complete series=%s removed=%s", config.series, len(cleaned_bvids))
    return duration_bvids | cleaned_bvids


def cleanup_retention_media(
    config: SeriesConfig, paths: SyncPaths, retained_episodes: list[dict],
) -> set[str]:
    """Remove media/json files for BVIDs outside the current retention target.

    The retention target is based on the current filtered feed, not only the
    RSS-playable subset. New target items may have metadata before media is
    downloaded, especially when max_downloads_per_run is low; deleting those
    files here makes the next run rediscover the same missing items forever.
    """
    if config.keep_last <= 0:
        return set()

    retained_bvids: set = {ep["bvid"] for ep in retained_episodes}

    # Scan disk for playable items (those with media — json-only is not playable)
    media_dir = paths.media_root / config.series
    json_dir = paths.json_root / config.series
    disk_playable: set = set()
    if media_dir.exists():
        for f in media_dir.glob("*.mp3"):
            bvid = f.stem.split("_")[0]
            if re.match(r"BV[0-9A-Za-z]+$", bvid):
                disk_playable.add(bvid)

    # Scan disk for ALL BVIDs (media + json) for deletion decisions
    disk_all: set = set(disk_playable)
    if json_dir.exists():
        for f in json_dir.glob("*.json"):
            bvid = f.stem.split("_")[0]
            if re.match(r"BV[0-9A-Za-z]+$", bvid):
                disk_all.add(bvid)

    all_bvids = retained_bvids | disk_all
    if not all_bvids:
        return set()

    # Decide which BVIDs to keep on disk. Target BVIDs are always protected,
    # even when they only have JSON so far. If the protected target has fewer
    # playable items than keep_last, pad with old playable media so sparse or
    # partially downloaded feeds do not lose useful back catalog entries.
    keep = set(retained_bvids)
    retained_playable_count = sum(
        1 for ep in retained_episodes
        if media_path(config, paths, ep["bvid"]).exists()
        or ep.get("_existing_enclosure_url")
    )
    if retained_playable_count < config.keep_last and disk_playable:
        # Only use disk_playable (BVIDs with media) as padding candidates
        candidates = sorted(disk_playable - keep)
        scored = []
        for bvid in candidates:
            meta_path = json_path(config, paths, bvid)
            pubdate = 0
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    pubdate = int(meta.get("pubdate", 0))
                except (OSError, json.JSONDecodeError):
                    pass
            scored.append((pubdate, bvid))
        scored.sort(key=lambda x: x[0], reverse=True)
        slots = config.keep_last - retained_playable_count
        for _, bvid in scored[:slots]:
            keep.add(bvid)

    to_delete = all_bvids - keep
    deleted_media = 0
    deleted_json = 0
    for bvid in to_delete:
        media_variants = list(media_dir.glob(f"{bvid}_*.mp3")) if media_dir.exists() else []
        json_variants = list(json_dir.glob(f"{bvid}_*.json")) if json_dir.exists() else []
        for med in media_variants:
            med.unlink()
            deleted_media += 1
        for meta_f in json_variants:
            meta_f.unlink()
            deleted_json += 1

    LOGGER.info(
        "retention cleanup series=%s keep_last=%s retained=%s deleted_media=%s deleted_json=%s",
        config.series, config.keep_last, len(keep), deleted_media, deleted_json,
    )
    return to_delete


def cleanup_excluded_media(
    config: SeriesConfig, paths: SyncPaths, episodes: list[dict], filtered: list[dict],
    other_excluded_bvids: set,
    skip_bvids: set | None = None,
) -> set:
    """Remove JSON and media for BVIDs excluded by keyword/bvid/include filters.

    Cleans current-fetch excluded BVIDs, orphaned JSON without media, and
    orphaned media whose JSON was already removed by a previous run.
    BVIDs in *skip_bvids* (already handled by paid/duration cleaners) are
    not counted again, though .exists() checks make double-deletion harmless.
    """
    skip = skip_bvids or set()
    filtered_bvids = {ep["bvid"] for ep in filtered}
    excluded_bvids: set = set()

    # Pass 1: current-fetch excluded BVIDs (non-paid, non-duration)
    for bvid in other_excluded_bvids:
        if bvid in skip:
            continue
        meta_path = json_path(config, paths, bvid)
        med = media_path(config, paths, bvid)
        removed = False
        if meta_path.exists():
            meta_path.unlink()
            removed = True
        if med.exists():
            med.unlink()
            removed = True
        if removed:
            excluded_bvids.add(bvid)
            LOGGER.debug("excluded cleanup series=%s bvid=%s", config.series, bvid)

    # Pass 2: scavenge orphaned JSON on disk (no media, not wanted)
    json_dir = paths.json_root / config.series
    if json_dir.exists():
        for json_file in sorted(json_dir.glob("*.json")):
            bvid = json_file.stem.split("_")[0]
            if bvid in filtered_bvids:
                continue
            if media_path(config, paths, bvid).exists():
                continue
            json_file.unlink()
            excluded_bvids.add(bvid)
            LOGGER.debug("excluded cleanup orphan json series=%s bvid=%s", config.series, bvid)

    # Pass 3: scavenge orphaned media on disk (no JSON, not wanted)
    media_dir = paths.media_root / config.series
    if media_dir.exists():
        for med_file in sorted(media_dir.glob("*.mp3")):
            bvid = med_file.stem.split("_")[0]
            if bvid in filtered_bvids or bvid in skip:
                continue
            if json_path(config, paths, bvid).exists():
                continue
            med_file.unlink()
            excluded_bvids.add(bvid)
            LOGGER.debug("excluded cleanup orphan media series=%s bvid=%s", config.series, bvid)

    if excluded_bvids:
        LOGGER.info("excluded cleanup complete series=%s removed=%s", config.series, len(excluded_bvids))
    return other_excluded_bvids | excluded_bvids


async def sync_series(
    config: SeriesConfig,
    paths: SyncPaths,
    credential,
    cookie_file: Optional[str],
    token: Optional[str],
    dry_run: bool,
    max_downloads_per_run: int,
    min_free_gb: float,
    browser_fallback: bool,
    browser_fallback_allowed_now: bool,
    browser_user_data_root: str,
) -> dict:
    series_start = time.time()
    strategy = fetch_strategy(config)
    fetch_source = "api"
    LOGGER.info(
        "series start series=%s uid=%s title=%s strategy=%s apply=%s",
        config.series,
        config.uid,
        config.title,
        strategy,
        not dry_run,
    )
    if config.source.get("sid"):
        up_info, episodes, fetched_count = await fetch_series_episodes(config, credential)
        fetch_source = "series"
        up_info["_bilibili_podcast_source"] = "series"
    elif strategy == "browser_first":
        if not browser_fallback or not browser_fallback_allowed_now:
            raise RuntimeError("browser_first requires browser_fallback and an available browser cooldown window")
        up_info, episodes, fetched_count = await fetch_space_episodes_with_playwright(
            config,
            cookie_file,
            browser_user_data_root,
        )
        fetch_source = "playwright"
    else:
        try:
            up_info, episodes, fetched_count = await fetch_space_episodes(config, credential)
        except Exception as exc:
            if not browser_fallback or not browser_fallback_allowed_now:
                raise
            LOGGER.warning("api fetch failed, using playwright series=%s error=%s", config.series, exc)
            up_info, episodes, fetched_count = await fetch_space_episodes_with_playwright(
                config,
                cookie_file,
                browser_user_data_root,
            )
            up_info["_bilibili_podcast_api_error"] = str(exc)
            fetch_source = "playwright"
    filtered, excluded = apply_filters(config, episodes)
    filtered = limit_episodes(config, filtered)
    target = config.keep_last if config.keep_last > 0 else sync_int(config, "page_size", 20)
    if (
        browser_fallback
        and fetch_source == "api"
        and browser_fallback_allowed_now
        and (
            (
                up_info.get("_bilibili_podcast_stopped_by_rate_limit", False)
                and len(filtered) < target
            )
            or paid_state_incomplete(config, episodes)
        )
    ):
        LOGGER.info(
            "playwright fallback triggered series=%s rate_limited=%s paid_state_incomplete=%s filtered=%s target=%s",
            config.series,
            up_info.get("_bilibili_podcast_stopped_by_rate_limit", False),
            paid_state_incomplete(config, episodes),
            len(filtered),
            target,
        )
        fallback_info, fallback_episodes, fallback_count = await fetch_space_episodes_with_playwright(
            config,
            cookie_file,
            browser_user_data_root,
        )
        if paid_state_incomplete(config, episodes):
            episodes = merge_browser_hints(episodes, fallback_episodes)
            filtered, excluded = apply_filters(config, episodes)
            filtered = limit_episodes(config, filtered)
            up_info["_bilibili_podcast_source"] = "api+playwright"
            fetch_source = "api+playwright"
        elif len(apply_filters(config, fallback_episodes)[0]) > len(filtered):
            up_info = fallback_info
            episodes = fallback_episodes
            fetched_count = fallback_count
            filtered, excluded = apply_filters(config, episodes)
            filtered = limit_episodes(config, filtered)
            fetch_source = "playwright"

    up_name = up_info.get("name", "")
    paid_preview_skipped = 0
    if config.paid_preview.get("enabled", False):
        paid_preview_skipped = sum(1 for episode in episodes if is_paid_content(episode))
        LOGGER.info(
            "paid preview series=%s skipped=%s",
            config.series, paid_preview_skipped,
        )
    missing = [
        episode
        for episode in filtered
        if not media_path(config, paths, episode["bvid"]).exists()
    ]
    to_download = missing
    if max_downloads_per_run >= 0:
        to_download = missing[:max_downloads_per_run]
    # Manual media series: never auto-download
    if config.sync.get("media_mode") == "manual":
        to_download = []
        LOGGER.info(
            "manual media series series=%s — auto-download disabled",
            config.series,
        )
    LOGGER.info(
        "series plan series=%s fetched=%s kept=%s missing=%s planned_downloads=%s max_downloads_per_run=%s",
        config.series,
        fetched_count,
        len(filtered),
        len(missing),
        len(to_download),
        max_downloads_per_run,
    )

    if not dry_run:
        (paths.media_root / config.series).mkdir(parents=True, exist_ok=True)
        (paths.json_root / config.series).mkdir(parents=True, exist_ok=True)
        for episode in filtered:
            write_metadata(config, paths, episode, dry_run=False)
        cleanup_paid_bvids = cleanup_paid_media(config, paths, episodes, filtered,
            excluded.get("paid_confirmed", set()))
        cleanup_duration_bvids = cleanup_duration_media(config, paths, episodes, filtered,
            excluded.get("duration", set()))
        static_blacklist = set(config.filters.get("exclude_bvids", [])) | set(config.filters.get("advertisement_bvids", []))
        other_excluded = (
            excluded.get("paid_unconfirmed", set())
            | excluded.get("bvid", set())
            | excluded.get("season", set())
            | excluded.get("keyword", set())
            | excluded.get("ad_keyword", set())
            | excluded.get("not_in_include", set())
            | static_blacklist
        )
        cleanup_filtered_bvids = cleanup_excluded_media(config, paths, episodes, filtered,
            other_excluded,
            skip_bvids=cleanup_paid_bvids | cleanup_duration_bvids)
        all_exclude_bvids = cleanup_paid_bvids | cleanup_duration_bvids | cleanup_filtered_bvids
        if to_download and not cookie_file:
            raise ValueError("--cookie-file is required when --apply needs to download media")
        if to_download:
            ensure_free_space(paths.media_root, min_free_gb)
            for episode in to_download:
                download_episode(config, paths, episode, cookie_file, dry_run=False)
        rss_episodes = merge_existing_rss_items(config, paths, filtered, all_exclude_bvids, apply_limit=False)
        target_bvids = {ep["bvid"] for ep in filtered}
        # Build RSS from the current target set first. Old RSS items may be
        # used as padding, but must not crowd out newly downloaded target items
        # at the retention boundary.
        playable_episodes = [
            ep for ep in rss_episodes
            if ep["bvid"] in target_bvids
            and (
                media_path(config, paths, ep["bvid"]).exists()
                or (
                    ep.get("_existing_enclosure_url")
                    and is_safe_enclosure_url(ep["_existing_enclosure_url"])
                )
            )
        ]
        playable_episodes = pad_with_existing_rss_items(
            config, paths, playable_episodes, all_exclude_bvids,
        )
        playable_episodes = limit_episodes(config, playable_episodes)
        cleanup_retention_media(config, paths, filtered)
        generate_rss(config, paths, up_info, playable_episodes, token, dry_run=False)

    return {
        "series": config.series,
        "title": config.title,
        "author": config.author,
        "uid": config.uid,
        "up_name": up_name,
        "fetch_source": fetch_source,
        "fetch_strategy": strategy,
        "fetched": fetched_count,
        "api_requests": up_info.get("_bilibili_podcast_request_count", 0),
        "stopped_by_rate_limit": up_info.get("_bilibili_podcast_stopped_by_rate_limit", False),
        "paid_state_incomplete": paid_state_incomplete(config, episodes),
        "browser_fallback_cooldown_seconds": browser_fallback_cooldown_seconds(config),
        "kept_after_filters": len(filtered),
        "paid_preview_skipped": paid_preview_skipped,
        "missing_media": len(missing),
        "planned_downloads": len(to_download),
        "download_limited": len(to_download) < len(missing),
        "rss_path": str(paths.rss_root / f"{config.series}.xml"),
        "media_dir": str(paths.media_root / config.series),
        "json_dir": str(paths.json_root / config.series),
        "missing_bvids": [episode["bvid"] for episode in missing],
        "duration_seconds": round(time.time() - series_start, 1),
    }


async def run(args: argparse.Namespace) -> int:
    run_start = time.time()
    store = make_store(args.config_dir, args.state_root, args.config_db)
    LOGGER.info(
        "run start config_dir=%s config_db=%s series=%s apply=%s media_root=%s json_root=%s rss_root=%s state_root=%s",
        args.config_dir,
        args.config_db,
        args.series,
        args.apply,
        args.media_root,
        args.json_root,
        args.rss_root,
        args.state_root,
    )
    if args.browser_login_check:
        result = await check_playwright_login(
            cookie_file=args.cookie_file,
            wait_seconds=args.browser_login_wait_seconds,
            browser_user_data_root=args.browser_user_data_root,
        )
        log_result(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    configs = store.load_configs(args.series)
    LOGGER.info("selected configs count=%s series=%s", len(configs), [config.series for config in configs])
    paths = SyncPaths(
        media_root=Path(args.media_root),
        json_root=Path(args.json_root),
        rss_root=Path(args.rss_root),
        media_base_url=args.media_base_url,
    )
    credential = load_cookie_file(args.cookie_file)
    dry_run = not args.apply
    had_error = False
    completed_sync = False

    for config in configs:
        state = store.read_state(config.series)
        LOGGER.info(
            "state read series=%s keys=%s",
            config.series, sorted(state.keys()) if state else [],
        )
        scheduled_retry = bool(getattr(args, "scheduled_retry", False))
        if scheduled_retry and not state.get("retry_pending", False):
            result = {
                "series": config.series,
                "title": config.title,
                "uid": config.uid,
                "skipped": True,
                "skip_reason": "retry_not_needed",
            }
            log_result(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            continue
        if scheduled_retry:
            rate_limited_until = int(state.get("rate_limited_until", 0) or 0)
            skip = rate_limited_until > now_timestamp()
            skip_reason = "rate_limit_cooldown" if skip else ""
            next_run_at = rate_limited_until
        else:
            skip, skip_reason, next_run_at = should_skip_series(config, state, args.force)
        if skip:
            result = {
                "series": config.series,
                "title": config.title,
                "uid": config.uid,
                "skipped": True,
                "skip_reason": skip_reason,
                "next_allowed_run_at": next_run_at,
                "update_period_seconds": update_period_seconds(config),
            }
            log_result(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            continue

        if scheduled_retry and not dry_run:
            # Consume the pending retry before making the external request.
            # A rate-limit skip above deliberately does not consume it.
            state["retry_pending"] = False
            state["last_attempt_at"] = now_timestamp()
            store.write_state(config.series, state)
            LOGGER.info("scheduled retry consumed series=%s", config.series)

        fallback_enabled = args.browser_fallback or bool(config.sync.get("browser_fallback", False))
        fallback_allowed_now = browser_fallback_allowed(config, state)
        state["last_attempt_at"] = now_timestamp()
        try:
            result = await sync_series(
                config=config,
                paths=paths,
                credential=credential,
                cookie_file=args.cookie_file,
                token=args.token,
                dry_run=dry_run,
                max_downloads_per_run=args.max_downloads_per_run,
                min_free_gb=args.min_free_gb,
                browser_fallback=fallback_enabled,
                browser_fallback_allowed_now=fallback_allowed_now,
                browser_user_data_root=args.browser_user_data_root,
            )
        except Exception as exc:
            had_error = True
            result = {
                "series": config.series,
                "title": config.title,
                "uid": config.uid,
                "error": type(exc).__name__,
                "message": str(exc),
            }
            if is_bilibili_rate_limited(exc):
                state["rate_limited_until"] = now_timestamp() + rate_limit_cooldown_seconds(config)
                result["rate_limited_until"] = state["rate_limited_until"]
            if not dry_run:
                if not scheduled_retry:
                    state["retry_pending"] = True
                store.write_state(config.series, state)
                LOGGER.info("state saved series=%s (error)", config.series)
            LOGGER.exception("series failed series=%s uid=%s", config.series, config.uid)
            log_result(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            continue

        if not dry_run:
            state["last_success_at"] = now_timestamp()
            state["retry_pending"] = False
            if "playwright" in result.get("fetch_source", ""):
                state["last_browser_fallback_at"] = now_timestamp()
            if result.get("stopped_by_rate_limit"):
                state["rate_limited_until"] = now_timestamp() + rate_limit_cooldown_seconds(config)
            elif state.get("rate_limited_until", 0) <= now_timestamp():
                state.pop("rate_limited_until", None)
            store.write_state(config.series, state)
            LOGGER.info("state saved series=%s (success)", config.series)
        log_result(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        completed_sync = True

    elapsed = time.time() - run_start
    LOGGER.info("run complete elapsed_seconds=%.1f", elapsed)

    # After successful sync with --apply, optionally run publish script.
    if args.apply and getattr(args, "publish_script", None) and completed_sync and not had_error:
        LOGGER.info("running publish script: %s", args.publish_script)
        try:
            result = subprocess.run(
                [args.publish_script], capture_output=True, text=True,
                timeout=getattr(args, "publish_timeout_seconds", 60),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOGGER.error("publish script failed: %s", exc)
            return EXIT_PUBLISH_ERROR
        if result.stdout:
            LOGGER.info(
                "publish script stdout bytes=%s", len(result.stdout.encode("utf-8")),
            )
        if result.stderr:
            LOGGER.info(
                "publish script stderr bytes=%s", len(result.stderr.encode("utf-8")),
            )
        if result.returncode != 0:
            details = sanitize_external_output(result.stderr or result.stdout)
            LOGGER.error(
                "publish script failed with code %s: %s",
                result.returncode,
                details or "(no output)",
            )
            return EXIT_PUBLISH_ERROR

    return EXIT_SYNC_ERROR if had_error else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync bilibili-podcast series configs.")
    config_source = parser.add_mutually_exclusive_group()
    config_source.add_argument("--config-db", help="One-run SQLite database path override.")
    config_source.add_argument("--config-dir", help="Explicit legacy YAML rollback directory.")
    parser.add_argument("--series", help="Comma-separated series ids to sync.")
    parser.add_argument("--cookie-file", help="Netscape cookie file for Bilibili.")
    parser.add_argument("--token", help="Media token to append to RSS enclosure URLs.")
    parser.add_argument("--media-root")
    parser.add_argument("--json-root")
    parser.add_argument("--rss-root")
    parser.add_argument("--media-base-url")
    parser.add_argument("--lock-file")
    parser.add_argument("--state-root")
    parser.add_argument("--max-downloads-per-run", type=int)
    parser.add_argument("--min-free-gb", type=float)
    parser.add_argument("--browser-fallback", action="store_true", help="Use one low-rate Playwright page visit if API fetching fails.")
    parser.add_argument("--browser-user-data-root")
    parser.add_argument("--browser-login-check", action="store_true", help="Open Bilibili once with Playwright and loaded cookies to verify browser login.")
    parser.add_argument("--browser-login-wait-seconds", type=float)
    parser.add_argument("--log-dir", help="Directory for bilibili-podcast and Playwright logs.")
    parser.add_argument("--log-level", type=parse_log_level, help="Log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
    parser.add_argument("--debug", action="store_true", help="Shortcut for --log-level DEBUG (per-item details).")
    parser.add_argument("--force", action="store_true", help="Ignore per-series update and rate-limit cooldown gates.")
    parser.add_argument("--scheduled-retry", action="store_true", help="Run only after a primary failure; bypass update-period gate only.")
    parser.add_argument("--apply", action="store_true", help="Write files and download missing media.")
    parser.add_argument("--publish-script", help="发布脚本路径；仅 --apply 全部成功后执行，失败返回非零。")
    return parser


def apply_config_defaults(args: argparse.Namespace, snapshot: ConfigSnapshot) -> argparse.Namespace:
    """Apply ``explicit CLI > TOML > schema default`` without hiding CLI intent."""
    yaml_rollback = args.config_dir is not None
    defaults = {
        "config_db": None if yaml_rollback else str(snapshot.app.database.path),
        "state_root": str(snapshot.app.paths.state_root),
        "media_root": str(snapshot.app.paths.media_root),
        "json_root": str(snapshot.app.paths.json_root),
        "rss_root": str(snapshot.app.paths.rss_root),
        "media_base_url": snapshot.publish.publish.media_base_url,
        "cookie_file": str(snapshot.sync.paths.cookie_file),
        "lock_file": str(snapshot.sync.paths.lock_file),
        "max_downloads_per_run": snapshot.sync.downloads.max_per_run,
        "min_free_gb": snapshot.sync.downloads.min_free_gb,
        "browser_user_data_root": str(snapshot.sync.browser.user_data_root),
        "browser_login_wait_seconds": snapshot.sync.browser.login_wait_seconds,
        "log_dir": str(snapshot.app.paths.log_dir),
        "log_level": snapshot.sync.logging.level,
        "publish_script": (
            str(snapshot.publish.publish.script)
            if snapshot.publish.publish.enabled and snapshot.publish.publish.script
            else None
        ),
        "publish_timeout_seconds": snapshot.sync.timeouts.publish_seconds,
    }
    for name, value in defaults.items():
        if getattr(args, name, None) is None:
            setattr(args, name, value)
    return args


def main() -> int:
    args = build_parser().parse_args()
    try:
        snapshot = ConfigManager().load()
        apply_config_defaults(args, snapshot)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(snapshot.sync.browser.playwright_browsers_path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    setup_logging(
        args.log_dir,
        args.log_level,
        args.debug,
        retention_days=snapshot.sync.logging.retention_days,
        max_bytes=snapshot.sync.logging.max_bytes,
        backup_count=snapshot.sync.logging.backup_count,
    )
    with process_lock(args.lock_file):
        return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
