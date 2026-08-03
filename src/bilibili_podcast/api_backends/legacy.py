"""legacy 后端：基于 bilibili-api 的实现（延迟 import，已弃用）。

bilibili-api 已收到 B 站侵权告知并永久关停，且不再随本包主依赖安装；
本后端仅保留供迁移期使用，如需启用需手动安装 bilibili-api。

行为与旧 sync.py 中的直接调用完全一致：
- get_user_info 失败时抛异常，由 sync 层捕获降级为配置默认值；
- 空间视频使用 user.User.get_videos(pn, ps, order=VideoOrder.PUBDATE) 的
  data.list.vlist 字段；
- 系列/剧集使用 channel_series.ChannelSeries 的 get_meta / get_videos，
  取 data.archives 字段。
"""

from __future__ import annotations

import logging

from .base import BackendCredential, NetworkError, UnsupportedError

LOGGER = logging.getLogger("bilibili_podcast.api_backends.legacy")


def _episode_from_vlist_item(item: dict) -> dict:
    """把 bilibili-api 空间视频 vlist 条目转换为统一 episode 格式。"""
    bvid = item.get("bv_id") or item.get("bvid") or ""
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


def _episode_from_archives_item(item: dict) -> dict:
    """把 bilibili-api 系列/剧集 archives 条目转换为统一 episode 格式。"""
    bvid = item.get("bvid", "")
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": "",
        "duration": item.get("duration", 0),
        "image": item.get("pic", ""),
        "pubdate": item.get("pubdate", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": item,
    }


class LegacyBackend:
    """基于 bilibili-api 的默认后端（延迟 import，不引入硬依赖）。"""

    def __init__(self, credential: BackendCredential | None = None):
        from bilibili_api import request_settings

        # 与旧 sync.py 保持一致：构造时设置一次浏览器指纹
        request_settings.set("impersonate", "chrome131")
        self._credential = None
        if credential:
            from bilibili_api import Credential

            if not credential.get("buvid3"):
                # 旧版 load_cookie_file 强制要求 buvid3；统一凭证放宽后，
                # legacy 的 WBI 签名可能更易触发风控，仅告警不阻断。
                LOGGER.warning("legacy 后端缺少 buvid3，WBI 签名可能不稳定")
            self._credential = Credential(
                sessdata=credential.get("sessdata"),
                bili_jct=credential.get("bili_jct"),
                dedeuserid=credential.get("dedeuserid"),
                buvid3=credential.get("buvid3"),
                buvid4=credential.get("buvid4", ""),
                ac_time_value=credential.get("ac_time_value", ""),
            )

    async def close(self) -> None:
        """bilibili-api 无独立资源需要释放。"""

    async def get_user_info(self, uid: int) -> dict:
        from bilibili_api import user

        user_obj = user.User(uid=uid, credential=self._credential)
        info = await user_obj.get_user_info()
        return {
            "name": info.get("name", ""),
            "face": info.get("face", ""),
            "sign": info.get("sign", ""),
        }

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        from bilibili_api import user

        user_obj = user.User(uid=uid, credential=self._credential)
        video_list = await user_obj.get_videos(
            pn=pn,
            ps=ps,
            order=user.VideoOrder.PUBDATE,
        )
        items = video_list.get("list", {}).get("vlist", [])
        return [_episode_from_vlist_item(item) for item in items]

    def _make_series(self, sid: int, series_type: str):
        from bilibili_api import channel_series

        if series_type == "series":
            type_ = channel_series.ChannelSeriesType.SERIES
        elif series_type == "season":
            type_ = channel_series.ChannelSeriesType.SEASON
        else:
            raise UnsupportedError(f"legacy 后端不支持的抓取类型：{series_type}")
        return channel_series.ChannelSeries(
            id_=sid,
            type_=type_,
            credential=self._credential,
        )

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        series = self._make_series(sid, series_type)
        meta = await series.get_meta()
        if series_type == "season":
            # season 的 get_meta 原始结构含 data.upper.name / data.upper.mid
            upper = meta.get("upper") or {}
            return {
                "name": upper.get("name", ""),
                "face": meta.get("cover", ""),
                "sign": meta.get("intro", ""),
                "author": upper.get("name", ""),
                "uid": upper.get("mid"),
            }
        # series 类型下旧逻辑不使用 meta 字段（info 直接取自配置），
        # 因此返回空字段，由 sync 层回退到 config.author 等默认值。
        return {"name": "", "face": "", "sign": "", "author": "", "uid": None}

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        from bilibili_api import channel_series

        series = self._make_series(sid, series_type)
        video_list = await series.get_videos(
            pn=pn,
            ps=ps,
            sort=channel_series.ChannelOrder.DEFAULT,
        )
        items = video_list.get("archives", [])
        return [_episode_from_archives_item(item) for item in items]

    async def get_video_owner(self, bvid: str) -> int | None:
        """返回视频所属 UP 主 mid；解析失败时抛异常（由 resolver / 降级链处理）。

        网络类异常包装为 NetworkError 供降级链识别切换；其余异常原样抛出。
        """
        from bilibili_api import video

        try:
            video_obj = video.Video(bvid=bvid, credential=self._credential)
            info = await video_obj.get_info()
        except Exception as exc:
            if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
                raise NetworkError(f"legacy 获取视频信息失败（BV={bvid}）：{exc}") from exc
            try:
                import requests  # noqa: F401

                if isinstance(exc, requests.RequestException):
                    raise NetworkError(f"legacy 获取视频信息失败（BV={bvid}）：{exc}") from exc
            except ImportError:
                pass
            raise
        owner = info.get("owner") or {}
        mid = owner.get("mid")
        return int(mid) if mid else None
