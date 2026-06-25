"""Bilibili URL resolver — parse B站 URLs into structured draft configs.

Parses space URLs, season/series URLs, or plain UIDs/sids using the
existing bilibili-api library and returns a structured draft dict.
Does NOT write to DB or any files — pure read-only resolution.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs

import bilibili_api

from ..utils.bilibili_url import parse_space_source

_SEASON_RE = re.compile(
    r"(?:https?://)?(?:www\.)?bilibili\.com/bangumi/play/ss(\d+)"
)
_SERIES_RE = re.compile(
    r"(?:https?://)?(?:www\.)?bilibili\.com/series/(\d+)"
)
_MEDIA_RE = re.compile(
    r"(?:https?://)?(?:www\.)?bilibili\.com/(?:video/)?(BV[a-zA-Z0-9]+)"
)
_SID_RE = re.compile(r"^\d+$")


async def resolve_url(url: str) -> dict:
    """Parse a Bilibili URL/UID/sid and return structured draft info.

    Returns dict with keys:
        series: suggested series slug (editable)
        title: suggested RSS title
        author: UP name
        cover_art: avatar/cover URL
        description: channel or season description
        source: {space_url, uid, type, sid}
        videos: list of recent videos
        error: error message if resolution failed
    """
    url = url.strip()

    # Determine source type from URL pattern
    m = _SEASON_RE.match(url)
    if m:
        return await _resolve_season(m.group(1))

    m = _SERIES_RE.match(url)
    if m:
        return await _resolve_series(m.group(1))

    space_source = parse_space_source(url)
    if space_source:
        return await _resolve_space(
            str(space_source["uid"]),
            space_source["space_url"],
        )

    m = _MEDIA_RE.match(url)
    if m:
        # Single video — try to extract UID from video info
        return await _resolve_video(m.group(1))

    # Plain sid — user must specify type
    if _SID_RE.match(url):
        return {
            "error": "纯数字 ID 无法判断类型。请用完整 URL（含 space/season/series 路径），或先选择合集/系列模式。",
        }

    return {"error": f"无法识别的 URL: {url}"}


async def _resolve_space(uid: str, space_url: str) -> dict:
    """Resolve a UP主 space URL."""
    try:
        user = bilibili_api.user.User(int(uid))
        info = await user.get_user_info()
        name = info.get("name", "")
        face = info.get("face", "")
        sign = info.get("sign", "")
    except Exception as e:
        return {"error": f"获取 UP 主信息失败 (UID={uid}): {e}"}

    # Fetch recent videos
    videos = []
    try:
        page = 1
        resp = await user.get_videos(ps=5, pn=page)
        vlist = resp.get("list", {}).get("vlist", [])
        for v in vlist:
            videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "created": v.get("created", 0),
                "length": _format_duration(v.get("length", "0:00")),
            })
    except Exception:
        pass

    slug = _make_slug(name)
    return {
        "series": slug,
        "title": name,
        "author": name,
        "cover_art": face,
        "description": sign,
        "source": {
            "type": "space",
            "uid": int(uid),
            "space_url": space_url,
            "sid": None,
        },
        "videos": videos,
    }


async def _resolve_season(sid: str) -> dict:
    """Resolve a bangumi season."""
    # season requires different API
    return await _resolve_series_or_season("season", sid)


async def _resolve_series(sid: str) -> dict:
    """Resolve a series (manual playlist)."""
    return await _resolve_series_or_season("series", sid)


async def _resolve_series_or_season(source_type: str, sid: str) -> dict:
    """Resolve a season or series by ID."""
    sid_int = int(sid)

    if source_type == "season":
        try:
            season = bilibili_api.season.Season(season_id=sid_int)
            meta = await season.get_meta()
            name = meta.get("title", "") or meta.get("series_title", "") or f"Season {sid}"
            up_name = meta.get("up_name", "")
            cover = meta.get("cover", "")
            desc = meta.get("description", "") or meta.get("evaluate", "")
            uid = meta.get("up_id", 0)
        except Exception as e:
            return {"error": f"获取合集信息失败 (sid={sid}): {e}"}

        videos = []
        try:
            episodes = await season.get_episodes()
            for ep in (episodes or [])[:10]:
                videos.append({
                    "bvid": ep.get("bvid", ""),
                    "title": ep.get("title", ""),
                    "created": 0,
                    "length": _format_duration(ep.get("duration", "0:00")),
                })
        except Exception:
            pass
    else:
        # series — use series API
        try:
            series = bilibili_api.series.Series(sid_int)
            meta = await series.get_meta()
            name = meta.get("name", "") or meta.get("title", "") or f"Series {sid}"
            up_name = meta.get("up_name", "")
            cover = meta.get("image_url", "") or meta.get("cover", "")
            desc = meta.get("description", "") or meta.get("subtitle", "")
            uid = meta.get("uid", 0) or meta.get("up_id", 0)
        except Exception as e:
            return {"error": f"获取系列信息失败 (sid={sid}): {e}"}

        videos = []
        try:
            result = await series.get_series_videos()
            arcs = (result or [])[:10]
            for arc in arcs:
                videos.append({
                    "bvid": arc.get("bvid", ""),
                    "title": arc.get("title", ""),
                    "created": 0,
                    "length": _format_duration(str(arc.get("duration", "0:00"))),
                })
        except Exception:
            pass

    slug = _make_slug(name)
    return {
        "series": slug,
        "title": name,
        "author": up_name,
        "cover_art": cover,
        "description": desc,
        "source": {
            "type": source_type,
            "sid": sid_int,
            "uid": uid if uid else None,
            "space_url": f"https://space.bilibili.com/{uid}" if uid else "",
        },
        "videos": videos,
    }


async def _resolve_video(bvid: str) -> dict:
    """Resolve from a single video URL — try to find its UP主."""
    try:
        video = bilibili_api.video.Video(bvid=bvid)
        info = await video.get_info()
        uid = info.get("owner", {}).get("mid", 0)
        if uid:
            return await _resolve_space(
                str(uid),
                f"https://space.bilibili.com/{uid}",
            )
        return {"error": f"无法从视频 {bvid} 提取 UP 主信息"}
    except Exception as e:
        return {"error": f"获取视频信息失败 (BV={bvid}): {e}"}


def _make_slug(name: str) -> str:
    """Generate a series slug from a Chinese UP name."""
    import unicodedata

    slug = unicodedata.normalize("NFKD", name)
    slug = slug.lower()
    slug = re.sub(r"[^a-z0-9]", "_", slug)
    slug = re.sub(r"_+", "_", slug)
    slug = slug.strip("_")
    if not slug:
        slug = "unknown"
    if not re.match(r"^[a-z0-9]", slug):
        slug = "u_" + slug
    return slug[:48]


def _format_duration(duration_str: str) -> str:
    """Format duration string (e.g. '3:00', '1:30:00') for display."""
    return duration_str
