"""降级链后端：按配置顺序尝试多个后端，单个失败自动切换到下一个。

后端是惰性构造的：首次调用某个方法时才构造当前活跃后端；构造失败
（依赖未安装等 BackendError）记录 warning 并跳到下一个名称。调用失败时，
若属于可切换的异常类型（BackendError 及其子类、httpx/网络相关异常），
同样记录 warning 并切换到下一个后端。切换是持久的：active_index 只前进
不重置，避免每次调用都从头重试已失败的后端；同一后端在同一方法内
连续失败不重复计数（每次调用只尝试一次当前后端）。

close() 关闭所有已构造的后端并清空列表。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .base import BackendCredential, BackendError, BilibiliApiBackend

LOGGER = logging.getLogger("bilibili_podcast.api_backends.chain")


def _is_switchable_error(exc: Exception) -> bool:
    """判断异常是否属于可触发降级切换的类型。"""
    if isinstance(exc, BackendError):
        return True
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True
    try:
        import httpx  # noqa: F401

        if isinstance(exc, (httpx.HTTPError, httpx.TimeoutException)):
            return True
    except ImportError:
        pass
    return False


class BackendChain:
    """多后端降级链，对外暴露与单个后端一致的异步接口。

    - 构造为纯同步操作，不触发任何网络请求；
    - 后端在首次调用方法时按需构造（惰性），构造失败自动跳过；
    - 调用失败（可切换异常）自动切换到下一个后端；全部失败抛最后一个异常。
    """

    def __init__(self, names: list[str], credential: BackendCredential | None = None):
        if not names:
            raise ValueError("降级链至少需要一个后端名称")
        self._names = list(names)
        self._credential = credential
        # 已构造的后端：index -> backend（构造失败的后端不记录）
        self._constructed: dict[int, BilibiliApiBackend] = {}
        # 会话统计：name -> {"calls": 成功调用次数, "failures": 触发切换的失败次数,
        #                    "switches": 该后端被切走的次数}；未构造的后端不记录
        self._stats: dict[str, dict[str, int]] = {}
        # 当前活跃后端的索引：成功调用后保持，失败后前进，不重置回 0
        self._active_index = 0

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        """返回各已构造后端的调用统计副本（不输出凭证等敏感信息）。"""
        return {name: dict(entry) for name, entry in self._stats.items()}

    async def _create(self, index: int) -> BilibiliApiBackend:
        """按名称构造第 index 个后端（延迟 import，规避循环依赖）。"""
        from . import _create_single_backend

        return await _create_single_backend(self._names[index], self._credential)

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """统一调用入口：尝试当前活跃后端，失败依次切换到下一个。"""
        last_error: Exception | None = None
        index = self._active_index
        while index < len(self._names):
            name = self._names[index]
            backend = self._constructed.get(index)
            if backend is None:
                try:
                    backend = await self._create(index)
                except BackendError as exc:
                    last_error = exc
                    LOGGER.warning(
                        "API 后端构造失败，跳过该后端 backend=%s error=%s",
                        self._names[index], exc,
                    )
                    index += 1
                    continue
                self._constructed[index] = backend
                # 构造成功才登记统计（未构造的后端不记录）
                self._stats.setdefault(name, {"calls": 0, "failures": 0, "switches": 0})
            try:
                result = await getattr(backend, method)(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if not _is_switchable_error(exc):
                    # 非可切换异常（如编程错误）：直接上抛，不触发降级
                    raise
                # 统计：失败与切走各 +1（仅统计已构造且被调用的后端）
                entry = self._stats.setdefault(name, {"calls": 0, "failures": 0, "switches": 0})
                entry["failures"] += 1
                entry["switches"] += 1
                next_name = self._names[index + 1] if index + 1 < len(self._names) else "（无）"
                LOGGER.warning(
                    "API 后端调用失败，降级切换 backend=%s method=%s error=%s next=%s",
                    self._names[index], method, exc, next_name,
                )
                index += 1
                continue
            # 成功：保持当前后端（持久，不重置回 0），并累计成功调用次数
            self._active_index = index
            self._stats[name]["calls"] += 1
            return result
        if last_error is None:
            # 防御性守卫：正常流程下所有退出路径都已 raise 或 return，
            # 此分支仅在循环逻辑被未来重构破坏时兜底（禁 assert，-O 下仍生效）
            raise RuntimeError("BackendChain: 所有后端已耗尽但未记录任何错误")
        # 全部后端都失败：抛出最后一个异常（保留原始 traceback 链）
        raise last_error

    async def get_user_info(self, uid: int) -> dict:
        return await self._call("get_user_info", uid)

    async def get_user_videos(self, uid: int, pn: int, ps: int) -> list[dict]:
        return await self._call("get_user_videos", uid, pn, ps)

    async def get_series_meta(self, sid: int, series_type: str) -> dict:
        return await self._call("get_series_meta", sid, series_type)

    async def get_series_videos(self, sid: int, series_type: str, pn: int, ps: int) -> list[dict]:
        return await self._call("get_series_videos", sid, series_type, pn, ps)

    async def get_video_owner(self, bvid: str) -> int | None:
        return await self._call("get_video_owner", bvid)

    async def close(self) -> None:
        """关闭所有已构造的后端并清空列表；debug 级输出会话统计汇总（不含凭证）。"""
        for backend in self._constructed.values():
            try:
                await backend.close()
            except Exception as exc:
                LOGGER.warning("关闭 API 后端失败 error=%s", exc)
        LOGGER.debug(
            "后端链会话统计汇总（不含凭证）: %s",
            json.dumps(self._stats, ensure_ascii=False),
        )
        self._constructed.clear()
        self._active_index = 0
        self._stats.clear()
