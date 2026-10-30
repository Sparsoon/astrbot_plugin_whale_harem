"""物品总表 —— 全插件唯一的物品字典。

规则
    * 物品只在这里定义，商店 / 掉落 / 背包 / UI 全部引用 id，避免文案漂移。
    * `price` = 商店售价（0 表示商店不卖，只能靠钓鱼/签到拿到）。
    * `sell`  = 背包回收价，约为售价的 1/3，构成"钓鱼 -> 卖鱼/卖杂物 -> 买饵"的经济闭环。
    * 品质严格对齐需求：小蓝/大蓝/小紫/大紫/小金/大金/小红/大红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..core import constants as C

# ---------------------------------------------------------------- 类别
CAT_FOOD = "food"          # 白米饭
CAT_BAIT = "bait"          # 饵料
CAT_CARD = "card"          # 卡券（娶/离/绿/钟情/补签/升塘）
CAT_TICKET = "ticket"      # 体验券
CAT_MAT = "mat"            # 池塘升级材料
CAT_COLLECT = "collect"    # 收藏品
CAT_SEED = "seed"          # 植物种子（S~E）
CAT_CROP = "crop"          # 植物果实/花朵
CAT_FERT = "fert"          # 肥料
CAT_MEAL = "meal"          # 果实餐（碗+果实）

CATEGORY_NAMES = {
    CAT_FOOD: "🍚 饭",
    CAT_BAIT: "🪱 饵料",
    CAT_CARD: "🎴 卡券",
    CAT_TICKET: "🎫 体验券",
    CAT_MAT: "🪨 材料",
    CAT_COLLECT: "🧸 收藏",
    CAT_SEED: "🌱 种子",
    CAT_CROP: "🍅 果实",
    CAT_FERT: "🧪 肥料",
    CAT_MEAL: "🍽️ 果实餐",
}


@dataclass(frozen=True, slots=True)
class ItemDef:
    id: str
    name: str
    rarity: str
    category: str
    desc: str
    price: int = 0
    sell: int = 0
    usable: bool = False
    bait_tier: int = 0
    aliases: tuple[str, ...] = ()

    @property
    def rarity_color(self) -> str:
        return C.RARITY_COLORS.get(self.rarity, C.ACCENT_BLUE)

    @property
    def rarity_rank(self) -> int:
        return C.RARITY_RANK.get(self.rarity, 0)

    @property
    def display(self) -> str:
        return f"{self.name}·{self.rarity}"


# ---------------------------------------------------------------- 定义
_RAW: tuple[ItemDef, ...] = (
    # ============================ 小蓝
    ItemDef(
        "rice_bowl",
        "一碗白米饭",
        C.RARITY_XIAOLAN,
        CAT_FOOD,
        "热气腾腾的一碗白米饭，喂给群友能加好感度。",
        price=8,
        sell=3,
        usable=True,
        aliases=("白米饭", "一碗大白饭", "大白饭", "白饭"),
    ),
    ItemDef(
        "mat_stone",
        "池塘基石",
        C.RARITY_XIAOLAN,
        CAT_MAT,
        "带青苔的鹅卵石，群友凑一凑就能把池塘垫高一级。",
        sell=2,
        aliases=("基石", "石头", "鹅卵石"),
    ),
    # ============================ 大蓝
    ItemDef(
        "rice_pot",
        "一铁盆白米饭",
        C.RARITY_DALAN,
        CAT_FOOD,
        "铁盆直接端上来，群友看了都沉默的那种分量。",
        price=22,
        sell=8,
        usable=True,
        aliases=("一铁盆大白饭", "铁盆白饭", "铁盆饭", "大盆饭"),
    ),
    ItemDef(
        "iron_pot",
        "铁盆",
        C.RARITY_DALAN,
        CAT_COLLECT,
        "装过白米饭的铁盆，洗洗还能煮泡面。",
        price=0,
        sell=25,
        aliases=("空铁盆", "盆"),
    ),
    ItemDef(
        "whale_pillow",
        "鲸鱼抱枕",
        C.RARITY_DALAN,
        CAT_COLLECT,
        "一只胖乎乎的鲸鱼抱枕，抱起来像抱住整个深海。",
        sell=60,
        aliases=("抱枕", "鲸鱼枕头"),
    ),
    ItemDef(
        "bait_normal",
        "普通饵料",
        C.RARITY_DALAN,
        CAT_BAIT,
        "最基础的饵，能上鱼，但别指望大鱼。",
        price=8,
        sell=3,
        usable=True,
        bait_tier=1,
        aliases=("普通饵", "基础饵"),
    ),
    ItemDef(
        "mat_coral",
        "珊瑚簇",
        C.RARITY_DALAN,
        CAT_MAT,
        "从深水区捞上来的珊瑚，是池塘中段升级的主料。",
        sell=6,
        aliases=("珊瑚",),
    ),
    # ============================ 小紫
    ItemDef(
        "bait_worm",
        "蚯蚓",
        C.RARITY_XIAOZI,
        CAT_BAIT,
        "扭来扭去的万能饵，小鱼特别爱。",
        price=14,
        sell=4,
        usable=True,
        bait_tier=2,
        aliases=("虫", "蚯蚓饵"),
    ),
    ItemDef(
        "bait_bloodworm",
        "红虫",
        C.RARITY_XIAOZI,
        CAT_BAIT,
        "血红色的细虫，腥味能穿透整个水层。",
        price=18,
        sell=5,
        usable=True,
        bait_tier=2,
        aliases=("血虫",),
    ),
    ItemDef(
        "bait_corn",
        "酒酿玉米",
        C.RARITY_XIAOZI,
        CAT_BAIT,
        "酒香玉米粒，专治不开口的老滑鱼。",
        price=22,
        sell=6,
        usable=True,
        bait_tier=2,
        aliases=("玉米", "酒酿"),
    ),
    ItemDef(
        "mat_bone",
        "鲸骨",
        C.RARITY_XIAOZI,
        CAT_MAT,
        "沉在塘底的古老鲸骨，高级池塘的骨架材料。",
        sell=10,
        aliases=("骨头", "鲸鱼骨"),
    ),
    # ============================ 大紫
    ItemDef(
        "bait_shop1",
        "初级商品饵",
        C.RARITY_DAZI,
        CAT_BAIT,
        "商店货架第一排，包装比内容物唬人。",
        price=45,
        sell=15,
        usable=True,
        bait_tier=3,
        aliases=("初级饵", "初级商品饵料"),
    ),
    ItemDef(
        "card_makeup",
        "补签卡",
        C.RARITY_DAZI,
        CAT_CARD,
        "昨天的签到漏了？撕一张补回来。",
        sell=10,
        usable=True,
        aliases=("补签", "补签券"),
    ),
    # ============================ 小金
    ItemDef(
        "card_pond_up",
        "池塘一键升一级卡",
        C.RARITY_XIAOJIN,
        CAT_CARD,
        "无视材料，直接把本群池塘升一级。",
        sell=50,
        usable=True,
        aliases=("升塘卡", "池塘升级卡", "一键升级卡"),
    ),
    ItemDef(
        "ticket_reservoir",
        "水库一天体验券",
        C.RARITY_XIAOJIN,
        CAT_TICKET,
        "24 小时内上钩率 +12%，鱼获与物品品质提升。",
        price=120,
        sell=20,
        usable=True,
        aliases=("水库券", "水库体验券"),
    ),
    ItemDef(
        "bait_shop2",
        "中级商品饵",
        C.RARITY_XIAOJIN,
        CAT_BAIT,
        "黑坑老板不告诉你配方的那种。",
        price=90,
        sell=30,
        usable=True,
        bait_tier=4,
        aliases=("中级饵",),
    ),
    ItemDef(
        "mat_crystal",
        "深海结晶",
        C.RARITY_XIAOJIN,
        CAT_MAT,
        "在压强里长出来的晶体，顶级池塘的最后一块拼图。",
        sell=25,
        aliases=("结晶", "晶体"),
    ),
    # ============================ 大金
    ItemDef(
        "bait_shop3",
        "高级商品饵",
        C.RARITY_DAJIN,
        CAT_BAIT,
        "开袋有海腥味，高级货色，连大鱼都忍不住。",
        price=180,
        sell=60,
        usable=True,
        bait_tier=5,
        aliases=("高级饵",),
    ),
    ItemDef(
        "ticket_private",
        "私人放养鱼塘一天体验券",
        C.RARITY_DAJIN,
        CAT_TICKET,
        "24 小时内上钩率 +18%，鱼获品质大幅提升。",
        price=300,
        sell=50,
        usable=True,
        aliases=("放养塘券", "私人放养券"),
    ),
    # ============================ 小红
    ItemDef(
        "ticket_contract",
        "私人承包鱼塘一天体验券",
        C.RARITY_XIAOHONG,
        CAT_TICKET,
        "24 小时内上钩率 +18%、品质额外 +1 档，还有鲸币加成。",
        price=600,
        sell=90,
        usable=True,
        aliases=("承包塘券", "私人承包券"),
    ),
    ItemDef(
        "bait_shop4",
        "特级商品饵",
        C.RARITY_XIAOHONG,
        CAT_BAIT,
        "饵料界的顶配，一饵一世界。",
        price=360,
        sell=120,
        usable=True,
        bait_tier=6,
        aliases=("特级饵",),
    ),
    ItemDef(
        "figure_whale",
        "赛博鲸鱼娘手办",
        C.RARITY_XIAOHONG,
        CAT_COLLECT,
        "限量涂装，尾巴上的 LED 真的会亮。",
        sell=300,
        aliases=("手办", "鲸鱼娘手办"),
    ),
    ItemDef(
        "plush_whale",
        "赛博鲸鱼娘毛绒玩偶",
        C.RARITY_XIAOHONG,
        CAT_COLLECT,
        "一米二长，抱着睡会把手臂压麻。",
        sell=260,
        aliases=("毛绒玩偶", "玩偶", "毛绒"),
    ),
    # ============================ 大红
    ItemDef(
        "tear_deepseek",
        "DeepSeek之泪",
        C.RARITY_DAHONG,
        CAT_COLLECT,
        "传说中从模型深处滴落的一滴泪，全服都在找它。",
        sell=1500,
        aliases=("DeepSeek泪", "deepseek之泪", "DS之泪", "鲸泪"),
    ),
    # ============================ 道具（卡）
    ItemDef(
        "card_wife",
        "娶群友卡",
        C.RARITY_DAZI,
        CAT_CARD,
        "免鲸币直接发起一次求婚，成功率同上。",
        sell=40,
        usable=True,
        aliases=("娶妻卡", "结婚卡"),
    ),
    ItemDef(
        "card_good",
        "好人卡",
        C.RARITY_DAZI,
        CAT_CARD,
        "体面地结束一段关系，免 100 鲸币。",
        sell=40,
        usable=True,
        aliases=("好人卡牌",),
    ),
    ItemDef(
        "card_green",
        "绿帽卡",
        C.RARITY_DAZI,
        CAT_CARD,
        "免鲸币发起一次 NTR，成功率同上。",
        sell=40,
        usable=True,
        aliases=("绿帽", "ntr卡"),
    ),
    ItemDef(
        "card_loyal",
        "钟情卡",
        C.RARITY_XIAOJIN,
        CAT_CARD,
        "给某人上 24 小时护盾，期间他/她不会被 NTR 走。",
        price=150,
        sell=30,
        usable=True,
        aliases=("钟情", "护盾卡"),
    ),

    # ============================ 池塘材料（新增）
    ItemDef(
        "mat_shell",
        "贝壳",
        C.RARITY_XIAOZI,
        CAT_MAT,
        "池塘边捡到的贝壳，磨成粉能加固塘底。",
        sell=4,
        aliases=("贝壳粉", "海贝"),
    ),
    ItemDef(
        "mat_amber",
        "琥珀",
        C.RARITY_DAJIN,
        CAT_MAT,
        "封着一只小虫的琥珀，是池塘升级里最贵的一味。",
        sell=30,
        aliases=("琥珀石",),
    ),
    # ============================ 小紫（新增）
    ItemDef(
        "lucky_charm",
        "幸运护符",
        C.RARITY_XIAOZI,
        CAT_COLLECT,
        "一块刻着鲸鱼的小木牌，据说摸一摸能少空军一次。",
        sell=35,
        aliases=("护符", "幸运符"),
    ),
    ItemDef(
        "deep_pearl",
        "深海珍珠",
        C.RARITY_XIAOZI,
        CAT_COLLECT,
        "从很深的水底带上来的珍珠，泛着蓝紫色的光。",
        sell=45,
        aliases=("珍珠",),
    ),
    # ============================ 大紫（新增）
    ItemDef(
        "tide_bell",
        "潮汐铃",
        C.RARITY_DAZI,
        CAT_COLLECT,
        "涨潮时会自己响的铃铛，收藏家出价不低。",
        sell=120,
        aliases=("铃铛", "潮铃"),
    ),
    # ============================ 小金（新增）
    ItemDef(
        "ticket_star",
        "星空体验券",
        C.RARITY_XIAOJIN,
        CAT_TICKET,
        "24 小时内鱼星 +1（与天气、饵料叠加）。",
        sell=60,
        usable=True,
        aliases=("星空券", "星星券"),
    ),
    # ============================ 小红（新增）
    ItemDef(
        "bait_glow",
        "发光饵",
        C.RARITY_XIAOHONG,
        CAT_BAIT,
        "会自己发光的稀有饵料，上钩率与品质都很能打。",
        sell=80,
        bait_tier=5,
        aliases=("荧光饵", "发光鱼饵"),
    ),
    # ============================ 大红（新增）
    ItemDef(
        "weather_controller",
        "天气控制机",
        C.RARITY_DAHONG,
        CAT_COLLECT,
        "可以随意修改 3 天天气。使用：天气控制机 晴天（或小雨/雷雨/大雾/流星雨/寒潮/多云）。",
        sell=300,
        usable=True,
        aliases=("天气机", "控天机", "天气控制器"),
    ),
)

try:  # 花园物品单独维护，缺失也不影响主插件启动
    from .garden_items import GARDEN_ITEM_DEFS as _GARDEN_RAW
except Exception:  # noqa: BLE001
    _GARDEN_RAW = ()
_RAW = tuple(_RAW) + tuple(_GARDEN_RAW)

ITEMS: dict[str, ItemDef] = {item.id: item for item in _RAW}

# 便捷分组
FOOD_ITEMS = tuple(i.id for i in _RAW if i.category == CAT_FOOD)
BAIT_ITEMS = tuple(sorted((i for i in _RAW if i.category == CAT_BAIT), key=lambda x: x.bait_tier))
BAIT_IDS = tuple(i.id for i in BAIT_ITEMS)
TICKET_ITEMS = tuple(i.id for i in _RAW if i.category == CAT_TICKET)
MATERIAL_ITEMS = tuple(i.id for i in _RAW if i.category == CAT_MAT)
CARD_ITEMS = tuple(i.id for i in _RAW if i.category == CAT_CARD)
COLLECT_ITEMS = tuple(i.id for i in _RAW if i.category == CAT_COLLECT)

# 商店在售（顺序即展示顺序）
SHOP_ITEMS: tuple[str, ...] = (
    "rice_bowl",
    "rice_pot",
    "bait_normal",
    "bait_worm",
    "bait_bloodworm",
    "bait_corn",
    "bait_shop1",
    "bait_shop2",
    "bait_shop3",
    "bait_shop4",
    "card_loyal",
    "ticket_reservoir",
    "ticket_private",
    "ticket_contract",
    # 花园：只上架 D/E 种子与肥料、碗盆（S/A/B/C 靠钓鱼/收获）
    "seed_d",
    "seed_e",
    "fert_n",
    "fert_p",
    "fert_k",
    "bowl_plain",
    "pot_plain",
)

# 品质 -> 该品质的"物品池"（钓鱼用），由 data.drops 引用
ITEMS_BY_RARITY: dict[str, list[str]] = {}
for _item in _RAW:
    ITEMS_BY_RARITY.setdefault(_item.rarity, []).append(_item.id)

_ALIAS_INDEX: dict[str, str] = {}
for _item in _RAW:
    _ALIAS_INDEX[_item.name.lower()] = _item.id
    _ALIAS_INDEX[_item.id.lower()] = _item.id
    for _alias in _item.aliases:
        _ALIAS_INDEX[_alias.lower()] = _item.id


# ---------------------------------------------------------------- 查询
def get_item(item_id: str) -> ItemDef | None:
    return ITEMS.get(item_id)


def name_of(item_id: str) -> str:
    item = ITEMS.get(item_id)
    return item.name if item else item_id


def display_of(item_id: str) -> str:
    item = ITEMS.get(item_id)
    return item.display if item else item_id


def rarity_of(item_id: str) -> str:
    item = ITEMS.get(item_id)
    return item.rarity if item else C.RARITY_XIAOLAN


def sell_price(item_id: str) -> int:
    item = ITEMS.get(item_id)
    return item.sell if item else 0


def is_bait(item_id: str) -> bool:
    item = ITEMS.get(item_id)
    return bool(item and item.category == CAT_BAIT)


def is_food(item_id: str) -> bool:
    item = ITEMS.get(item_id)
    return bool(item and item.category == CAT_FOOD)


def is_material(item_id: str) -> bool:
    item = ITEMS.get(item_id)
    return bool(item and item.category == CAT_MAT)


def is_ticket(item_id: str) -> bool:
    item = ITEMS.get(item_id)
    return bool(item and item.category == CAT_TICKET)


def bait_tier(item_id: str) -> int:
    item = ITEMS.get(item_id)
    return item.bait_tier if item else 0


def resolve_item(text: str) -> ItemDef | None:
    """把用户输入（名字/别名/id/模糊前缀）解析成物品。"""
    if not text:
        return None
    key = text.strip().lower()
    for prefix in ("购买", "买", "使用", "用"):
        if key.startswith(prefix):
            key = key[len(prefix) :].strip()
    if not key:
        return None
    if key in _ALIAS_INDEX:
        return ITEMS.get(_ALIAS_INDEX[key])
    # 去掉数量后缀：白米饭x2 / 白米饭 2
    for sep in ("x", "X", "*", "×", " "):
        if sep in key:
            head = key.split(sep, 1)[0].strip()
            if head in _ALIAS_INDEX:
                return ITEMS.get(_ALIAS_INDEX[head])
    # 子串匹配（用户少打/多打几个字）
    best: ItemDef | None = None
    for item in _RAW:
        candidates = (item.name.lower(), item.id.lower(), *(a.lower() for a in item.aliases))
        if any(key in c or c in key for c in candidates if c):
            if best is None or len(item.name) < len(best.name):
                best = item
    return best


def shop_entries() -> Iterable[ItemDef]:
    for item_id in SHOP_ITEMS:
        item = ITEMS.get(item_id)
        if item is not None:
            yield item