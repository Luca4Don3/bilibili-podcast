"""native 后端：自研直连 B 站公开接口（延迟 import，无新增第三方依赖）。

不依赖 bilibili-api / bilix / yutto，直接用项目已有依赖 curl_cffi
（AsyncSession + impersonate="chrome131"）请求 B 站公开接口。接口清单
（全部 GET，统一 UA 与 Referer，cookie 由 AsyncSession 携带，WBI 签名
接口自动加签）：

- get_user_info      GET /x/space/wbi/acc/info?mid=          （wbi 签名）
- get_user_videos    GET /x/space/wbi/arc/search?mid=&pn=&ps=&order=pubdate（wbi）
- get_series_meta    series → GET /x/series/series?series_id=（无 wbi）
                    season  → GET /pgc/view/web/season?season_id=
- get_series_videos  series → GET /x/series/archives?mid=&series_id=&ps=（全量再切片）
                    season  → /pgc/view/web/season 的 result.episodes（全量再切片）
- get_video_owner    GET /x/web-interface/view?bvid= → data.owner.mid

会话指纹（防 -352 风控）采用「本地生成 + 页面补充」：构造时即本地生成合法
格式的 buvid3 / buvid4 / b_nut 指纹 cookie 注入会话（首次请求不再依赖页面
会话，风控韧性更强）；首次请求前仍访问一次 https://space.bilibili.com/ 作为
补充（更新 b_nut 等字段、下发 __at_once 等本地未覆盖的 cookie），该前置失败
仅记录 warning，不阻断后续请求。

buvid 指纹生成来源：bilibili-API-collect 文档 docs/misc/buvid3_4.md（原页面
已随仓库归档下线，采用 Wayback Machine 存档副本，见 _generate_buvid_fingerprints
注释）：
- buvid3 = 大写 UUID（去横线）+ 毫秒时间戳尾部数字 + "infoc"，社区通用格式带
  "XZ02" 前缀（真实浏览器 cookie 两种形态均存在，B 站按 UUID 段校验）；
- buvid4 = 大写 UUID（带横线）+ 毫秒尾部数字 + "-" + 9 位随机数 + "-666" +
  base64 随机串（对应 /x/frontend/finger/spi 接口 b_4 字段结构）；
- b_nut = UNIX 秒级时间戳（文档明确为秒级，与部分资料描述的毫秒不同，以文档
  为准；指纹仅需格式合法与一致）。

WBI 签名实现来源：bilibili-API-collect 文档 docs/misc/sign/wbi.md
（https://socialsisteryi.github.io/bilibili-API-collect/docs/misc/sign/wbi.html）
- img_key/sub_key 取自 /x/web-interface/nav 响应 data.wbi_img 的 img_url/sub_url
  文件名（去扩展名），nav 失败时回退到公开备用常量；
- 重排映射表 MIXIN_KEY_ENC_TAB 长 64，按表取出 img_key+sub_key 对应位置的
  字符拼接后截取前 32 位，即为 mixin_key；
- 签名：参数加 wts=当前秒级时间戳 → 按键名升序排序 → 百分号编码（字母大写、
  空格 %20、过滤 "!'()*"）→ 拼接 mixin_key → 取 md5 十六进制得 w_rid。
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import logging
import random
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING, Any

from .base import BackendCredential, BackendError, NetworkError, RateLimitError, UnsupportedError

if TYPE_CHECKING:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

LOGGER = logging.getLogger("bilibili_podcast.api_backends.native")

_BASE_URL = "https://api.bilibili.com"
# 网络类临时错误（连接失败/超时/5xx）的最大重试次数与退避间隔（秒）
_MAX_NETWORK_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# WBI key 每日轮换；缓存超过该时长自动重新获取（长驻实例防过期签名失败）
_WBI_KEY_TTL_SECONDS = 6 * 3600
# 会话前置页面：首次请求前访问一次 space 主页作为指纹补充手段（本地已注入
# buvid3/buvid4/b_nut，页面访问用于更新 b_nut、下发 __at_once 等额外 cookie），
# 避免全新会话触发 -352 风控
_WEB_URL = "https://space.bilibili.com"
_TIMEOUT_SECONDS = 15.0

# 统一请求头：UA 与 Referer 与 Web 端一致（UA 带 Edg 后缀，与 bilibili_api 相同）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    "Referer": "https://www.bilibili.com",
}

# (B 站 cookie 名, 统一凭证键名)
_COOKIE_KEYS = (
    ("SESSDATA", "sessdata"),
    ("bili_jct", "bili_jct"),
    ("DedeUserID", "dedeuserid"),
    ("buvid3", "buvid3"),
)

# WBI 重排映射表（长度 64），来源见模块文档字符串（bilibili-API-collect）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# nav 接口不可用时的公开备用 img_key/sub_key（观测到的全站统一值，会随每日
# 更替过期；优先使用进程内缓存 key，无缓存时才回退到这里，见"待线上验证"）
_FALLBACK_IMG_KEY = "7cd084941338484aae1ad9425b84077c"
_FALLBACK_SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"

# WBI 签名接口附加的鼠标风控参数用到的随机字母表（与 bilibili_api 的 _enc_dm 一致）
_DM_ALPHABET = "ABCDEFGHIJK"


def _generate_buvid_fingerprints() -> dict[str, str]:
    """本地生成合法格式的会话指纹 cookie（buvid3 / buvid4 / b_nut）。

    来源：bilibili-API-collect 文档 docs/misc/buvid3_4.md（页面已随仓库归档下线，
    采用 Wayback Machine 存档副本
    https://web.archive.org/web/20250902163850/https://socialsisteryi.github.io/bilibili-API-collect/docs/misc/buvid3_4.html
    与示例：Set-Cookie 的 buvid3=<uuid>-infoc、
    b_nut=1721975923；spi 接口 b_4=F6E0FD4B-...-E461D8D1F5AB79044-024072309-666onEZSnlHVPjoRp4kDYg==）：
    - buvid3：大写 UUID（去横线）+ 毫秒时间戳尾部数字 + "infoc"；社区通用格式
      带 "XZ02" 前缀（浏览器真实 cookie 两种形态都存在），本实现采用带前缀格式；
    - buvid4：大写 UUID（带横线）+ 毫秒尾部 5 位数字 + "-" + 9 位随机数 + "-666"
      + base64 随机串（16 字节 → 24 字符含填充，与 b_4 示例同长）；
    - b_nut：UNIX 秒级时间戳（文档明确，非毫秒；指纹一致性与格式合法即可）。

    B 站仅校验指纹的格式与一致性（页面访问补充时不会因本地指纹触发异常
    响应），不要求特定取值；返回的字典可直接合并进会话 cookie。
    """
    uuid_hex = str(uuid.uuid4()).replace("-", "").upper()
    uuid_dash = str(uuid.uuid4()).upper()
    ms_tail = str(int(time.time() * 1000))[-7:]
    rand9 = f"{random.randrange(0, 10**9):09d}"
    b64 = base64.b64encode(random.randbytes(16)).decode("ascii")
    return {
        "buvid3": f"XZ02{uuid_hex}{ms_tail}infoc",
        "buvid4": f"{uuid_dash}{ms_tail[-5:]}-{rand9}-666{b64}",
        "b_nut": str(int(time.time())),
    }


def get_mixin_key(raw_key: str) -> str:
    """按 MIXIN_KEY_ENC_TAB 打乱 raw_key 并截取前 32 位，返回 mixin_key。"""
    return "".join(raw_key[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _clean_value(value: Any) -> str:
    """过滤参数值中的 "!'()*" 字符（与公开文档 Demo 的行为一致）。"""
    return "".join(ch for ch in str(value) if ch not in "!'()*")


def sign_wbi_params(
    params: dict[str, Any],
    img_key: str,
    sub_key: str,
    wts: int | None = None,
) -> dict[str, Any]:
    """为参数 dict 原地添加 wts/w_rid 并返回（WBI 签名）。

    - wts 缺省取当前秒级时间戳，测试可显式传入固定值；
    - 参数按键名升序排序后百分号编码（字母大写、空格 %20），拼接 mixin_key
      后取 md5 十六进制即 w_rid。
    """
    mixin_key = get_mixin_key(img_key + sub_key)
    params["wts"] = int(wts if wts is not None else time.time())
    signed = {key: _clean_value(value) for key, value in sorted(params.items())}
    query = urllib.parse.urlencode(signed, quote_via=urllib.parse.quote, safe="")
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def _episode_from_vlist_item(item: dict) -> dict:
    """把 x/space/wbi/arc/search 的 vlist 条目转换为统一 episode 格式。"""
    bvid = item.get("bvid") or item.get("bv_id") or ""
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": item.get("description") or item.get("intro", ""),
        "duration": item.get("length") or item.get("duration", 0),
        "image": item.get("pic") or item.get("cover", ""),
        "pubdate": item.get("created") or item.get("pubtime") or item.get("pubdate", 0),
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


def _episode_from_season_item(item: dict) -> dict:
    """把 pgc/view/web/season 的 episodes 条目转换为统一 episode 格式。

    pgc 接口的 duration 为毫秒，统一格式约定为秒（与空间/系列接口一致），
    这里除以 1000 取整。
    """
    bvid = item.get("bvid", "")
    duration_ms = item.get("duration", 0) or 0
    return {
        "bvid": bvid,
        "title": item.get("title", ""),
        "description": "",
        "duration": max(int(duration_ms) // 1000, 0),
        "image": item.get("cover", ""),
        "pubdate": item.get("pub_time", 0),
        "link": f"https://www.bilibili.com/video/{bvid}",
        "raw": item,
    }


class NativeBackend:
    """自研直连 B 站公开接口的后端（基于 curl_cffi，延迟 import）。"""

    def __init__(self, credential: BackendCredential | None = None):
        from curl_cffi.requests import AsyncSession

        cookies: dict[str, str] = {}
        if credential:
            for cookie_name, key in _COOKIE_KEYS:
                value = credential.get(key)
                if value:
                    cookies[cookie_name] = value
        # 本地生成会话指纹并合并：用户凭证 buvid3 优先（覆盖本地生成），
        # 本地生成补齐 buvid4 / b_nut 等字段，首次请求不依赖页面会话
        for name, value in _generate_buvid_fingerprints().items():
            cookies.setdefault(name, value)
        self._session: CurlAsyncSession = AsyncSession(
            cookies=cookies or None,
            timeout=_TIMEOUT_SECONDS,
            headers=_HEADERS,
            impersonate="chrome131",
        )
        # 进程内缓存 img_key/sub_key：nav 成功一次后复用；nav 失败回退备用常量
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_keys_fetched_at: float = 0.0
        # 会话前置标志：首次请求前访问一次 space 页面建立指纹 cookie
        self._ready = False
        # 系列/剧集全量结果缓存：(type, sid) -> episode 列表，避免分页重复请求
        self._series_cache: dict[tuple[str, int], list[dict]] = {}

    async def close(self) -> None:
        try:
            await self._session.close()
        except Exception as exc:
            LOGGER.warning("关闭 native 后端会话失败 error=%s", exc)

    async def _ensure_ready(self) -> None:
        """本地指纹已注入（buvid3/buvid4/b_nut），页面访问仅作补充手段。

        构造时已本地生成合法格式的指纹 cookie 注入会话，首次请求不再依赖页面
        会话；仍访问一次 space 页面作为补充（更新 b_nut 等字段、下发 __at_once
        等本地未覆盖的 cookie），进一步降低 -352 风控概率（bilibili_api 同样
        依赖页面会话）。失败不阻断：记录 warning 后继续，接口本身失败时按既有
        异常语义处理。
        """
        if self._ready:
            return
        self._ready = True
        try:
            await self._session.get(_WEB_URL, headers=_HEADERS)
        except Exception as exc:
            LOGGER.warning("native 补充会话指纹失败（本地指纹已注入），接口可能触发风控 error=%s", exc)

    @staticmethod
    def _enc_dm(params: dict[str, Any]) -> dict[str, Any]:
        """附加鼠标移动风控参数（与 bilibili_api 的 _enc_dm 行为一致）。

        全新会话即使带 buvid 指纹，WBI 接口仍可能返回 -352；附加这些参数后
        风控放行（实测验证）。参数在签名前加入，因此参与 w_rid 计算。
        """
        params.update(
            {
                "dm_img_list": "[]",  # 鼠标/键盘操作记录
                "dm_img_str": "".join(random.sample(_DM_ALPHABET, 2)),
                "dm_cover_img_str": "".join(random.sample(_DM_ALPHABET, 2)),
                "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
            }
        )
        return params

    async def _get_wbi_keys(self) -> tuple[str, str]:
        """获取/缓存 WBI img_key/sub_key；nav 请求失败时回退到公开备用常量。

        WBI key 每日轮换：缓存超过 _WBI_KEY_TTL_SECONDS 后自动重新获取，
        避免长驻实例（如 web 内复用的后端）用过期 key 触发签名失败（-352）。
        """
        if self._wbi_keys is not None:
            if time.monotonic() - self._wbi_keys_fetched_at < _WBI_KEY_TTL_SECONDS:
                return self._wbi_keys
            LOGGER.debug("native WBI key 缓存过期，重新获取")
        payload: dict = {}
        try:
            response = await self._session.get(
                f"{_BASE_URL}/x/web-interface/nav",
                headers=_HEADERS,
            )
            payload = response.json()
        except Exception as exc:
            LOGGER.warning("native 获取 WBI key 失败，回退到公开备用 key error=%s", exc)
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}

        def key_from_url(url: str) -> str:
            # 取 URL 最后一段文件名（去扩展名），如 .../7cd....png -> 7cd...
            return url.rsplit("/", 1)[-1].split(".")[0] if url else ""

        img_key = key_from_url(wbi_img.get("img_url") or "")
        sub_key = key_from_url(wbi_img.get("sub_url") or "")
        if img_key and sub_key:
            self._wbi_keys = (img_key, sub_key)
        else:
            LOGGER.warning("native nav 未返回有效 wbi_img，使用公开备用 key")
            if self._wbi_keys is None:
                self._wbi_keys = (_FALLBACK_IMG_KEY, _FALLBACK_SUB_KEY)
        self._wbi_keys_fetched_at = time.monotonic()
        return self._wbi_keys

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        wbi: bool = False,
    ) -> dict:
        """发起 GET 请求并统一解析 B 站 JSON 响应。

        - wbi=True 时先做 WBI 签名（自动获取/缓存 img_key/sub_key）；
        - HTTP 403/412 → RateLimitError（风控），其余 HTTP 错误 → NetworkError；
        - JSON code!=0：-799/请求过于频繁 → RateLimitError；-412/412/403 →
          RateLimitError（风控）；其他 → BackendError（中文消息含 code/message）；
        - 网络/超时异常统一包装 NetworkError。
        """
        request_params = dict(params or {})
        await self._ensure_ready()
        if wbi:
            self._enc_dm(request_params)  # 鼠标风控参数（参与签名）
            request_params.setdefault("web_location", 1550101)
            img_key, sub_key = await self._get_wbi_keys()
            sign_wbi_params(request_params, img_key, sub_key)
        url = f"{_BASE_URL}{path}"
        response = None
        # 网络类（连接/超时）与 5xx 服务端错误分别独立计数重试，
        # 各自上限 _MAX_NETWORK_RETRIES，混合场景不会叠加超限。
        network_attempts = 0
        server_attempts = 0
        while True:
            try:
                response = await self._session.get(url, params=request_params, headers=_HEADERS)
            except Exception as exc:
                # 连接/超时等网络类临时错误：退避重试；限流与风控在响应层处理，不在此重试
                last_exc = exc
                if network_attempts < _MAX_NETWORK_RETRIES:
                    LOGGER.debug("native 网络请求失败，退避重试 path=%s attempt=%s error=%s", path, network_attempts + 1, exc)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS[network_attempts])
                    network_attempts += 1
                    continue
                raise NetworkError(f"native 请求 B 站接口失败（{path}）：{last_exc}") from last_exc
            status = getattr(response, "status_code", None)
            if status is not None and status >= 400:
                if status in (403, 412):
                    raise RateLimitError(f"native 接口触发风控（{path}，HTTP {status}）")
                if status >= 500 and server_attempts < _MAX_NETWORK_RETRIES:
                    # 服务端 5xx：短退避后重试
                    LOGGER.debug("native 接口 5xx，退避重试 path=%s attempt=%s", path, server_attempts + 1)
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS[server_attempts])
                    server_attempts += 1
                    continue
                raise NetworkError(f"native 请求 B 站接口返回 HTTP {status}（{path}）")
            break
        try:
            data = response.json()
        except Exception as exc:
            raise BackendError(f"native 解析 B 站接口响应失败（{path}）：{exc}") from exc
        code = data.get("code")
        if code != 0:
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = None
            message = str(data.get("message") or data.get("msg") or "未知错误")
            if code_int == -799 or "请求过于频繁" in message:
                raise RateLimitError(f"native 接口限流（{path}，错误码 -799）：{message}")
            if code_int in (-412, 412, 403) or "风控" in message:
                raise RateLimitError(f"native 接口触发风控（{path}，错误码 {code}）：{message}")
            raise BackendError(f"native 接口调用失败（{path}，错误码 {code}）：{message}")
        return data

    async def get_user_info(self, uid: int) -> dict:
        data = await self._request("/x/space/wbi/acc/info", {"mid": uid}, wbi=True)
        info = data.get("data") or {}
        return {
            "name": info.get("name", ""),
            "face": info.get("face", ""),
            "sign": info.get("sign", ""),
        }

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        data = await self._request(
            "/x/space/wbi/arc/search",
            {"mid": uid, "pn": pn, "ps": ps, "order": "pubdate"},
            wbi=True,
        )
        vlist = ((data.get("data") or {}).get("list") or {}).get("vlist") or []
        return [_episode_from_vlist_item(item) for item in vlist]

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        if series_type == "series":
            data = await self._request("/x/series/series", {"series_id": sid})
            meta = (data.get("data") or {}).get("meta") or {}
            mid = meta.get("mid")
            return {
                "name": meta.get("name", ""),
                "face": meta.get("cover", ""),
                "sign": meta.get("intro", ""),
                # x/series/series 的 meta 不含 UP 主昵称字段，author 留空
                "author": "",
                "uid": int(mid) if mid else None,
            }
        if series_type == "season":
            data = await self._request("/pgc/view/web/season", {"season_id": sid})
            result = data.get("result") or {}
            up_info = result.get("up_info") or {}
            mid = up_info.get("mid")
            return {
                "name": result.get("title", ""),
                "face": result.get("cover", ""),
                "sign": result.get("evaluate", ""),
                # pgc/view/web/season 的 up_info 字段名为 uname（bilibili-API-collect）
                "author": up_info.get("uname", ""),
                "uid": int(mid) if mid else None,
            }
        raise UnsupportedError(f"native 后端不支持的抓取类型：{series_type}")

    async def _fetch_series_all(self, sid: int) -> list[dict]:
        """series 全量：meta 拿 mid/total，再请求 archives 全量（ps 取 total）后缓存。"""
        cached = self._series_cache.get(("series", sid))
        if cached is not None:
            return cached
        meta_data = await self._request("/x/series/series", {"series_id": sid})
        meta = (meta_data.get("data") or {}).get("meta") or {}
        mid = meta.get("mid")
        if not mid:
            raise BackendError(f"native 获取系列元数据缺少 mid（sid={sid}）")
        try:
            total = int(meta.get("total") or 0)
        except (TypeError, ValueError):
            # total 非数字（接口异常格式）时回退默认页大小，避免崩溃
            total = 200
        if total <= 0:
            total = 200
        data = await self._request(
            "/x/series/archives",
            {"mid": mid, "series_id": sid, "ps": max(total, 1)},
        )
        archives = (data.get("data") or {}).get("archives") or []
        episodes = [_episode_from_archives_item(item) for item in archives]
        self._series_cache[("series", sid)] = episodes
        return episodes

    async def _fetch_season_all(self, sid: int) -> list[dict]:
        """season 全量：取 pgc/view/web/season 的 result.episodes 后缓存。"""
        cached = self._series_cache.get(("season", sid))
        if cached is not None:
            return cached
        data = await self._request("/pgc/view/web/season", {"season_id": sid})
        result = data.get("result") or {}
        episodes = [_episode_from_season_item(item) for item in (result.get("episodes") or [])]
        self._series_cache[("season", sid)] = episodes
        return episodes

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        if series_type == "series":
            all_episodes = await self._fetch_series_all(sid)
        elif series_type == "season":
            all_episodes = await self._fetch_season_all(sid)
        else:
            raise UnsupportedError(f"native 后端不支持的抓取类型：{series_type}")
        start = (pn - 1) * ps
        return all_episodes[start : start + ps]

    async def get_video_owner(self, bvid: str) -> int | None:
        data = await self._request("/x/web-interface/view", {"bvid": bvid})
        owner = (data.get("data") or {}).get("owner") or {}
        mid = owner.get("mid")
        return int(mid) if mid else None
