"""bilix 后端：基于 bilix 的 B 站接口实现（延迟 import）。

限制与适配说明（经本地阅读 bilix 源码确认）：
- bilix 仅提供系列（series）相关 API，不支持剧集（season/bangumi）；
- bilix 的公开函数 get_up_video_info 只返回 (up_name, total_size, bvids)，
  不含 title/pubdate/duration 等字段；因此本后端复用其内部的
  x/space/wbi/arc/search 接口（含 wbi 签名逻辑 _add_sign）补全字段；
- 系列视频同理：公开函数 get_list_info 只返回 bvid 列表，本后端额外请求
  x/series/archives 补全详情；若详情请求失败则降级为仅含 bvid 的最小条目
  （标题用 bvid 占位）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import BackendCredential, BackendError, RateLimitError, UnsupportedError

if TYPE_CHECKING:
    import httpx

LOGGER = logging.getLogger("bilibili_podcast.api_backends.bilix")

_BILIX_TIMEOUT_SECONDS = 15.0

# (B 站 cookie 名, 统一凭证键名)
_BILIX_COOKIE_KEYS = (
    ("SESSDATA", "sessdata"),
    ("bili_jct", "bili_jct"),
    ("DedeUserID", "dedeuserid"),
    ("buvid3", "buvid3"),
)


def _episode_from_vlist_item(item: dict) -> dict:
    """把 arc/search 的 vlist 条目转换为统一 episode 格式。"""
    bvid = item.get("bvid") or item.get("bv_id") or ""
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "duration": item.get("length") or item.get("duration", 0),
        "image": item.get("pic") or item.get("cover", ""),
        "pubdate": item.get("created") or item.get("pubdate", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": item,
    }


def _episode_from_archives_item(item: dict) -> dict:
    """把 x/series/archives 条目转换为统一 episode 格式。"""
    bvid = item.get("bvid", "")
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "duration": item.get("duration", 0),
        "image": item.get("pic", ""),
        "pubdate": item.get("pubdate", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": item,
    }


def _minimal_episode(bvid: str) -> dict:
    """仅含 bvid 的最小条目（详情接口失败时的兜底，标题用 bvid 占位）。"""
    return {
        "bvid": bvid,
        "title": bvid,
        "description": "",
        "duration": 0,
        "image": "",
        "pubdate": 0,
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": {"bvid": bvid},
    }


class BilixBackend:
    """基于 bilix 的可切换后端：仅支持空间（space）与系列（series）。"""

    def __init__(self, credential: BackendCredential | None = None):
        import httpx
        from bilix.sites.bilibili import api as bilix_api

        self._bilix_api = bilix_api
        cookies: dict[str, str] = {}
        if credential:
            for cookie_name, key in _BILIX_COOKIE_KEYS:
                value = credential.get(key)
                if value:
                    cookies[cookie_name] = value
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            cookies=cookies,
            timeout=_BILIX_TIMEOUT_SECONDS,
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "referer": "https://www.bilibili.com",
            },
        )
        # 系列全量结果缓存：sid -> 全量 episode 列表（避免每次分页重复请求全量）
        self._series_cache: dict[int, list[dict]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def get_user_info(self, uid: int) -> dict:
        info = await self._bilix_api.get_up_info(self._client, str(uid))
        return {
            "name": info.get("name", ""),
            "face": info.get("face", ""),
            "sign": info.get("sign", ""),
        }

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        # bilix 公开函数只返回 bvid 列表，这里复用其内部的 arc/search 接口
        # 与 wbi 签名函数 _add_sign 以补全 title/pubdate/duration 等字段。
        try:
            params: dict[str, Any] = {
                "mid": str(uid),
                "order": "pubdate",
                "ps": ps,
                "pn": pn,
                "keyword": "",
            }
            await self._bilix_api._add_sign(self._client, params)
            res = await self._client.get(
                "https://api.bilibili.com/x/space/wbi/arc/search",
                params=params,
            )
            res.raise_for_status()
            data = res.json()
            if data.get("code") != 0:
                raise RuntimeError(f"bilix 空间视频接口返回错误：{data.get('message')}")
            vlist = data.get("data", {}).get("list", {}).get("vlist", [])
            return [_episode_from_vlist_item(item) for item in vlist]
        except Exception as exc:
            # 编程错误（参数/类型/键值问题）直接上抛，不进入降级路径掩盖根因；
            # 其余（接口错误、网络、超时等）走降级
            if isinstance(exc, (TypeError, ValueError, KeyError, AttributeError)):
                raise
            text = str(exc)
            if "-799" in text or "请求过于频繁" in text or "rate limit" in text.lower():
                raise RateLimitError(f"bilix 空间视频接口限流：{exc}") from exc
            LOGGER.warning("bilix 空间视频详情失败，降级为仅 bvid 条目 uid=%s error=%s", uid, exc)
            try:
                _, _, bvids = await self._bilix_api.get_up_video_info(
                    self._client,
                    str(uid),
                    pn=pn,
                    ps=ps,
                    order="pubdate",
                )
            except Exception as fallback_exc:
                fallback_text = str(fallback_exc)
                if (
                    "-799" in fallback_text
                    or "请求过于频繁" in fallback_text
                    or "rate limit" in fallback_text.lower()
                ):
                    raise RateLimitError(f"bilix 空间视频接口限流：{fallback_exc}") from fallback_exc
                raise
            return [_minimal_episode(bvid) for bvid in bvids]

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        if series_type != "series":
            raise UnsupportedError("bilix 后端不支持剧集(season)类型，请使用 bilibili-api 或 yutto 后端")
        res = await self._client.get(
            "https://api.bilibili.com/x/series/series",
            params={"series_id": sid},
        )
        res.raise_for_status()
        data = res.json()
        if data.get("code") != 0:
            raise RuntimeError(f"bilix 系列元数据接口返回错误：{data.get('message')}")
        meta = data.get("data", {}).get("meta", {})
        mid = meta.get("mid")
        # x/series/series 的 meta 以接口实际返回为准，缺失字段回落为空字符串；
        # 该接口不含 UP 主昵称字段，author 留空；uid 取 meta.mid。
        return {
            "name": meta.get("name", ""),
            "face": meta.get("cover", ""),
            "sign": meta.get("intro", ""),
            "author": "",
            "uid": int(mid) if mid else None,
        }

    async def _fetch_series_all(self, sid: int) -> list[dict]:
        """获取系列全量 episode 列表（带缓存，避免分页循环重复请求全量）。"""
        cached = self._series_cache.get(sid)
        if cached is not None:
            return cached
        # 优先用公开函数 get_list_info 拿全量 bvid（bilix 无分页 API），
        # 再请求 series/archives 补全完整字段。
        _, _, bvids = await self._bilix_api.get_list_info(self._client, str(sid))
        try:
            res = await self._client.get(
                "https://api.bilibili.com/x/series/series",
                params={"series_id": sid},
            )
            res.raise_for_status()
            series_data = res.json()
            if series_data.get("code") != 0:
                raise RuntimeError(f"bilix 系列元数据接口返回错误：{series_data.get('message')}")
            meta = series_data.get("data", {}).get("meta", {})
            mid = meta.get("mid")
            if mid is None:
                # 元数据缺少 mid 时显式失败（进入 except 降级分支），禁止裸 KeyError 穿透
                raise BackendError(f"bilix 获取系列元数据缺少 mid（sid={sid}）")
            total = int(meta.get("total", len(bvids)) or len(bvids))
            res2 = await self._client.get(
                "https://api.bilibili.com/x/series/archives",
                params={"mid": mid, "series_id": sid, "ps": total},
            )
            res2.raise_for_status()
            archives_data = res2.json()
            if archives_data.get("code") != 0:
                raise RuntimeError(f"bilix 系列视频接口返回错误：{archives_data.get('message')}")
            archives = archives_data.get("data", {}).get("archives", [])
            episodes = [_episode_from_archives_item(item) for item in archives]
        except Exception as exc:
            text = str(exc)
            if "-799" in text or "请求过于频繁" in text or "rate limit" in text.lower():
                raise RateLimitError(f"bilix 系列接口限流：{exc}") from exc
            LOGGER.warning("bilix 系列详情失败，降级为仅 bvid 条目 sid=%s error=%s", sid, exc)
            episodes = [_minimal_episode(bvid) for bvid in bvids]
        self._series_cache[sid] = episodes
        return episodes

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        if series_type != "series":
            raise UnsupportedError("bilix 后端不支持剧集(season)类型，请使用 bilibili-api 或 yutto 后端")
        episodes = await self._fetch_series_all(sid)
        start = (pn - 1) * ps
        return episodes[start : start + ps]

    async def get_video_owner(self, bvid: str) -> int | None:
        raise UnsupportedError(
            "bilix 后端无法获取视频所属 UP 主（VideoInfo 无 owner/mid 字段），"
            "请使用 bilibili-api 或 yutto 后端"
        )
