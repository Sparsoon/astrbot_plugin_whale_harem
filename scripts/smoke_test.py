"""离线冒烟测试：不需要 AstrBot、不需要 QQ，直接跑完整玩法链路。

用法：
    python scripts/smoke_test.py

它会：
    1. 建一个临时 SQLite（WAL、连接池 5）并建表；
    2. 用假 SvcCtx 依次跑 签到/喂食/打工/钓鱼/卖鱼/池塘/捐献/娶/后宫/商店/道具/面板；
    3. 校验每个 service 都返回 `ui.schema.Card`，并打印每张卡的标题；
    4. 检查保底、扣费、幂等（重复签到必须报错）等关键行为；
    5. 全程不 mock asyncio，能暴露忘记 await / 死锁 / 连接泄漏。

退出码 0 = 全部通过；非 0 = 有断言失败。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

os.environ.setdefault("WHALE_FEED_ANYTIME", "1")
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_package() -> str:
    """把插件目录当"包"导入。

    core/*、services/* 内部用的都是相对导入（`from ..core.errors import ...`），
    直接 `from core.config import ...` 会报 "attempted relative import beyond top-level
    package"，所以必须把 **父目录** 放进 sys.path，再以 `<插件目录名>.<子模块>` 的形式导入。
    """
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    candidates: list[str] = []
    try:
        for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                candidates.append(line.split(":", 1)[1].strip().strip("'\""))
                break
    except OSError:
        pass
    candidates.extend([ROOT.name, "astrbot_plugin_whale_harem"])

    tried: list[str] = []
    last_error: BaseException | None = None
    for name in candidates:
        if not name or not name.isidentifier() or name in tried:
            continue
        tried.append(name)
        try:
            pkg = importlib.import_module(name)
            importlib.import_module(f"{name}.core.config")  # 验证子模块可用
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        if Path(getattr(pkg, "__file__", "") or "").resolve() == ROOT / "__init__.py":
            return name
        # 命名空间包（没有 __init__.py）也能用：只要路径对得上
        if str(ROOT) in list(getattr(pkg, "__path__", []) or []):
            return name
    raise SystemExit(
        "无法把插件目录作为包导入。\n"
        f"  目录: {ROOT}\n"
        f"  试过: {tried}\n"
        f"  最后错误: {last_error!r}\n"
        "请确认目录名是合法的 Python 标识符（如 astrbot_plugin_whale_harem），"
        "且本文件位于 <插件目录>/scripts/ 下。"
    )


PKG = _bootstrap_package()
_config = importlib.import_module(f"{PKG}.core.config")
_context = importlib.import_module(f"{PKG}.core.context")
_database = importlib.import_module(f"{PKG}.core.db.database")
_repos = importlib.import_module(f"{PKG}.core.db.repos")
_errors = importlib.import_module(f"{PKG}.core.errors")
_llm = importlib.import_module(f"{PKG}.core.llm")
_schema = importlib.import_module(f"{PKG}.ui.schema")

PluginConfig = _config.PluginConfig
SvcCtx = _context.SvcCtx
Database = _database.Database
Repo = _repos.Repo
WhaleError = _errors.WhaleError
LLMClient = _llm.LLMClient
Card = _schema.Card

daily = importlib.import_module(f"{PKG}.services.daily")
feed = importlib.import_module(f"{PKG}.services.feed")
work = importlib.import_module(f"{PKG}.services.work")
fishing = importlib.import_module(f"{PKG}.services.fishing")
pond = importlib.import_module(f"{PKG}.services.pond")
marriage = importlib.import_module(f"{PKG}.services.marriage")
shop = importlib.import_module(f"{PKG}.services.shop")
item_svc = importlib.import_module(f"{PKG}.services.items")
profile = importlib.import_module(f"{PKG}.services.profile")
warehouse = importlib.import_module(f"{PKG}.services.warehouse")
_time = importlib.import_module(f"{PKG}.core.time_utils")

now_ts = _time.now_ts

UID = "10001"
TARGET = "20002"
GROUP = "30003"

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [ok]   {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        _failures.append(f"{name} {detail}")


def expect_card(name: str, value: object) -> object:
    ok = isinstance(value, Card) and bool(getattr(value, "title", ""))
    check(name, ok, f"返回 {type(value).__name__}")
    if isinstance(value, Card):
        print(f"         -> 《{value.title}》")
    return value


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="whale_smoke_"))
    db = Database(tmp / "whale.db", max_connections=5)
    await db.init()
    cfg = PluginConfig()
    cfg.llm_enabled = False          # 冒烟测试不联网
    repo = Repo(db, cfg)
    ctx = SvcCtx(repo=repo, cfg=cfg, llm=LLMClient(None, cfg))

    print(f"\n== 临时库: {db.path} ==")

    # ---------------------------------------------------------- 准备资产
    user = await repo.get_user(UID, "测试群友")
    await repo.add_coins(UID, 5000)
    await repo.add_items(
        UID,
        {
            "rice_bowl": 20,
            "rice_pot": 5,
            "bait_normal": 30,
            "bait_worm": 10,
            "card_makeup": 2,
            "card_loyal": 1,
            "ticket_reservoir": 1,
            "mat_stone": 30,
            "mat_coral": 20,
            "card_wife": 1,
            "card_good": 1,
            "card_green": 1,
        },
    )
    await repo.get_user(TARGET, "测试老婆")

    # ---------------------------------------------------------- 签到
    print("\n[1] 签到 / 补签")
    expect_card("签到", await daily.do_checkin(ctx, UID, "测试群友", GROUP))
    try:
        await daily.do_checkin(ctx, UID, "测试群友", GROUP)
        check("同日重复签到必须报错", False)
    except WhaleError:
        check("同日重复签到必须报错", True)
    try:
        await daily.do_makeup(ctx, UID, "测试群友", GROUP)
        check("无漏签时补签应报错", False)
    except WhaleError:
        check("无漏签时补签应报错", True)

    # ---------------------------------------------------------- 喂食（喂 bot）
    print("\n[2] 喂食（投喂鲸鱼娘）")
    rice_before = (await repo.get_bag(UID)).count("rice_bowl")
    expect_card("喂食", await feed.do_feed(ctx, UID, "测试群友", GROUP, ""))
    try:
        await feed.do_feed(ctx, UID, "测试群友", GROUP, "")
        check("同一饭点重复喂食必须报错", False)
    except WhaleError:
        check("同一饭点重复喂食必须报错", True)
    bag = await repo.get_bag(UID)
    check("喂食消耗了米饭", bag.count("rice_bowl") == rice_before - 1, f"{rice_before}->{bag.count('rice_bowl')}")
    check("鲸鱼娘好感已累计", (await repo.get_user(UID)).bot_affection > 0,
          f"bot_affection={(await repo.get_user(UID)).bot_affection}")

    # ---------------------------------------------------------- 打工（上工/收工）
    print("\n[3] 打工（上工 -> 等待 -> 收工）")
    expect_card("上工", await work.do_work(ctx, UID, "测试群友", GROUP))
    try:
        await work.do_work(ctx, UID, "测试群友", GROUP)
        check("等待期内再打工必须报错", False)
    except WhaleError:
        check("等待期内再打工必须报错", True)
    # 钓鱼与打工解耦：上班期间也能抛竿
    expect_card("打工中抛竿", await fishing.do_fish(ctx, UID, "测试群友", GROUP, None, ""))
    await repo.update_user(UID, fish_ready_ts=0, fish_bait="", fish_wait_min=0, fish_group="")
    check("打工与钓鱼可同时进行", True)
    await repo.update_user(UID, work_ready_ts=now_ts() - 1)
    expect_card("收工", await work.do_work(ctx, UID, "测试群友", GROUP))
    check("收工后状态清空", (await repo.get_user(UID)).work_pending_coins == 0)

    # ---------------------------------------------------------- 钓鱼（抛竿/咬钩）
    print("\n[4] 钓鱼（抛竿 -> 等待 -> 群聊咬钩）")
    for i in range(3):
        expect_card(f"抛竿 #{i + 1}", await fishing.do_fish(ctx, UID, "测试群友", GROUP, None, ""))
        u = await repo.get_user(UID)
        check("抛竿后进入等待", u.fish_ready_ts > now_ts(), f"ready={u.fish_ready_ts}")
        try:
            await fishing.do_fish(ctx, UID, "测试群友", GROUP, None, "")
            check("等待期内再钓鱼必须报错", False)
        except WhaleError:
            check("等待期内再钓鱼必须报错", True)
        await repo.update_user(UID, fish_ready_ts=now_ts() - 1)
        try:
            await fishing.do_fish(ctx, UID, "测试群友", GROUP, None, "")
            check("到点未咬钩时不能再抛竿", False)
        except WhaleError:
            check("到点未咬钩时不能再抛竿", True)
        u = await repo.get_user(UID)
        expect_card(f"咬钩 #{i + 1}", await fishing.reel_catch(ctx, u))
        check("收竿后等待状态清空", (await repo.get_user(UID)).fish_ready_ts == 0)
    expect_card("背包", await fishing.do_bag(ctx, UID, "测试群友", GROUP))
    expect_card("卖鱼", await fishing.do_sell(ctx, UID, "测试群友", GROUP, ""))
    bag = await repo.get_bag(UID)
    check("卖鱼后鱼护清空", bag.fish_count() == 0, f"剩 {bag.fish_count()} 条")

    # ---------------------------------------------------------- 池塘
    print("\n[5] 池塘 / 捐献")
    expect_card("池塘", await pond.do_pond(ctx, UID, "测试群友", GROUP))
    expect_card("捐献", await pond.do_donate(ctx, UID, "测试群友", GROUP, "all"))
    pond_row = await repo.get_pond(GROUP)
    check("捐献后池塘有材料或已升级", pond_row.level > 1 or bool(pond_row.materials),
          f"Lv.{pond_row.level} 材料 {pond_row.materials}")

    # ---------------------------------------------------------- 娶群友（保底）
    print("\n[6] 娶群友（5 次保底）")
    for i in range(5):
        card = await marriage.do_marry(ctx, UID, "测试群友", GROUP, TARGET, "测试老婆", use_card=(i == 0))
        print(f"  第 {i + 1} 次 -> {card.title}")
        if card.title.startswith("成婚"):
            break
    entry = await repo.get_harem(GROUP, TARGET)
    check("娶群友 5 次内必中", bool(entry and entry.owner_uid == UID), f"owner={entry.owner_uid if entry else None}")
    expect_card("我的后宫", await marriage.do_harem(ctx, UID, "测试群友", GROUP))
    expect_card("恩爱榜", await marriage.do_love_ranking(ctx, GROUP))
    expect_card("公共老婆", await marriage.do_public_wives(ctx, GROUP))

    # ---------------------------------------------------------- 离婚 / NTR
    print("\n[7] 离婚 / NTR")
    for i in range(2):
        card = await marriage.do_divorce(ctx, UID, "测试群友", GROUP, TARGET, "测试老婆", use_card=(i == 0))
        print(f"  离婚第 {i + 1} 次 -> {card.title}")
        if card.title.startswith("离婚成功"):
            break
    entry = await repo.get_harem(GROUP, TARGET)
    check("离婚 2 次内必成", bool(entry and not entry.owner_uid))

    # 先重新娶回来，再让另一个用户 NTR
    await repo.add_coins(UID, 2000)
    for _ in range(5):
        if (await repo.get_harem(GROUP, TARGET)).owner_uid == UID:
            break
        await marriage.do_marry(ctx, UID, "测试群友", GROUP, TARGET, "测试老婆", use_card=False)
    other = "40004"
    await repo.get_user(other, "隔壁老王")
    await repo.add_coins(other, 5000)
    await repo.add_items(other, {"card_green": 10})
    for i in range(10):
        card = await marriage.do_ntr(ctx, other, "隔壁老王", GROUP, TARGET, "测试老婆", use_card=True)
        print(f"  NTR 第 {i + 1} 次 -> {card.title}")
        if card.title.startswith("NTR 成功"):
            break
    entry = await repo.get_harem(GROUP, TARGET)
    check("NTR 10 次内必中", bool(entry and entry.owner_uid == other))

    # ---------------------------------------------------------- 商店
    print("\n[8] 商店 / 购买")
    expect_card("商店", await shop.do_shop(ctx, UID, "测试群友", GROUP))
    before = (await repo.get_user(UID)).coins
    expect_card("购买 红虫 x2", await shop.do_buy(ctx, UID, "测试群友", GROUP, "红虫 2"))
    after = (await repo.get_user(UID)).coins
    check("购买扣费正确", before - after == 18 * 2, f"{before}->{after}")
    try:
        await shop.do_buy(ctx, UID, "测试群友", GROUP, "娶群友卡 1")
        check("非卖品必须拒绝", False)
    except WhaleError:
        check("非卖品必须拒绝", True)

    # ---------------------------------------------------------- 道具
    print("\n[9] 使用道具")
    expect_card("使用 水库体验券", await item_svc.do_use(ctx, UID, "测试群友", GROUP, "水库一天体验券", "", "", ""))
    check("体验券 buff 生效", await repo.has_buff(UID, "ticket_reservoir", ""))
    expect_card("使用 钟情卡 @目标", await item_svc.do_use(ctx, UID, "测试群友", GROUP, "钟情卡", TARGET, "测试老婆", ""))
    check("钟情卡 buff 生效", await repo.has_buff(TARGET, "loyal", GROUP))

    # 天气控制机（3 天）
    await repo.add_items(UID, {"weather_controller": 1})
    expect_card("使用 天气控制机 晴天", await item_svc.do_use(ctx, UID, "测试群友", GROUP, "天气控制机 晴天", "", "", ""))
    pond_after = await repo.get_pond(GROUP)
    check("天气控制机生效", pond_after.weather == "sunny" and pond_after.weather_until > 0,
          f"{pond_after.weather}/{pond_after.weather_until}")

    # ---------------------------------------------------------- 面板
    print("\n[10] 面板 / 仓库 / 新数值")
    expect_card("我的", await profile.do_profile(ctx, UID, "测试群友", GROUP))
    expect_card("仓库", await warehouse.do_warehouse(ctx, UID, "测试群友", GROUP))

    # 喂食自动购买：设置偏好只改配置，不消耗饭点
    expect_card("喂食 自动 白米饭", await feed.do_feed(ctx, UID, "测试群友", GROUP, "", "自动 白米饭"))
    check("喂食自动购买偏好已保存", (await repo.get_user(UID)).feed_auto_food == "rice_bowl",
          (await repo.get_user(UID)).feed_auto_food)

    # 新数值：饭比小紫饵料便宜、打工 1h~24h、池塘材料更丰富
    importlib.import_module(f"{PKG}.core.constants")
    rice = importlib.import_module(f"{PKG}.data.items").get_item("rice_bowl")
    worm = importlib.import_module(f"{PKG}.data.items").get_item("bait_worm")
    check("米饭不再比紫饵贵", rice.price < worm.price, f"{rice.price} vs {worm.price}")
    check("打工时长 1h~24h", ctx.cfg.work_cooldown_min == 60 and ctx.cfg.work_cooldown_max == 1440,
          f"{ctx.cfg.work_cooldown_min}-{ctx.cfg.work_cooldown_max}")
    drops_mod = importlib.import_module(f"{PKG}.data.drops")
    mats = set()
    for lv in range(1, 11):
        mats.update(str(x) for x in drops_mod.MATERIAL_CHOICES[lv])
    check("池塘材料 ≥ 6 种", len(mats) >= 6, str(sorted(mats)))

    # ---------------------------------------------------------- 连接池/收尾
    stats = db.stats()
    print(f"\n连接池: {stats}")
    await db.close()
    print("\n" + ("=" * 52))
    if _failures:
        print(f"失败 {len(_failures)} 项:")
        for f in _failures:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)