"""花园物品表（种子 / 果实 / 肥料 / 碗盆）。

契约见 `docs/GARDEN_SPEC.md` 2.3。由花园数据子智能体维护；文件缺失或为空时
items.py 会吞掉导入异常并照常启动，所以这里出错不会拖垮主插件。

约束
    * 只从 `.items` 取 `ItemDef` 与类别常量，**绝不 import ITEMS**
      （items.py 会在构建 ITEMS 之前 import 本文件，反向 import 会循环）。
    * 植物名 / 品质 / 果实 id 一律从 `.plants.PLANTS` 取，避免两份名单漂移；
      plants.py 不依赖 items，所以这条依赖链是单向的。
"""

from __future__ import annotations

from .items import CAT_CROP, CAT_FERT, CAT_MEAL, CAT_SEED, ItemDef
from ..core import constants as C
from .plants import PLANTS

# 种子档位 -> 物品 id（服务层拼 seed_<grade> 时用这张表，别去匹配中文名）
SEED_ITEM_BY_GRADE: dict[str, str] = {
    grade: f"seed_{grade.lower()}" for grade in C.GARDEN_SEED_GRADES
}

# 品质 -> 果实/花朵回收价（GARDEN_SPEC 2.3，必须全部覆盖）
CROP_SELL_BY_GRADE: dict[str, int] = {
    C.RARITY_XIAOLAN: 4,
    C.RARITY_DALAN: 7,
    C.RARITY_XIAOZI: 10,
    C.RARITY_DAZI: 18,
    C.RARITY_XIAOHONG: 40,
    C.RARITY_DAHONG: 120,
    C.RARITY_STAR: 500,
}


# ---------------------------------------------------------------- 固定物品
# 种子：price=0 表示商店不卖（S/A/B/C 只能钓鱼或收获得到，见 drops.py SEED_WEIGHTS）；
# D/E 是唯二可购买的入门种子，价格 8 / 4（薄利，配合果实回收不亏）。
_SEED_ITEMS: tuple[ItemDef, ...] = (
    ItemDef(
        "seed_s",
        "S 级种子",
        C.RARITY_STAR,
        CAT_SEED,
        "最高一档的种子，能种出星彩、大红或小红；商店不卖，只能钓鱼或高级植物回收。",
        aliases=("S种子", "S级种子"),
    ),
    ItemDef(
        "seed_a",
        "A 级种子",
        C.RARITY_DAHONG,
        CAT_SEED,
        "最差也能种出小紫，运气好能出大红；商店不卖。",
        aliases=("A种子", "A级种子"),
    ),
    ItemDef(
        "seed_b",
        "B 级种子",
        C.RARITY_XIAOHONG,
        CAT_SEED,
        "主力出小紫和大紫，极低概率出小红，也可能种出杂草；商店不卖。",
        aliases=("B种子", "B级种子"),
    ),
    ItemDef(
        "seed_c",
        "C 级种子",
        C.RARITY_DAZI,
        CAT_SEED,
        "保底种子，必出小紫或大紫，不会掉到蓝卡；商店不卖。",
        aliases=("C种子", "C级种子"),
    ),
    ItemDef(
        "seed_d",
        "D 级种子",
        C.RARITY_XIAOZI,
        CAT_SEED,
        "店里有售，能种出小蓝到大紫，是新手稳赚的一档。",
        price=8,
        aliases=("D种子", "D级种子"),
    ),
    ItemDef(
        "seed_e",
        "E 级种子",
        C.RARITY_XIAOLAN,
        CAT_SEED,
        "最便宜的入门种子，主要出小蓝和大蓝，偶尔蹦一颗小紫。",
        price=4,
        aliases=("E种子", "E级种子"),
    ),
)

# 肥料：成长 / 突变侧重不同，数值在 services/garden.py 里，这里只负责上架与文案。
_FERT_ITEMS: tuple[ItemDef, ...] = (
    ItemDef(
        "fert_n",
        "氮肥",
        C.RARITY_XIAOLAN,
        CAT_FERT,
        "管叶子管长个儿：成长多、突变少。",
        price=4,
        aliases=("氮", "N肥"),
    ),
    ItemDef(
        "fert_p",
        "磷肥",
        C.RARITY_XIAOZI,
        CAT_FERT,
        "管开花管变异：突变多、成长少。",
        price=6,
        aliases=("磷", "P肥"),
    ),
    ItemDef(
        "fert_k",
        "钾肥",
        C.RARITY_DALAN,
        CAT_FERT,
        "管根茎管均衡：成长和突变都不偏科。",
        price=5,
        aliases=("钾", "K肥"),
    ),
)

# 碗盆：CAT_MEAL，装果实喂鱼用；真正扣道具由服务层处理，所以 usable 保持 False。
_VESSEL_ITEMS: tuple[ItemDef, ...] = (
    ItemDef(
        "bowl_plain",
        "空碗",
        C.RARITY_XIAOLAN,
        CAT_MEAL,
        "装一份果实就能喂鱼，分量小，鱼也吃得开心。",
        price=2,
        aliases=("空碗子",),
    ),
    ItemDef(
        "pot_plain",
        "空盆",
        C.RARITY_DALAN,
        CAT_MEAL,
        "装大份果实餐，一顿顶三碗。",
        price=6,
        aliases=("空盆子",),
    ),
)


# 果实名默认 = 植物名（规范 2.3）。只有和现有物品重名时才加后缀：
# items.py 的 _ALIAS_INDEX 里，后定义的花园物品会覆盖先定义的别名，若果实也叫「玉米」，
# 会把 bait_corn（酒酿玉米）的别名抢走，导致 /购买 玉米 买不到饵料。
# 这是唯一一处有意偏离"植物名"的地方，其余 69 种果实都直接复用植物名。
_CROP_NAME_OVERRIDES: dict[str, str] = {"corn": "玉米棒"}


def _crop_items() -> tuple[ItemDef, ...]:
    """每株植物一件 `crop_<plant_id>`，品质跟植物走，sell 按品质价。"""
    items: list[ItemDef] = []
    for plant in PLANTS.values():
        sell = CROP_SELL_BY_GRADE.get(plant.grade, 0)
        if sell <= 0:
            raise ValueError(f"植物品质缺少回收价: {plant.id} / {plant.grade}")
        kind = "花朵" if plant.is_flower else "作物"
        items.append(
            ItemDef(
                id=plant.fruit_item,
                name=_CROP_NAME_OVERRIDES.get(plant.id, plant.name),
                rarity=plant.grade,
                category=CAT_CROP,
                desc=f"{plant.name}，花园收获的{kind}，可卖给商店换 {sell} 鲸币。",
                sell=sell,  # price 保持 0：果实只能卖，商店不卖
                # usable 保持 False：收获后的卖钱/做果实餐由服务层处理
            )
        )
    return tuple(items)


GARDEN_ITEM_DEFS: tuple[ItemDef, ...] = (
    _SEED_ITEMS + _FERT_ITEMS + _VESSEL_ITEMS + _crop_items()
)
