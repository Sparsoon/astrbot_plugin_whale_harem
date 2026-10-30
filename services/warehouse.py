"""仓库：查看自己已经获得的非鱼类物品（饵料/食物/卡券/材料/收藏）。

与 `/背包` 的分工：
    * 背包 = 鱼护（鱼）+ 物品概览，偏"当前状态"；
    * 仓库 = 按类别展开所有物品、数量与估值，偏"资产管理"。

省资源：
    * 只读一次 bags 行，不 join、不查 items 表（物品定义在内存字典）；
    * 每类只展示数量最多的 6 种，避免长图与超长渲染；
    * 不消耗任何物品，纯只读。
"""

from __future__ import annotations

from ..core import constants as C
from ..core.context import SvcCtx, fmt_num
from ..data.items import (
    CATEGORY_NAMES,
    CAT_BAIT,
    CAT_CROP,
    CAT_FERT,
    CAT_MEAL,
    CAT_SEED,
    CAT_BAIT,
    CAT_CARD,
    CAT_COLLECT,
    CAT_FOOD,
    CAT_MAT,
    CAT_TICKET,
    get_item,
)
from ..ui.schema import (
    ART_WAREHOUSE,
    Card,
    card,
    list_block,
    stat,
    text_block,
)

# 展示顺序：常用 -> 稀有
_ORDER = (
    CAT_BAIT, CAT_FOOD, CAT_SEED, CAT_FERT, CAT_MEAL, CAT_CROP,
    CAT_CARD, CAT_TICKET, CAT_MAT, CAT_COLLECT,
)
_MAX_BLOCKS = 6
_PER_BLOCK = 6


async def do_warehouse(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """仓库面板：按类别列出所有物品、数量与回收估值（只读，无消耗）。"""
    bag = await ctx.repo.get_bag(uid)

    grouped: dict[str, list[tuple[str, int]]] = {}
    total_count = 0
    total_value = 0
    for item_id, count in bag.items.items():
        count = int(count)
        if count <= 0:
            continue
        item = get_item(item_id)
        if item is None:
            continue
        grouped.setdefault(item.category, []).append((item_id, count))
        total_count += count
        total_value += item.sell * count

    c = card(
        "我的仓库",
        f"{name or '你'} 的家当 · 共 {total_count} 件",
        art=ART_WAREHOUSE,
        accent=C.ACCENT_BLUE,
        badge=f"估值 {fmt_num(total_value)}",
        footer="材料和卡券建议留着升级/使用；收藏品可用 /卖鱼 杂物 回收",
    )
    c.stats = [
        stat("物品种类", f"{len(bag.items)} 种"),
        stat("总件数", f"{total_count} 件", C.RARITY_COLORS[C.RARITY_XIAOLAN]),
        stat("回收估值", f"{fmt_num(total_value)} 鲸币", C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("鱼护", f"{bag.fish_count()} 条", C.RARITY_COLORS[C.RARITY_XIAOZI]),
    ]

    if not grouped:
        c.blocks.append(text_block("仓库空空如也，去 /钓鱼 或 /签到 攒点东西吧", title="空仓库"))
        return c.validate()

    used = 0
    for category in _ORDER:
        entries = sorted(grouped.get(category, []), key=lambda pair: -pair[1])[:_PER_BLOCK]
        if not entries:
            continue
        lines = []
        for item_id, count in entries:
            item = get_item(item_id)
            name_text = item.name if item else item_id
            rarity = item.rarity if item else ""
            value = (item.sell if item else 0) * count
            lines.append(f"{name_text}·{rarity} ×{count}（估值 {value}）")
        c.blocks.append(list_block(CATEGORY_NAMES.get(category, category), lines))
        used += 1
        if used >= _MAX_BLOCKS:
            break

    hidden = len(grouped) - used
    if hidden > 0:
        c.blocks.append(text_block(f"还有 {hidden} 类物品未展示，先清一清仓库吧"))
    return c.validate()
