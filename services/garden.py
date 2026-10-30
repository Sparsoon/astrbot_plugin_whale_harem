"""花园（养植物）服务 —— 6 个入口 + 内部惰性结算。

契约：``docs/GARDEN_SPEC.md`` 第 3 节。设计要点：
    * 全部时间用整数时间戳 + 惰性比较，**没有任何定时器 / 轮询 / sleep**；
    * 每日上限一律用 ``day_key()``（凌晨 4 点重置）判断，跨天自动清零；
    * 数值集中在本文件顶部；``core/constants.py`` 只读；
    * 状态（缺水/缺氮/缺磷/缺钾/缺光）只影响"后续动作"的成长收益，
      不会倒扣已经获得的成长值；
    * 被动成长按整小时结算、单次最多补 72 小时，防止离线暴涨。

关于时间戳字段（schema 没有单独的"上次结算"列，这里做明确约定）：
    * ``planted_ts``  = **被动成长结算标记**（播种时等于播种时间，
      每次结算只前进"已发放的整小时"，保留不足一小时的余量）；
    * ``created_at``  = **播种时间**（ISO 字符串，用于面板"播种"一栏，
      同时在老数据没有该值时用 ``planted_ts`` 兜底）；
    * ``watered_ts`` / ``sun_ts`` / ``last_n|p|k`` = 各项动作的最后时间。
    这样既满足"惰性按小时结算"，也不需要改动任何数据层文件。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from ..core import constants as C
from ..core import rng
from ..core import weather as weather_mod
from ..core.context import SvcCtx
from ..core.errors import NotEnoughItem, WhaleError
from ..core.models import Garden
from ..core.time_utils import day_key, days_between, fmt_date, now_ts
from ..data.items import CAT_SEED, get_item, name_of, resolve_item
from ..data import plants as plant_data
from ..ui import garden as garden_ui
from ..ui.schema import Card

# ---------------------------------------------------------------- 数值（唯一来源）
GROWTH_MAX = C.GARDEN_GROWTH_MAX              # 600
STAGE_CAP = C.GARDEN_STAGE_CAP                # 每阶段 100
STAGES = C.GARDEN_STAGES

PASSIVE_PER_HOUR = 2                          # 被动成长 +2/小时
PASSIVE_CATCHUP_HOURS = 72                    # 单次最多补 72 小时
SECONDS_PER_HOUR = 3600

WATER_COOLDOWN_SECONDS = 3600                 # 浇水冷却 1 小时
WATER_GROWTH = 8                              # 浇水 +8 成长
WATER_MUTATION = 2                            # 浇水 +2 突变
WATER_MUTATION_FREE_AFTER = 3                 # 当天第 4 次起突变 +0

FERT_DAILY_LIMIT = 2                          # 施肥每天 2 次
FERT_SPECS: dict[str, tuple[str, str, int, int]] = {
    # key: (物品 id, 面板名, 成长, 突变)
    "n": ("fert_n", "氮肥", 20, 1),           # 成长多、突变少
    "p": ("fert_p", "磷肥", 8, 5),            # 突变多、成长少
    "k": ("fert_k", "钾肥", 12, 3),           # 均衡
}

SUN_DAILY_LIMIT = 1                           # 晒太阳每天 1 次
# 天气 -> (成长, 突变)：天气 key 见 core/weather.py
SUN_WEATHER_GAINS: dict[str, tuple[int, int]] = {
    "sunny": (15, 3),
    "cloudy": (10, 2),
    "rain": (5, 0),
    "storm": (2, -2),
    "fog": (6, 1),
    "meteor": (18, 5),
    "cold": (3, 0),
}
SUN_DEFAULT_GAIN = (5, 0)                     # 未知天气的兜底

THIRST_DAYS = 2                               # ≥2 天没浇水：缺水（成长 -50%）
WITHER_DAYS = 5                               # ≥5 天没浇水：枯萎
FERT_LACK_DAYS = 3                            # 对应肥料 ≥3 天没施：缺氮/磷/钾
FERT_LACK_STEP = 0.15                         # 每种 -15%
FERT_LACK_CAP = 0.30                          # 最多 -30%
DARK_DAYS = 2                                 # ≥2 天没晒太阳：缺光
THIRST_GROWTH_MULT = 0.50
DARK_GROWTH_MULT = 0.30

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_DATE_FORMAT = "%Y-%m-%d"

# 突变封顶按"植物品质的种子区间上限"换算，同一品质只需算一次
_MUTATION_CAP_CACHE: dict[tuple[str, str, str], int] = {}

_FERT_ALIASES = {
    "n": "n", "氮": "n", "氮肥": "n", "fert_n": "n", "成长": "n",
    "p": "p", "磷": "p", "磷肥": "p", "fert_p": "p", "突变": "p",
    "k": "k", "钾": "k", "钾肥": "k", "fert_k": "k", "均衡": "k",
}


# ---------------------------------------------------------------- 时间工具
def _fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime(_TS_FORMAT)
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_ts(text: object) -> int:
    raw = str(text or "").strip()
    if not raw:
        return 0
    for fmt in (_TS_FORMAT, _DATE_FORMAT):
        try:
            return int(datetime.strptime(raw, fmt).timestamp())
        except (ValueError, OverflowError, OSError):
            continue
    return 0


def _days_since(ts: int, now: int) -> int:
    """两个时间戳之间隔了几个游戏日（4 点重置），用于"几天没做某事"。"""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return max(0, days_between(day_key(ts), day_key(int(now))))


def _date_text(ts: int) -> str:
    if ts <= 0:
        return "-"
    return fmt_date(day_key(int(ts)))


def _ago_text(ts: int, now: int) -> str:
    if ts <= 0:
        return "从未"
    days = _days_since(ts, now)
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


# ---------------------------------------------------------------- 状态
@dataclass(slots=True)
class _Status:
    """一次结算后的植物状态；``mult`` 是后续动作的成长倍率。"""

    thirsty: bool = False
    withered: bool = False
    lack_n: bool = False
    lack_p: bool = False
    lack_k: bool = False
    dark: bool = False
    lack_count: int = 0
    mult: float = 1.0
    labels: list[str] = field(default_factory=list)
    penalty_text: str = "成长收益正常"


def _status_of(garden: Garden, now: int, sow_ts: int) -> _Status:
    status = _Status()
    water_ref = int(garden.watered_ts) if int(garden.watered_ts) > 0 else sow_ts
    thirst_days = _days_since(water_ref, now)

    status.withered = bool(garden.dead) or thirst_days >= WITHER_DAYS
    status.thirsty = (not status.withered) and thirst_days >= THIRST_DAYS

    labels: list[str] = []
    if status.withered:
        labels.append("枯萎")
        status.mult = 0.0
        status.penalty_text = "无法成长，只能重新播种"
        status.labels = labels
        return status

    if status.thirsty:
        labels.append("缺水")
    for key, flag_name in (("n", "lack_n"), ("p", "lack_p"), ("k", "lack_k")):
        last = int(getattr(garden, f"last_{key}") or 0)
        base = last if last > 0 else sow_ts
        if _days_since(base, now) >= FERT_LACK_DAYS:
            setattr(status, flag_name, True)
            status.lack_count += 1
            labels.append(f"缺{ {'n': '氮', 'p': '磷', 'k': '钾'}[key] }")
    sun_ref = int(garden.sun_ts) if int(garden.sun_ts) > 0 else sow_ts
    status.dark = _days_since(sun_ref, now) >= DARK_DAYS
    if status.dark:
        labels.append("缺光")

    mult = 1.0
    if status.thirsty:
        mult *= THIRST_GROWTH_MULT
    if status.lack_count:
        mult *= 1.0 - min(FERT_LACK_CAP, FERT_LACK_STEP * status.lack_count)
    if status.dark:
        mult *= 1.0 - DARK_GROWTH_MULT
    status.mult = max(0.0, mult)
    status.labels = labels
    if mult < 1.0:
        status.penalty_text = f"成长收益 ×{int(round(mult * 100))}%"
    else:
        status.penalty_text = "成长收益正常"
    return status


# ---------------------------------------------------------------- 成长 / 阶段
def _stage_index(growth: int) -> int:
    try:
        value = int(growth)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(len(STAGES) - 1, value // STAGE_CAP))


def _growth_speed(plant) -> float:
    """按植物品种的基准生长时间给成长速度：常见作物快、传说植物慢。"""
    minutes = int(getattr(plant, "grow_minutes", 0) or 0)
    if minutes <= 0:
        return 1.0
    ratio = 300.0 / max(60, minutes)          # 300 分钟（小紫）为基准
    return max(0.45, min(1.6, ratio ** 0.5))


def _scale(base: int, mult: float, speed: float = 1.0) -> int:
    """按状态倍率与品种速度缩放动作收益，至少给 1 点（避免减益把动作变成无效）。"""
    if base <= 0:
        return 0
    value = int(base * max(0.0, float(mult)) * max(0.0, float(speed)) + 0.5)
    return max(1, value)


def _sow_reference(garden: Garden) -> int:
    """播种时间：取 created_at 与 planted_ts 里更早的那个（兼容老数据 / 测试构造）。"""
    candidates = [t for t in (_parse_ts(garden.created_at), int(garden.planted_ts)) if t > 0]
    return min(candidates) if candidates else 0


def _settle(garden: Garden, now: int) -> tuple[Garden, _Status, bool]:
    """惰性结算：被动成长 + 枯萎判定 + 阶段刷新。返回 (garden, status, 是否有改动)。"""
    changed = False
    now = int(now)

    ref = _sow_reference(garden)
    if ref <= 0 or ref > now:
        ref = now
        changed = True
    stamp = _fmt_ts(ref)
    if stamp and garden.created_at != stamp:
        garden.created_at = stamp
        changed = True

    status = _status_of(garden, now, ref)

    if not garden.dead:
        marker = int(garden.planted_ts)
        if marker <= 0 or marker > now:
            # 没有有效标记：从当前时间重新起算，绝不按 epoch 补成长
            garden.planted_ts = now
            changed = True
        else:
            elapsed_hours = (now - marker) // SECONDS_PER_HOUR
            if elapsed_hours > 0:
                capped = elapsed_hours > PASSIVE_CATCHUP_HOURS
                grant_hours = PASSIVE_CATCHUP_HOURS if capped else elapsed_hours
                speed = _growth_speed(_get_plant(garden.plant_id))
                gain = int(round(grant_hours * PASSIVE_PER_HOUR * speed * status.mult))
                if int(garden.growth) < GROWTH_MAX:
                    garden.growth = min(GROWTH_MAX, int(garden.growth) + gain)
                # 只前进"已发放的整小时"，保留不足一小时的余量；
                # 触发 72 小时封顶时直接对齐到现在，避免离线补过头；
                # 若这一轮算出 0（低速度+多重减益），不推进标记，把小时留到下次。
                if gain > 0 or capped:
                    garden.planted_ts = now if capped else marker + grant_hours * SECONDS_PER_HOUR
                changed = True

    if status.withered and not garden.dead:
        garden.dead = 1
        changed = True

    stage = _stage_index(garden.growth)
    if int(garden.stage) != stage:
        garden.stage = stage
        changed = True
    return garden, status, changed


# ---------------------------------------------------------------- 品质 / 种子
def _get_plant(plant_id: str):
    if not plant_id:
        return None
    fn = getattr(plant_data, "get_plant", None)
    if callable(fn):
        try:
            return fn(str(plant_id))
        except Exception:  # noqa: BLE001 - 数据层异常不该拖垮指令
            return None
    return (getattr(plant_data, "PLANTS", {}) or {}).get(str(plant_id))


def _plants_of_grade(grade: str) -> tuple:
    fn = getattr(plant_data, "plants_by_grade", None)
    if callable(fn):
        try:
            found = tuple(fn(str(grade)))
            if found:
                return found
        except Exception:  # noqa: BLE001
            pass
    plants = getattr(plant_data, "PLANTS", {}) or {}
    return tuple(p for p in plants.values() if getattr(p, "grade", "") == grade)


def _seed_grade(item_id: str) -> str:
    """seed_s -> S；也兼容"名称里带 S 级"的写法。"""
    low = str(item_id or "").lower()
    if low.startswith("seed_"):
        grade = low[5:].upper()
        if grade in C.GARDEN_SEED_GRADES:
            return grade
    item = get_item(str(item_id))
    if item is not None:
        name = str(getattr(item, "name", "") or "").upper().replace(" ", "")
        for grade in C.GARDEN_SEED_GRADES:
            if f"{grade}级" in name:
                return grade
    return ""


def _seed_item_id(grade: str) -> str:
    return f"seed_{str(grade or '').strip().lower()}"


def _seed_name(item_id: str, grade: str = "") -> str:
    name = name_of(str(item_id))
    if name and name != item_id:
        return name
    return f"{grade} 级种子" if grade else "种子"


def _is_seed(item_id: str) -> bool:
    item = get_item(str(item_id))
    if item is not None and getattr(item, "category", "") == CAT_SEED:
        return True
    return str(item_id or "").lower().startswith("seed_")


def _range_for_grade(grade: str) -> tuple[str, str]:
    ranges = getattr(plant_data, "PLANT_SEED_RANGE", {}) or {}
    pair = ranges.get(str(grade))
    if isinstance(pair, (tuple, list)) and len(pair) == 2:
        return str(pair[0]), str(pair[1])
    for plant in _plants_of_grade(grade):
        return str(getattr(plant, "seed_best", "") or "S"), str(getattr(plant, "seed_worst", "") or "E")
    return "S", "E"


def _seed_range(plant) -> tuple[str, str]:
    best = str(getattr(plant, "seed_best", "") or "")
    worst = str(getattr(plant, "seed_worst", "") or "")
    if not best or not worst:
        fallback_best, fallback_worst = _range_for_grade(str(getattr(plant, "grade", "") or ""))
        best = best or fallback_best
        worst = worst or fallback_worst
    return best, worst


def _grade_span(best: str, worst: str) -> tuple[str, ...]:
    order = C.GARDEN_SEED_GRADES
    try:
        start = order.index(str(best))
    except ValueError:
        start = 0
    try:
        end = order.index(str(worst))
    except ValueError:
        end = len(order) - 1
    if start > end:
        start, end = end, start
    return tuple(order[start : end + 1])


def _cap_from_span(count: int) -> int:
    """把"区间档位数"换算成"达到最好档位所需的最小突变值"。

    与数据层 ``roll_seed_for_plant`` 的线性取档一致：
        n 档时，mutation = 100*(n-1.5)/(n-1) 恰好够到最好档；
    例如 S~C（4 档）：0→C、34→B、67→A、84→S，封顶值 84。
    """
    if count <= 1:
        return 0
    raw = 100.0 * (count - 1.5) / (count - 1)
    return max(0, min(C.GARDEN_MUTATION_CAP, int(math.ceil(raw))))


def _cap_from_data(plant, best: str) -> int | None:
    """用数据层的 roll_seed_for_plant 反推封顶值（保证与真实取档完全一致）。"""
    fn = getattr(plant_data, "roll_seed_for_plant", None)
    if not callable(fn):
        return None
    grade = str(getattr(plant, "grade", "") or "")

    def roll(mutation: int) -> str:
        # 不传 rng：中点取档固定向"更好的一档"取整，封顶值才是确定的
        return str(fn(grade, int(mutation)))

    try:
        if roll(C.GARDEN_MUTATION_CAP) != best:
            return C.GARDEN_MUTATION_CAP
        low, high = 0, C.GARDEN_MUTATION_CAP
        while low < high:
            mid = (low + high) // 2
            if roll(mid) == best:
                high = mid
            else:
                low = mid + 1
        return low
    except Exception:  # noqa: BLE001
        return None


def _mutation_cap(plant) -> int:
    """突变封顶值：达到该植物品质种子区间最好档位后不再增长。"""
    if plant is None:
        return C.GARDEN_MUTATION_CAP
    best, worst = _seed_range(plant)
    key = (str(getattr(plant, "grade", "") or ""), best, worst)
    cached = _MUTATION_CAP_CACHE.get(key)
    if cached is not None:
        return cached
    span = _grade_span(best, worst)
    cap = 0 if len(span) <= 1 else _cap_from_span(len(span))
    from_data = _cap_from_data(plant, best) if len(span) > 1 else 0
    if from_data is not None:
        cap = from_data
    _MUTATION_CAP_CACHE[key] = cap
    return cap


def _add_mutation(garden: Garden, plant, delta: int) -> int:
    """加/减突变并封顶；返回实际变化量（封顶后为 0）。"""
    old = max(0, int(garden.mutation))
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        delta = 0
    if delta > 0:
        cap = _mutation_cap(plant)
        new = min(cap, old + delta) if old < cap else old
    else:
        new = max(0, old + delta)
    garden.mutation = new
    return new - old


def _roll_plant_for_seed(seed_grade: str):
    fn = getattr(plant_data, "roll_plant_for_seed", None)
    if callable(fn):
        plant = None
        try:
            plant = fn(seed_grade, rng.rng())
        except TypeError:
            plant = fn(seed_grade)
        if plant is not None:
            return plant
    return _fallback_roll_plant(seed_grade)


def _fallback_roll_plant(seed_grade: str):
    """数据层函数缺失时的兜底：按 SEED_GRADE_WEIGHTS 加权。"""
    weights = (getattr(plant_data, "SEED_GRADE_WEIGHTS", {}) or {}).get(str(seed_grade)) or {}
    table = {
        str(grade): float(weight)
        for grade, weight in weights.items()
        if float(weight or 0) > 0 and _plants_of_grade(str(grade))
    }
    if not table:
        raise WhaleError(
            "花园种子数据还没准备好",
            hint="稍后再试；如果一直这样，请管理员检查 data/plants.py",
            commands=["/养植物"],
        )
    grade = rng.pick_weighted(table)
    return rng.choice(_plants_of_grade(str(grade)))


def _roll_seed_for_plant(plant_grade: str, mutation: int) -> str:
    fn = getattr(plant_data, "roll_seed_for_plant", None)
    if callable(fn):
        # 不传 rng：收获档位只由突变值决定（可复现、便于验收）
        try:
            seed_grade = fn(str(plant_grade), int(mutation))
        except (TypeError, ValueError):
            seed_grade = ""
        if seed_grade:
            return str(seed_grade)
    best, worst = _range_for_grade(plant_grade)
    span = _grade_span(best, worst)
    if len(span) <= 1:
        return span[0]
    index = int(int(mutation) * (len(span) - 1) / 100.0 + 0.5)
    return span[max(0, min(len(span) - 1, index))]


# ---------------------------------------------------------------- 种子解析
def _grade_from_text(text: str) -> str:
    value = str(text or "").strip().upper().replace(" ", "")
    for suffix in ("级种子", "种子", "级", "的"):
        value = value.replace(suffix, "")
    return value if value in C.GARDEN_SEED_GRADES else ""


def _seed_entries(bag) -> list[garden_ui.SeedEntry]:
    entries: list[garden_ui.SeedEntry] = []
    for item_id, count in (bag.items or {}).items():
        if int(count) <= 0 or not _is_seed(item_id):
            continue
        grade = _seed_grade(item_id)
        entries.append(
            garden_ui.SeedEntry(
                name=_seed_name(item_id, grade),
                count=int(count),
                grade=grade or "?",
            )
        )
    order = {grade: idx for idx, grade in enumerate(C.GARDEN_SEED_GRADES)}
    entries.sort(key=lambda e: (order.get(e.grade, 99), e.name))
    return entries


def _pick_seed(text: str, bag) -> tuple[str, str, str]:
    """把用户输入解析成背包里的一种种子，返回 (seed_id, 显示名, 档位)。"""
    seeds = {
        item_id: int(count)
        for item_id, count in (bag.items or {}).items()
        if int(count) > 0 and _is_seed(item_id)
    }
    if not seeds:
        raise NotEnoughItem(
            "种子",
            1,
            hint="可以 /商店 买 D 级 / E 级种子，钓鱼和收获也会给种子",
        )

    raw = str(text or "").strip()
    low = raw.lower()
    if low in seeds:
        grade = _seed_grade(low)
        return low, _seed_name(low, grade), grade

    grade = _grade_from_text(raw)
    if grade:
        seed_id = _seed_item_id(grade)
        if seed_id in seeds:
            return seed_id, _seed_name(seed_id, grade), grade

    # 只接受"完整 id / 完整名字 / 完整别名"，绝不做子串猜测（否则 /播种 种子 会吞掉 S 种子）
    hits: list[str] = []
    for item_id in seeds:
        item = get_item(item_id)
        keys = {item_id.lower()}
        if item is not None:
            keys.add(item.name.strip().lower())
            keys.update(str(a).strip().lower() for a in item.aliases)
        if low in keys:
            hits.append(item_id)
    if len(hits) == 1:
        item_id = hits[0]
        grade = _seed_grade(item_id)
        return item_id, _seed_name(item_id, grade), grade
    if len(hits) > 1:
        names = "、".join(_seed_name(x, _seed_grade(x)) for x in hits)
        raise WhaleError(
            f"「{raw}」对应多种种子：{names}",
            hint="请写完整种子名，例如 /播种 S 级种子",
            commands=["/播种"],
        )

    raise WhaleError(
        f"背包里没有「{raw}」这种种子",
        hint="不带参数的 /播种 会列出背包里所有种子",
        commands=["/播种"],
    )


# ---------------------------------------------------------------- 天气 / 配置
async def _weather_for(ctx: SvcCtx, group_id: str, now: int):
    """按群取当前天气；天气控制机的覆盖优先。取不到池塘时退回自然天气。"""
    override, until = "", 0
    try:
        pond = await ctx.repo.get_pond(group_id)
        override = str(getattr(pond, "weather", "") or "")
        until = int(getattr(pond, "weather_until", 0) or 0)
    except Exception:  # noqa: BLE001 - 花园不依赖池塘，失败就用自然天气
        override, until = "", 0
    scope = str(group_id or "garden")
    return weather_mod.resolve(scope, int(now), override, until)




def _local_hour(ctx: SvcCtx, now: int) -> int:
    """按配置时区取本地小时；zoneinfo 不可用时退回服务器本地时间。"""
    tz_name = str(getattr(ctx.cfg, "garden_timezone", "") or "")
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(int(now), ZoneInfo(tz_name)).hour
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(int(now)).hour

def _sun_hours(ctx: SvcCtx) -> tuple[int, int]:
    cfg = ctx.cfg
    start = int(getattr(cfg, "garden_sun_start", C.GARDEN_SUN_START_HOUR))
    end = int(getattr(cfg, "garden_sun_end", C.GARDEN_SUN_END_HOUR))
    start = max(0, min(23, start))
    end = max(1, min(24, end))
    if end <= start:
        return C.GARDEN_SUN_START_HOUR, C.GARDEN_SUN_END_HOUR
    return start, end


# ---------------------------------------------------------------- 面板视图
def _progress_text(garden: Garden, stage: int) -> str:
    if garden.dead:
        return "已经枯萎，只能重新播种"
    if int(garden.growth) >= GROWTH_MAX:
        return "已经成熟，可以 /收获"
    if stage >= len(STAGES) - 1:
        return f"距离成熟还差 {max(1, GROWTH_MAX - int(garden.growth))} 成长"
    next_stage = STAGES[min(len(STAGES) - 1, stage + 1)]
    need = (stage + 1) * STAGE_CAP - int(garden.growth)
    return f"到{next_stage}还差 {max(1, need)} 成长"


def _view(
    garden: Garden,
    plant,
    status: _Status,
    now: int,
    *,
    owner: str,
    weather_text: str,
    sun_start: int,
    sun_end: int,
) -> garden_ui.GardenView:
    stage = _stage_index(garden.growth)
    revealed = int(garden.growth) >= STAGE_CAP
    plant_name = ""
    grade = ""
    if revealed:
        plant_name = str(getattr(plant, "name", "") or garden.plant_id or "")
        grade = str(getattr(plant, "grade", "") or "")
    return garden_ui.GardenView(
        owner=owner,
        revealed=revealed,
        plant_name=plant_name,
        grade=grade,
        stage_name=STAGES[stage],
        stage_index=stage,
        growth=max(0, min(GROWTH_MAX, int(garden.growth))),
        growth_max=GROWTH_MAX,
        mutation=max(0, min(C.GARDEN_MUTATION_CAP, int(garden.mutation))),
        mutation_cap=_mutation_cap(plant) if revealed else 0,
        status_text="、".join(status.labels) if status.labels else "健康",
        penalty_text=status.penalty_text,
        weather_text=weather_text,
        sow_text=_date_text(_parse_ts(garden.created_at) or int(garden.planted_ts)),
        water_text=_ago_text(int(garden.watered_ts), now),
        fert_text=_ago_text(max(int(garden.last_n), int(garden.last_p), int(garden.last_k)), now),
        sun_text=_ago_text(int(garden.sun_ts), now),
        progress_text=_progress_text(garden, stage),
        dead=bool(garden.dead),
        sun_start=sun_start,
        sun_end=sun_end,
    )


# ---------------------------------------------------------------- 读写助手
async def _load_garden(ctx: SvcCtx, group_id: str, uid: str) -> Garden:
    garden = await ctx.repo.get_garden(group_id, uid)
    if garden is None or garden.is_empty() or not garden.plant_id:
        raise WhaleError(
            "你还没有养植物",
            hint="先 /播种 一株；不带参数的 /播种 会列出背包里的种子",
            commands=["/播种"],
        )
    return garden


async def _load_alive(
    ctx: SvcCtx, group_id: str, uid: str, now: int
) -> tuple[Garden, _Status, object | None]:
    """读 + 惰性结算 + 枯萎检查；成功路径由调用方负责 save。"""
    garden = await _load_garden(ctx, group_id, uid)
    garden, status, _changed = _settle(garden, now)
    if garden.dead:
        # 枯萎状态要落库，避免用户反复看到"刚枯"的旧数据
        await ctx.repo.save_garden(garden)
        raise WhaleError(
            "这株植物已经枯萎了",
            hint="/收获 清理掉它，然后 /播种 重新开始",
            commands=["/收获", "/播种"],
        )
    return garden, status, _get_plant(garden.plant_id)


# ---------------------------------------------------------------- 6 个入口
async def do_garden(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """``/养植物``：帮助（6 条指令）+ 总体状态（阶段/成长/突变/状态/天气）。"""
    now = now_ts()
    sun_start, sun_end = _sun_hours(ctx)
    weather = await _weather_for(ctx, group_id, now)
    weather_text = f"{weather.emoji} {weather.name}"

    garden = await ctx.repo.get_garden(group_id, uid)
    if garden is None or garden.is_empty() or not garden.plant_id:
        view = garden_ui.GardenView(
            owner=name,
            stage_name="空地",
            stage_index=0,
            growth=0,
            growth_max=GROWTH_MAX,
            mutation=0,
            status_text="还没有植物，先 /播种 一株",
            penalty_text="正常",
            weather_text=weather_text,
            sow_text="-",
            water_text="从未",
            fert_text="从未",
            sun_text="从未",
            progress_text="",
            sun_start=sun_start,
            sun_end=sun_end,
        )
        return garden_ui.panel(view, empty=True)

    garden, status, changed = _settle(garden, now)
    if changed:
        await ctx.repo.save_garden(garden)
    plant = _get_plant(garden.plant_id)
    view = _view(
        garden,
        plant,
        status,
        now,
        owner=name,
        weather_text=weather_text,
        sun_start=sun_start,
        sun_end=sun_end,
    )
    return garden_ui.panel(view)


async def do_sow(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str = "") -> Card:
    """``/播种``：不带参数列出背包种子；带参数种下指定种子。"""
    repo = ctx.repo
    now = now_ts()
    sun_start, sun_end = _sun_hours(ctx)
    text = (arg or "").strip()

    if not text:
        bag = await repo.get_bag(uid)
        return garden_ui.seed_list_card(name, _seed_entries(bag))

    existing = await repo.get_garden(group_id, uid)
    if (
        existing is not None
        and not existing.is_empty()
        and existing.plant_id
        and not existing.dead
    ):
        raise WhaleError(
            "你已经养着一株植物了",
            hint="先用 /收获 收掉它，或者等它枯萎后再 /播种",
            commands=["/养植物", "/收获"],
        )

    bag = await repo.get_bag(uid)
    seed_id, seed_name, seed_grade = _pick_seed(text, bag)
    plant = _roll_plant_for_seed(seed_grade)
    plant_id = str(getattr(plant, "id", "") or "")
    if not plant_id:
        raise WhaleError("这颗种子暂时种不出来", hint="换一颗种子，或稍后再试")

    if existing is not None:
        # 枯萎/异常残留的旧行必须删掉，否则新植物会继承旧的 created_at 直接再枯萎
        await repo.delete_garden(group_id, uid)

    ok, _bag = await repo.consume_items(uid, {seed_id: 1})
    if not ok:
        raise NotEnoughItem(
            seed_name,
            1,
            hint="不带参数的 /播种 可以看背包里还有哪些种子",
        )

    planted = Garden(
        group_id=group_id,
        uid=uid,
        seed_id=seed_id,
        plant_id=plant_id,
        planted_ts=now,
        growth=0,
        mutation=0,
        stage=0,
        created_at=_fmt_ts(now),
        dead=0,
    )
    await repo.save_garden(planted)
    return garden_ui.sow_card(seed_name, seed_grade, sun_start=sun_start, sun_end=sun_end)


async def do_water(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """``/浇水``：每小时 1 次，+8 成长 / +2 突变（当天第 4 次起突变 +0）。"""
    now = now_ts()
    garden, status, plant = await _load_alive(ctx, group_id, uid, now)
    if int(garden.growth) >= GROWTH_MAX:
        raise WhaleError("已经成熟了，先 /收获 吧", hint="收获后才能重新播种", commands=["/收获"])

    last_water = int(garden.watered_ts)
    if last_water > 0 and now - last_water < WATER_COOLDOWN_SECONDS:
        wait = WATER_COOLDOWN_SECONDS - (now - last_water)
        minutes = max(1, (wait + 59) // 60)
        raise WhaleError(
            f"刚浇过水，{minutes} 分钟后才能再浇",
            hint="每小时只能浇 1 次，可以先 /施肥 或 /晒太阳",
            commands=["/施肥", "/晒太阳", "/养植物"],
        )

    today = day_key(now)
    water_count = (int(garden.water_count) if garden.watered_day == today else 0) + 1

    growth_gain = _scale(WATER_GROWTH, status.mult, _growth_speed(plant))
    before = int(garden.growth)
    garden.growth = min(GROWTH_MAX, before + growth_gain)
    growth_gain = int(garden.growth) - before

    mutation_delta = WATER_MUTATION if water_count <= WATER_MUTATION_FREE_AFTER else 0
    mutation_gain = _add_mutation(garden, plant, mutation_delta)

    garden.watered_ts = now
    garden.watered_day = today
    garden.water_count = water_count
    garden.stage = _stage_index(garden.growth)
    await ctx.repo.save_garden(garden)

    return garden_ui.water_card(
        growth=garden.growth,
        growth_max=GROWTH_MAX,
        growth_gain=growth_gain,
        mutation_gain=mutation_gain,
        water_count=water_count,
        mutation_capped=mutation_delta > 0 and mutation_gain == 0,
    )


async def do_fertilize(
    ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str = ""
) -> Card:
    """``/施肥 氮|磷|钾``：每天 2 次，三种肥料成长/突变侧重不同。"""
    now = now_ts()
    garden, status, plant = await _load_alive(ctx, group_id, uid, now)
    if int(garden.growth) >= GROWTH_MAX:
        raise WhaleError("已经成熟了，先 /收获 吧", hint="成熟后施肥会被浪费", commands=["/收获"])

    today = day_key(now)
    used = int(garden.fert_count) if garden.fert_day == today else 0
    if used >= FERT_DAILY_LIMIT:
        raise WhaleError(
            f"今天已经施过 {FERT_DAILY_LIMIT} 次肥了",
            hint="每天最多 2 次，明天再来；现在可以 /浇水 或 /晒太阳",
            commands=["/浇水", "/晒太阳", "/养植物"],
        )

    text = (arg or "").strip()
    key = _normalize_fert(text)
    bag = await ctx.repo.get_bag(uid)
    if key is None:
        if text:
            raise WhaleError(
                f"没有「{text}」这种肥料",
                hint="用法：/施肥 氮、/施肥 磷、/施肥 钾",
                commands=["/施肥 氮", "/养植物"],
            )
        owned = [k for k, spec in FERT_SPECS.items() if bag.count(spec[0]) > 0]
        if not owned:
            raise WhaleError(
                "背包里没有肥料",
                hint=f"肥料在 /商店 有卖（氮 {get_item('fert_n').price} / 钾 {get_item('fert_k').price} / 磷 {get_item('fert_p').price} 鲸币），也可以钓鱼捞到",
                commands=["/商店", "/施肥 氮"],
            )
        if len(owned) > 1:
            names = "、".join(FERT_SPECS[k][1] for k in owned)
            raise WhaleError(
                f"要施哪种肥？你有：{names}",
                hint="带肥料名再发一次：/施肥 氮、/施肥 磷 或 /施肥 钾",
                commands=[f"/施肥 {FERT_SPECS[k][1][0]}" for k in owned],
            )
        key = owned[0]

    item_id, fert_name, base_growth, base_mutation = FERT_SPECS[key]
    if bag.count(item_id) <= 0:
        raise NotEnoughItem(
            fert_name,
            1,
            hint=(
                f"肥料在 /商店 有卖（氮 {get_item('fert_n').price} / 钾 {get_item('fert_k').price}"
                f" / 磷 {get_item('fert_p').price} 鲸币），也可以钓鱼捞到"
            ),
        )
    ok, _bag = await ctx.repo.consume_items(uid, {item_id: 1})
    if not ok:
        raise NotEnoughItem(fert_name, 1)

    growth_gain = _scale(base_growth, status.mult, _growth_speed(plant))
    before = int(garden.growth)
    garden.growth = min(GROWTH_MAX, before + growth_gain)
    growth_gain = int(garden.growth) - before
    mutation_gain = _add_mutation(garden, plant, base_mutation)

    setattr(garden, f"last_{key}", now)
    garden.fert_day = today
    garden.fert_count = used + 1
    garden.stage = _stage_index(garden.growth)
    await ctx.repo.save_garden(garden)

    return garden_ui.fertilize_card(
        fert_name=fert_name,
        growth_gain=growth_gain,
        mutation_gain=mutation_gain,
        used=used + 1,
        limit=FERT_DAILY_LIMIT,
        mutation_capped=base_mutation > 0 and mutation_gain == 0,
    )


def _normalize_fert(text: str) -> str | None:
    if not text:
        return None
    key = text.strip().lower().replace(" ", "")
    if key in _FERT_ALIASES:
        return _FERT_ALIASES[key]
    for suffix in ("肥料", "肥"):
        if key.endswith(suffix):
            head = key[: -len(suffix)]
            if head in _FERT_ALIASES:
                return _FERT_ALIASES[head]
    for alias in sorted(_FERT_ALIASES, key=len, reverse=True):
        if key.startswith(alias):
            return _FERT_ALIASES[alias]
    return None


async def do_sun(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """``/晒太阳``：每天 1 次（默认 7:00~19:00），收益由天气决定。"""
    now = now_ts()
    garden, status, plant = await _load_alive(ctx, group_id, uid, now)
    if int(garden.growth) >= GROWTH_MAX:
        raise WhaleError("已经成熟了，先 /收获 吧", hint="成熟后晒太阳会被浪费", commands=["/收获"])

    start, end = _sun_hours(ctx)
    hour = _local_hour(ctx, now)
    if not (start <= hour < end):
        raise WhaleError(
            f"现在不是晒太阳的时间（{start}:00~{end}:00）",
            hint="白天再来；现在可以 /浇水 或 /施肥",
            commands=["/浇水", "/施肥", "/养植物"],
        )

    today = day_key(now)
    if garden.sun_day == today:
        raise WhaleError(
            "今天已经晒过太阳了",
            hint="每天只能晒 1 次，明天再来",
            commands=["/养植物"],
        )

    weather = await _weather_for(ctx, group_id, now)
    base_growth, base_mutation = SUN_WEATHER_GAINS.get(weather.key, SUN_DEFAULT_GAIN)

    growth_gain = _scale(base_growth, status.mult, _growth_speed(plant))
    before = int(garden.growth)
    garden.growth = min(GROWTH_MAX, before + growth_gain)
    growth_gain = int(garden.growth) - before
    mutation_gain = _add_mutation(garden, plant, base_mutation)

    garden.sun_day = today
    garden.sun_ts = now
    garden.stage = _stage_index(garden.growth)
    await ctx.repo.save_garden(garden)

    return garden_ui.sun_card(
        weather_text=f"{weather.emoji} {weather.name}",
        growth_gain=growth_gain,
        mutation_gain=mutation_gain,
        mutation_capped=base_mutation > 0 and mutation_gain == 0,
    )


async def do_harvest(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """``/收获``：成长满 600 收 1 个果实 + 1 颗种子；枯萎时返回 0 并清空。"""
    now = now_ts()
    garden = await _load_garden(ctx, group_id, uid)
    garden, _status, changed = _settle(garden, now)
    plant = _get_plant(garden.plant_id)
    revealed = int(garden.growth) >= STAGE_CAP
    plant_name = (str(getattr(plant, "name", "") or garden.plant_id) if revealed else "???")

    if garden.dead:
        await ctx.repo.delete_garden(group_id, uid)
        return garden_ui.withered_card(plant_name=plant_name, wither_days=WITHER_DAYS)

    if int(garden.growth) < GROWTH_MAX:
        if changed:
            await ctx.repo.save_garden(garden)
        raise WhaleError(
            f"还没成熟：{garden.growth}/{GROWTH_MAX}",
            hint="继续 /浇水 /施肥 /晒太阳，成长满了再来 /收获",
            commands=["/养植物", "/浇水"],
        )

    grade = str(getattr(plant, "grade", "") or "")
    if not grade:
        # 数据层异常时用种子档位兜底，保证玩家不会白种
        grade = _seed_grade(garden.seed_id) or C.RARITY_XIAOLAN
    seed_grade = _roll_seed_for_plant(grade, int(garden.mutation))
    fruit_id = str(getattr(plant, "fruit_item", "") or f"crop_{garden.plant_id}")
    seed_id = _seed_item_id(seed_grade)
    fruit_count = 1

    await ctx.repo.add_items(uid, {fruit_id: fruit_count, seed_id: 1})
    await ctx.repo.delete_garden(group_id, uid)

    return garden_ui.harvest_card(
        plant_name=plant_name,
        grade=grade,
        fruit_name=name_of(fruit_id),
        fruit_count=fruit_count,
        seed_name=_seed_name(seed_id, seed_grade),
        seed_grade=seed_grade,
        mutation=int(garden.mutation),
    )
