"""出图缓存：条目数 + 总字节 + TTL 三重上限，淘汰时同步删文件。

为什么必须三重限制（2 核 2G）：
    * 只限条数：PNG 体积不可控（一张复杂卡片可能 300KB），24 张也能吃掉 7MB；
    * 只限字节：几十张 1KB 的小图不占内存但占 inode，读起来反而慢；
    * 只限 TTL：昨天的签到图今天基本不会命中，留着只是白占。
三重一起卡死，缓存总量恒定在 24MB 以内，磁盘也不会被慢慢吃满（40G 也经不起只增不减）。

线程安全：渲染在 to_thread 里跑，get/put 都可能在别的线程调用，所以全部加锁。
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from ..core.log import log

DEFAULT_MAX_BYTES = 24 * 1024 * 1024  # 24MB


def safe_unlink(path: str | Path) -> bool:
    """删文件，失败不抛（缓存文件删不掉不该影响出图）。"""
    try:
        os.unlink(str(path))
        return True
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return False


@dataclass(slots=True)
class _Entry:
    path: Path
    size: int
    created: float
    used: float


class ImageCache:
    __slots__ = (
        "_max_entries",
        "_ttl",
        "_dir",
        "_max_bytes",
        "_lock",
        "_entries",
        "_bytes",
        "_hits",
        "_misses",
        "_evictions",
    )

    def __init__(
        self,
        max_entries: int = 24,
        ttl_sec: int = 300,
        cache_dir: str | Path = ".",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._max_entries = max(0, int(max_entries))
        self._ttl = max(0, int(ttl_sec))
        self._dir = Path(cache_dir)
        self._max_bytes = max(1024 * 1024, int(max_bytes))
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning(f"图片缓存目录创建失败: {exc!r}")

    # ------------------------------------------------------------ 读写
    @property
    def enabled(self) -> bool:
        return self._max_entries > 0 and self._ttl > 0

    def get(self, key: str) -> Path | None:
        """命中返回文件路径（并刷新 LRU），未命中/过期/文件丢失返回 None。"""
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if now - entry.created > self._ttl:
                self._drop_locked(key, entry)
                self._misses += 1
                return None
            if not entry.path.is_file():
                self._drop_locked(key, entry)
                self._misses += 1
                return None
            entry.used = now
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.path

    def put(self, key: str, path: str | Path) -> None:
        """登记一张新图；超限立即淘汰（并删文件）。"""
        if not self.enabled:
            return
        target = Path(path)
        try:
            size = target.stat().st_size
        except OSError:
            return
        now = time.time()
        with self._lock:
            old = self._entries.get(key)
            if old is not None:
                if old.path == target:
                    # 同 key 的路径恒为 {cache_key}.png：绝不 unlink，否则会删掉刚写好的图
                    self._entries[key] = _Entry(path=target, size=size, created=now, used=now)
                    self._entries.move_to_end(key)
                    self._bytes = max(0, self._bytes - old.size + size)
                    self._enforce_locked(now)
                    return
                self._drop_locked(key, old)
            self._entries[key] = _Entry(path=target, size=size, created=now, used=now)
            self._entries.move_to_end(key)
            self._bytes += size
            self._enforce_locked(now)

    # ------------------------------------------------------------ 淘汰
    def _drop_locked(self, key: str, entry: _Entry) -> None:
        self._entries.pop(key, None)
        self._bytes = max(0, self._bytes - entry.size)
        safe_unlink(entry.path)

    def _enforce_locked(self, now: float) -> None:
        # 1) TTL
        for key, entry in list(self._entries.items()):
            if now - entry.created > self._ttl:
                self._evictions += 1
                self._drop_locked(key, entry)
        # 2) 条目数
        while len(self._entries) > self._max_entries:
            key, entry = next(iter(self._entries.items()))
            self._evictions += 1
            self._drop_locked(key, entry)
        # 3) 总字节（继续按 LRU 淘汰，至少保留 1 张）
        while self._bytes > self._max_bytes and len(self._entries) > 1:
            key, entry = next(iter(self._entries.items()))
            self._evictions += 1
            self._drop_locked(key, entry)

    # ------------------------------------------------------------ 维护
    def prune(self) -> int:
        """清理过期项与目录里的孤儿文件（只在启动或缓存压力大时调用）。"""
        removed = 0
        now = time.time()
        with self._lock:
            for key, entry in list(self._entries.items()):
                if now - entry.created > self._ttl or not entry.path.is_file():
                    self._drop_locked(key, entry)
                    removed += 1
            known = {str(entry.path) for entry in self._entries.values()}
        # 清理 cache 目录里不属于当前索引的旧文件（进程重启后索引会丢）
        try:
            for path in self._dir.glob("*.png"):
                if str(path) not in known:
                    safe_unlink(path)
                    removed += 1
        except OSError:
            pass
        return removed

    def clear(self) -> None:
        with self._lock:
            for entry in list(self._entries.values()):
                safe_unlink(entry.path)
            self._entries.clear()
            self._bytes = 0
        try:
            for path in self._dir.glob("*.png"):
                safe_unlink(path)
        except OSError:
            pass

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "bytes": self._bytes,
                "max_bytes": self._max_bytes,
                "ttl_sec": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
                "evictions": self._evictions,
            }