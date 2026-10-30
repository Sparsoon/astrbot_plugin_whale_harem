#!/usr/bin/env python3
"""数据库压测脚本（纯 asyncio，不依赖 pytest）。

为什么在 2G 机器上要专门压测存储层：
    * 2 核意味着任何"锁等待"都会直接变成用户可感知的延迟，必须量化 P95/max；
    * 连接池只有 5 条，一旦某个方法借了连接不还（漏 finally / 事务里 await 慢操作），
      表现为"越跑越慢最后卡死"，本脚本专门做**泄漏检测**（回池数量 + 满额并发探针）；
    * 写事务都是 BEGIN IMMEDIATE（全局单写者），要确认 200 并发下不会大面积 lock 超时；
    * 全程用临时库，跑完自动清理，不碰真实数据，可在生产机上安全运行。

用法：
    python scripts/bench_db.py                # 默认 200 并发任务、5 条连接
    python scripts/bench_db.py -n 400 --conns 5
    python scripts/bench_db.py --keep         # 保留临时库，方便事后用 sqlite3 检查

退出码：0 = 全部成功且无连接泄漏；1 = 有报错 / 泄漏 / 导入失败。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import math
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent

# 4 类操作按轮转均匀分布在 N 个任务里，保证"同一用户/同一群"有真实竞争
OP_GET_USER = "get_user"
OP_ADD_COINS = "add_coins"
OP_MUTATE_BAG = "mutate_bag"
OP_DONATE = "donate_materials"
OP_ORDER = (OP_GET_USER, OP_ADD_COINS, OP_MUTATE_BAG, OP_DONATE)

USERS = 20   # 参与压测的用户数：少而集中，才能制造同键写竞争
GROUPS = 4   # 群数：池塘行竞争
MAX_ERRORS_PRINTED = 12


# --------------------------------------------------------------------------- 导入
def _metadata_name() -> str:
    """从 metadata.yaml 里抠出插件名（不引 yaml 依赖）。"""
    path = ROOT / "metadata.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _import_backend() -> tuple[Any, Any, Any]:
    """把仓库根目录的父目录塞进 sys.path，再以"包"的形式导入 core.db.*。

    core/* 内部用的是相对导入（`from ..errors import ...`），所以必须把整个插件目录
    当成一个包来导入；直接 `python scripts/bench_db.py` 时脚本目录不在包内，
    因此这里做一次 sys.path 兜底。
    """
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    candidates = [name for name in (_metadata_name(), ROOT.name, "astrbot_plugin_whale_harem")
                  if name and name.isidentifier()]
    # 去重但保序
    seen: list[str] = []
    for name in candidates:
        if name not in seen:
            seen.append(name)

    last_error: BaseException | None = None
    for name in seen:
        try:
            database_mod = importlib.import_module(f"{name}.core.db.database")
            repos_mod = importlib.import_module(f"{name}.core.db.repos")
            config_mod = importlib.import_module(f"{name}.core.config")
        except Exception as exc:  # noqa: BLE001 - 逐个候选试
            last_error = exc
            continue
        loaded = Path(getattr(database_mod, "__file__", "") or "").resolve()
        if ROOT not in loaded.parents:
            last_error = RuntimeError(f"导入到的是别的同名包: {loaded}")
            continue
        return database_mod, repos_mod, config_mod

    hint = (
        f"无法以包的形式导入本仓库的 core.db.*（最后错误: {last_error!r}）\n"
        f"  仓库根目录: {ROOT}\n"
        "  排查清单:\n"
        "    1) 依赖是否安装: python -m pip install -r requirements.txt   "
        "（至少需要 aiosqlite）\n"
        "    2) 目录名必须是合法的 Python 标识符（不能带空格/横线），"
        "或改用 metadata.yaml 里的 name\n"
        "    3) 请在仓库根目录执行: python scripts/bench_db.py\n"
        "    4) 若 site-packages 里装了同名插件包，脚本会拒绝加载它（避免压错代码）"
    )
    raise SystemExit(hint)


# --------------------------------------------------------------------------- 计时
def _pct(values: list[float], p: float) -> float:
    """最近秩百分位（nearest-rank），返回秒。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(math.ceil(p / 100.0 * len(ordered))) - 1
    if idx < 0:
        idx = 0
    elif idx >= len(ordered):
        idx = len(ordered) - 1
    return ordered[idx]


async def _timed(
    name: str,
    factory: Callable[[], Awaitable[Any]],
    results: dict[str, list[float]],
    errors: list[tuple[str, str]],
) -> None:
    start = time.perf_counter()
    try:
        await factory()
    except Exception as exc:  # noqa: BLE001 - 压测要统计失败而不是中断
        if len(errors) < 200:
            errors.append((name, f"{type(exc).__name__}: {exc}"))
        return
    results.setdefault(name, []).append(time.perf_counter() - start)


# --------------------------------------------------------------------------- 主体
async def run(args: argparse.Namespace) -> int:
    database_mod, repos_mod, config_mod = _import_backend()

    Database = getattr(database_mod, "Database")
    Repo = getattr(repos_mod, "Repo")
    cfg = getattr(config_mod, "PluginConfig")()

    tmpdir = Path(tempfile.mkdtemp(prefix="whale_bench_"))
    db_path = tmpdir / "bench.db"
    db = None
    exit_code = 0

    print("=" * 74)
    print("赛博鲸鱼馆 · DB 压测")
    print(f"  仓库      : {ROOT}")
    print(f"  临时库    : {db_path}")
    print(f"  任务数    : {args.tasks}  连接池: {args.conns}  用户: {USERS}  群: {GROUPS}")
    print("=" * 74)

    try:
        db = Database(db_path, max_connections=args.conns)
        t0 = time.perf_counter()
        await db.init()
        print(f"[init] 建库完成，耗时 {(time.perf_counter() - t0) * 1000:.1f} ms")
        repo = Repo(db, cfg)

        # ---------------------------------------------------------- 预热
        t0 = time.perf_counter()
        for i in range(USERS):
            await repo.get_user(f"bench_{i}", f"压测员{i}")
        for gid in range(GROUPS):
            await repo.get_pond(f"group_{gid}")
        print(f"[warm] 预建 {USERS} 用户 + {GROUPS} 池塘，耗时 {(time.perf_counter() - t0) * 1000:.1f} ms")

        # ---------------------------------------------------------- 并发压测
        results: dict[str, list[float]] = {}
        errors: list[tuple[str, str]] = []

        def get_user_op(uid: str) -> Callable[[], Awaitable[Any]]:
            return lambda: repo.get_user(uid, "压测员")

        def add_coins_op(uid: str) -> Callable[[], Awaitable[Any]]:
            return lambda: repo.add_coins(uid, 5)

        def mutate_bag_op(uid: str) -> Callable[[], Awaitable[Any]]:
            def _mut(bag: Any) -> None:
                bag.add("bait_normal", 1)

            return lambda: repo.mutate_bag(uid, _mut)

        def donate_op(gid: str, uid: str) -> Callable[[], Awaitable[Any]]:
            return lambda: repo.donate_materials(gid, uid, {"mat_stone": 1})

        builders = (get_user_op, add_coins_op, mutate_bag_op)
        jobs: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        for i in range(args.tasks):
            kind = i % len(OP_ORDER)
            uid = f"bench_{i % USERS}"
            gid = f"group_{i % GROUPS}"
            if kind == len(OP_ORDER) - 1:
                jobs.append((OP_DONATE, donate_op(gid, uid)))
            else:
                jobs.append((OP_ORDER[kind], builders[kind](uid)))

        print(f"[run ] 并发 {len(jobs)} 个任务 ...")
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_timed(name, factory, results, errors) for name, factory in jobs)),
                timeout=args.timeout,
            )
        except asyncio.TimeoutError:
            print(f"[FAIL] 压测超过 {args.timeout}s 仍未结束：疑似连接泄漏或锁等待（重点查 BEGIN IMMEDIATE 里是否有慢 await）")
            exit_code = 1
        wall = time.perf_counter() - t0

        # ---------------------------------------------------------- 结果
        all_times: list[float] = []
        print("-" * 74)
        header = f"{'操作':<18}{'次数':>6}{'失败':>6}{'P50(ms)':>10}{'P95(ms)':>10}{'max(ms)':>10}{'mean(ms)':>10}"
        print(header)
        for name in OP_ORDER:
            values = results.get(name, [])
            all_times.extend(values)
            fails = sum(1 for op, _ in errors if op == name)
            if values:
                p50 = _pct(values, 50) * 1000
                p95 = _pct(values, 95) * 1000
                worst = max(values) * 1000
                mean = statistics.fmean(values) * 1000
                print(f"{name:<18}{len(values):>6}{fails:>6}{p50:>10.2f}{p95:>10.2f}{worst:>10.2f}{mean:>10.2f}")
            else:
                print(f"{name:<18}{0:>6}{fails:>6}{'-':>10}{'-':>10}{'-':>10}{'-':>10}")
        if all_times:
            p50 = _pct(all_times, 50) * 1000
            p95 = _pct(all_times, 95) * 1000
            worst = max(all_times) * 1000
            print("-" * 74)
            print(f"{'合计':<18}{len(all_times):>6}{len(errors):>6}{p50:>10.2f}{p95:>10.2f}{worst:>10.2f}"
                  f"{statistics.fmean(all_times) * 1000:>10.2f}")
        throughput = len(all_times) / wall if wall > 0 else 0.0
        print(f"[time] 墙钟 {wall * 1000:.1f} ms，吞吐 {throughput:.0f} op/s")

        # ---------------------------------------------------------- 泄漏检测
        print("-" * 74)
        stats = db.stats()
        idle = int(stats.get("idle_connections", -1))
        max_conns = int(stats.get("max_connections", args.conns))
        pool_size = len(getattr(db, "_conns", []) or [])
        print(f"[pool] idle={idle}/{max_conns}  conns={pool_size}  运行 {stats.get('uptime_sec', 0)}s")
        if idle != max_conns or pool_size != max_conns:
            print("[FAIL] 连接池泄漏：压测结束后应有全部连接空闲并回到池中")
            exit_code = 1
        else:
            # 满额并发探针：只有连接全部归还才可能同时完成
            probe_ok = 0

            async def _probe() -> None:
                nonlocal probe_ok
                await db.fetch_value("SELECT 1", (), 0)
                probe_ok += 1

            try:
                await asyncio.wait_for(asyncio.gather(*(_probe() for _ in range(max_conns))), timeout=10.0)
            except Exception as exc:  # noqa: BLE001 - 超时/池超时都算泄漏信号
                print(f"[FAIL] 满额探针失败（连接未全部可用）: {type(exc).__name__}: {exc}")
                exit_code = 1
            else:
                print(f"[pool] 满额探针 {probe_ok}/{max_conns} 通过，无泄漏")

        # ---------------------------------------------------------- 文件体积
        for label, path in (
            ("db", db_path),
            ("wal", Path(str(db_path) + "-wal")),
            ("shm", Path(str(db_path) + "-shm")),
        ):
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            if size >= 0:
                print(f"[file] {label:<4}{size / 1024:>10.1f} KB")

        # ---------------------------------------------------------- 错误明细
        if errors:
            print("-" * 74)
            print(f"[errors] 共 {len(errors)} 条，前 {min(len(errors), MAX_ERRORS_PRINTED)} 条：")
            for name, message in errors[:MAX_ERRORS_PRINTED]:
                print(f"  - {name}: {message}")
            if all("DBPoolTimeout" in message for _, message in errors):
                print("  提示：全部是连接池超时 → 检查是否有事务里 await 了慢操作，或降低 -n 再看")
            exit_code = 1

        print("=" * 74)
        if exit_code == 0:
            print("结论: PASS —— 无错误、无连接泄漏，P95 见上表")
        else:
            print("结论: FAIL —— 见上面的 [FAIL] / [errors] 行")
        return exit_code

    finally:
        if db is not None:
            try:
                await db.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 关闭数据库时出错（不影响结论）: {type(exc).__name__}: {exc}")
        if args.keep:
            print(f"[keep] 临时库已保留: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)
            print(f"[clean] 已清理临时目录: {tmpdir}")


# --------------------------------------------------------------------------- 入口
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="赛博鲸鱼馆数据库压测（纯 asyncio，临时库）")
    parser.add_argument("-n", "--tasks", type=int, default=200, help="并发任务数（默认 200）")
    parser.add_argument("--conns", type=int, default=5, help="连接池大小（默认 5，上限由 Database 夹到 5）")
    parser.add_argument("--timeout", type=float, default=90.0, help="整体超时秒数（默认 90）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据库文件")
    args = parser.parse_args(argv)
    args.tasks = max(len(OP_ORDER), int(args.tasks))
    args.conns = max(1, min(5, int(args.conns)))
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[abort] 用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())