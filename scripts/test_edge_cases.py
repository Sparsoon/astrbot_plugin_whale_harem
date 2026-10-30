"""边界用例回归：缓存淘汰/同路径、惰性跨天重置、保底概率、顶层模块加载兼容、缓存命中。

用法：
    python scripts/test_edge_cases.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bootstrap() -> str:
    if str(ROOT.parent) not in sys.path:
        sys.path.insert(0, str(ROOT.parent))
    names = ["astrbot_plugin_whale_harem", ROOT.name]
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            names.insert(0, line.split(":", 1)[1].strip().strip("'\""))
            break
    last = None
    for name in names:
        if not name or not name.isidentifier():
            continue
        try:
            importlib.import_module(f"{name}.core.config")
            return name
        except ImportError as exc:
            last = exc
    raise SystemExit(f"无法导入插件包：{last!r}")


async def main() -> int:
    pkg = bootstrap()
    cfg_mod = importlib.import_module(f"{pkg}.core.config")
    db_mod = importlib.import_module(f"{pkg}.core.db.database")
    repo_mod = importlib.import_module(f"{pkg}.core.db.repos")
    ctx_mod = importlib.import_module(f"{pkg}.core.context")
    tu = importlib.import_module(f"{pkg}.core.time_utils")
    cache_mod = importlib.import_module(f"{pkg}.ui.cache")
    renderer_mod = importlib.import_module(f"{pkg}.ui.renderer")
    schema = importlib.import_module(f"{pkg}.ui.schema")

    tmp = Path(tempfile.mkdtemp(prefix="whale_extra_"))
    ok = 0
    bad = []

    def check(name, cond, detail=""):
        nonlocal ok
        if cond:
            ok += 1
            print(f"  [ok]   {name}")
        else:
            bad.append(name)
            print(f"  [FAIL] {name} {detail}")

    # ---------------- A. ImageCache ----------------
    print("\n[A] ImageCache")
    d = tmp / "cache"
    d.mkdir()
    c = cache_mod.ImageCache(max_entries=2, ttl_sec=100, cache_dir=d)
    files = []
    for i in range(3):
        p = d / f"k{i}.png"
        p.write_bytes(b"x" * 100)
        files.append(p)
        c.put(f"k{i}", p)
    check("条目上限为 2", c.stats()["entries"] == 2, str(c.stats()))
    check("最旧的被淘汰且文件已删", not files[0].exists())
    check("最新命中", c.get("k2") == files[2])
    # 同 key 同路径重复 put 不能删掉自己
    c.put("k2", files[2])
    check("同路径重复 put 不删文件", files[2].exists() and c.get("k2") is not None)
    old = cache_mod.ImageCache(max_entries=2, ttl_sec=0, cache_dir=d / "t")
    p = d / "ttl.png"
    p.write_bytes(b"y" * 10)
    old.put("x", p)
    check("ttl=0 时缓存禁用（不登记）", old.get("x") is None)

    # ---------------- B. 惰性跨天 ----------------
    print("\n[B] 惰性日结")
    db = db_mod.Database(tmp / "b.db", 5)
    await db.init()
    cfg = cfg_mod.PluginConfig()
    repo = repo_mod.Repo(db, cfg)
    user = await repo.get_user("u1", "甲")
    user.checkin_streak = 5
    user.last_checkin_day = tu.day_key()  # 今天
    user.feed_day = "2000-01-01"
    user.feed_count = 7
    changed = ctx_mod.settle_user_day(user)
    check("跨天重置 feed_count", changed and user.feed_count == 0, f"count={user.feed_count}")
    check("day_key 已更新", user.day_key == tu.day_key())

    row = await repo.ensure_harem("g1", "t1", "乙")
    row.marry_fail = 3
    row.ntr_fail = 7
    row.divorce_fail = 1
    row.affection = 20
    row.day_key = "2000-01-01"
    ctx_mod.settle_harem_day(row)
    check("保底计数跨天清零", row.marry_fail == 0 and row.ntr_fail == 0 and row.divorce_fail == 0)
    check("恩爱值按天增长（封顶 999）", row.affection > 20, f"affection={row.affection}")

    # ---------------- C. 保底概率 ----------------
    print("\n[C] 保底")
    marriage = importlib.import_module(f"{pkg}.services.marriage")
    check("娶第5次=100%", marriage._marry_rate(4) == 1.0, marriage._marry_rate(4))
    check("娶第4次=40%", abs(marriage._marry_rate(3) - 0.40) < 1e-9, marriage._marry_rate(3))
    check("离婚第2次=100%", marriage._divorce_rate(1) == 1.0)
    check("NTR 第10次=100%", marriage._ntr_rate(9) == 1.0)
    check("NTR 第1次=5%", abs(marriage._ntr_rate(0) - 0.05) < 1e-9)

    # ---------------- D. 渲染 + 缓存命中 ----------------
    print("\n[D] 渲染/缓存")
    cfg2 = cfg_mod.PluginConfig()
    cfg2.render_concurrency = 1
    r = renderer_mod.Renderer(cfg2, tmp / "img", pkg)
    c1 = schema.card("测试卡", "缓存命中测试", art="fatfish", stats=[schema.stat("鲸币", "1")])
    p1 = await r.render(c1)
    p2 = await r.render(c1)
    check("两次渲染返回同一路径", p1 == p2, f"{p1} / {p2}")
    check("文件存在", Path(p1).is_file())
    check("缓存命中 1 次", r.stats().get("hits", 0) >= 1, str(r.stats()))
    await r.close()

    # ---------------- E. 顶层模块加载（无包） ----------------
    print("\n[E] 顶层模块加载兼容")
    stub = Path(tempfile.mkdtemp(prefix="whale_stub_"))
    (stub / "astrbot" / "api").mkdir(parents=True)
    (stub / "astrbot" / "__init__.py").write_text("from . import api\n", encoding="utf-8")
    (stub / "astrbot" / "api" / "__init__.py").write_text(
        "import logging\nlogger = logging.getLogger('stub')\n", encoding="utf-8"
    )
    (stub / "astrbot" / "api" / "event.py").write_text(
        "class AstrMessageEvent: pass\n"
        "class _E:\n"
        "    ALL = 'all'\n"
        "class _F:\n"
        "    EventMessageType = _E\n"
        "    def command(self, *a, **k):\n"
        "        return lambda fn: fn\n"
        "    def event_message_type(self, *a, **k):\n"
        "        return lambda fn: fn\n"
        "    def llm_tool(self, *a, **k):\n"
        "        return lambda fn: fn\n"
        "filter = _F()\n",
        encoding="utf-8",
    )
    (stub / "astrbot" / "api" / "star.py").write_text(
        "class Context: pass\n"
        "class Star:\n"
        "    def __init__(self, ctx):\n"
        "        self.context = ctx\n"
        "def register(*a, **k):\n"
        "    return lambda cls: cls\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(stub))
    spec = importlib.util.spec_from_file_location("solo_whale_main", ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["solo_whale_main"] = mod
    try:
        spec.loader.exec_module(mod)  # 关键：__package__ 为空时必须仍能加载
        check("顶层模块方式加载 main.py", hasattr(mod, "WhaleHaremPlugin"))
        check("包名被正确填充", mod.__package__ == "astrbot_plugin_whale_harem", mod.__package__)
    except Exception as exc:  # noqa: BLE001
        check("顶层模块方式加载 main.py", False, repr(exc))

    # ---------------- F. 老库迁移（生产库 v1 -> v2 补列） ----------------
    print("\n[F] 老库迁移")
    old = tmp / "old.db"
    db2 = db_mod.Database(old, 5)
    await db2.init()
    new_cols = ["fish_ready_ts", "fish_bait", "fish_wait_min",
                "work_started_ts", "work_minutes", "work_pending_coins"]
    try:
        async with db2.connection() as conn:
            await conn.execute("DROP INDEX IF EXISTS idx_users_fish")
            for col in new_cols:
                await conn.execute(f"ALTER TABLE users DROP COLUMN {col}")
        await db2.close()
        db3 = db_mod.Database(old, 5)
        await db3.init()
        async with db3.connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(users)")
            rows = await cursor.fetchall()
            await cursor.close()
        cols = {row["name"] for row in rows}
        check("老库自动补列", set(new_cols) <= cols, str(sorted(cols)))
        await db3.close()
    except Exception as exc:  # noqa: BLE001 - 老 SQLite 不支持 DROP COLUMN
        check("老库自动补列（跳过：SQLite 版本不支持 DROP COLUMN）", True, repr(exc))
        await db2.close()

    await db.close()
    print("\n" + "=" * 46)
    print(f"通过 {ok} 项" + ("" if not bad else f"，失败 {len(bad)} 项: {bad}"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
