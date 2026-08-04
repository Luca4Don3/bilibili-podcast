"""B 站 API 多后端抽象层测试（全部 mock，不发起真实网络请求）。"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bilibili_podcast.api_backends import (
    BACKEND_NAMES,
    BackendError,
    NetworkError,
    RateLimitError,
    UnsupportedError,
    create_backend,
    parse_backend_spec,
)
from bilibili_podcast.sync import (
    fetch_series_episodes,
    fetch_space_episodes,
    is_bilibili_rate_limited,
)
from bilibili_podcast.utils.series_config import SeriesConfig


def _config(**overrides) -> SeriesConfig:
    """构造最小合法 SeriesConfig，覆盖字段可传关键字覆盖。"""
    base = dict(
        series="test-series",
        enabled=True,
        title="Test Series",
        description="",
        author="Demo Author",
        cover_art="",
        category="",
        subcategories=[],
        explicit=False,
        lang="zh-CN",
        source={"uid": 123456, "type": "space"},
        sync={"request_interval_seconds": 0, "request_jitter_seconds": 0},
        filters={},
        paid_preview={},
        keep_last=100,
    )
    base.update(overrides)
    return SeriesConfig(**base)


def _episode(bvid: str, pubdate: int = 0) -> dict:
    return {
        "bvid": bvid,
        "title": f"title-{bvid}",
        "description": "",
        "duration": 10,
        "image": "",
        "pubdate": pubdate,
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": {"bvid": bvid},
    }


class FakeBackend:
    """固定数据的假后端：可配置分页数据、抛错行为，并记录调用。"""

    def __init__(
        self,
        pages=None,
        *,
        meta=None,
        series_pages=None,
        videos_error=None,
        series_error=None,
        videos_error_at=None,
        series_error_at=None,
    ):
        self.pages = pages if pages is not None else []
        self.series_pages = series_pages if series_pages is not None else []
        self.meta = meta if meta is not None else {"name": "Up", "face": "f", "sign": "s"}
        self.videos_error = videos_error
        self.series_error = series_error
        self.videos_error_at = videos_error_at
        self.series_error_at = series_error_at
        self.video_calls = []
        self.series_calls = []
        self.closed = False

    async def get_user_info(self, uid: int) -> dict:
        return {"name": "Fake", "face": "f", "sign": "s"}

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        self.video_calls.append((pn, ps))
        if self.videos_error and (
            self.videos_error_at is None or len(self.video_calls) == self.videos_error_at
        ):
            raise self.videos_error
        index = pn - 1
        if index < len(self.pages):
            return self.pages[index]
        return []

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        return self.meta

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        self.series_calls.append((pn, ps))
        if self.series_error and (
            self.series_error_at is None or len(self.series_calls) == self.series_error_at
        ):
            raise self.series_error
        index = pn - 1
        if index < len(self.series_pages):
            return self.series_pages[index]
        return []

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------- fetch 逻辑


def test_fetch_space_dedup_and_pagination():
    """分页抓取：跨页去重 + 继续拉页直到空页。"""
    backend = FakeBackend(
        pages=[
            [_episode("BV1", 30), _episode("BV2", 20)],
            # 第 2 页与第 1 页重复 BV2，并新增 BV3、BV4
            [_episode("BV2", 20), _episode("BV3", 10), _episode("BV4", 8)],
            [_episode("BV5", 5)],
        ]
    )
    config = _config(
        keep_last=0,
        sync={
            "request_interval_seconds": 0,
            "request_jitter_seconds": 0,
            "page_size": 2,
            "incremental_page_size": 2,
        },
    )
    info, episodes, count = asyncio.run(fetch_space_episodes(config, backend))

    assert [ep["bvid"] for ep in episodes] == ["BV1", "BV2", "BV3", "BV4", "BV5"]
    assert count == 5
    assert info["name"] == "Fake"
    assert info["_bilibili_podcast_request_count"] == len(backend.video_calls)
    assert info["_bilibili_podcast_stopped_by_rate_limit"] is False


def test_fetch_space_rate_limit_stops():
    """后续页限流（RateLimitError）被识别并置 stopped_by_rate_limit，不向上抛。"""
    backend = FakeBackend(
        pages=[[_episode("BV1")], [_episode("BV2")]],
        videos_error=RateLimitError(),
        videos_error_at=2,
    )
    config = _config(
        keep_last=0,
        sync={
            "request_interval_seconds": 0,
            "request_jitter_seconds": 0,
            "page_size": 2,
            "incremental_page_size": 1,
        },
    )
    info, episodes, _ = asyncio.run(fetch_space_episodes(config, backend))

    assert [ep["bvid"] for ep in episodes] == ["BV1"]
    assert info["_bilibili_podcast_stopped_by_rate_limit"] is True


def test_fetch_space_other_error_raises():
    """后续页非限流异常原样上抛。"""
    backend = FakeBackend(
        pages=[[_episode("BV1")]],
        videos_error=RuntimeError("boom"),
        videos_error_at=2,
    )
    config = _config(
        keep_last=0,
        sync={
            "request_interval_seconds": 0,
            "request_jitter_seconds": 0,
            "page_size": 2,
            "incremental_page_size": 1,
        },
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(fetch_space_episodes(config, backend))


def test_fetch_series_season_meta_used():
    """season 类型：info 优先取 meta 的 name/face/sign。"""
    backend = FakeBackend(
        meta={"name": "SeasonName", "face": "face-x", "sign": "sign-x"},
        series_pages=[[_episode("BV1"), _episode("BV2")], []],
    )
    config = _config(source={"uid": 1, "sid": 99, "type": "season"}, keep_last=0)
    info, episodes, count = asyncio.run(fetch_series_episodes(config, backend))

    assert info["name"] == "SeasonName"
    assert info["face"] == "face-x"
    assert info["sign"] == "sign-x"
    assert [ep["bvid"] for ep in episodes] == ["BV1", "BV2"]
    assert count == 2


def test_fetch_series_series_type_uses_config_fields():
    """series 类型：info 使用配置字段（与旧行为一致），meta 仅作请求。"""
    backend = FakeBackend(series_pages=[[_episode("BV1")], []])
    config = _config(
        source={"uid": 1, "sid": 99, "type": "series"},
        author="Config Author",
        cover_art="cfg-cover",
        description="cfg-desc",
        keep_last=0,
    )
    info, episodes, _ = asyncio.run(fetch_series_episodes(config, backend))

    assert info["name"] == "Config Author"
    assert info["face"] == "cfg-cover"
    assert info["sign"] == "cfg-desc"
    assert [ep["bvid"] for ep in episodes] == ["BV1"]


def test_fetch_series_rate_limit_stops():
    backend = FakeBackend(
        series_pages=[[_episode("BV1"), _episode("BV2")]],
        series_error=RateLimitError("自定义限流消息"),
        series_error_at=2,
    )
    config = _config(source={"uid": 1, "sid": 99, "type": "season"}, keep_last=0)
    info, episodes, _ = asyncio.run(fetch_series_episodes(config, backend))

    assert [ep["bvid"] for ep in episodes] == ["BV1", "BV2"]
    assert info["_bilibili_podcast_stopped_by_rate_limit"] is True


# ---------------------------------------------------------------- create_backend


def test_create_backend_unknown_name():
    with pytest.raises(ValueError, match="未知的 API 后端名称"):
        asyncio.run(create_backend("nope", None))


@pytest.mark.parametrize("name,module", [("bilix", "bilix"), ("yutto", "yutto"), ("bilibili-api", "bilibili_api"), ("native", "curl_cffi")])
def test_create_backend_missing_dependency(name, module, monkeypatch):
    """依赖未安装时 create_backend 不立即失败（惰性构造），首次调用方法时才抛 BackendError。"""
    monkeypatch.setitem(sys.modules, module, None)
    chain = asyncio.run(create_backend(name, None))
    with pytest.raises(BackendError):
        asyncio.run(chain.get_user_info(1))


def test_create_backend_legacy_works(monkeypatch):
    """legacy 后端在依赖可用时正常构造（monkeypatch 假 bilibili_api 顶层模块），返回 BackendChain。"""
    pytest.importorskip("bilibili_api")
    import bilibili_api

    monkeypatch.setattr(bilibili_api, "request_settings", types.SimpleNamespace(set=lambda *a, **k: None))
    chain = asyncio.run(create_backend("bilibili-api", None))
    try:
        assert type(chain).__name__ == "BackendChain"
    finally:
        asyncio.run(chain.close())


def test_create_backend_native_works():
    """native 后端正常构造（curl_cffi 为主依赖已安装），返回 BackendChain。"""
    chain = asyncio.run(create_backend("native", None))
    try:
        assert type(chain).__name__ == "BackendChain"
    finally:
        asyncio.run(chain.close())


def test_create_backend_comma_spec_returns_chain():
    """逗号分隔配置解析为降级链；非法名 ValueError 列出可用名。"""
    chain = asyncio.run(create_backend(" yutto , bilix,yutto ", None))
    assert chain._names == ["yutto", "bilix"]
    with pytest.raises(ValueError, match="未知的 API 后端名称"):
        asyncio.run(create_backend("yutto,nope", None))
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(create_backend("", None))
    with pytest.raises(ValueError, match="不能为空"):
        asyncio.run(create_backend([], None))


# ---------------------------------------------------------------- legacy 字段映射


def test_legacy_backend_field_mapping(monkeypatch):
    """legacy 后端：替换 bilibili_api 底层对象，验证统一格式字段映射。"""
    pytest.importorskip("bilibili_api")
    import bilibili_api

    from bilibili_podcast.api_backends.legacy import LegacyBackend

    class FakeUser:
        def __init__(self, uid, credential=None):
            self.uid = uid
            self.credential = credential

        async def get_user_info(self):
            return {"name": "UP", "face": "face", "sign": "sign"}

        async def get_videos(self, pn, ps, order):
            return {
                "list": {
                    "vlist": [
                        {"bv_id": "BV1", "title": "T1", "intro": "D1", "length": 12, "pic": "P1", "created": 111},
                        {"bvid": "BV2", "title": "T2", "duration": 34, "cover": "P2", "pubtime": 222},
                    ]
                }
            }

    class FakeChannelSeries:
        def __init__(self, id_, type_, credential=None):
            self.type_ = type_

        async def get_meta(self):
            return {"upper": {"name": "UP", "mid": 777}, "cover": "c", "intro": "i"}

        async def get_videos(self, pn, ps, sort):
            return {"archives": [{"bvid": "BV9", "title": "S", "duration": 5000, "pic": "p", "pubdate": 1}]}

    class FakeVideo:
        def __init__(self, bvid, credential=None):
            self.bvid = bvid

        async def get_info(self):
            return {"owner": {"mid": 555}}

    monkeypatch.setattr(bilibili_api, "request_settings", types.SimpleNamespace(set=lambda *a, **k: None))
    monkeypatch.setattr(bilibili_api, "Credential", lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(bilibili_api, "user", types.SimpleNamespace(User=FakeUser, VideoOrder=types.SimpleNamespace(PUBDATE="pubdate")))
    monkeypatch.setattr(bilibili_api, "video", types.SimpleNamespace(Video=FakeVideo))
    monkeypatch.setattr(
        bilibili_api,
        "channel_series",
        types.SimpleNamespace(
            ChannelSeries=FakeChannelSeries,
            ChannelSeriesType=types.SimpleNamespace(SERIES="series", SEASON="season"),
            ChannelOrder=types.SimpleNamespace(DEFAULT="default"),
        ),
    )

    backend = LegacyBackend({"sessdata": "s", "bili_jct": "j", "dedeuserid": "d"})
    assert asyncio.run(backend.get_user_info(1)) == {"name": "UP", "face": "face", "sign": "sign"}

    eps = asyncio.run(backend.get_user_videos(1, 1, 10))
    assert eps[0] == {
        "bvid": "BV1",
        "title": "T1",
        "description": "D1",
        "duration": 12,
        "image": "P1",
        "pubdate": 111,
        "link": "https://www.bilibili.com/video/BV1",
        "raw": {"bv_id": "BV1", "title": "T1", "intro": "D1", "length": 12, "pic": "P1", "created": 111},
    }
    assert eps[1]["bvid"] == "BV2"
    assert eps[1]["duration"] == 34
    assert eps[1]["pubdate"] == 222

    assert asyncio.run(backend.get_series_meta(9, "season")) == {
        "name": "UP", "face": "c", "sign": "i", "author": "UP", "uid": 777,
    }
    assert asyncio.run(backend.get_series_meta(9, "series")) == {
        "name": "", "face": "", "sign": "", "author": "", "uid": None,
    }
    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_meta(9, "weird"))

    s_eps = asyncio.run(backend.get_series_videos(9, "season", 1, 100))
    assert s_eps[0]["bvid"] == "BV9"
    assert s_eps[0]["duration"] == 5  # 5000ms → 5 秒
    assert s_eps[0]["pubdate"] == 1

    assert asyncio.run(backend.get_video_owner("BV1")) == 555


# ---------------------------------------------------------------- bilix 字段映射


class _FakeRes:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """记录请求并按 URL 返回固定响应。"""

    def __init__(self, arc_payload=None, series_payload=None, archives_payload=None):
        self.calls = []
        self.arc_payload = arc_payload
        self.series_payload = series_payload
        self.archives_payload = archives_payload

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if "arc/search" in url:
            return _FakeRes(self.arc_payload or {"code": 0, "data": {"list": {"vlist": []}}})
        if "/x/series/series" in url:
            return _FakeRes(self.series_payload or {"code": 0, "data": {"meta": {}}})
        return _FakeRes(self.archives_payload or {"code": 0, "data": {"archives": []}})


def _bilix_backend(client, api):
    from bilibili_podcast.api_backends.bilix import BilixBackend

    backend = BilixBackend.__new__(BilixBackend)  # 绕过 __init__，注入假依赖
    backend._client = client
    backend._bilix_api = api
    backend._series_cache = {}  # 模拟 __init__ 中的缓存初始化
    return backend


def test_bilix_get_user_videos_field_mapping():
    from bilibili_podcast.api_backends.bilix import _episode_from_vlist_item

    item = {"bvid": "BV1", "title": "T", "description": "D", "length": 45, "pic": "P", "created": 111}
    ep = _episode_from_vlist_item(item)
    assert ep["bvid"] == "BV1"
    assert ep["duration"] == 45
    assert ep["image"] == "P"
    assert ep["pubdate"] == 111
    assert ep["link"] == "https://www.bilibili.com/video/BV1"

    client = _FakeClient(
        arc_payload={
            "code": 0,
            "data": {"list": {"vlist": [item, {"bvid": "BV2", "title": "T2", "created": 222, "pic": "", "length": 0}]}},
        }
    )
    api = types.SimpleNamespace(_add_sign=AsyncMock(return_value=None))
    backend = _bilix_backend(client, api)
    eps = asyncio.run(backend.get_user_videos(1, 1, 30))
    assert [ep["bvid"] for ep in eps] == ["BV1", "BV2"]
    assert api._add_sign.await_count == 1


def test_bilix_get_user_videos_fallback_to_bvid_only():
    """arc/search 详情失败（非限流）时降级为仅 bvid 条目。"""
    client = _FakeClient(arc_payload={"code": -404, "message": "视频不存在"})
    api = types.SimpleNamespace(
        _add_sign=AsyncMock(return_value=None),
        get_up_video_info=AsyncMock(return_value=("up", 2, ["BV1", "BV2"])),
    )
    backend = _bilix_backend(client, api)
    eps = asyncio.run(backend.get_user_videos(1, 1, 30))
    assert [ep["bvid"] for ep in eps] == ["BV1", "BV2"]
    assert eps[0]["title"] == "BV1"  # bvid 占位
    assert api.get_up_video_info.await_count == 1


def test_bilix_get_user_videos_rate_limit_raises():
    """arc/search 返回限流特征时上抛 RateLimitError（由 sync 层处理）。"""
    from bilibili_podcast.api_backends.base import RateLimitError

    client = _FakeClient(arc_payload={"code": -799, "message": "请求过于频繁"})
    api = types.SimpleNamespace(
        _add_sign=AsyncMock(return_value=None),
        get_up_video_info=AsyncMock(return_value=("up", 2, ["BV1", "BV2"])),
    )
    backend = _bilix_backend(client, api)
    with pytest.raises(RateLimitError):
        asyncio.run(backend.get_user_videos(1, 1, 30))
    assert api.get_up_video_info.await_count == 0  # 限流不降级


def test_bilix_series_support_and_season_rejected():
    from bilibili_podcast.api_backends.bilix import _episode_from_archives_item

    item = {"bvid": "BV9", "title": "S", "duration": 5, "pic": "p", "pubdate": 1, "description": "d"}
    assert _episode_from_archives_item(item)["duration"] == 5

    client = _FakeClient(
        series_payload={"code": 0, "data": {"meta": {"mid": 42, "total": 2, "name": "List"}}},
        archives_payload={
            "code": 0,
            "data": {"archives": [item, {"bvid": "BV8", "title": "S2", "duration": 6, "pic": "", "pubdate": 2}]},
        },
    )
    api = types.SimpleNamespace(get_list_info=AsyncMock(return_value=("List", "UP", ["BV9", "BV8"])))
    backend = _bilix_backend(client, api)

    meta = asyncio.run(backend.get_series_meta(9, "series"))
    assert meta["name"] == "List"
    assert meta["author"] == ""
    assert meta["uid"] == 42

    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_video_owner("BV1"))

    # 第 2 页（ps=1）取全量列表第 2 条
    eps = asyncio.run(backend.get_series_videos(9, "series", pn=2, ps=1))
    assert [ep["bvid"] for ep in eps] == ["BV8"]

    with pytest.raises(UnsupportedError, match="season"):
        asyncio.run(backend.get_series_meta(9, "season"))
    with pytest.raises(UnsupportedError, match="season"):
        asyncio.run(backend.get_series_videos(9, "season", 1, 100))


# ---------------------------------------------------------------- yutto 字段映射


def _install_fake_yutto(monkeypatch, space=None, ugc_video=None, collection=None, bangumi=None, fetcher_json=None):
    """向 sys.modules 注入假 yutto 模块树，避免真实 import。"""
    fake_yutto = types.ModuleType("yutto")
    fake_api = types.ModuleType("yutto.api")

    class _Id:
        def __init__(self, value):
            self.value = str(value)

        def __str__(self):
            return self.value

    fake_types = types.ModuleType("yutto.types")
    fake_types.MId = _Id
    fake_types.SeriesId = _Id
    fake_types.SeasonId = _Id

    fake_utils = types.ModuleType("yutto.utils")
    fake_fetcher = types.ModuleType("yutto.utils.fetcher")
    class FakeFetcher:
        fetch_json = staticmethod(AsyncMock(return_value=fetcher_json))
    fake_fetcher.Fetcher = FakeFetcher

    fake_api.space = space or types.SimpleNamespace()
    fake_api.ugc_video = ugc_video or types.SimpleNamespace()
    fake_api.collection = collection or types.SimpleNamespace()
    fake_api.bangumi = bangumi or types.SimpleNamespace()

    monkeypatch.setitem(sys.modules, "yutto", fake_yutto)
    monkeypatch.setitem(sys.modules, "yutto.api", fake_api)
    monkeypatch.setitem(sys.modules, "yutto.api.space", fake_api.space)
    monkeypatch.setitem(sys.modules, "yutto.api.ugc_video", fake_api.ugc_video)
    monkeypatch.setitem(sys.modules, "yutto.api.collection", fake_api.collection)
    monkeypatch.setitem(sys.modules, "yutto.api.bangumi", fake_api.bangumi)
    monkeypatch.setitem(sys.modules, "yutto.types", fake_types)
    monkeypatch.setitem(sys.modules, "yutto.utils", fake_utils)
    monkeypatch.setitem(sys.modules, "yutto.utils.fetcher", fake_fetcher)
    return fake_types


def _yutto_backend():
    from bilibili_podcast.api_backends.yutto import YuttoBackend

    backend = YuttoBackend.__new__(YuttoBackend)  # 绕过 __init__
    backend._ctx = None
    backend._client = None
    backend._series_mid_cache = {}
    return backend


def test_yutto_get_user_videos_field_mapping(monkeypatch):
    from bilibili_podcast.api_backends.yutto import _episode_from_ugc_info

    info = {"bvid": "BV1", "title": "T", "description": "D", "picture": "P", "pubdate": 111}
    ep = _episode_from_ugc_info(info)
    assert ep["duration"] == 0  # yutto 无时长字段
    assert ep["image"] == "P"
    assert ep["pubdate"] == 111

    fake_types = _install_fake_yutto(monkeypatch)
    avids = [fake_types.MId("BV1"), fake_types.MId("BV2"), fake_types.MId("BV3")]
    space = types.SimpleNamespace(
        get_user_space_all_videos_avids=AsyncMock(return_value=avids)
    )
    ugc_video = types.SimpleNamespace(
        get_ugc_video_info=AsyncMock(
            side_effect=lambda ctx, client, avid: {
                "bvid": str(avid),
                "title": f"T{avid}",
                "description": "d",
                "picture": f"P{avid}",
                "pubdate": 1,
            }
        )
    )
    fake_types = _install_fake_yutto(monkeypatch, space=space, ugc_video=ugc_video)
    backend = _yutto_backend()

    # 全量 3 条，pn=2, ps=1 只取第 2 条，且只对该条请求详情
    eps = asyncio.run(backend.get_user_videos(1, pn=2, ps=1))
    assert [ep["bvid"] for ep in eps] == ["BV2"]
    assert eps[0]["title"] == "TBV2"
    assert ugc_video.get_ugc_video_info.await_count == 1
    # 首页 ps=2 取前两条
    eps = asyncio.run(backend.get_user_videos(1, pn=1, ps=2))
    assert [ep["bvid"] for ep in eps] == ["BV1", "BV2"]


def test_yutto_series_and_season_mapping(monkeypatch):
    from bilibili_podcast.api_backends.yutto import (
        _episode_from_bangumi_item,
        _episode_from_collection_item,
    )

    coll_item = {"id": 1, "title": "", "avid": "BV1"}
    ep = _episode_from_collection_item(coll_item)
    assert ep["title"] == "BV1"  # 空标题回退 bvid 占位

    bangumi_item = {"id": 1, "name": "EP1", "avid": "BV9", "metadata": {"plot": "p", "thumb": "t", "premiered": 5}}
    ep = _episode_from_bangumi_item(bangumi_item)
    assert ep["title"] == "EP1"
    assert ep["image"] == "t"
    assert ep["pubdate"] == 5
    assert ep["duration"] == 0

    collection = types.SimpleNamespace(
        get_collection_details=AsyncMock(
            return_value={
                "title": "Coll",
                "pages": [{"id": 1, "title": "", "avid": "BV1"}, {"id": 2, "title": "", "avid": "BV2"}],
            }
        )
    )
    bangumi = types.SimpleNamespace(
        get_bangumi_list=AsyncMock(
            return_value={
                "title": "Anime",
                "pages": [{"id": 1, "name": "EP1", "avid": "BV9", "metadata": {"plot": "p", "thumb": "t", "premiered": 5}}],
            }
        )
    )
    fetcher_json = {"data": {"meta": {"mid": 42}}}
    _install_fake_yutto(monkeypatch, collection=collection, bangumi=bangumi, fetcher_json=fetcher_json)
    backend = _yutto_backend()

    meta = asyncio.run(backend.get_series_meta(9, "series"))
    assert meta["name"] == "Coll"
    assert meta["uid"] == 42

    eps = asyncio.run(backend.get_series_videos(9, "series", pn=2, ps=1))
    assert [ep["bvid"] for ep in eps] == ["BV2"]

    meta = asyncio.run(backend.get_series_meta(9, "season"))
    assert meta["name"] == "Anime"
    assert meta["face"] == "t"
    assert meta["author"] == ""
    assert meta["uid"] is None

    eps = asyncio.run(backend.get_series_videos(9, "season", pn=1, ps=100))
    assert eps[0]["bvid"] == "BV9"
    assert eps[0]["pubdate"] == 5

    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_videos(9, "weird", 1, 100))


def test_yutto_get_video_owner(monkeypatch):
    """yutto 后端 get_video_owner：回退请求 view 接口取 data.owner.mid。"""
    _install_fake_yutto(monkeypatch, fetcher_json={"data": {"owner": {"mid": 555}}})
    backend = _yutto_backend()
    assert asyncio.run(backend.get_video_owner("BV1")) == 555

    _install_fake_yutto(monkeypatch, fetcher_json={"data": {}})
    assert asyncio.run(backend.get_video_owner("BV1")) is None


# ---------------------------------------------------------------- native 字段映射


class _NativeFakeRes:
    """假响应：固定 status_code 与 JSON payload。"""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _NativeFakeSession:
    """按完整 URL 返回固定响应的假 curl_cffi session，记录所有请求。"""

    def __init__(self, routes, status_code=200):
        self.routes = routes  # {完整 URL: payload}
        self.status_code = status_code
        self.calls = []  # [(url, params, headers)]

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), headers))
        payload = self.routes.get(url)
        if payload is None:
            payload = {"code": 0, "data": {}}
        return _NativeFakeRes(payload, self.status_code)

    async def close(self):
        pass


def _native_backend(session, wbi_keys=("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")):
    """绕过 __init__ 注入假 session 构造 NativeBackend（预置 WBI key 跳过 nav）。"""
    from bilibili_podcast.api_backends.native import NativeBackend

    backend = NativeBackend.__new__(NativeBackend)
    backend._session = session
    backend._wbi_keys = wbi_keys
    backend._wbi_keys_fetched_at = time.monotonic()  # 缓存视为新鲜，跳过 nav 刷新
    backend._ready = True
    backend._series_cache = {}
    return backend


def test_native_wbi_sign_vectors():
    """WBI 签名与公开文档示例向量一致（mixin_key、w_rid、中文/空格编码）。"""
    import hashlib

    from bilibili_podcast.api_backends.native import get_mixin_key, sign_wbi_params

    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    # 文档示例：按重排映射表打乱后截取前 32 位
    assert get_mixin_key(img_key + sub_key) == "ea1db124af3c7062474693fa704f4ff8"
    # 文档"计算签名"章节示例：foo/bar/zab + wts=1702204169 → w_rid
    params = sign_wbi_params({"foo": "114", "bar": "514", "zab": 1919810}, img_key, sub_key, wts=1702204169)
    assert params["wts"] == 1702204169
    assert params["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    # 文档示例 3：中文与空格编码（字母大写、空格 %20）
    params = sign_wbi_params({"foo": "one one four", "bar": "五一四", "baz": 1919810}, img_key, sub_key, wts=1702204169)
    query = "bar=%E4%BA%94%E4%B8%80%E5%9B%9B&baz=1919810&foo=one%20one%20four&wts=1702204169"
    assert params["w_rid"] == hashlib.md5((query + "ea1db124af3c7062474693fa704f4ff8").encode("utf-8")).hexdigest()


def test_native_get_user_info_field_mapping():
    """acc/info → {name, face, sign}；请求自动附加 wbi 签名。"""
    url = "https://api.bilibili.com/x/space/wbi/acc/info"
    session = _NativeFakeSession({url: {"code": 0, "data": {"name": "UP", "face": "face", "sign": "签名"}}})
    backend = _native_backend(session)
    assert asyncio.run(backend.get_user_info(1)) == {"name": "UP", "face": "face", "sign": "签名"}
    _, params, headers = session.calls[0]
    assert params["mid"] == 1
    assert "w_rid" in params and "wts" in params  # wbi 签名已附加
    assert headers["Referer"] == "https://www.bilibili.com"


def test_native_get_user_videos_field_mapping():
    """arc/search vlist → 统一 episode；兼容 description/intro、length/duration、created/pubtime。"""
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    vlist = [
        {"bvid": "BV1", "title": "T1", "description": "D1", "length": 12, "pic": "P1", "created": 111},
        {"bvid": "BV2", "title": "T2", "intro": "I2", "duration": 34, "cover": "P2", "pubtime": 222},
    ]
    session = _NativeFakeSession({url: {"code": 0, "data": {"list": {"vlist": vlist}}}})
    backend = _native_backend(session)
    eps = asyncio.run(backend.get_user_videos(1, 1, 30))
    assert eps[0] == {
        "bvid": "BV1",
        "title": "T1",
        "description": "D1",
        "duration": 12,
        "image": "P1",
        "pubdate": 111,
        "link": "https://www.bilibili.com/video/BV1",
        "raw": vlist[0],
    }
    assert eps[1]["description"] == "I2"
    assert eps[1]["duration"] == 34
    assert eps[1]["pubdate"] == 222
    _, params, _ = session.calls[0]
    assert params["order"] == "pubdate"
    assert params["pn"] == 1 and params["ps"] == 30


def test_native_series_meta_mapping():
    """series → x/series/series 的 data.meta；season → pgc result（up_info.uname）。"""
    session = _NativeFakeSession({
        "https://api.bilibili.com/x/series/series": {"code": 0, "data": {"meta": {"mid": 42, "name": "合集", "cover": "c", "intro": "i"}}},
        "https://api.bilibili.com/pgc/view/web/season": {"code": 0, "result": {"title": "番剧", "cover": "cv", "evaluate": "e", "up_info": {"uname": "UP", "mid": 7}}},
    })
    backend = _native_backend(session)
    assert asyncio.run(backend.get_series_meta(9, "series")) == {
        "name": "合集", "face": "c", "sign": "i", "author": "", "uid": 42,
    }
    assert asyncio.run(backend.get_series_meta(9, "season")) == {
        "name": "番剧", "face": "cv", "sign": "e", "author": "UP", "uid": 7,
    }
    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_meta(9, "weird"))


def test_native_series_videos_pagination():
    """series/season 均一次拿全量再按 pn/ps 切片；缓存避免重复请求。"""
    session = _NativeFakeSession({
        "https://api.bilibili.com/x/series/series": {"code": 0, "data": {"meta": {"mid": 42, "total": 5}}},
        "https://api.bilibili.com/x/series/archives": {
            "code": 0,
            "data": {"archives": [{"bvid": f"BV{i}", "title": f"T{i}", "duration": 10, "pic": "", "pubdate": i} for i in range(1, 6)]},
        },
        "https://api.bilibili.com/pgc/view/web/season": {
            "code": 0,
            "result": {"episodes": [{"bvid": f"BV{i}", "title": f"T{i}", "duration": 10, "cover": "", "pub_time": i} for i in range(1, 6)]},
        },
    })
    backend = _native_backend(session)
    # series 第 2 页 ps=2 → BV3/BV4（archives 全量 5 条再切片）
    eps = asyncio.run(backend.get_series_videos(9, "series", pn=2, ps=2))
    assert [ep["bvid"] for ep in eps] == ["BV3", "BV4"]
    # 缓存生效：再次分页不再请求
    eps = asyncio.run(backend.get_series_videos(9, "series", pn=1, ps=10))
    assert [ep["bvid"] for ep in eps] == ["BV1", "BV2", "BV3", "BV4", "BV5"]
    assert len(session.calls) == 2  # series 元数据 + archives 各一次
    # season 第 3 页 ps=2 → BV5；pub_time → pubdate
    eps = asyncio.run(backend.get_series_videos(9, "season", pn=3, ps=2))
    assert [ep["bvid"] for ep in eps] == ["BV5"]
    assert eps[0]["pubdate"] == 5
    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_videos(9, "weird", 1, 100))


def test_native_get_video_owner():
    """view 接口 → data.owner.mid；owner 缺失返回 None。"""
    url = "https://api.bilibili.com/x/web-interface/view"
    backend = _native_backend(_NativeFakeSession({url: {"code": 0, "data": {"owner": {"mid": 555}}}}))
    assert asyncio.run(backend.get_video_owner("BV1")) == 555
    backend = _native_backend(_NativeFakeSession({url: {"code": 0, "data": {}}}))
    assert asyncio.run(backend.get_video_owner("BV1")) is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"code": -799, "message": "请求过于频繁，请稍后重试"}, RateLimitError),
        ({"code": -412, "message": "请求被拦截"}, RateLimitError),
        ({"code": 412, "message": "请求被拦截"}, RateLimitError),
        ({"code": 403, "message": "权限不足"}, RateLimitError),
        ({"code": -404, "message": "啥都木有"}, BackendError),
        ({"code": 0, "data": {}}, None),
    ],
)
def test_native_error_code_mapping(payload, expected):
    """code!=0 统一转异常：-799/请求过于频繁、-412/412/403 → RateLimitError；其他 → BackendError。"""
    url = "https://api.bilibili.com/x/space/wbi/acc/info"
    backend = _native_backend(_NativeFakeSession({url: payload}))
    if expected is None:
        assert asyncio.run(backend.get_user_info(1)) == {"name": "", "face": "", "sign": ""}
    else:
        with pytest.raises(expected):
            asyncio.run(backend.get_user_info(1))


def test_native_network_error_wrapped():
    """session.get 抛网络异常 → NetworkError。"""

    class _BoomSession:
        async def get(self, url, params=None, headers=None):
            raise ConnectionError("connection refused")

        async def close(self):
            pass

    backend = _native_backend(_BoomSession())
    with pytest.raises(NetworkError, match="connection refused"):
        asyncio.run(backend.get_user_info(1))


def test_native_http_status_errors():
    """HTTP 412 → RateLimitError（风控）；HTTP 500 → NetworkError。"""
    backend = _native_backend(_NativeFakeSession({}, status_code=412))
    with pytest.raises(RateLimitError):
        asyncio.run(backend.get_user_info(1))
    backend = _native_backend(_NativeFakeSession({}, status_code=500))
    with pytest.raises(NetworkError):
        asyncio.run(backend.get_user_info(1))


def test_native_wbi_keys_from_nav_and_fallback():
    """nav 的 img_url/sub_url 解析出 img_key/sub_key；nav 失败回退公开备用常量。"""
    from bilibili_podcast.api_backends.native import NativeBackend, _FALLBACK_IMG_KEY, _FALLBACK_SUB_KEY

    nav = "https://api.bilibili.com/x/web-interface/nav"
    acc = "https://api.bilibili.com/x/space/wbi/acc/info"
    session = _NativeFakeSession({
        nav: {"code": 0, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png", "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}},
        acc: {"code": 0, "data": {"name": "UP"}},
    })
    backend = NativeBackend.__new__(NativeBackend)
    backend._session = session
    backend._wbi_keys = None
    backend._ready = True
    backend._series_cache = {}
    assert asyncio.run(backend.get_user_info(1))["name"] == "UP"
    assert backend._wbi_keys == ("7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45")
    # nav 未返回有效 wbi_img（未注册 URL → 空 data）→ 回退备用常量
    session2 = _NativeFakeSession({acc: {"code": 0, "data": {"name": "UP"}}})
    backend2 = NativeBackend.__new__(NativeBackend)
    backend2._session = session2
    backend2._wbi_keys = None
    backend2._ready = True
    backend2._series_cache = {}
    assert asyncio.run(backend2.get_user_info(1))["name"] == "UP"
    assert backend2._wbi_keys == (_FALLBACK_IMG_KEY, _FALLBACK_SUB_KEY)


# ---------------------------------------------------------------- native buvid 会话指纹


def test_native_buvid_fingerprint_formats():
    """本地生成指纹符合合法格式：buvid3 带 XZ02 前缀的大写 UUID + 数字 + infoc；
    buvid4 为带横线大写 UUID + 数字段 + -666 + base64 串；b_nut 为秒级时间戳。"""
    import re

    from bilibili_podcast.api_backends.native import _generate_buvid_fingerprints

    fp = _generate_buvid_fingerprints()
    assert re.fullmatch(r"XZ02[0-9A-F]{32}\d{1,10}infoc", fp["buvid3"])
    assert re.fullmatch(
        r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}"
        r"\d{5}-\d{9}-666[A-Za-z0-9+/]{22}==",
        fp["buvid4"],
    )
    assert re.fullmatch(r"\d{10}", fp["b_nut"])


def test_native_buvid_fingerprint_uniqueness():
    """多次调用生成的指纹各不相同（UUID 随机）。"""
    from bilibili_podcast.api_backends.native import _generate_buvid_fingerprints

    fp1 = _generate_buvid_fingerprints()
    fp2 = _generate_buvid_fingerprints()
    assert fp1["buvid3"] != fp2["buvid3"]
    assert fp1["buvid4"] != fp2["buvid4"]


def test_native_buvid_credential_overrides_local(monkeypatch):
    """用户凭证 buvid3 优先于本地生成的指纹；buvid4/b_nut 由本地补齐。"""
    from bilibili_podcast.api_backends.native import NativeBackend

    captured: dict = {}

    class _FakeAsyncSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            pass

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeAsyncSession)
    backend = NativeBackend(
        {"sessdata": "s", "bili_jct": "j", "dedeuserid": "d", "buvid3": "USER-BUVID3-123"}
    )
    cookies = captured["cookies"]
    assert cookies["buvid3"] == "USER-BUVID3-123"  # 用户凭证覆盖本地生成
    assert "buvid4" in cookies and "b_nut" in cookies  # 本地指纹补齐缺失字段
    assert cookies["SESSDATA"] == "s"  # 既有 cookie 传递不受影响


def test_native_buvid_injected_without_credential(monkeypatch):
    """无凭证（匿名会话）时本地指纹仍注入会话。"""
    from bilibili_podcast.api_backends.native import NativeBackend

    captured: dict = {}

    class _FakeAsyncSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            pass

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _FakeAsyncSession)
    NativeBackend(None)
    cookies = captured["cookies"]
    assert cookies["buvid3"].startswith("XZ02")
    assert "buvid4" in cookies and "b_nut" in cookies


# ---------------------------------------------------------------- BackendChain 会话统计


def test_chain_stats_counts_calls_failures_switches(monkeypatch):
    """统计：首个后端失败切换后，失败/切走/成功调用计数正确。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=RateLimitError())
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    assert chain.stats == {}  # 未构造的后端不记录
    asyncio.run(chain.get_series_meta(1, "series"))
    assert chain.stats["first"] == {"calls": 0, "failures": 1, "switches": 1}
    assert chain.stats["second"] == {"calls": 1, "failures": 0, "switches": 0}

    # 切换持久：第二次调用仍走 second，first 计数不变
    asyncio.run(chain.get_series_meta(1, "series"))
    assert chain.stats["second"]["calls"] == 2
    assert chain.stats["first"] == {"calls": 0, "failures": 1, "switches": 1}

    asyncio.run(chain.close())


def test_chain_stats_returns_copy(monkeypatch):
    """stats 返回副本，外部修改不影响内部统计。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first")
    _install_chain_factory(monkeypatch, [first])
    chain = BackendChain(["first"])
    asyncio.run(chain.get_user_info(1))

    chain.stats["first"]["calls"] = 999
    assert chain.stats["first"]["calls"] == 1

    asyncio.run(chain.close())


def test_chain_stats_construction_failure_not_recorded(monkeypatch):
    """构造失败的后端不进入统计（未构造不记录）。"""
    from bilibili_podcast.api_backends import BackendChain

    second = _ChainBackend("second")

    async def factory(name, credential):
        if name == "first":
            raise BackendError("first 依赖未安装")
        return second

    from bilibili_podcast import api_backends as pkg

    monkeypatch.setattr(pkg, "_create_single_backend", factory)
    chain = BackendChain(["first", "second"])
    asyncio.run(chain.get_series_meta(1, "series"))

    assert "first" not in chain.stats  # 构造失败未记录
    assert chain.stats["second"] == {"calls": 1, "failures": 0, "switches": 0}
    asyncio.run(chain.close())


def test_chain_close_logs_stats_summary(monkeypatch, caplog):
    """close() 在 debug 级输出统计汇总（不含凭证字段）。"""
    import logging

    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first")
    _install_chain_factory(monkeypatch, [first])
    chain = BackendChain(["first"])
    asyncio.run(chain.get_user_info(1))

    with caplog.at_level(logging.DEBUG, logger="bilibili_podcast.api_backends.chain"):
        asyncio.run(chain.close())
    assert "后端链会话统计汇总" in caplog.text
    assert '"calls": 1' in caplog.text


# ---------------------------------------------------------------- parse_backend_spec


def test_parse_backend_spec():
    from bilibili_podcast.api_backends import parse_backend_spec

    assert parse_backend_spec("yutto, bilix ,yutto") == ["yutto", "bilix"]
    assert parse_backend_spec("bilibili-api") == ["bilibili-api"]
    assert parse_backend_spec(" , yutto , ") == ["yutto"]
    with pytest.raises(ValueError, match="不能为空"):
        parse_backend_spec("")
    with pytest.raises(ValueError, match="不能为空"):
        parse_backend_spec(" , , ")


# ---------------------------------------------------------------- BackendChain 降级链


class _ChainBackend:
    """可配置抛错/记录 close 与调用的假后端，用于 BackendChain 测试。"""

    def __init__(self, name, *, error=None, owner=42):
        self.name = name
        self.error = error
        self.owner = owner
        self.closed = False
        self.meta_calls = 0

    async def get_user_info(self, uid):
        return {"name": self.name, "face": "", "sign": ""}

    async def get_user_videos(self, uid, pn, ps):
        return []

    async def get_series_meta(self, sid, series_type):
        self.meta_calls += 1
        if self.error:
            raise self.error
        return {"name": self.name, "face": "", "sign": "", "author": self.name, "uid": 1}

    async def get_series_videos(self, sid, series_type, pn, ps):
        if self.error:
            raise self.error
        return []

    async def get_video_owner(self, bvid):
        if self.error:
            raise self.error
        return self.owner

    async def close(self):
        self.closed = True


def _install_chain_factory(monkeypatch, backends):
    """替换包的 _create_single_backend 为按名称返回假后端的工厂。"""
    from bilibili_podcast import api_backends as pkg

    async def factory(name, credential):
        for b in backends:
            if b.name == name:
                return b
        raise BackendError(f"未知后端：{name}")

    monkeypatch.setattr(pkg, "_create_single_backend", factory)
    return backends


def test_chain_switches_on_rate_limit(monkeypatch):
    """第一个后端 RateLimitError → 切换到第二个成功；成功后端保持不重置。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=RateLimitError())
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    meta = asyncio.run(chain.get_series_meta(1, "series"))
    assert meta["name"] == "second"
    assert first.meta_calls == 1  # 每次调用只试一次当前后端
    assert second.meta_calls == 1

    # 切换持久：第二次调用仍走 second，first 不再被尝试
    asyncio.run(chain.get_series_meta(1, "series"))
    assert first.meta_calls == 1
    assert second.meta_calls == 2

    asyncio.run(chain.close())
    assert first.closed and second.closed


def test_chain_switches_on_unsupported_error(monkeypatch):
    """UnsupportedError 同样触发降级切换。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=UnsupportedError("不支持"))
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    assert asyncio.run(chain.get_video_owner("BV1")) == 42


def test_chain_all_fail_raises_last(monkeypatch):
    """全部后端失败：抛最后一个异常（保留原始信息）。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=RateLimitError("第一限流"))
    second = _ChainBackend("second", error=NetworkError("第二网络错误"))
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    with pytest.raises(NetworkError, match="第二网络错误"):
        asyncio.run(chain.get_series_meta(1, "series"))


def test_chain_construction_failure_skips(monkeypatch):
    """构造失败（BackendError）记录 warning 并跳到下一个后端。"""
    from bilibili_podcast.api_backends import BackendChain

    second = _ChainBackend("second")

    async def factory(name, credential):
        if name == "first":
            raise BackendError("first 后端依赖未安装")
        return second

    from bilibili_podcast import api_backends as pkg

    monkeypatch.setattr(pkg, "_create_single_backend", factory)
    chain = BackendChain(["first", "second"])

    meta = asyncio.run(chain.get_series_meta(1, "series"))
    assert meta["name"] == "second"
    # first 构造失败未记录进已构造列表，close 只关闭 second
    asyncio.run(chain.close())
    assert second.closed


def test_chain_all_construction_fail_raises(monkeypatch):
    """所有后端构造失败：抛最后一个构造异常。"""
    from bilibili_podcast.api_backends import BackendChain

    async def factory(name, credential):
        raise BackendError(f"{name} 不可用")

    from bilibili_podcast import api_backends as pkg

    monkeypatch.setattr(pkg, "_create_single_backend", factory)
    chain = BackendChain(["first", "second"])

    with pytest.raises(BackendError, match="second 不可用"):
        asyncio.run(chain.get_user_info(1))


def test_chain_non_switchable_error_raises(monkeypatch):
    """非可切换异常（如 RuntimeError）直接上抛，不触发降级。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=RuntimeError("boom"))
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(chain.get_series_meta(1, "series"))
    assert second.meta_calls == 0


def test_chain_success_keeps_active_index(monkeypatch):
    """首个后端成功时保持 active_index=0，不重置、不重试。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first")
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    assert asyncio.run(chain.get_series_meta(1, "series"))["name"] == "first"
    assert asyncio.run(chain.get_series_meta(1, "series"))["name"] == "first"
    assert first.meta_calls == 2
    assert second.meta_calls == 0


def test_chain_empty_names_raises():
    from bilibili_podcast.api_backends import BackendChain

    with pytest.raises(ValueError, match="至少需要一个后端名称"):
        BackendChain([])


# ---------------------------------------------------------------- resolver / server 迁移


class _ResolverFakeBackend:
    """resolver 测试用假后端：固定返回并记录调用。"""

    def __init__(self):
        self.closed = False
        self.calls = []

    async def get_user_info(self, uid):
        self.calls.append(("user_info", uid))
        return {"name": "UP1", "face": "face-url", "sign": "签名"}

    async def get_user_videos(self, uid, pn, ps):
        self.calls.append(("user_videos", uid, pn, ps))
        return [{"bvid": "BV1", "title": "T1", "pubdate": 111, "duration": 65}]

    async def get_series_meta(self, sid, series_type):
        self.calls.append(("series_meta", sid, series_type))
        return {"name": "合集名", "face": "cover", "sign": "描述", "author": "UP2", "uid": 99}

    async def get_series_videos(self, sid, series_type, pn, ps):
        self.calls.append(("series_videos", sid, series_type, pn, ps))
        return [{"bvid": "BV2", "title": "T2", "pubdate": 222, "duration": 100}]

    async def get_video_owner(self, bvid):
        self.calls.append(("video_owner", bvid))
        return 123

    async def close(self):
        self.closed = True


def test_resolver_space_mapping():
    """space 解析：user_info + user_videos 字段映射（created=pubdate, length=duration）。"""
    from bilibili_podcast.web import resolver

    backend = _ResolverFakeBackend()
    result = asyncio.run(resolver.resolve_url("https://space.bilibili.com/123456", backend))

    assert result["author"] == "UP1"
    assert result["title"] == "UP1"
    assert result["cover_art"] == "face-url"
    assert result["description"] == "签名"
    assert result["source"] == {"type": "space", "uid": 123456, "space_url": "https://space.bilibili.com/123456", "sid": None}
    assert result["videos"] == [{"bvid": "BV1", "title": "T1", "created": 111, "length": 65}]
    assert backend.closed is False  # 传入后端由调用方负责生命周期


def test_resolver_season_mapping():
    """season 解析：统一 meta 格式映射到旧输出字段（title/author/cover_art/description/uid）。"""
    from bilibili_podcast.web import resolver

    backend = _ResolverFakeBackend()
    result = asyncio.run(resolver.resolve_url("https://www.bilibili.com/bangumi/play/ss123", backend))

    assert result["title"] == "合集名"
    assert result["author"] == "UP2"
    assert result["cover_art"] == "cover"
    assert result["description"] == "描述"
    assert result["source"]["uid"] == 99
    assert result["source"]["type"] == "season"
    assert result["source"]["sid"] == 123
    assert result["videos"] == [{"bvid": "BV2", "title": "T2", "created": 222, "length": 100}]
    assert ("series_videos", 123, "season", 1, 10) in backend.calls


def test_resolver_series_mapping():
    """series 解析：同样的字段映射，source.type 为 series。"""
    from bilibili_podcast.web import resolver

    backend = _ResolverFakeBackend()
    result = asyncio.run(resolver.resolve_url("https://www.bilibili.com/series/123", backend))

    assert result["author"] == "UP2"
    assert result["source"]["type"] == "series"
    assert ("series_meta", 123, "series") in backend.calls


def test_resolver_video_mapping():
    """视频解析：get_video_owner 拿到 mid 后转 space 解析。"""
    from bilibili_podcast.web import resolver

    backend = _ResolverFakeBackend()
    result = asyncio.run(resolver.resolve_url("https://www.bilibili.com/video/BV1xx", backend))

    assert ("video_owner", "BV1xx") in backend.calls
    assert result["author"] == "UP1"
    assert result["source"]["uid"] == 123


def test_resolver_video_no_owner_returns_error():
    """get_video_owner 返回 None 时按现有错误语义返回 error dict。"""
    from bilibili_podcast.web import resolver

    backend = _ResolverFakeBackend()

    async def no_owner(bvid):
        return None

    backend.get_video_owner = no_owner
    result = asyncio.run(resolver.resolve_url("https://www.bilibili.com/video/BV1xx", backend))
    assert "无法从视频 BV1xx 提取 UP 主信息" in result["error"]


def test_resolver_default_backend_created_and_closed(monkeypatch):
    """backend 为 None 时内部创建默认 native 后端并在结束 finally close。"""
    import importlib

    mod = importlib.import_module("bilibili_podcast.web.resolver")
    backend = _ResolverFakeBackend()

    async def fake_create(spec, credential):
        assert spec == "native"
        return backend

    monkeypatch.setattr(mod, "create_backend", fake_create)
    result = asyncio.run(mod.resolve_url("https://space.bilibili.com/123456"))
    assert result["author"] == "UP1"
    assert backend.closed is True


def test_server_fetch_up_face_url(monkeypatch):
    """server._fetch_up_face_url 走 api_backends 并 finally close。"""
    from bilibili_podcast.web import server

    class _Fake:
        def __init__(self):
            self.closed = False

        async def get_user_info(self, uid):
            return {"name": "x", "face": "FACE-URL", "sign": ""}

        async def close(self):
            self.closed = True

    fake = _Fake()

    async def fake_create(spec, credential):
        assert spec == "native"
        return fake

    from bilibili_podcast import api_backends as pkg

    monkeypatch.setattr(pkg, "create_backend", fake_create)
    assert asyncio.run(server._fetch_up_face_url(123)) == "FACE-URL"
    assert fake.closed is True
    assert asyncio.run(server._fetch_up_face_url(0)) is None


# ---------------------------------------------------------------- 限流识别


def test_rate_limit_error_detected():
    assert is_bilibili_rate_limited(RateLimitError())
    assert is_bilibili_rate_limited(RateLimitError("自定义消息"))
    assert not is_bilibili_rate_limited(RuntimeError("boom"))
    assert issubclass(RateLimitError, BackendError)
    assert issubclass(UnsupportedError, BackendError)


# ---------------------------------------------------------------- SeriesConfig


def test_series_config_api_backend_default():
    config = _config()
    assert config.api_backend == "native"


def test_series_config_from_yaml_valid_api_backend(tmp_path: Path):
    path = tmp_path / "yutto-series.yaml"
    path.write_text(
        """
series: yutto-series
title: Yutto Series
author: Demo Author
api_backend: yutto
source:
  uid: 123456
""",
        encoding="utf-8",
    )
    config = SeriesConfig.from_yaml(path)
    assert config.api_backend == "yutto"


def test_series_config_from_yaml_invalid_api_backend(tmp_path: Path):
    path = tmp_path / "bad-backend.yaml"
    path.write_text(
        """
series: bad-backend
title: Bad
author: Demo Author
api_backend: nope
source:
  uid: 123456
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="api_backend"):
        SeriesConfig.from_yaml(path)


def test_backend_names_constant():
    assert BACKEND_NAMES == ("bilibili-api", "bilix", "yutto", "native")


def test_native_season_duration_ms_to_seconds():
    """pgc 剧集接口的 duration 为毫秒，统一格式必须转换为秒。"""
    from bilibili_podcast.api_backends.native import _episode_from_season_item

    item = {"bvid": "BV1", "title": "T", "duration": 1203160, "cover": "c", "pub_time": 1675566000}
    ep = _episode_from_season_item(item)
    assert ep["duration"] == 1203  # 毫秒 → 秒（整除）
    assert ep["pubdate"] == 1675566000


def test_legacy_season_duration_ms_to_seconds_and_series_unchanged():
    """legacy 剧集 duration 毫秒→秒；合集（series）duration 保持秒原值。"""
    from bilibili_podcast.api_backends.legacy import _episode_from_archives_item

    season_item = {"bvid": "BV1", "title": "S", "duration": 1203160, "pic": "p", "pubdate": 1}
    assert _episode_from_archives_item(season_item, season=True)["duration"] == 1203
    series_item = {"bvid": "BV2", "title": "U", "duration": 1203, "pic": "p", "pubdate": 2}
    assert _episode_from_archives_item(series_item, season=False)["duration"] == 1203
    assert _episode_from_archives_item(series_item)["duration"] == 1203  # 默认非 season


class _RetryFakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _RetrySession:
    def __init__(self, failures_before_success=0, fail_status=None, always_fail=False):
        self.calls = 0
        self.failures_before_success = failures_before_success
        self.fail_status = fail_status
        self.always_fail = always_fail

    async def get(self, *args, **kwargs):
        self.calls += 1
        if self.always_fail:
            raise TimeoutError("network down")
        if self.calls <= self.failures_before_success:
            if self.fail_status:
                return _RetryFakeResp(status=self.fail_status)
            raise ConnectionError("connection reset")
        return _RetryFakeResp(payload={"code": 0, "data": {}})


def _retry_backend(session):
    from bilibili_podcast.api_backends.native import NativeBackend

    backend = NativeBackend.__new__(NativeBackend)
    backend._session = session
    backend._ready = True
    backend._wbi_keys = ("k", "s")
    return backend


def test_native_network_error_retries_then_succeeds(monkeypatch):
    from bilibili_podcast.api_backends import native as m

    async def _no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    session = _RetrySession(failures_before_success=1)
    backend = _retry_backend(session)
    result = asyncio.run(backend._request("/x/t", {}))
    assert result == {"code": 0, "data": {}}
    assert session.calls == 2  # 1 次失败 + 1 次成功


def test_native_5xx_retries_then_succeeds(monkeypatch):
    from bilibili_podcast.api_backends import native as m

    async def _no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    session = _RetrySession(failures_before_success=1, fail_status=502)
    backend = _retry_backend(session)
    result = asyncio.run(backend._request("/x/t", {}))
    assert result == {"code": 0, "data": {}}
    assert session.calls == 2


def test_native_persistent_network_error_raises_after_retries(monkeypatch):
    from bilibili_podcast.api_backends.base import NetworkError
    from bilibili_podcast.api_backends import native as m

    async def _no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    session = _RetrySession(always_fail=True)
    backend = _retry_backend(session)
    with pytest.raises(NetworkError):
        asyncio.run(backend._request("/x/t", {}))
    assert session.calls == 3  # 初始 + 2 次退避重试


def test_native_rate_limit_403_not_retried(monkeypatch):
    from bilibili_podcast.api_backends.base import RateLimitError
    from bilibili_podcast.api_backends import native as m

    async def _no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    session = _RetrySession(failures_before_success=5, fail_status=403)
    backend = _retry_backend(session)
    with pytest.raises(RateLimitError):
        asyncio.run(backend._request("/x/t", {}))
    assert session.calls == 1  # 风控不重试


@pytest.mark.parametrize(
    "fetcher_json",
    [
        None,  # 响应为空
        {"data": {}},  # meta 缺失
        {"data": {"meta": {}}},  # mid 缺失
    ],
)
def test_yutto_get_series_mid_raises_backend_error_on_bad_payload(monkeypatch, fetcher_json):
    """yutto 系列元数据异常结构必须转 BackendError（禁 assert，-O 下仍生效）。"""
    from bilibili_podcast.api_backends.base import BackendError

    _install_fake_yutto(monkeypatch, fetcher_json=fetcher_json)
    backend = _yutto_backend()
    with pytest.raises(BackendError, match="yutto 获取系列元数据"):
        asyncio.run(backend.get_series_meta(9, "series"))


def test_bilix_series_missing_mid_degrades_gracefully():
    """bilix 系列元数据缺少 mid 时降级为 bvid 条目，禁止裸 KeyError 穿透。"""
    client = _FakeClient(series_payload={"code": 0, "data": {"meta": {}}})
    api = types.SimpleNamespace(get_list_info=AsyncMock(return_value=("List", "UP", ["BV1", "BV2"])))
    backend = _bilix_backend(client, api)
    eps = asyncio.run(backend.get_series_videos(9, "series", pn=1, ps=10))
    assert [ep["bvid"] for ep in eps] == ["BV1", "BV2"]  # 降级为 bvid 占位
    assert eps[0]["title"] == "BV1"


def test_chain_non_switchable_error_advances_index(monkeypatch):
    """非可切换异常上抛后 active_index 前进：下次调用不再命中问题后端。"""
    from bilibili_podcast.api_backends import BackendChain

    first = _ChainBackend("first", error=RuntimeError("boom"))
    second = _ChainBackend("second")
    _install_chain_factory(monkeypatch, [first, second])
    chain = BackendChain(["first", "second"])

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(chain.get_series_meta(1, "series"))
    # 异常后索引已前进到第二个后端：再次调用直接从 second 开始
    meta = asyncio.run(chain.get_series_meta(1, "series"))
    assert meta["name"] == "second"
    assert first.meta_calls == 1  # 只有第一次命中 first


class _MixedFailSession:
    """按脚本序列失败：先网络异常 ×n，再 5xx ×n，最后成功。"""

    def __init__(self, net_failures=2, server_failures=2):
        self.calls = 0
        self.net_failures = net_failures
        self.server_failures = server_failures

    async def get(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.net_failures:
            raise ConnectionError("network down")
        if self.calls <= self.net_failures + self.server_failures:
            return _RetryFakeResp(status=502)
        return _RetryFakeResp(payload={"code": 0, "data": {}})


def test_native_mixed_retry_counters_are_independent(monkeypatch):
    """网络重试与 5xx 重试独立计数：混合失败不超过各自上限。"""
    from bilibili_podcast.api_backends import native as m

    async def _no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    session = _MixedFailSession(net_failures=2, server_failures=2)
    backend = _retry_backend(session)
    result = asyncio.run(backend._request("/x/t", {}))
    assert result == {"code": 0, "data": {}}
    # 网络 2 次失败 + 5xx 2 次 + 1 次成功 = 5 次调用（网络阶段以首个 5xx 响应为成功）
    assert session.calls == 5


def test_native_wbi_key_expiry_refetches(monkeypatch):
    """WBI key 缓存过期后自动重新获取（nav 请求）。"""
    import time

    from bilibili_podcast.api_backends import native as m

    class _NavSession:
        def __init__(self):
            self.calls = 0

        async def get(self, url, **kwargs):
            self.calls += 1
            if "nav" in url:
                return _RetryFakeResp(payload={
                    "code": 0,
                    "data": {"wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
                    }},
                })
            return _RetryFakeResp(payload={"code": 0, "data": {}})

    session = _NavSession()
    backend = _retry_backend(session)
    # 预置过期缓存（7 小时前）
    backend._wbi_keys = ("oldimg", "oldsub")
    backend._wbi_keys_fetched_at = time.monotonic() - 7 * 3600
    result = asyncio.run(backend._request("/x/t", {}, wbi=True))
    assert result == {"code": 0, "data": {}}
    assert backend._wbi_keys == ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")  # 已刷新
    assert session.calls >= 2  # nav + 目标请求
