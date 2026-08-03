"""B 站 API 多后端抽象层：legacy / bilix / yutto 三个可切换后端。

create_backend 按名称构造后端；bilix / yutto / bilibili-api 均为延迟
import（在各自后端模块内部），未安装时抛出带中文提示的 BackendError。
"""

from __future__ import annotations

from .base import (
    BackendCredential,
    BackendError,
    BilibiliApiBackend,
    RateLimitError,
    UnsupportedError,
)
from .bilix import BilixBackend
from .legacy import LegacyBackend
from .yutto import YuttoBackend

BACKEND_NAMES = ("bilibili-api", "bilix", "yutto")

_BACKEND_INSTALL_HINT = '请执行 pip install "bilibili-podcast[api-backends]" 安装可选后端'


async def create_backend(name: str, credential: BackendCredential | None) -> BilibiliApiBackend:
    """按名称构造后端实例。

    - 未知名称抛出 ValueError（消息列出可用名称）；
    - legacy 缺依赖（bilibili_api import 失败）抛出 BackendError；
    - bilix / yutto 未安装抛出 BackendError（含安装提示）。
    """
    if name == "bilibili-api":
        try:
            return LegacyBackend(credential)
        except ImportError as exc:
            raise BackendError("legacy 后端（bilibili-api 依赖）未安装或已损坏") from exc
    if name == "bilix":
        try:
            import bilix  # noqa: F401  提前验证依赖是否可用
        except ImportError as exc:
            raise BackendError(f"bilix 后端未安装，{_BACKEND_INSTALL_HINT}") from exc
        return BilixBackend(credential)
    if name == "yutto":
        try:
            import yutto  # noqa: F401  提前验证依赖是否可用
        except ImportError as exc:
            raise BackendError(f"yutto 后端未安装，{_BACKEND_INSTALL_HINT}") from exc
        return YuttoBackend(credential)
    raise ValueError(f"未知的 API 后端名称：{name}（可选：{'、'.join(BACKEND_NAMES)}）")


__all__ = [
    "BACKEND_NAMES",
    "BackendCredential",
    "BackendError",
    "BilibiliApiBackend",
    "RateLimitError",
    "UnsupportedError",
    "BilixBackend",
    "LegacyBackend",
    "YuttoBackend",
    "create_backend",
]
