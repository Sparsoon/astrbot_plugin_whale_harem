"""商店与购买 —— 全插件唯一的售卖入口（A4）。

设计要点（2 核 2G 硬约束）
    1. 价格唯一真源是 `data/items.py::ItemDef.price`，本模块不写任何硬编码价格，
       平衡调整只改 items.py 一处，商店/文案/校验自动一致；price <= 0 一律非卖品。
    2. 扣费走 `repo.spend_coins`（CAS：余额不足不改任何行），再加物品走 `repo.add_items`
       （单事务读-改-写），不会出现"扣了钱没给货"。
    3. 只拼 `ui.schema.Card` 纯数据，不碰 PIL / 文件 / 网络，渲染交给 main.py。
    4. 全部 await，无同步 IO、无定时器。
"""

from __future__ import annotations

import re

from ..core.context import SvcCtx, fmt_num
from ..core.errors import NotEnoughCoins, WhaleError
from ..data import items as item_data
from ..ui.schema import ART_SHOP, Card, card, grid_block, kv, kv_block, stat, text_block

_SHOP_IDS = frozenset(item_data.SHOP_ITEMS)

# 展示顺序：饭 -> 饵料 -> 卡券 -> 体验券（材料和收藏品不上架）
_SHOP_GROUPS = (
    item_data.CAT_FOOD,
    item_data.CAT_BAIT,
    item_data.CAT_CARD,
    item_data.CAT_TICKET,
    item_data.CAT_SEED,
    item_data.CAT_FERT,
    item_data.CAT_MEAL,
)

_MAX_BUY = 99
_BUY_PREFIXES = ("购买", "买", "兑换")
# 红虫 / 红虫3 / 红虫 x 3 / 红虫 3
_BUY_TAIL_RE = re.compile(r"^(?P<name>.*?)\s*[xX×*]?\s*(?P<num>\d{1,3})$")


def _parse_buy_arg(arg: str) -> tuple[str, int]:
    """把 `/购买 xx` 的参数解析成 (物品文本, 数量)，默认数量 1。"""
    text = (arg or "").strip()
    for prefix in _BUY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if not text:
        return "", 1
    matched = _BUY_TAIL_RE.match(text)
    if matched and matched.group("name").strip():
        return matched.group("name").strip(), int(matched.group("num"))
    return text, 1


async def do_shop(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """商店在售清单：按类别分组，价格只取 ItemDef.price（price<=0 视为非卖品）。"""
    user = await ctx.repo.get_user(uid, name)
    bag = await ctx.repo.get_bag(uid)

    on_sale = [
        item
        for item in (item_data.get_item(item_id) for item_id in item_data.SHOP_ITEMS)
        if item is not None and item.price > 0
    ]

    c = card(
        "赛博鲸鱼馆 · 商店",
        "钓鱼攒鲸币，鲸币换装备",
        art=ART_SHOP,
        badge=f"{len(on_sale)} 件在售",
        stats=[
            stat("鲸币", fmt_num(user.coins)),
            stat("今日鱼护", f"{bag.fish_count()}/{ctx.cfg.fish_bag_limit}"),
        ],
        footer="下单：/购买 红虫 3",
    )

    for category in _SHOP_GROUPS:
        items = [item for item in on_sale if item.category == category]
        if not items:
            continue
        # 名称 + 「品质 · 价格」：行标签上限 12 字，长名字（体验券）用 name 避免品质被截掉
        rows = [kv(f"{item.rarity} · {item.price} 鲸币", item.name) for item in items]
        c.blocks.append(
            grid_block(item_data.CATEGORY_NAMES.get(category, category), rows)
        )

    if not c.blocks:
        c.blocks.append(text_block("货架空了，等老板补货……"))

    c.blocks.append(
        text_block(
            f"鲸币不够？去钓鱼 / 卖鱼 / 签到打工，一次最多买 {_MAX_BUY} 个。",
            footer="鱼饵在 /钓鱼 时自动消耗；卡券用 /使用 生效",
        )
    )
    return c.validate()


async def do_buy(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str) -> Card:
    """购买：支持 `红虫` / `红虫 3` / `红虫x3` / `买 红虫 3`。

    消耗：单价 x 数量 的鲸币（spend_coins 原子扣，失败即 NotEnoughCoins）。
    产出：物品按数量进背包。
    """
    item_text, num = _parse_buy_arg(arg)
    if not item_text:
        raise WhaleError("要买什么呀？", hint="例如：/购买 红虫 3")
    if num < 1 or num > _MAX_BUY:
        raise WhaleError(
            f"一次只能买 1~{_MAX_BUY} 个，你填了 {num}",
            hint="分几次买就好，别把鱼护挤爆",
        )

    item = item_data.resolve_item(item_text)
    if item is None:
        raise WhaleError(f"没找到「{item_text}」这件东西", hint="输入 /商店 看看在卖什么")
    if item.id not in _SHOP_IDS or item.price <= 0:
        raise WhaleError("商店不卖这个，只能靠钓鱼获得", hint="钓鱼 / 签到能拿到它")

    total = item.price * num
    user = await ctx.repo.get_user(uid, name)
    if user.coins < total:
        raise NotEnoughCoins(total, user.coins)
    if not await ctx.repo.spend_coins(uid, total):
        latest = await ctx.repo.get_user(uid, name)  # 并发花掉了余额，取最新余额报错
        raise NotEnoughCoins(total, latest.coins)

    bag = await ctx.repo.add_items(uid, {item.id: num})
    after = await ctx.repo.get_user(uid, name)

    c = card(
        "购买成功",
        f"{item.display} x{num}",
        art=ART_SHOP,
        tone="success",
        badge=f"-{total} 鲸币",
        stats=[
            stat("余额", fmt_num(after.coins)),
            stat("单价", item.price),
            stat("总价", total),
        ],
    )
    c.blocks.append(
        kv_block(
            "订单",
            [
                kv("商品", item.display),
                kv("数量", f"x{num}"),
                kv("单价", f"{item.price} 鲸币"),
                kv("总价", f"{total} 鲸币"),
                kv("余额", f"{after.coins} 鲸币"),
                kv("背包持有", f"x{bag.count(item.id)}"),
            ],
            footer="已放进背包",
        )
    )
    if item.category == item_data.CAT_BAIT:
        tip = f"用「/钓鱼 {item.name}」就能带着它下水。"
    elif item.category == item_data.CAT_SEED:
        tip = f"用「/播种 {item.name}」种进花园。"
    elif item.category == item_data.CAT_FERT:
        tip = "用「/施肥 氮|磷|钾」施给花园里的植物。"
    elif item.category == item_data.CAT_MEAL:
        tip = "果实餐要配一份果实：/喂食 <果实名>。"
    else:
        tip = f"用「/使用 {item.name}」查看怎么生效。"
    c.blocks.append(text_block(tip))
    return c.validate()