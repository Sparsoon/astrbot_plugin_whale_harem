"""钓鱼掉落池 —— 纯数据 + import 时一次性编译好的 O(1) 采样表。

为什么省 CPU / 内存：
    * 固定权重（大类 / 道具 / 品质 / 星级 / 材料）在 import 时编译成 Alias 表，
      运行期每竿只做 2~4 次 random.random()，绝不重复累加权重；
    * 星级与品质随 boost 浮动，按 round(boost,2) 缓存、上限 16 档、FIFO 淘汰，内存恒定；
    * 材料表按池塘等级预建 10 张，运行期直接下标取，不排序不重算。
数值口径（POOL_VERSION 变更时请同步 docs/ 里的概率表）：上钩后 鱼 50 / 道具 25 / 物品 25，
上钩成功必掉 1~2 个池塘材料；boost 与两张权重表的关系见 shift_weights() 与 STAR_BOOST_CAP。
"""

from __future__ import annotations

from typing import Mapping

from ..core import constants as C
from ..core.rng import AliasSampler, randint
from .items import name_of

POOL_VERSION = "2026.10.26-weather-r1"

# ================================================================ 1. 上钩后大类
# 直接引用 constants.CATEGORY_WEIGHTS（fish 50 / prop 25 / item 25），只在这里编译一次
CATEGORY_FISH = "fish"
CATEGORY_PROP = "prop"
CATEGORY_ITEM = "item"
CATEGORY_WEIGHTS: dict[str, float] = {str(k): float(v) for k, v in C.CATEGORY_WEIGHTS.items()}

# ================================================================ 2. 道具池（25% 大类）
# 抽到就是 1 张卡，进背包，不占鱼护
PROP_POOL: dict[str, float] = {
    "card_wife": 30.0,   # 娶群友卡 30%
    "card_good": 28.0,   # 好人卡   28%
    "card_green": 24.0,  # 绿帽卡   24%
    "card_loyal": 18.0,  # 钟情卡   18%
}

# ================================================================ 3. 物品品质池（25% 大类）
# 虚拟物品：不进背包，直接折成鲸币（文案上算"钓上一袋鲸币"）
COINS_SMALL = "coins_small"
COINS_BIG = "coins_big"
VIRTUAL_COIN_ITEMS: dict[str, tuple[int, int]] = {
    COINS_SMALL: (40, 90),    # 金币袋·小：40~90 鲸币
    COINS_BIG: (260, 520),    # 金币袋·大：260~520 鲸币
}
VIRTUAL_COIN_NAMES: dict[str, str] = {
    COINS_SMALL: "一小袋鲸币",
    COINS_BIG: "一大袋鲸币",
}

# 严格按需求分档：材料（mat_*）不进物品池，改由"上钩必掉"的独立材料通道产出。
ITEM_POOL_BY_RARITY: dict[str, tuple[str, ...]] = {
    C.RARITY_XIAOLAN: ("rice_bowl",),
    C.RARITY_DALAN: ("rice_pot", "iron_pot", "whale_pillow", "bait_normal", "bowl_plain"),
    C.RARITY_XIAOZI: ("bait_worm", "bait_bloodworm", "bait_corn", "lucky_charm", "deep_pearl"),
    C.RARITY_DAZI: ("bait_shop1", "card_makeup", "tide_bell", "pot_plain"),
    C.RARITY_XIAOJIN: ("card_pond_up", COINS_SMALL, "ticket_reservoir", "bait_shop2", "ticket_star"),
    C.RARITY_DAJIN: (COINS_BIG, "bait_shop3", "ticket_private"),
    C.RARITY_XIAOHONG: ("ticket_contract", "bait_shop4", "figure_whale", "plush_whale", "bait_glow"),
    C.RARITY_DAHONG: ("tear_deepseek", "weather_controller"),
}

# 品质权重基数（未受 boost 提升时）—— 参考"绝区零贝果计划"的陡峭曲线：
# 蓝 70.4% / 紫 24% / 金 4.8% / 红 0.8%，其中大红只有 0.2%
RARITY_WEIGHTS: dict[str, float] = {
    C.RARITY_XIAOLAN: 460.0,
    C.RARITY_DALAN: 300.0,
    C.RARITY_XIAOZI: 140.0,
    C.RARITY_DAZI: 60.0,
    C.RARITY_XIAOJIN: 25.0,
    C.RARITY_DAJIN: 10.0,
    C.RARITY_XIAOHONG: 4.0,
    C.RARITY_DAHONG: 1.0,
}

# ================================================================ 4. 鱼星权重基数
# 1★ 52% / 2★ 30% / 3★ 13% / 4★ 4% / 5★ 1%
STAR_WEIGHTS: dict[int, float] = {
    1: 628.0,
    2: 260.0,
    3: 90.0,
    4: 18.0,
    5: 4.0,
}

# 池塘等级保底品质档位：Lv1-3 小蓝、Lv4-6 大蓝、Lv7-9 小紫、Lv10 大紫
MIN_RARITY_RANK_BY_LEVEL: dict[int, int] = {}
for _lv in range(C.POND_BASE_LEVEL, C.POND_MAX_LEVEL + 1):
    MIN_RARITY_RANK_BY_LEVEL[_lv] = min(3, max(0, (_lv - 1) // 3))

# ================================================================ 5. 材料（上钩必掉）
MATERIAL_STONE = "mat_stone"
MATERIAL_CORAL = "mat_coral"
MATERIAL_BONE = "mat_bone"
MATERIAL_CRYSTAL = "mat_crystal"
MATERIAL_POOL: tuple[str, ...] = (
    MATERIAL_STONE, MATERIAL_CORAL, MATERIAL_BONE, MATERIAL_CRYSTAL, "mat_shell", "mat_amber"
)

# 规划原文是「1-3 级基石 / 4-6 珊瑚 / 7-9 鲸骨 / 10 结晶」，但直接照抄会**卡死**：
# POND_UPGRADE_REQUIREMENTS 的进度比它快半档 —— 3 级就要 10 珊瑚、6 级要 12 鲸骨、
# 7 级还要 3 结晶，而那时按粗糙分段只会掉低一档材料，群永远升不上去。
# 所以这里按「下一级需求」推导，保证任何等级都能靠钓鱼凑齐升级材料（已上报队长）。
MATERIAL_CHOICES: dict[int, tuple[str, ...]] = {}
MATERIAL_TIERS: dict[int, str] = {}
for _level in range(C.POND_BASE_LEVEL, C.POND_MAX_LEVEL + 1):
    _req = C.POND_UPGRADE_REQUIREMENTS.get(_level + 1) or {}
    _keys = tuple(_req.keys())
    if not _keys:  # 满级没有下一级了，继续掉最高档材料，别让玩家白钓
        _keys = (MATERIAL_TIERS.get(_level - 1, MATERIAL_CRYSTAL),)
    MATERIAL_CHOICES[_level] = _keys
    MATERIAL_TIERS[_level] = _keys[0]  # 需求里第一个就是主材料（dict 顺序 = 需求量从大到小）

# 每级一张表：双材料等级（8 级要鲸骨+结晶）按需求量加权，主材料更常出
MATERIAL_SAMPLERS: dict[int, AliasSampler] = {}
for _level, _mats in MATERIAL_CHOICES.items():
    _need = C.POND_UPGRADE_REQUIREMENTS.get(_level + 1) or {}
    MATERIAL_SAMPLERS[_level] = AliasSampler({mat: float(_need.get(mat, 1)) for mat in _mats})

# ================================================================ 6. boost（品质提升）合成
BAIT_QUALITY_STEP = 0.06   # 每高一个饵料 tier 的加成：(tier-1) * 0.06
QUALITY_BOOST_SCALE = 0.22  # 品质池有 8 档（指数 0~7），直接套满 boost 会把大红概率抬到 4%（大红单价 1500），
                           # 通胀风险太大，所以物品池只用一半加成。
# 星级池指数是 0~4：(1+1.35)^4 ≈ 30 倍，5★ 权重会被抬到全部鱼获的 58%，
# 一条 680 鲸币的传说鱼等于白送 —— 必须封顶。0.90 正好是「池塘满级 0.6 + 顶级饵料 0.3」，
# 也就是说满级池塘 + 顶级饵料刚好摸到星级上限；体验券的星级溢价被这个上限吸收（它仍然提升上钩率与物品品质）。
# A5 若要调平衡，只改这一个数：0.6 => 5★ 10.3%（更稀有），0.9 => 5★ 14.9%。
STAR_BOOST_CAP = 0.35
BOOST_CACHE_LIMIT = 16     # 采样表缓存上限，防无限增长

# 体验券：上钩率加成与 items.py 描述一致（水库 +12%，放养/承包 +18%）
TICKET_PRIORITY: tuple[str, ...] = ("ticket_contract", "ticket_private", "ticket_reservoir")
TICKET_HOOK_BONUS_BY_KIND: dict[str, float] = {
    "ticket_reservoir": C.TICKET_HOOK_BONUS,
    "ticket_private": C.PRIVATE_POOL_HOOK_BONUS,
    "ticket_contract": C.PRIVATE_POOL_HOOK_BONUS,
}
# 品质加成：承包券文案是"额外 +1 档"，折算成 boost 0.45
TICKET_QUALITY_BOOST_BY_KIND: dict[str, float] = {
    "ticket_reservoir": 0.15,
    "ticket_private": 0.30,
    "ticket_contract": 0.45,
}


def roll_rarity(boost: float, min_rank: int = 0, shift: int = 0) -> str:
    """抽品质：先按权重抽，再叠加天气/券的档位偏移，最后被池塘保底托住。

    天气的正向偏移最多把物品推到"小红"（rank 6），不能直接送大红；
    大红只能靠自然抽中，避免"多云=大红批发"。
    """
    picked = str(quality_sampler(min(float(boost), 0.50)).pick())
    rank = C.RARITY_RANK.get(picked, 0)
    top = len(C.RARITY_ORDER) - 1
    if shift > 0 and rank < top:
        rank = min(rank + int(shift), top - 1)
    elif shift < 0:
        rank = max(0, rank + int(shift))
    rank = max(int(min_rank), min(top, rank))
    return C.RARITY_ORDER[rank]


def roll_star(boost: float, shift: int = 0) -> int:
    """抽鱼星：先按权重抽，再叠加天气偏移，钳到 1~5。"""
    star = int(star_sampler(boost).pick()) + int(shift)
    return max(1, min(5, star))


def min_rarity_rank(level: int) -> int:
    """池塘等级对应的最低品质档位（0=小蓝，3=大紫）。"""
    key = max(C.POND_BASE_LEVEL, min(C.POND_MAX_LEVEL, int(level)))
    return MIN_RARITY_RANK_BY_LEVEL.get(key, 0)


def min_rarity_name(level: int) -> str:
    return C.RARITY_ORDER[min_rarity_rank(level)]


def pond_hook_bonus(level: int) -> float:
    """池塘等级给的上钩率加成（绝对值，封顶 0.20）。"""
    return min(max(0, int(level)) * C.POND_HOOK_BONUS_PER_LEVEL, C.POND_HOOK_BONUS_CAP)


def pond_quality_boost(level: int) -> float:
    """池塘等级给的品质/星级加成（封顶 0.6）。"""
    return min(max(0, int(level)) * C.POND_STAR_BONUS_PER_LEVEL, C.POND_STAR_BONUS_CAP)


def compose_boost(pond_level: int, bait_tier: int, ticket_boost: float = 0.0) -> float:
    """品质提升总加成 = 池塘 + 饵料 + 体验券，四舍五入到 0.01 以便做缓存键。"""
    bait = max(0, int(bait_tier) - 1) * BAIT_QUALITY_STEP
    total = pond_quality_boost(pond_level) + bait + max(0.0, float(ticket_boost))
    return round(total, 2)


def _rank_of(key: object) -> int:
    """品质档位序号（0 起）；整数键按 星级-1 处理。"""
    if isinstance(key, int):
        return max(0, key - 1)
    return max(0, C.RARITY_RANK.get(str(key), 0))


def shift_weights(base: Mapping[object, float], boost: float, star_power: bool = False) -> dict:
    """品质提升纯函数：w_i *= (1 + boost) ** rank_i。

    * rank 从 0 起：品质池用 RARITY_RANK（小蓝 0 … 大红 7）；
    * star_power=True 时按「星级 - 1」次幂（1★ 0 次方，5★ 4 次方），供星级池使用；
    * boost <= 0 时原样复制一份返回，调用方可以安全改写。
    """
    level = max(0.0, float(boost))
    out: dict = {}
    factor = 1.0 + level
    for key, weight in base.items():
        rank = _rank_of(key)
        if star_power:
            try:
                rank = max(0, int(key) - 1)
            except (TypeError, ValueError):
                pass
        out[key] = float(weight) if level <= 0.0 else float(weight) * (factor ** rank)
    return out


def build_alias(weights: Mapping[object, float]) -> AliasSampler:
    """把权重字典编译成 O(1) 采样表（只给缓存层调用，主路径禁止每竿重建）。"""
    return AliasSampler(weights)


# ================================================================ 7. 采样表缓存
_STAR_CACHE: dict[float, AliasSampler] = {}
_QUALITY_CACHE: dict[float, AliasSampler] = {}


def _cache_key(boost: float) -> float:
    return round(max(0.0, float(boost)), 2)


def _cached(cache: dict[float, AliasSampler], key: float, weights: Mapping[object, float], star_power: bool) -> AliasSampler:
    sampler = cache.get(key)
    if sampler is not None:
        return sampler
    if len(cache) >= BOOST_CACHE_LIMIT:  # FIFO：最早建的表先淘汰，容量恒定
        cache.pop(next(iter(cache)), None)
    sampler = build_alias(shift_weights(weights, key, star_power))
    cache[key] = sampler
    return sampler


def star_sampler(boost: float) -> AliasSampler:
    """鱼星采样表（boost 先按 STAR_BOOST_CAP 封顶，再按档位缓存，最多 16 档）。"""
    key = _cache_key(min(float(boost), STAR_BOOST_CAP))
    return _cached(_STAR_CACHE, key, STAR_WEIGHTS, True)


def quality_sampler(boost: float) -> AliasSampler:
    """物品品质采样表（按 boost 档位缓存，最多 16 档）。"""
    key = _cache_key(boost)
    return _cached_scaled(key, round(key * QUALITY_BOOST_SCALE, 2))


def _cached_scaled(key: float, scaled: float) -> AliasSampler:
    """品质池用衰减后的 boost 建表，缓存键仍是原始 boost（方便 A5 对账）。"""
    sampler = _QUALITY_CACHE.get(key)
    if sampler is not None:
        return sampler
    if len(_QUALITY_CACHE) >= BOOST_CACHE_LIMIT:
        _QUALITY_CACHE.pop(next(iter(_QUALITY_CACHE)), None)
    sampler = build_alias(shift_weights(RARITY_WEIGHTS, scaled, False))
    _QUALITY_CACHE[key] = sampler
    return sampler


def star_weights(boost: float) -> dict:
    """诊断/数值审查用：当前 boost 下的鱼星权重（已按 STAR_BOOST_CAP 封顶）。"""
    return shift_weights(STAR_WEIGHTS, min(max(0.0, float(boost)), STAR_BOOST_CAP), True)


def quality_weights(boost: float) -> dict:
    """诊断/数值审查用：当前 boost 下的品质权重。"""
    return shift_weights(RARITY_WEIGHTS, max(0.0, float(boost)) * QUALITY_BOOST_SCALE, False)


def cache_sizes() -> dict[str, int]:
    """给 A5 看缓存有没有失控（任何情况下都 <= BOOST_CACHE_LIMIT）。"""
    return {"star": len(_STAR_CACHE), "quality": len(_QUALITY_CACHE), "limit": BOOST_CACHE_LIMIT}


# ================================================================ 8. 便捷查询
# 花园种子：所有等级都可能钓到，但 S/A 极低；不受鱼饵/天气影响，纯概率
SEED_WEIGHTS: dict[str, float] = {
    "seed_e": 45.0,
    "seed_d": 35.0,
    "seed_c": 15.0,
    "seed_b": 4.5,
    "seed_a": 0.4,
    "seed_s": 0.1,
}
SEED_DROP_CHANCE = 0.12
SEED_SAMPLER = AliasSampler(SEED_WEIGHTS)


def roll_seed(rng=None) -> str:
    """钓到种子：返回 seed_s ~ seed_e。"""
    return str(SEED_SAMPLER.pick(rng))


def is_virtual_coin(item_id: str) -> bool:
    return item_id in VIRTUAL_COIN_ITEMS


def roll_coin_bag(item_id: str, rng=None) -> int:
    """金币袋开袋：返回随机鲸币数；不是金币袋返回 0。"""
    low, high = VIRTUAL_COIN_ITEMS.get(item_id, (0, 0))
    if high <= 0:
        return 0
    return int(randint(low, high, rng))


def pool_item_name(item_id: str) -> str:
    """掉落物的展示名：金币袋用虚拟名，其它走 items.py 的唯一真源。"""
    virtual = VIRTUAL_COIN_NAMES.get(item_id)
    if virtual:
        return virtual
    return name_of(item_id)


def material_for_level(level: int) -> str:
    """当前池塘等级钓鱼必掉的主材料。"""
    key = max(C.POND_BASE_LEVEL, min(C.POND_MAX_LEVEL, int(level)))
    return MATERIAL_TIERS.get(key, MATERIAL_CRYSTAL)


def material_sampler(level: int) -> AliasSampler:
    """当前池塘等级的材料采样表（O(1) 取表，双材料等级按需求量加权）。"""
    key = max(C.POND_BASE_LEVEL, min(C.POND_MAX_LEVEL, int(level)))
    sampler = MATERIAL_SAMPLERS.get(key)
    if sampler is None:
        return MATERIAL_SAMPLERS[C.POND_MAX_LEVEL]
    return sampler


# ================================================================ 9. 预构建的固定表
# 这三张表运行期不会变，import 时构建一次就再也不用碰
CATEGORY_SAMPLER: AliasSampler = build_alias(CATEGORY_WEIGHTS)
PROP_SAMPLER: AliasSampler = build_alias(PROP_POOL)