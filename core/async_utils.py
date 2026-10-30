"""并发与资源控制 —— 2核2G 的"刹车系统"。

包含：
    * Debouncer        全局防抖（同一用户 N 毫秒内重复触发直接丢弃）
    * KeyedLock        按 uid / 群号串行化，避免同一份数据被并发改写
    * LimitedRunner    全局并发闸门（DB 写、Pillow 渲染、LLM 调用）
    * to_thread        Pillow 等阻塞操作一律丢线程池，不堵事件循环
    * collect_now      有节流的 gc.collect()，配合渲染后立即释放大图
"""

from __future__ import annotations

import asyncio
import gc
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

T = TypeVar("T")

_LAST_GC = 0.0


class Debouncer:
    """极轻量防抖：只存 {key: 上次通过时间}，定期惰性清理，不占内存。"""

    __slots__ = ("_interval", "_last", "_hits")

    def __init__(self, interval_ms: int = 1500) -> None:
        self._interval = max(0, interval_ms) / 1000.0
        self._last: dict[str, float] = {}
        self._hits = 0

    def allow(self, key: str) -> bool:
        """返回 True 表示放行；False 表示触发太快，应静默丢弃。"""
        if self._interval <= 0:
            return True
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last < self._interval:
            return False
        self._last[key] = now
        self._hits += 1
        if self._hits % 512 == 0 and len(self._last) > 512:
            self._prune(now)
        return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._interval * 60
        for k in [k for k, v in self._last.items() if v < cutoff]:
            self._last.pop(k, None)

    def remaining(self, key: str) -> float:
        if self._interval <= 0:
            return 0.0
        return max(0.0, self._interval - (time.monotonic() - self._last.get(key, 0.0)))

    def clear(self) -> None:
        self._last.clear()


class KeyedLock:
    """按 key 的串行锁；引用计数归零即删，不会无限增长（防内存泄漏）。

    ⚠️ 不能用 "release 后 if not lock.locked(): pop" 的写法：那会在有等待者时把锁删掉，
    新来的协程会拿到一把新锁，于是同一个 uid 出现两把锁并发执行（典型的 ABA 竞态）。
    这里用 [lock, refcount]：持有/等待期间 refcount>0，减到 0 才 pop，且判断的是同一个对象。
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[str, list] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = [asyncio.Lock(), 0]
            self._locks[key] = entry
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            # 单线程事件循环：这行到 pop 之间没有 await，不存在竞态
            if entry[1] <= 0 and self._locks.get(key) is entry:
                self._locks.pop(key, None)

    def __len__(self) -> int:
        return len(self._locks)


class LimitedRunner:
    """全局并发闸门。超出上限的请求排队，而不是继续吃内存。"""

    __slots__ = ("_sem", "_name", "_timeout")

    def __init__(self, limit: int, name: str = "runner", timeout: float = 20.0) -> None:
        self._sem = asyncio.Semaphore(max(1, limit))
        self._name = name
        self._timeout = timeout

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
        try:
            yield
        finally:
            self._sem.release()

    async def run(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        async with self.slot():
            return await fn(*args, **kwargs)


async def to_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """把阻塞调用（Pillow、文件 IO）丢进线程池。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


def collect_now(min_interval: float = 2.0, force: bool = False) -> bool:
    """有节流的 gc.collect()。

    2 核机器上频繁全量 GC 会明显抢 CPU，所以默认 2 秒内最多一次；
    渲染完大图后可以 force=True 立即回收。
    """
    global _LAST_GC
    now = time.monotonic()
    if not force and now - _LAST_GC < min_interval:
        return False
    gc.collect()
    _LAST_GC = now
    return True


async def bounded_gather(
    tasks: list[Awaitable[T]], limit: int = 8
) -> list[T | None]:
    """带并发上限的 gather，任何单个异常都不影响其它任务。"""
    sem = asyncio.Semaphore(max(1, limit))

    async def _wrap(coro: Awaitable[T]) -> T | None:
        async with sem:
            try:
                return await coro
            except Exception:  # noqa: BLE001 - 单点失败不应拖垮整批
                return None

    return await asyncio.gather(*(_wrap(t) for t in tasks))


def fire_and_forget(coro: Awaitable[Any], *, name: str = "whale-task") -> asyncio.Task | None:
    """安全的后台任务：保留强引用，异常只记日志，不做轮询。"""
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError:
        return None
    task.add_done_callback(_consume)
    return task


def _consume(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        return
    if exc is not None:
        try:
            from .log import log

            log.warning(f"后台任务 {task.get_name()} 异常: {exc!r}")
        except Exception:  # noqa: BLE001
            pass