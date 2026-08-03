"""B 站 API 多后端抽象层：legacy / bilix / yutto / native 四个可切换后端。

create_backend 按名称（可逗号分隔组成降级链）构造后端；返回 BackendChain，
对外暴露与单个后端一致的接口，单个后端失败自动切换到下一个。bilix / yutto /
bilibili-api 均为延迟 import（在各自后端模块内部），未安装时抛出带中文提示的
BackendError（由 BackendChain 惰性构造并跳过）。native 为自研直连后端
（curl_cffi，无额外依赖），同样延迟 import。
"""

from __future__ import annotations

from .base import (
    BackendCredential,
    BackendError,
    BilibiliApiBackend,
    NetworkError,
    RateLimitError,
    UnsupportedError,
    parse_backend_spec,
)
from .bilix import BilixBackend
from .chain import BackendChain
from .legacy import LegacyBackend
from .native import NativeBackend
from .yutto import YuttoBackend

BACKEND_NAMES = ("bilibili-api", "bilix", "yutto", "native")

_BACKEND_INSTALL_HINT = '请执行 pip install "bilibili-podcast[api-backends]" 安装可选后端'


async def _create_single_backend(name: str, credential: BackendCredential | None) -> BilibiliApiBackend:
    """按名称构造单个后端实例（BackendChain 内部调用，不校验名称）。

    - legacy 缺依赖（bilibili_api import 失败）抛出 BackendError；
    - bilix / yutto 未安装抛出 BackendError（含安装提示）；
    - 未知名称抛出 ValueError（列出可用名称）。
    """
    if name == "bilibili-api":
        try:
            return LegacyBackend(credential)
        except ImportError as exc:
            raise BackendError(
                "legacy 后端依赖（bilibili-api）未安装；该库已停止维护，"
                "如需使用请手动安装（不推荐），或改用 native/yutto 后端"
            ) from exc
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
    if name == "native":
        # native 为自研直连后端，无额外第三方依赖（复用主依赖 curl_cffi）
        try:
            import curl_cffi  # noqa: F401  提前验证依赖是否可用
        except ImportError as exc:
            raise BackendError(
                "native 后端依赖（curl_cffi）未安装，请执行 pip install curl_cffi"
            ) from exc
        return NativeBackend(credential)
    raise ValueError(f"未知的 API 后端名称：{name}（可选：{'、'.join(BACKEND_NAMES)}）")


async def create_backend(spec: str | list[str], credential: BackendCredential | None) -> BackendChain:
    """按名称（可逗号分隔的降级链）构造后端链实例。

    - spec 为字符串时支持逗号分隔多个后端名称，按顺序组成降级链（去空白、
      去重）；也接受名称列表；
    - 未知名称抛出 ValueError（消息列出可用名称）；
    - 名称合法但依赖未安装时不会立即失败——BackendChain 惰性构造，首次调用
      某方法时才构造，失败自动跳到下一个可用后端。
    """
    if isinstance(spec, str):
        names = parse_backend_spec(spec)
    else:
        names = list(spec)
        if not names:
            raise ValueError("api_backend 配置不能为空，请至少指定一个后端名称（可选：bilibili-api、bilix、yutto、native）")
    for name in names:
        if name not in BACKEND_NAMES:
            raise ValueError(f"未知的 API 后端名称：{name}（可选：{'、'.join(BACKEND_NAMES)}）")
    return BackendChain(names, credential)


__all__ = [
    "BACKEND_NAMES",
    "BackendCredential",
    "BackendError",
    "BilibiliApiBackend",
    "NetworkError",
    "RateLimitError",
    "UnsupportedError",
    "BackendChain",
    "BilixBackend",
    "LegacyBackend",
    "NativeBackend",
    "YuttoBackend",
    "create_backend",
    "parse_backend_spec",
]
