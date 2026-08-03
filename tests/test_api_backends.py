"""B 站 API 多后端抽象层测试（全部 mock，不发起真实网络请求）。"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bilibili_podcast.api_backends import (
    BACKEND_NAMES,
    BackendError,
    RateLimitError,
    UnsupportedError,
    create_backend,
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


@pytest.mark.parametrize("name,module", [("bilix", "bilix"), ("yutto", "yutto"), ("bilibili-api", "bilibili_api")])
def test_create_backend_missing_dependency(name, module, monkeypatch):
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(BackendError):
        asyncio.run(create_backend(name, None))


def test_create_backend_legacy_works(monkeypatch):
    """legacy 后端在依赖可用时正常构造（monkeypatch 假 bilibili_api 顶层模块）。"""
    import bilibili_api

    monkeypatch.setattr(bilibili_api, "request_settings", types.SimpleNamespace(set=lambda *a, **k: None))
    backend = asyncio.run(create_backend("bilibili-api", None))
    try:
        assert type(backend).__name__ == "LegacyBackend"
    finally:
        asyncio.run(backend.close())


# ---------------------------------------------------------------- legacy 字段映射


def test_legacy_backend_field_mapping(monkeypatch):
    """legacy 后端：替换 bilibili_api 底层对象，验证统一格式字段映射。"""
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
            return {"upper": {"name": "UP"}, "cover": "c", "intro": "i"}

        async def get_videos(self, pn, ps, sort):
            return {"archives": [{"bvid": "BV9", "title": "S", "duration": 5, "pic": "p", "pubdate": 1}]}

    monkeypatch.setattr(bilibili_api, "request_settings", types.SimpleNamespace(set=lambda *a, **k: None))
    monkeypatch.setattr(bilibili_api, "Credential", lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(bilibili_api, "user", types.SimpleNamespace(User=FakeUser, VideoOrder=types.SimpleNamespace(PUBDATE="pubdate")))
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

    assert asyncio.run(backend.get_series_meta(9, "season")) == {"name": "UP", "face": "c", "sign": "i"}
    assert asyncio.run(backend.get_series_meta(9, "series")) == {"name": "", "face": "", "sign": ""}
    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_meta(9, "weird"))

    s_eps = asyncio.run(backend.get_series_videos(9, "season", 1, 100))
    assert s_eps[0]["bvid"] == "BV9"
    assert s_eps[0]["duration"] == 5
    assert s_eps[0]["pubdate"] == 1


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

    eps = asyncio.run(backend.get_series_videos(9, "series", pn=2, ps=1))
    assert [ep["bvid"] for ep in eps] == ["BV2"]

    meta = asyncio.run(backend.get_series_meta(9, "season"))
    assert meta["name"] == "Anime"
    assert meta["face"] == "t"

    eps = asyncio.run(backend.get_series_videos(9, "season", pn=1, ps=100))
    assert eps[0]["bvid"] == "BV9"
    assert eps[0]["pubdate"] == 5

    with pytest.raises(UnsupportedError):
        asyncio.run(backend.get_series_videos(9, "weird", 1, 100))


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
    assert config.api_backend == "bilibili-api"


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
    assert BACKEND_NAMES == ("bilibili-api", "bilix", "yutto")
