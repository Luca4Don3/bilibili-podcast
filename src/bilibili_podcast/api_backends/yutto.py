"""yutto 后端：基于 yutto 的 B 站接口实现（延迟 import）。

适配说明（经本地阅读 yutto 2.2.0 源码确认，注意与早期版本的 API 差异）：
- yutto 2.2.0 使用 FetcherContext + httpx.AsyncClient 的调用约定
  （不存在旧文档中的 ExecutionScope）；
- 空间视频使用 space.get_user_space_all_videos_avids 一次性取回全部
  bvid，按 pn/ps 切片后仅对当前页条目调用 ugc_video.get_ugc_video_info
  补全字段（_UgcVideoInfo 无时长字段，duration 记 0；每条视频额外请求
  tag，请求量较大，属 yutto 固有限制）；
- 系列（series）使用 collection.get_collection_details（其条目 title 为
  空字符串，标题回退为 bvid 占位）；剧集（season）使用
  bangumi.get_bangumi_list（无时长字段，duration 记 0）；
- 用户信息：yutto 仅提供 space.get_user_name 获取用户名，face/sign 无
  公开接口，返回空字符串。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import BackendCredential, UnsupportedError

if TYPE_CHECKING:
    import httpx

LOGGER = logging.getLogger("bilibili_podcast.api_backends.yutto")

_YUTTO_TIMEOUT_SECONDS = 15.0


def _episode_from_ugc_info(info: dict) -> dict:
    """把 yutto _UgcVideoInfo 转换为统一 episode 格式（无时长字段）。"""
    bvid = str(info.get("bvid", ""))
    return {
        "bvid": bvid,
        "title": info.get("title", ""),
        "description": info.get("description", ""),
        "duration": 0,  # yutto 的 ugc 视频信息不包含时长
        "image": info.get("picture", ""),
        "pubdate": info.get("pubdate", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": dict(info),
    }


def _episode_from_collection_item(item: dict) -> dict:
    """把 yutto CollectionDetailsItem 转换为统一 episode 格式。

    yutto 的 collection 条目 title 为空字符串（上游 TODO），标题回退为 bvid。
    """
    bvid = str(item.get("avid", ""))
    return {
        "bvid": bvid,
        "title": item.get("title") or bvid,
        "description": "",
        "duration": 0,
        "image": "",
        "pubdate": 0,
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": dict(item),
    }


def _episode_from_bangumi_item(item: dict) -> dict:
    """把 yutto BangumiListItem 转换为统一 episode 格式（无时长字段）。"""
    bvid = str(item.get("avid", ""))
    metadata = item.get("metadata") or {}
    return {
        "bvid": bvid,
        "title": item.get("name", ""),
        "description": metadata.get("plot", ""),
        "duration": 0,  # yutto 的 bangumi 列表项不包含时长
        "image": metadata.get("thumb", ""),
        "pubdate": metadata.get("premiered", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": dict(item),
    }


def _slice_page(items: list, pn: int, ps: int) -> list:
    """按 pn/ps 对全量列表切片（pn 从 1 开始）。"""
    start = (pn - 1) * ps
    return items[start : start + ps]


class YuttoBackend:
    """基于 yutto 的可切换后端：支持空间、系列（series）与剧集（season）。"""

    def __init__(self, credential: BackendCredential | None = None):
        from yutto.auth import AuthInfo
        from yutto.utils.fetcher import FetcherContext, create_client

        self._ctx = FetcherContext()
        if credential:
            auth = AuthInfo(
                SESSDATA=credential.get("sessdata", ""),
                bili_jct=credential.get("bili_jct"),
            )
            self._ctx.set_auth_info(auth)
        self._client: httpx.AsyncClient = create_client(
            cookies=self._ctx.cookies,
            timeout=_YUTTO_TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_user_info(self, uid: int) -> dict:
        from yutto.api import space
        from yutto.types import MId

        name = await space.get_user_name(self._ctx, self._client, MId(str(uid)))
        # yutto 仅提供用户名，face/sign 无公开接口，返回空字符串
        return {"name": name, "face": "", "sign": ""}

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        from yutto.api import space, ugc_video
        from yutto.types import MId

        avids = await space.get_user_space_all_videos_avids(
            self._ctx,
            self._client,
            MId(str(uid)),
        )
        episodes = []
        for avid in _slice_page(avids, pn, ps):
            info = await ugc_video.get_ugc_video_info(self._ctx, self._client, avid)
            episodes.append(_episode_from_ugc_info(info))
        return episodes

    async def _get_series_mid(self, sid: int) -> int:
        """获取系列所属 UP 主 mid；yutto 的 collection API 需要 mid 参数。"""
        from yutto.utils.fetcher import Fetcher

        url = f"https://api.bilibili.com/x/series/series?series_id={sid}"
        json_data = await Fetcher.fetch_json(self._ctx, self._client, url)
        assert json_data is not None
        return int(json_data["data"]["meta"]["mid"])

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        if series_type == "series":
            from yutto.api import collection
            from yutto.types import MId, SeriesId

            mid = await self._get_series_mid(sid)
            details = await collection.get_collection_details(
                self._ctx,
                self._client,
                SeriesId(str(sid)),
                MId(str(mid)),
            )
            # get_collection_details 不含 UP 主昵称字段，author 留空；
            # uid 复用 _get_series_mid 拿到的 mid。
            return {
                "name": details.get("title", ""),
                "face": "",
                "sign": "",
                "author": "",
                "uid": int(mid) if mid else None,
            }
        if series_type == "season":
            from yutto.api import bangumi
            from yutto.types import SeasonId

            bangumi_list = await bangumi.get_bangumi_list(
                self._ctx,
                self._client,
                SeasonId(str(sid)),
            )
            cover = ""
            pages = bangumi_list.get("pages") or []
            if pages:
                cover = (pages[0].get("metadata") or {}).get("thumb", "")
            # get_bangumi_list 不含 UP 主昵称/mid 字段，author/uid 留空
            return {
                "name": bangumi_list.get("title", ""),
                "face": cover,
                "sign": "",
                "author": "",
                "uid": None,
            }
        raise UnsupportedError(f"yutto 后端不支持的抓取类型：{series_type}")

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        if series_type == "series":
            from yutto.api import collection
            from yutto.types import MId, SeriesId

            mid = await self._get_series_mid(sid)
            details = await collection.get_collection_details(
                self._ctx,
                self._client,
                SeriesId(str(sid)),
                MId(str(mid)),
            )
            pages = details.get("pages") or []
            return [_episode_from_collection_item(item) for item in _slice_page(pages, pn, ps)]
        if series_type == "season":
            from yutto.api import bangumi
            from yutto.types import SeasonId

            bangumi_list = await bangumi.get_bangumi_list(
                self._ctx,
                self._client,
                SeasonId(str(sid)),
            )
            pages = bangumi_list.get("pages") or []
            return [_episode_from_bangumi_item(item) for item in _slice_page(pages, pn, ps)]
        raise UnsupportedError(f"yutto 后端不支持的抓取类型：{series_type}")

    async def get_video_owner(self, bvid: str) -> int | None:
        """返回视频所属 UP 主 mid。

        当前版本 yutto 的 _UgcVideoInfo 无 owner 字段，回退直接请求
        x/web-interface/view 接口（与 get_ugc_video_info 底层同源）取
        data.owner.mid；网络失败抛 httpx 异常（降级链可切换），无法提取返回 None。
        """
        from yutto.utils.fetcher import Fetcher

        url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        json_data = await Fetcher.fetch_json(self._ctx, self._client, url)
        if json_data is None:
            return None
        data = json_data.get("data") or {}
        owner = data.get("owner") or {}
        mid = owner.get("mid")
        return int(mid) if mid else None
