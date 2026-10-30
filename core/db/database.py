"""aiosqlite 连接池 —— 全异步、WAL、连接数硬上限 5。

设计约束（2核2G）：
    * 连接数 = min(5, CPU*2+1)，默认 5，绝不无限开连接。
    * 每个连接独立 PRAGMA：busy_timeout=5000 / cache_size=-2000(2MB) / temp_store=MEMORY。
    * 写操作统一走 `BEGIN IMMEDIATE`：SQLite 只有一个写者，先把写锁拿到手，
      彻底避免 "database is locked" 抖动，也不会出现两个事务互相等待的死锁。
    * 禁止在持有连接时再去抢第二条连接（重入即死锁），所有仓库方法都在单连接内完成。
    * isolation_level=None（自动提交）+ 显式事务，避免 sqlite3 隐式事务带来的意外回滚。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence, TypeVar

import aiosqlite

from ..errors import DBPoolTimeout
from ..log import log
from .schema import SCHEMA_SQL, SCHEMA_VERSION

T = TypeVar("T")
Row = aiosqlite.Row

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",       # 持久化设置，读写并发靠它
    "PRAGMA synchronous=NORMAL",     # WAL 下足够安全，比 FULL 快一个数量级
    "PRAGMA busy_timeout=5000",
    "PRAGMA cache_size=-2000",       # 每连接 2MB（5 条约 10MB，2G 机器可接受）
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=0",            # 关 mmap，避免 RSS 虚高被 OOM Killer 盯上
    "PRAGMA foreign_keys=ON",
    "PRAGMA wal_autocheckpoint=200", # 每 200 页自动 checkpoint，WAL 不无限增长
)

# SQLite 的"可重试抖动"错误关键字：命中后转成 DBPoolTimeout(WhaleError)，
# 让指令层回一张"稍后重试"的提示卡，而不是把原生异常冒到 AstrBot 核心。
_TRANSIENT_HINTS = ("locked", "busy", "disk i/o error")

# 增量迁移：老库（v1）没有"等待中"相关列，启动时就地补上（幂等）
_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("work_started_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("work_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ("work_pending_coins", "INTEGER NOT NULL DEFAULT 0"),
        ("fish_ready_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("fish_bait", "TEXT NOT NULL DEFAULT ''"),
        ("fish_wait_min", "INTEGER NOT NULL DEFAULT 0"),
        ("fish_group", "TEXT NOT NULL DEFAULT ''"),
        ("bot_affection", "INTEGER NOT NULL DEFAULT 0"),
        ("feed_slots", "TEXT NOT NULL DEFAULT ''"),
        ("feed_total", "INTEGER NOT NULL DEFAULT 0"),
        ("feed_milestone", "INTEGER NOT NULL DEFAULT 0"),
        ("feed_auto_food", "TEXT NOT NULL DEFAULT ''"),
    ),
    "ponds": (
        ("weather", "TEXT NOT NULL DEFAULT ''"),
        ("weather_until", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


def _is_transient(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in _TRANSIENT_HINTS)


async def _rollback_quietly(conn: aiosqlite.Connection) -> None:
    """尽力回滚；连接已坏时吞掉异常，保证 finally 里还能把连接放回池子。"""
    try:
        await conn.rollback()
    except (sqlite3.Error, aiosqlite.Error):  # pragma: no cover - 防御性
        pass


class Database:
    """固定大小连接池。用法：

        db = Database(path, max_connections=5)
        await db.init()
        async with db.transaction() as conn:
            await conn.execute(...)
        await db.close()
    """

    __slots__ = ("_path", "_max", "_queue", "_conns", "_closed", "_timeout", "_started_at")

    def __init__(self, path: str | Path, max_connections: int = 5, acquire_timeout: float = 6.0) -> None:
        self._path = Path(path)
        self._max = max(1, min(5, int(max_connections)))
        self._timeout = max(0.5, float(acquire_timeout))
        self._queue: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue(maxsize=self._max)
        self._conns: list[aiosqlite.Connection] = []
        self._closed = False
        self._started_at = 0.0

    # ------------------------------------------------------------ 生命周期
    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_connections(self) -> int:
        return self._max

    async def init(self) -> None:
        if self._conns:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            for _ in range(self._max):
                conn = await aiosqlite.connect(
                    str(self._path),
                    timeout=self._timeout,
                    isolation_level=None,
                    check_same_thread=False,
                )
                conn.row_factory = aiosqlite.Row
                await self._apply_pragmas(conn)
                self._conns.append(conn)
                self._queue.put_nowait(conn)

            # executescript() 在 legacy 事务模式下会先隐式 COMMIT 掉挂起事务，
            # 所以这里不套 BEGIN IMMEDIATE：建表靠 IF NOT EXISTS 幂等，
            # 少一次写锁往返，也就少一次和 NapCat 抢 IO 的机会。
            async with self.connection() as conn:
                await conn.executescript(SCHEMA_SQL)
            await self._migrate()
            await self._check_version()
        except BaseException:
            # 建库中途失败（磁盘满 / 权限 / 版本不兼容）：把已开的连接关掉再抛，
            # 避免"半初始化"的池子留在内存里，下次 init 又因为 _conns 非空直接返回。
            await self._dispose_partial()
            raise
        self._started_at = time.monotonic()
        log.info(f"鲸鱼馆数据库就绪: {self._path} (连接数 {self._max}, WAL)")

    async def _dispose_partial(self) -> None:
        for conn in self._conns:
            try:
                await conn.close()
            except (sqlite3.Error, aiosqlite.Error):
                pass
        self._conns.clear()
        self._queue = asyncio.Queue(maxsize=self._max)

    async def _apply_pragmas(self, conn: aiosqlite.Connection) -> None:
        for pragma in _PRAGMAS:
            try:
                cursor = await conn.execute(pragma)
                await cursor.close()
            except aiosqlite.Error as exc:  # 某些旧版本不支持，降级即可
                log.debug(f"PRAGMA 失败 {pragma}: {exc}")

    async def _migrate(self) -> None:
        """老库补列（幂等）。只做 ADD COLUMN，不重建表、不丢数据。"""
        async with self.connection() as conn:
            for table, columns in _MIGRATIONS.items():
                cursor = await conn.execute(f"PRAGMA table_info({table})")
                rows = await cursor.fetchall()
                await cursor.close()
                existing = {row["name"] for row in rows}
                for name, ddl in columns:
                    if name in existing:
                        continue
                    try:
                        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                        log.info(f"数据库迁移：{table}.{name} 已补列")
                    except aiosqlite.Error as exc:  # 并发迁移/权限问题：不阻塞启动
                        log.warning(f"迁移 {table}.{name} 失败: {exc}")
            # 依赖新列的索引必须等补列之后再建
            try:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_users_fish ON users(fish_group, fish_ready_ts)"
                )
            except aiosqlite.Error as exc:
                log.debug(f"索引 idx_users_fish 创建失败: {exc}")

    async def _check_version(self) -> None:
        async with self.connection() as conn:
            cursor = await conn.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            current = int(row[0]) if row else 0
            if current < SCHEMA_VERSION:
                await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                if current:
                    log.info(f"数据库结构升级 {current} -> {SCHEMA_VERSION}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for conn in self._conns:
            try:
                cursor = await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await cursor.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns.clear()
        self._queue = asyncio.Queue(maxsize=self._max)
        log.info("鲸鱼馆数据库已关闭")

    # ------------------------------------------------------------ 借还
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._closed:
            raise DBPoolTimeout("数据库已关闭")
        try:
            conn = await asyncio.wait_for(self._queue.get(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            raise DBPoolTimeout() from exc
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            # 单连接已经出错（典型：busy_timeout 内仍未抢到锁 / 磁盘抖动）。
            # 先回滚，避免把"事务残留"的脏连接放回池子，再由调用方决定重试。
            await _rollback_quietly(conn)
            if _is_transient(exc):
                log.warning(f"数据库抖动，已转为可重试错误: {exc}")
                raise DBPoolTimeout("数据库繁忙，请稍后再试") from exc
            raise
        finally:
            try:
                self._queue.put_nowait(conn)
            except asyncio.QueueFull:  # 理论上不可能，兜底防连接泄漏
                log.error("连接池回池失败，连接已丢弃")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """写事务。BEGIN IMMEDIATE 抢写锁；异常回滚。"""
        async with self.connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # 起事务就失败：连接没进事务，直接抛，不用回滚
                if _is_transient(exc):
                    raise DBPoolTimeout("数据库繁忙，请稍后再试") from exc
                raise
            try:
                yield conn
            except BaseException:
                await _rollback_quietly(conn)
                raise
            else:
                try:
                    await conn.commit()
                except BaseException:
                    # 提交失败（磁盘满 / IO 错误）：必须回滚，不能让连接带着
                    # 未提交事务回到池子，否则下一个使用者会读到脏数据。
                    await _rollback_quietly(conn)
                    raise

    # ------------------------------------------------------------ 便捷方法
    async def run_tx(self, fn: Callable[[aiosqlite.Connection], Awaitable[T]]) -> T:
        async with self.transaction() as conn:
            return await fn(conn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self.connection() as conn:
            await conn.execute(sql, params)

    async def execute_many(self, sql: str, seq: Sequence[Sequence[Any]]) -> int:
        async with self.transaction() as conn:
            cursor = await conn.executemany(sql, seq)
            try:
                return cursor.rowcount or 0
            finally:
                await cursor.close()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self.connection() as conn:
            cursor = await conn.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.connection() as conn:
            cursor = await conn.execute(sql, params)
            try:
                rows = await cursor.fetchall()
                return list(rows or [])
            finally:
                await cursor.close()

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        try:
            value = row[0]
        except (IndexError, KeyError):
            return default
        return default if value is None else value

    # ------------------------------------------------------------ 运维
    async def vacuum_if_needed(self, threshold_mb: float = 48.0) -> bool:
        """库文件偏大时才 VACUUM（阻塞且耗 IO，绝不能每天跑）。"""
        try:
            size_mb = self._path.stat().st_size / 1048576
        except OSError:
            return False
        if size_mb < threshold_mb:
            return False
        try:
            async with self.connection() as conn:
                await conn.execute("VACUUM")
            log.info(f"VACUUM 完成，原大小 {size_mb:.1f}MB")
            return True
        except (aiosqlite.Error, DBPoolTimeout) as exc:
            # 运维操作失败不能影响主流程：connection() 会把 "database is locked"
            # 转成 DBPoolTimeout，这里一并吞掉并返回 False。
            log.warning(f"VACUUM 失败: {exc}")
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self._path),
            "max_connections": self._max,
            "idle_connections": self._queue.qsize() if not self._closed else 0,
            "uptime_sec": int(time.monotonic() - self._started_at) if self._started_at else 0,
        }