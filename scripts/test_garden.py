"""花园系统离线验收测试 —— 覆盖 ``docs/GARDEN_SPEC.md`` §4 的 1~9 项。

用法::

    python scripts/test_garden.py

设计要点（不依赖 AstrBot / QQ / Pillow）：
    * 临时 SQLite + 真 ``Repo`` / ``SvcCtx`` / ``services.garden`` / ``ui.garden``，
      和 smoke_test.py 同一套装配方式；
    * 时间不 sleep：把 ``services.garden.now_ts`` 换成可拨的 ``Clock``（只改测试进程里
      的模块属性，不动生产代码），需要"几天没浇水"时直接构造 ``Garden.watered_ts`` /
      ``planted_ts`` 再 ``save_garden``；
    * 晒太阳要按配置时区判断小时：测试里把 ``cfg.garden_timezone`` 置空（走服务器本地
      时间）+ 用本地时刻构造时间戳，结果在任何时区的机器上都可复现；时段本身用
      ``cfg.garden_sun_start/end`` 控制（默认 0~24，时间窗用例临时改回 7~19）；
    * 天气是随机的：用池塘的 ``weather`` 覆盖强制"晴天 / 小雨"，保证天气差异可断言；
    * 所有断言走 ``check()``，打印 PASS/FAIL，退出码 0（全过）/ 1（有失败）。

只读生产代码，不修改任何生产文件。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_package() -> str:
    """把插件目录当作包导入（core/services 内部全是相对导入）。

    与 smoke_test.py 完全同样的做法：父目录进 sys.path，再以 ``<目录名>.<子模块>`` 导入。
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
        if str(ROOT) in list(getattr(pkg, "__path__", []) or []):
            return name
    raise SystemExit(
        "无法把插件目录作为包导入。\n"
        f"  目录: {ROOT}\n"
        f"  试过: {tried}\n"
        f"  最后错误: {last_error!r}\n"
        "请确认目录名是合法 Python 标识符，且本文件位于 <插件目录>/scripts/ 下。"
    )


PKG = _bootstrap_package()

_config = importlib.import_module(f"{PKG}.core.config")
_context = importlib.import_module(f"{PKG}.core.context")
_database = importlib.import_module(f"{PKG}.core.db.database")
_repos = importlib.import_module(f"{PKG}.core.db.repos")
_errors = importlib.import_module(f"{PKG}.core.errors")
_llm = importlib.import_module(f"{PKG}.core.llm")
_models = importlib.import_module(f"{PKG}.core.models")
_time = importlib.import_module(f"{PKG}.core.time_utils")
_schema = importlib.import_module(f"{PKG}.ui.schema")

C = importlib.import_module(f"{PKG}.core.constants")
plants = importlib.import_module(f"{PKG}.data.plants")
garden_items = importlib.import_module(f"{PKG}.data.garden_items")
items = importlib.import_module(f"{PKG}.data.items")
garden_service = importlib.import_module(f"{PKG}.services.garden")

PluginConfig = _config.PluginConfig
SvcCtx = _context.SvcCtx
Database = _database.Database
Repo = _repos.Repo
WhaleError = _errors.WhaleError
LLMClient = _llm.LLMClient
Card = _schema.Card
Garden = _models.Garden
day_key = _time.day_key

# 基准时间：本地 2026-05-20 08:00（游戏日从凌晨 4 点算，08:00 不会踩到跨日边界）
BASE = int(datetime(2026, 5, 20, 8, 0, 0).timestamp())
_BASE_DAY = datetime(2026, 5, 20)

_checks = 0
_failures: list[str] = []


# ---------------------------------------------------------------- 断言 / 工具
def check(name: str, cond: bool, detail: str = "") -> bool:
    """唯一的断言入口：打印 PASS/FAIL，失败只登记不中断（最后统一退出 1）。"""
    global _checks
    _checks += 1
    if cond:
        print(f"  [PASS] {name}")
        return True
    text = f"  [FAIL] {name}"
    if detail:
        text += f"  <- {detail}"
    print(text)
    _failures.append(f"{name}  <- {detail}" if detail else name)
    return False


async def expect_error(label: str, coro, *, contains: str = "") -> str:
    """期待一次业务异常（WhaleError）；返回错误文案，没抛或抛错类型都记 FAIL。"""
    try:
        await coro
    except WhaleError as exc:
        message = str(getattr(exc, "message", "") or exc)
        check(label, contains in message if contains else True, f"实际报错：{message}")
        return message
    except Exception as exc:  # noqa: BLE001 - 测试里把非业务异常也记成 FAIL，方便定位
        check(label, False, f"抛了非 WhaleError：{type(exc).__name__}: {exc}")
        return ""
    check(label, False, "没有抛出 WhaleError")
    return ""


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def local_ts(hour: int, minute: int = 0) -> int:
    """构造"本地时间 hour:minute"的时间戳（配合 cfg.garden_timezone="" 使用）。"""
    return int(_BASE_DAY.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def cap_of(plant_grade: str) -> int:
    """按数据层的取档规则扫出"突变封顶值"，与 services.garden._mutation_cap 同口径。"""
    best = plants.PLANT_SEED_RANGE[plant_grade][0]
    for mutation in range(101):
        if plants.roll_seed_for_plant(plant_grade, mutation) == best:
            return mutation
    return 100


class Clock:
    """可拨的假时钟：整个验收过程替换 ``services.garden.now_ts``。"""

    def __init__(self, value: int) -> None:
        self.value = int(value)

    def now_ts(self) -> int:
        return int(self.value)

    def set(self, ts: int) -> None:
        self.value = int(ts)

    def advance(self, seconds: int) -> None:
        self.value += int(seconds)


class Harness:
    """临时库 + Repo 的轻封装：直接造 Garden / 读背包 / 强制天气。"""

    def __init__(self, ctx: SvcCtx, clock: Clock) -> None:
        self.ctx = ctx
        self.clock = clock

    @property
    def repo(self):
        return self.ctx.repo

    async def put_garden(
        self,
        uid: str,
        group_id: str,
        plant_id: str,
        *,
        growth: int = 0,
        mutation: int = 0,
        anchor: int | None = None,
        planted_ts: int | None = None,
        watered_ts: int = 0,
        sun_ts: int | None = None,
        dead: int = 0,
        seed_id: str = "seed_e",
    ) -> Garden:
        """直接落一株花园（测试专用：growth / planted_ts / watered_ts 想设多少设多少）。"""
        now = self.clock.value
        anchor = now if anchor is None else int(anchor)
        garden = Garden(
            group_id=group_id,
            uid=uid,
            seed_id=seed_id,
            plant_id=plant_id,
            planted_ts=now if planted_ts is None else int(planted_ts),
            growth=int(growth),
            mutation=int(mutation),
            stage=min(len(C.GARDEN_STAGES) - 1, int(growth) // C.GARDEN_STAGE_CAP),
            watered_ts=int(watered_ts),
            sun_ts=anchor if sun_ts is None else int(sun_ts),
            last_n=anchor,
            last_p=anchor,
            last_k=anchor,
            dead=int(dead),
            created_at=fmt_ts(anchor),
        )
        await self.repo.save_garden(garden)
        return garden

    async def load(self, uid: str, group_id: str) -> Garden | None:
        return await self.repo.get_garden(group_id, uid)

    async def bag_items(self, uid: str) -> dict[str, int]:
        return dict((await self.repo.get_bag(uid)).items or {})

    async def force_weather(self, group_id: str, key: str, *, days: int = 7) -> None:
        pond = await self.repo.get_pond(group_id)
        pond.weather = key
        pond.weather_until = self.clock.value + int(days) * 86400
        await self.repo.save_pond(pond)


# ---------------------------------------------------------------- [0] 数据层自检
async def case_00_data(h: Harness) -> None:
    print("\n[0] 数据层自检（种子概率 / 品质区间 / 果实回收价）")

    for grade in C.GARDEN_SEED_GRADES:
        weights = plants.SEED_GRADE_WEIGHTS.get(grade) or {}
        check(
            f"{grade} 级种子概率合计 100",
            abs(sum(weights.values()) - 100.0) < 1e-6,
            str(sum(weights.values())),
        )

    # B 级：大红 0.5%（≤1%）、小红 0（"小红从 C 起"）
    b_weights = plants.SEED_GRADE_WEIGHTS.get("B") or {}
    check("B 级出大红概率 ≤ 1%（实际 0.5%）",
          0.0 < float(b_weights.get(C.RARITY_DAHONG, 0.0)) <= 1.0,
          str(b_weights.get(C.RARITY_DAHONG)))
    check("B 级不出小红（0%）", float(b_weights.get(C.RARITY_XIAOHONG, 0.0)) == 0.0,
          str(b_weights.get(C.RARITY_XIAOHONG)))

    # C 级：保底小紫及以上 → 最低档是小紫，且小红概率 < 1%
    c_weights = plants.SEED_GRADE_WEIGHTS.get("C") or {}
    check("C 级种子最低为小紫（不出大蓝 / 小蓝）",
          set(c_weights) <= {C.RARITY_XIAOHONG, C.RARITY_DAZI, C.RARITY_XIAOZI},
          str(c_weights))
    check("C 级出小红概率 < 1%（实际 0.3%）",
          0.0 < float(c_weights.get(C.RARITY_XIAOHONG, 0.0)) < 1.0,
          str(c_weights.get(C.RARITY_XIAOHONG)))
    check(
        "星彩只能由 S 级种子种出",
        all(
            C.RARITY_STAR not in (plants.SEED_GRADE_WEIGHTS[grade] or {})
            for grade in C.GARDEN_SEED_GRADES
            if grade != "S"
        ),
    )

    expect_ranges = {
        C.RARITY_XIAOLAN: ("B", "E"),
        C.RARITY_DALAN: ("B", "E"),
        C.RARITY_XIAOZI: ("A", "C"),
        C.RARITY_DAZI: ("S", "C"),
        C.RARITY_XIAOHONG: ("S", "B"),
        C.RARITY_DAHONG: ("S", "A"),
        C.RARITY_STAR: ("S", "S"),
    }
    check("PLANT_SEED_RANGE 与规范 1.4 一致", plants.PLANT_SEED_RANGE == expect_ranges)

    bad_fruit: list[str] = []
    for plant in plants.all_plants():
        fruit = items.get_item(plant.fruit_item)
        if fruit is None:
            bad_fruit.append(f"{plant.id}: 缺少 {plant.fruit_item}")
            continue
        if fruit.category != items.CAT_CROP:
            bad_fruit.append(f"{plant.id}: 类别不是 CAT_CROP")
        if fruit.sell != garden_items.CROP_SELL_BY_GRADE[plant.grade]:
            bad_fruit.append(
                f"{plant.id}: sell={fruit.sell} 应为 {garden_items.CROP_SELL_BY_GRADE[plant.grade]}"
            )
    check(
        f"全部 {len(plants.PLANTS)} 种植物都有果实、卖价按品质",
        not bad_fruit,
        "; ".join(bad_fruit[:5]),
    )

    for grade in C.GARDEN_PLANT_GRADES:
        check(f"{grade} 品质植物数量 ≥ 6", len(plants.plants_by_grade(grade)) >= 6,
              f"{len(plants.plants_by_grade(grade))}")

    expect_sell = {
        C.RARITY_XIAOLAN: 4,
        C.RARITY_DALAN: 7,
        C.RARITY_XIAOZI: 10,
        C.RARITY_DAZI: 18,
        C.RARITY_XIAOHONG: 40,
        C.RARITY_DAHONG: 120,
        C.RARITY_STAR: 500,
    }
    check("果实回收价与规范 2.3 一致（小蓝 4 / 大蓝 7 / … / 星彩 500）",
          garden_items.CROP_SELL_BY_GRADE == expect_sell,
          str(garden_items.CROP_SELL_BY_GRADE))

    seed_d = items.get_item("seed_d")
    seed_e = items.get_item("seed_e")
    check(
        "D / E 种子售价 = 8 / 4",
        seed_d is not None and seed_e is not None and (seed_d.price, seed_e.price) == (8, 4),
        f"{getattr(seed_d, 'price', None)} / {getattr(seed_e, 'price', None)}",
    )
    check(
        "S / A / B / C 种子不可购买（price=0）",
        all((items.get_item(sid) is not None and items.get_item(sid).price == 0)
            for sid in ("seed_s", "seed_a", "seed_b", "seed_c")),
    )
    check(
        "肥料售价 = 氮 4 / 磷 6 / 钾 5",
        all(getattr(items.get_item(fid), "price", None) == price
            for fid, price in (("fert_n", 4), ("fert_p", 6), ("fert_k", 5))),
    )

    # §5：买 E 种子不能"果实期望 < 售价"（本轮定价 E=4，果实期望 5.95）
    def expected_fruit(seed_grade: str) -> float:
        weights = plants.SEED_GRADE_WEIGHTS.get(seed_grade) or {}
        return sum(w * garden_items.CROP_SELL_BY_GRADE[g] for g, w in weights.items()) / 100.0

    e_fruit = expected_fruit("E")
    check(
        f"E 种子期望果实 {e_fruit:.2f} ≥ 售价 {seed_e.price if seed_e else '?'}",
        seed_e is not None and e_fruit >= seed_e.price,
        f"{e_fruit:.2f} vs {getattr(seed_e, 'price', None)}",
    )
    d_fruit = expected_fruit("D")
    # D 单轮果实 7.69 < 8，靠收获返还种子回正；这里只记录，不做"必须 ≥ 售价"的硬断言
    print(f"        · D 种子期望果实 {d_fruit:.2f}（售价 8，单轮略亏，靠返还种子回正）")

    # 任意突变都必须落在该品质的种子区间内，且随突变升高只会变好、不会变差
    off_range: list[str] = []
    not_monotonic: list[str] = []
    for grade in C.GARDEN_PLANT_GRADES:
        tiers = plants.seed_grades_for_plant(grade)
        previous = len(tiers)                      # 0 = 最好档，越大越差
        for mutation in range(101):
            tier = plants.roll_seed_for_plant(grade, mutation)
            if tier not in tiers:
                off_range.append(f"{grade}/{mutation}->{tier}")
                continue
            index = tiers.index(tier)
            if index > previous:
                not_monotonic.append(f"{grade}/{mutation}->{tier}")
            previous = index
    check("任意突变都落在植物种子区间内", not off_range, "; ".join(off_range[:5]))
    check("突变越高种子档位只升不降", not not_monotonic, "; ".join(not_monotonic[:5]))
    check(
        "星彩（S~S）任意突变都只出 S",
        all(plants.roll_seed_for_plant(C.RARITY_STAR, mutation) == "S"
            for mutation in (0, 30, 50, 84, 100)),
    )


# ---------------------------------------------------------------- [1] 播种
async def case_01_sow(h: Harness) -> None:
    print("\n[1] 播种：阶段=播种期，品质未公开（规范 §4.1）")
    uid, gid = "u-sow", "g-sow"
    await h.repo.add_items(uid, {"seed_e": 5})

    card = await garden_service.do_sow(h.ctx, uid, "播种者", gid, "")
    check(
        "/播种 不带参数：返回背包种子卡",
        isinstance(card, Card) and "背包种子" in card.title,
        getattr(card, "title", ""),
    )
    card = await garden_service.do_sow(h.ctx, uid, "播种者", gid, "E")
    check("/播种 E：返回 Card", isinstance(card, Card), type(card).__name__)

    garden = await h.load(uid, gid)
    if not check("播种后花园里有植物", garden is not None and bool(garden.plant_id),
                 f"garden={garden}"):
        return
    plant = plants.get_plant(garden.plant_id)
    check("播种后成长=0、阶段=播种期", int(garden.growth) == 0 and int(garden.stage) == 0,
          f"growth={garden.growth} stage={garden.stage}")
    check("播种消耗 1 颗种子（5 -> 4）", (await h.bag_items(uid)).get("seed_e") == 4,
          str(await h.bag_items(uid)))

    card = await garden_service.do_garden(h.ctx, uid, "播种者", gid)
    text = card.to_text()
    check("总览卡：品质未公开（???）", "???" in text)
    check("总览卡：阶段=播种期", "播种期" in text)
    check("总览卡：不泄露植物真名", plant.name not in text, f"plant={plant.name}")

    await expect_error(
        "已有存活植物时再播种报错",
        garden_service.do_sow(h.ctx, uid, "播种者", gid, "E"),
        contains="已经养着",
    )


# ---------------------------------------------------------------- [2] 发芽
async def case_02_sprout(h: Harness) -> None:
    print("\n[2] 成长到 100：发芽期，品质公开（规范 §4.2）")
    uid, gid = "u-sow", "g-sow"
    garden = await h.load(uid, gid)
    if garden is None or not garden.plant_id:
        check("前置：播种用例留下了植物", False)
        return
    plant = plants.get_plant(garden.plant_id)

    garden.growth = 100
    garden.planted_ts = h.clock.value
    await h.repo.save_garden(garden)

    card = await garden_service.do_garden(h.ctx, uid, "播种者", gid)
    text = card.to_text()
    check("阶段=发芽期", "发芽期" in text, text[:60])
    check("品质公开：植物名 + 品质都出现", plant.name in text and plant.grade in text,
          f"{plant.name}/{plant.grade}")
    check("不再出现 ???", "???" not in text)

    garden = await h.load(uid, gid)
    check("stage 字段刷新为 1", int(garden.stage) == 1, f"stage={garden.stage}")


# ---------------------------------------------------------------- [3] 浇水
async def case_03_water(h: Harness) -> None:
    print("\n[3] 浇水：1 小时冷却、当天第 4 次起突变 +0、窄卡（规范 §4.3）")
    uid, gid = "u-water", "g-water"
    base = BASE
    h.clock.set(base)
    await h.put_garden(uid, gid, "rice", growth=0)

    card = await garden_service.do_water(h.ctx, uid, "浇水者", gid)
    garden = await h.load(uid, gid)
    check("第 1 次浇水返回 Card", isinstance(card, Card))
    check("浇水卡是窄卡：blocks / stats 均为空", card.blocks == [] and card.stats == [],
          f"blocks={len(card.blocks)} stats={len(card.stats)}")
    check("浇水卡 accent 是花园绿", card.accent == C.GARDEN_ACCENT, card.accent)
    check("浇水 +8 成长 / +2 突变", (int(garden.growth), int(garden.mutation)) == (8, 2),
          f"{garden.growth}/{garden.mutation}")

    h.clock.set(base + 60)
    await expect_error(
        "冷却内（+60 秒）再浇水报错",
        garden_service.do_water(h.ctx, uid, "浇水者", gid),
        contains="刚浇过水",
    )
    check("冷却内报错不改变成长", int((await h.load(uid, gid)).growth) == 8)

    h.clock.set(base + 3600)
    await garden_service.do_water(h.ctx, uid, "浇水者", gid)
    garden = await h.load(uid, gid)
    check("满 1 小时可以再浇（含被动 +2 共 18）", int(garden.growth) == 18,
          f"growth={garden.growth}")
    check("第 2 次突变累计 +4", int(garden.mutation) == 4, f"mutation={garden.mutation}")

    h.clock.set(base + 7200)
    await garden_service.do_water(h.ctx, uid, "浇水者", gid)
    garden = await h.load(uid, gid)
    check("第 3 次突变累计 +6", int(garden.mutation) == 6, f"mutation={garden.mutation}")

    h.clock.set(base + 10800)
    card = await garden_service.do_water(h.ctx, uid, "浇水者", gid)
    garden = await h.load(uid, gid)
    check("当天第 4 次：突变不再增长（仍为 6）", int(garden.mutation) == 6,
          f"mutation={garden.mutation}")
    check("当天第 4 次：成长仍然增加（38）", int(garden.growth) == 38,
          f"growth={garden.growth}")
    check("第 4 次卡片写着 成长 +8 / 突变 +0",
          "成长 +8" in card.subtitle and "突变 +0" in card.subtitle, card.subtitle)


async def case_03b_water_compare(h: Harness) -> None:
    print("\n[3b] §3.3 硬检查：同一天浇水 1 次 vs 3 次")
    base = BASE
    h.clock.set(base)
    await h.put_garden("u-w1", "g-w1", "rice", growth=0)
    await h.put_garden("u-w3", "g-w3", "rice", growth=0)

    await garden_service.do_water(h.ctx, "u-w1", "浇一次", "g-w1")
    for index in range(3):
        h.clock.set(base + index * 3600)
        await garden_service.do_water(h.ctx, "u-w3", "浇三次", "g-w3")

    one = await h.load("u-w1", "g-w1")
    three = await h.load("u-w3", "g-w3")
    check("浇水 3 次成长更高（差值 ≥ 16）",
          int(three.growth) - int(one.growth) >= 16,
          f"{one.growth} -> {three.growth}")
    check("浇水 3 次突变更高", int(three.mutation) > int(one.mutation),
          f"{one.mutation} -> {three.mutation}")


# ---------------------------------------------------------------- [4] 施肥
async def case_04_fertilize(h: Harness) -> None:
    print("\n[4] 施肥：每日 2 次上限 + 三种肥料收益侧重 + 必须消耗肥料（规范 §4.4）")
    h.clock.set(BASE)
    fert_key = {"氮": "fert_n", "磷": "fert_p", "钾": "fert_k"}
    gains: dict[str, tuple[int, int]] = {}

    # 用小紫（速度 s=1.0），动作收益与规范 §3.2 的 20 / 8 / 12 完全一致
    for fert in ("氮", "磷", "钾"):
        uid, gid = f"u-fert-{fert_key[fert]}", f"g-fert-{fert_key[fert]}"
        await h.put_garden(uid, gid, "rice", growth=0)
        await h.repo.add_items(uid, {"fert_n": 3, "fert_p": 3, "fert_k": 3})

        card = await garden_service.do_fertilize(h.ctx, uid, f"施{fert}肥", gid, fert)
        garden = await h.load(uid, gid)
        gains[fert] = (int(garden.growth), int(garden.mutation))
        check(f"{fert}肥：返回 Card 且生效",
              isinstance(card, Card) and gains[fert] != (0, 0), str(gains[fert]))
        check(f"{fert}肥：消耗对应肥料 1 个",
              (await h.bag_items(uid)).get(fert_key[fert]) == 2,
              str(await h.bag_items(uid)))

    n_growth, n_mut = gains["氮"]
    p_growth, p_mut = gains["磷"]
    k_growth, k_mut = gains["钾"]
    check("氮肥成长最高、突变最少（20/1）", (n_growth, n_mut) == (20, 1),
          f"{gains['氮']}")
    check("磷肥突变最高、成长最少（8/5）", (p_growth, p_mut) == (8, 5),
          f"{gains['磷']}")
    check("钾肥均衡（12/3）", (k_growth, k_mut) == (12, 3), f"{gains['钾']}")
    check("三种肥料的 (成长, 突变) 各不相同",
          len({gains["氮"], gains["磷"], gains["钾"]}) == 3, str(gains))

    # 每日 2 次上限
    uid_lim, gid_lim = "u-fert-limit", "g-fert-limit"
    await h.put_garden(uid_lim, gid_lim, "rice", growth=0)
    await h.repo.add_items(uid_lim, {"fert_n": 5, "fert_k": 5})
    await garden_service.do_fertilize(h.ctx, uid_lim, "上限", gid_lim, "氮")
    await garden_service.do_fertilize(h.ctx, uid_lim, "上限", gid_lim, "钾")
    await expect_error(
        "同一天第 3 次施肥报错",
        garden_service.do_fertilize(h.ctx, uid_lim, "上限", gid_lim, "磷"),
        contains="2 次",
    )
    limit_garden = await h.load(uid_lim, gid_lim)
    check("第 3 次报错不消耗任何肥料",
          (await h.bag_items(uid_lim)).get("fert_k") == 4
          and (await h.bag_items(uid_lim)).get("fert_p") is None,
          str(await h.bag_items(uid_lim)))

    # 不施肥的对照组
    await h.put_garden("u-fert-none", "g-fert-none", "rice", growth=0)
    none_garden = await h.load("u-fert-none", "g-fert-none")
    check("施肥 2 次比不施肥成长更高",
          int(limit_garden.growth) > int(none_garden.growth),
          f"施肥 {limit_garden.growth} vs 不施肥 {none_garden.growth}")

    # 没有肥料时不能施肥
    await h.put_garden("u-fert-empty", "g-fert-empty", "rice", growth=0)
    await expect_error(
        "背包没有肥料时施肥报错",
        garden_service.do_fertilize(h.ctx, "u-fert-empty", "没肥料", "g-fert-empty", "氮"),
        contains="没有足够",
    )
    empty_garden = await h.load("u-fert-empty", "g-fert-empty")
    check("施不了肥时不涨成长 / 突变",
          (int(empty_garden.growth), int(empty_garden.mutation)) == (0, 0),
          f"{empty_garden.growth}/{empty_garden.mutation}")

    # 无参 /施肥：只有一种肥料时用它；有两种以上时要求选择（不再默认氮肥）
    uid_one, gid_one = "u-fert-one", "g-fert-one"
    await h.put_garden(uid_one, gid_one, "rice", growth=0)
    await h.repo.add_items(uid_one, {"fert_p": 1})
    await garden_service.do_fertilize(h.ctx, uid_one, "只有磷", gid_one, "")
    one = await h.load(uid_one, gid_one)
    check("无参 /施肥：背包只有一种肥料时直接用它（磷 8/5）",
          (int(one.growth), int(one.mutation)) == (8, 5),
          f"{one.growth}/{one.mutation}")

    uid_two, gid_two = "u-fert-two", "g-fert-two"
    await h.put_garden(uid_two, gid_two, "rice", growth=0)
    await h.repo.add_items(uid_two, {"fert_p": 2, "fert_k": 2})
    await expect_error(
        "无参 /施肥：有磷+钾但没有氮时要求选择，不再默认氮",
        garden_service.do_fertilize(h.ctx, uid_two, "两种肥", gid_two, ""),
        contains="要施哪种肥",
    )
    two = await h.load(uid_two, gid_two)
    check("提示选择时不涨成长 / 突变、不消耗肥料",
          (int(two.growth), int(two.mutation)) == (0, 0)
          and (await h.bag_items(uid_two)).get("fert_p") == 2,
          f"{two.growth}/{two.mutation} bag={await h.bag_items(uid_two)}")


# ---------------------------------------------------------------- [5] 晒太阳
async def case_05_sun(h: Harness) -> None:
    print("\n[5] 晒太阳：时间窗 / 每日 1 次 / 天气差异（规范 §4.5）")
    ctx = h.ctx
    h.clock.set(BASE)

    # ---- 时间窗：临时改成 7:00~19:00
    ctx.cfg.garden_sun_start, ctx.cfg.garden_sun_end = 7, 19
    uid, gid = "u-sun-window", "g-sun-window"
    await h.put_garden(uid, gid, "rice", growth=0)

    h.clock.set(local_ts(3))
    await expect_error(
        "凌晨 3 点不能晒太阳",
        garden_service.do_sun(ctx, uid, "晒太阳", gid),
        contains="不是晒太阳的时间",
    )
    check("时段外不涨成长", int((await h.load(uid, gid)).growth) == 0)

    h.clock.set(local_ts(12))
    card = await garden_service.do_sun(ctx, uid, "晒太阳", gid)
    garden = await h.load(uid, gid)
    check("中午 12 点可以晒太阳", isinstance(card, Card))
    check("记录 sun_ts / sun_day",
          int(garden.sun_ts) == h.clock.value and garden.sun_day == day_key(h.clock.value),
          f"sun_ts={garden.sun_ts} sun_day={garden.sun_day}")
    await expect_error(
        "同一天第 2 次晒太阳报错",
        garden_service.do_sun(ctx, uid, "晒太阳", gid),
        contains="今天已经晒过",
    )
    ctx.cfg.garden_sun_start, ctx.cfg.garden_sun_end = 0, 24

    # ---- 天气差异：池塘覆盖强制晴天 / 小雨
    h.clock.set(BASE)
    await h.put_garden("u-sun-sunny", "g-sun-sunny", "rice", growth=0)
    await h.put_garden("u-sun-rain", "g-sun-rain", "rice", growth=0)
    await h.force_weather("g-sun-sunny", "sunny")
    await h.force_weather("g-sun-rain", "rain")

    card_sunny = await garden_service.do_sun(ctx, "u-sun-sunny", "晴天组", "g-sun-sunny")
    card_rain = await garden_service.do_sun(ctx, "u-sun-rain", "雨天组", "g-sun-rain")
    sunny = await h.load("u-sun-sunny", "g-sun-sunny")
    rain = await h.load("u-sun-rain", "g-sun-rain")
    check("晴天卡标题带天气", "晴天" in card_sunny.title, card_sunny.title)
    check("小雨卡标题带天气", "小雨" in card_rain.title, card_rain.title)
    check("晴天 +15 成长 / 小雨 +5 成长",
          int(sunny.growth) == 15 and int(rain.growth) == 5,
          f"晴 {sunny.growth} / 雨 {rain.growth}")
    check("晴天突变高于小雨", int(sunny.mutation) > int(rain.mutation),
          f"{sunny.mutation} / {rain.mutation}")


# ---------------------------------------------------------------- [6] 缺水 / 枯萎
async def case_06_thirst(h: Harness) -> None:
    print("\n[6] 缺水 2 天 / 枯萎 5 天（构造时间戳，规范 §4.6）")
    ctx = h.ctx
    h.clock.set(BASE)

    # 1 天：还没到缺水
    await h.put_garden("u-thirst-1", "g-thirst-1", "rice", growth=100, anchor=BASE,
                       watered_ts=BASE - 86400)
    card = await garden_service.do_garden(ctx, "u-thirst-1", "一天", "g-thirst-1")
    check("1 天没浇水不算缺水", "缺水" not in card.to_text())
    await garden_service.do_water(ctx, "u-thirst-1", "一天", "g-thirst-1")
    check("1 天没浇水：浇水收益仍是 +8",
          int((await h.load("u-thirst-1", "g-thirst-1")).growth) == 108,
          f"growth={(await h.load('u-thirst-1', 'g-thirst-1')).growth}")

    # 3 天：缺水，成长收益 -50%
    await h.put_garden("u-thirst-3", "g-thirst-3", "rice", growth=100, anchor=BASE,
                       watered_ts=BASE - 3 * 86400)
    card = await garden_service.do_garden(ctx, "u-thirst-3", "三天", "g-thirst-3")
    text = card.to_text()
    check("3 天没浇水进入缺水", "缺水" in text)
    check("面板提示成长收益 ×50%", "×50%" in text, text[:80])
    await garden_service.do_water(ctx, "u-thirst-3", "三天", "g-thirst-3")
    thirsty_gain = int((await h.load("u-thirst-3", "g-thirst-3")).growth) - 100
    check("缺水时浇水只 +4（-50%）", thirsty_gain == 4, f"gain={thirsty_gain}")
    check("缺水收益低于健康收益（4 < 8）", thirsty_gain < 8, f"{thirsty_gain}")

    # 被动成长也吃状态惩罚：同样挂了 4 小时，缺水组只 +4、健康组 +8
    four_hours_ago = BASE - 4 * 3600
    await h.put_garden("u-passive-thirst", "g-passive-thirst", "rice", growth=100,
                       planted_ts=four_hours_ago, anchor=BASE,
                       watered_ts=BASE - 3 * 86400)
    await h.put_garden("u-passive-ok", "g-passive-ok", "rice", growth=100,
                       planted_ts=four_hours_ago, anchor=BASE, watered_ts=BASE)
    await garden_service.do_garden(ctx, "u-passive-thirst", "被动缺水", "g-passive-thirst")
    await garden_service.do_garden(ctx, "u-passive-ok", "被动健康", "g-passive-ok")
    passive_thirst = int((await h.load("u-passive-thirst", "g-passive-thirst")).growth) - 100
    passive_ok = int((await h.load("u-passive-ok", "g-passive-ok")).growth) - 100
    check("被动成长也吃缺水惩罚（4 小时只 +4）", passive_thirst == 4, f"gain={passive_thirst}")
    check("健康对照 4 小时被动 +8", passive_ok == 8, f"gain={passive_ok}")

    # 5 天：枯萎
    await h.put_garden("u-wither", "g-wither", "rice", growth=100, anchor=BASE,
                       watered_ts=BASE - 5 * 86400)
    await expect_error(
        "5 天没浇水：浇水报枯萎",
        garden_service.do_water(ctx, "u-wither", "枯萎", "g-wither"),
        contains="枯萎",
    )
    withered = await h.load("u-wither", "g-wither")
    check("枯萎状态已落库", int(withered.dead) == 1, f"dead={withered.dead}")
    card = await garden_service.do_garden(ctx, "u-wither", "枯萎", "g-wither")
    check("总览面板显示枯萎", "枯萎" in card.to_text())

    before_items = await h.bag_items("u-wither")
    card = await garden_service.do_harvest(ctx, "u-wither", "枯萎", "g-wither")
    check("枯萎后收获返回 0", card.badge == "0" and "枯萎" in card.title,
          f"{card.title} / {card.badge}")
    check("枯萎收获后花园清空", await h.load("u-wither", "g-wither") is None)
    check("枯萎收获不给果实 / 种子", await h.bag_items("u-wither") == before_items,
          f"{before_items} -> {await h.bag_items('u-wither')}")


# ---------------------------------------------------------------- [7] 收获
async def case_07_harvest(h: Harness) -> None:
    print("\n[7] 收获：满 600 才可收，产出果实 + 区间内种子（规范 §4.7）")
    ctx = h.ctx
    h.clock.set(BASE)

    await h.put_garden("u-harv-599", "g-harv-599", "foxtail", growth=599, mutation=0)
    await expect_error(
        "成长 599 不能收获",
        garden_service.do_harvest(ctx, "u-harv-599", "差一点", "g-harv-599"),
        contains="还没成熟",
    )
    check("未成熟收获后花园还在", await h.load("u-harv-599", "g-harv-599") is not None)

    await h.put_garden("u-harv-best", "g-harv-best", "foxtail", growth=600, mutation=100)
    card = await garden_service.do_harvest(ctx, "u-harv-best", "满成长", "g-harv-best")
    bag = await h.bag_items("u-harv-best")
    check("成熟收获返回 Card", isinstance(card, Card))
    check("收获得到 1 个果实", bag.get("crop_foxtail") == 1, str(bag))
    check("突变 100 -> 最好档种子 B", bag.get("seed_b") == 1, str(bag))
    check("种子档位在植物区间内（小蓝 B~E）",
          "B" in plants.seed_grades_for_plant("小蓝"))
    check("收获后花园清空", await h.load("u-harv-best", "g-harv-best") is None)

    await h.put_garden("u-harv-worst", "g-harv-worst", "foxtail", growth=600, mutation=0)
    await garden_service.do_harvest(ctx, "u-harv-worst", "零突变", "g-harv-worst")
    bag = await h.bag_items("u-harv-worst")
    check("突变 0 -> 最差档种子 E", bag.get("seed_e") == 1, str(bag))

    expected_mid = plants.roll_seed_for_plant("小蓝", 50)
    await h.put_garden("u-harv-mid", "g-harv-mid", "foxtail", growth=600, mutation=50)
    card = await garden_service.do_harvest(ctx, "u-harv-mid", "中突变", "g-harv-mid")
    bag = await h.bag_items("u-harv-mid")
    check(f"突变 50 -> 数据层档位 {expected_mid}",
          bag.get(f"seed_{expected_mid.lower()}") == 1, str(bag))
    check("收获卡 badge 就是种子档位", card.badge == expected_mid, card.badge)


# ---------------------------------------------------------------- [8] 突变封顶
async def case_08_mutation_cap(h: Harness) -> None:
    print("\n[8] 突变封顶后不再增长（成长仍涨，规范 §4.8）")
    ctx = h.ctx
    h.clock.set(BASE)

    cap = cap_of("大红")
    check("大红突变封顶 = 50（S~A 两档，100 才够 S，50 已取到 S）",
          cap == 50, f"cap={cap}")

    await h.put_garden("u-cap", "g-cap", "epiphyllum", growth=100, mutation=cap - 2)
    await garden_service.do_water(ctx, "u-cap", "封顶", "g-cap")
    garden = await h.load("u-cap", "g-cap")
    check("封顶前最后一次浇水把突变推到封顶值",
          int(garden.mutation) == cap, f"mutation={garden.mutation}")
    check("这次仍然实加 2 点突变", int(garden.mutation) - (cap - 2) == 2)
    growth_before = int(garden.growth)

    h.clock.advance(3600)
    card = await garden_service.do_water(ctx, "u-cap", "封顶", "g-cap")
    garden = await h.load("u-cap", "g-cap")
    check("封顶后浇水突变不涨", int(garden.mutation) == cap, f"mutation={garden.mutation}")
    check("封顶后浇水成长仍涨", int(garden.growth) > growth_before,
          f"{growth_before} -> {garden.growth}")
    check("封顶后卡片显示「突变已封顶」", "突变已封顶" in card.subtitle, card.subtitle)

    card = await garden_service.do_garden(ctx, "u-cap", "封顶", "g-cap")
    check(f"面板显示突变上限（{cap}/{cap}）", f"{cap}/{cap}" in card.to_text())

    # 星彩 S~S：区间只有一档，突变恒为 0（同样满足"达到上限后不再增长"）
    await h.put_garden("u-star", "g-star", "snow_lotus", growth=100, mutation=0)
    await garden_service.do_water(ctx, "u-star", "星彩", "g-star")
    star = await h.load("u-star", "g-star")
    check("星彩（S~S）突变不增长", int(star.mutation) == 0, f"mutation={star.mutation}")
    check("星彩浇水仍涨成长", int(star.growth) > 100, f"growth={star.growth}")


# ---------------------------------------------------------------- [9] 6 入口 / 窄卡
async def case_09_cards(h: Harness) -> None:
    print("\n[9] 6 个入口都返回 Card，浇水卡明显更矮（规范 §4.9）")
    ctx = h.ctx
    ctx.cfg.garden_sun_start, ctx.cfg.garden_sun_end = 0, 24
    h.clock.set(BASE)
    uid, gid = "u-all", "g-all"
    await h.repo.add_items(uid, {"seed_d": 1, "fert_n": 1})

    cards: dict[str, Card] = {}
    cards["播种(列表)"] = await garden_service.do_sow(ctx, uid, "全流程", gid, "")
    cards["播种"] = await garden_service.do_sow(ctx, uid, "全流程", gid, "D")
    cards["养植物"] = await garden_service.do_garden(ctx, uid, "全流程", gid)
    cards["浇水"] = await garden_service.do_water(ctx, uid, "全流程", gid)
    cards["施肥"] = await garden_service.do_fertilize(ctx, uid, "全流程", gid, "氮")
    cards["晒太阳"] = await garden_service.do_sun(ctx, uid, "全流程", gid)

    garden = await h.load(uid, gid)
    garden.growth = 600
    garden.planted_ts = h.clock.value
    await h.repo.save_garden(garden)
    cards["收获"] = await garden_service.do_harvest(ctx, uid, "全流程", gid)

    for label, card in cards.items():
        check(f"/{label} 返回 Card：《{card.title}》",
              isinstance(card, Card) and bool(card.title), type(card).__name__)
        check(f"/{label} 卡片 accent 是花园绿", card.accent == C.GARDEN_ACCENT, card.accent)

    entrances = {"养植物", "播种", "浇水", "施肥", "晒太阳", "收获"}
    check("6 个入口全部覆盖", entrances <= set(cards), str(entrances - set(cards)))

    water = cards["浇水"]
    panel = cards["养植物"]
    check("浇水卡 blocks 为空", water.blocks == [], f"blocks={len(water.blocks)}")
    check("浇水卡 stats 为空", water.stats == [])
    water_lines = len(water.to_text().splitlines())
    panel_lines = len(panel.to_text().splitlines())
    check("浇水卡文本行数不到总览卡的一半", water_lines * 2 < panel_lines,
          f"{water_lines} vs {panel_lines}")


# ---------------------------------------------------------------- 汇总
def _summary() -> int:
    print("\n" + "=" * 60)
    if _failures:
        print(f"FAIL：{_checks} 项检查中有 {len(_failures)} 项未通过")
        for failure in _failures:
            print("  -", failure)
        return 1
    print(f"PASS：{_checks} 项检查全部通过 ✅")
    return 0


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="whale_garden_test_"))
    db = Database(tmp / "garden.db", max_connections=4)
    await db.init()

    cfg = PluginConfig()
    cfg.llm_enabled = False
    cfg.garden_sun_start = 0       # 默认放宽时段，时间窗用例再临时改回 7~19
    cfg.garden_sun_end = 24
    cfg.garden_timezone = ""       # 空 -> 走服务器本地时区，构造的本地时刻才可复现

    repo = Repo(db, cfg)
    ctx = SvcCtx(repo=repo, cfg=cfg, llm=LLMClient(None, cfg))
    clock = Clock(BASE)
    harness = Harness(ctx, clock)

    print("=" * 60)
    print("花园系统离线验收（GARDEN_SPEC §4 第 1~9 项）")
    print(f"临时库：{db.path}")
    print(f"基准时间：{fmt_ts(BASE)}（本地时区）")

    original_now_ts = garden_service.now_ts
    garden_service.now_ts = clock.now_ts   # 只改测试进程的模块属性，不动生产代码
    try:
        for case in (
            case_00_data,
            case_01_sow,
            case_02_sprout,
            case_03_water,
            case_03b_water_compare,
            case_04_fertilize,
            case_05_sun,
            case_06_thirst,
            case_07_harvest,
            case_08_mutation_cap,
            case_09_cards,
        ):
            await case(harness)
    except Exception:  # noqa: BLE001 - 运行期异常也要收敛成退出码 1
        traceback.print_exc()
        _failures.append("测试运行时异常（见 traceback）")
        print("\n  [FAIL] 测试异常中断")
    finally:
        garden_service.now_ts = original_now_ts
        await db.close()

    return _summary()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
