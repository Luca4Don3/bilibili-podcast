"""B 站 API 多后端抽象层 —— 基础类型与协议。

定义统一的后端协议（BilibiliApiBackend）与异常类型，供 legacy（bilibili-api）/
bilix / yutto 三个后端实现并在 sync 层无缝切换。

统一 episode 条目格式（get_user_videos / get_series_videos 返回列表中的元素）：
{
    "bvid": str,        # 视频 BV 号
    "title": str,       # 视频标题
    "description": str, # 视频简介（后端无法提供时可能为空）
    "duration": int,    # 时长（秒，后端无法提供时可能为 0）
    "image": str,       # 封面 URL（可能为空）
    "pubdate": int,     # 发布时间（Unix 时间戳，缺失时可能为 0）
    "link": str,        # https://www.bilibili.com/video/{bvid}
    "raw": dict,        # 后端原始响应条目（用于调试与兜底）
}
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class BackendError(Exception):
    """后端调用失败时抛出的基础异常。"""


class RateLimitError(BackendError):
    """请求被 B 站限流时抛出。

    str() 固定包含 "-799" 与 "请求过于频繁"，以便 sync 层的
    is_bilibili_rate_limited 检测限流并触发退避。
    """

    def __init__(self, message: str = "请求过于频繁（B 站错误码 -799），已触发速率限制"):
        super().__init__(message)

    def __str__(self) -> str:
        # 无论消息内容如何，固定附加识别关键词，确保 sync 层的限流检测生效
        return f"{super().__str__()}（请求过于频繁，B 站错误码 -799，rate limit）"


class UnsupportedError(BackendError):
    """当前后端不支持该操作时抛出（例如 bilix 后端不支持剧集 season 类型）。"""


class NetworkError(BackendError):
    """网络/超时类错误统一包装（连接失败、超时、HTTP 状态错误），供降级链识别切换。"""


BackendCredential = dict[str, str]
"""后端凭证：键为 sessdata / bili_jct / dedeuserid / buvid3（buvid3 可能缺失）。"""


def parse_backend_spec(spec: str) -> list[str]:
    """把逗号分隔的后端配置解析为名称列表。

    - 去空白、去空项、去重（保持首次出现顺序）；
    - 空串或解析后为空列表抛出 ValueError（中文消息）。
    """
    names: list[str] = []
    for raw in spec.split(","):
        name = raw.strip()
        if not name:
            continue
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError(
            "api_backend 配置不能为空，请至少指定一个后端名称（可选：bilibili-api、bilix、yutto、native）"
        )
    return names


@runtime_checkable
class BilibiliApiBackend(Protocol):
    """B 站 API 后端协议，所有方法均为异步。

    - 各方法对"参数不合法 / 后端不支持"抛出 UnsupportedError 或 ValueError。
    - 网络失败时抛异常，由 sync 层按既有语义降级或触发限流退避。
    """

    async def get_user_info(self, uid: int) -> dict:
        """返回用户信息 {"name", "face", "sign"}。"""
        ...

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        """返回 UP 主空间视频的统一 episode 条目列表（按发布时间倒序分页）。"""
        ...

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        """返回系列/剧集元数据 {"name", "face", "sign", "author", "uid"}。

        - series_type 取值 "series"（系列）或 "season"（剧集）；
        - author 为 UP 主昵称，后端无法提供时为空字符串；
        - uid 为 UP 主 mid，后端无法提供时为 None。
        """
        ...

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        """返回系列/剧集条目的统一 episode 列表，支持 pn/ps 分页。"""
        ...

    async def get_video_owner(self, bvid: str) -> int | None:
        """返回视频所属 UP 主的 mid；无法获取时返回 None。

        不支持该操作的后端应抛出 UnsupportedError（供降级链切换）。
        """
        ...

    async def close(self) -> None:
        """释放后端持有的资源（如 httpx 客户端）。"""
        ...
