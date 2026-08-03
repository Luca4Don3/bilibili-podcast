"""Bilibili URL resolver — parse B站 URLs into structured draft configs.

Parses space URLs, season/series URLs, or plain UIDs/sids using the
api_backends 抽象层（默认 native 后端，可传入自定义后端）and returns
a structured draft dict. Does NOT write to DB or any files — pure read-only
resolution.
"""

from __future__ import annotations

import re

from ..api_backends import BilibiliApiBackend, create_backend
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


async def resolve_url(url: str, backend: BilibiliApiBackend | None = None) -> dict:
    """Parse a Bilibili URL/UID/sid and return structured draft info.

    backend 为 None 时内部创建默认后端（native）并在结束时 close；
    由调用方传入 backend 时其生命周期由调用方负责（不 close）。

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

    owns_backend = backend is None
    if owns_backend:
        backend = await create_backend("native", None)
    try:
        # Determine source type from URL pattern
        m = _SEASON_RE.match(url)
        if m:
            return await _resolve_season(m.group(1), backend)

        m = _SERIES_RE.match(url)
        if m:
            return await _resolve_series(m.group(1), backend)

        space_source = parse_space_source(url)
        if space_source:
            return await _resolve_space(
                str(space_source["uid"]),
                space_source["space_url"],
                backend,
            )

        m = _MEDIA_RE.match(url)
        if m:
            # Single video — try to extract UID from video info
            return await _resolve_video(m.group(1), backend)

        # Plain sid — user must specify type
        if _SID_RE.match(url):
            return {
                "error": "纯数字 ID 无法判断类型。请用完整 URL（含 space/season/series 路径），或先选择合集/系列模式。",
            }

        return {"error": f"无法识别的 URL: {url}"}
    finally:
        if owns_backend and backend is not None:
            await backend.close()


async def _resolve_space(uid: str, space_url: str, backend: BilibiliApiBackend) -> dict:
    """Resolve a UP主 space URL."""
    try:
        info = await backend.get_user_info(int(uid))
        name = info.get("name", "")
        face = info.get("face", "")
        sign = info.get("sign", "")
    except Exception as e:
        return {"error": f"获取 UP 主信息失败 (UID={uid}): {e}"}

    # Fetch recent videos
    videos = []
    try:
        vlist = await backend.get_user_videos(int(uid), pn=1, ps=5)
        for v in vlist:
            videos.append({
                "bvid": v.get("bvid", ""),
                "title": v.get("title", ""),
                "created": v.get("pubdate", 0),
                "length": _format_duration(v.get("duration", 0)),
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


async def _resolve_season(sid: str, backend: BilibiliApiBackend) -> dict:
    """Resolve a bangumi season."""
    # season requires different API
    return await _resolve_series_or_season("season", sid, backend)


async def _resolve_series(sid: str, backend: BilibiliApiBackend) -> dict:
    """Resolve a series (manual playlist)."""
    return await _resolve_series_or_season("series", sid, backend)


async def _resolve_series_or_season(
    source_type: str, sid: str, backend: BilibiliApiBackend,
) -> dict:
    """Resolve a season or series by ID."""
    sid_int = int(sid)

    if source_type == "season":
        try:
            meta = await backend.get_series_meta(sid_int, "season")
            name = meta.get("name", "") or meta.get("title", "") or f"Season {sid}"
            up_name = meta.get("author", "")
            cover = meta.get("face", "")
            desc = meta.get("sign", "")
            uid = meta.get("uid") or 0
        except Exception as e:
            return {"error": f"获取合集信息失败 (sid={sid}): {e}"}

        videos = []
        try:
            episodes = await backend.get_series_videos(sid_int, "season", pn=1, ps=10)
            for ep in (episodes or []):
                videos.append({
                    "bvid": ep.get("bvid", ""),
                    "title": ep.get("title", ""),
                    "created": ep.get("pubdate", 0),
                    "length": _format_duration(ep.get("duration", 0)),
                })
        except Exception:
            pass
    else:
        # series — use series API
        try:
            meta = await backend.get_series_meta(sid_int, "series")
            name = meta.get("name", "") or meta.get("title", "") or f"Series {sid}"
            up_name = meta.get("author", "")
            cover = meta.get("face", "")
            desc = meta.get("sign", "")
            uid = meta.get("uid") or 0
        except Exception as e:
            return {"error": f"获取系列信息失败 (sid={sid}): {e}"}

        videos = []
        try:
            arcs = await backend.get_series_videos(sid_int, "series", pn=1, ps=10)
            for arc in (arcs or []):
                videos.append({
                    "bvid": arc.get("bvid", ""),
                    "title": arc.get("title", ""),
                    "created": arc.get("pubdate", 0),
                    "length": _format_duration(arc.get("duration", 0)),
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


async def _resolve_video(bvid: str, backend: BilibiliApiBackend) -> dict:
    """Resolve from a single video URL — try to find its UP主."""
    try:
        uid = await backend.get_video_owner(bvid)
        if uid:
            return await _resolve_space(
                str(uid),
                f"https://space.bilibili.com/{uid}",
                backend,
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
