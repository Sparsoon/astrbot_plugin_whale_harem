"""物品使用 —— 只做"解析 + 扣道具 + 分派"，玩法效果一律复用别人的服务（A4）。

设计要点（2 核 2G 硬约束）
    1. 效果零重复：补签卡转 daily.do_makeup、升塘卡转 pond.do_use_pond_card，
       本模块绝不复制粘贴别人的数值逻辑，避免两处数值漂移。
    2. 延迟 import（函数体内 import daily / pond），斩断 services 之间的循环依赖。
    3. 所有扣除走 `repo.consume_items`（单事务全有或全无），不会"扣了卡没生效"。
    4. 只拼 `ui.schema.Card` 纯数据，不碰 PIL / 文件 / 网络，无同步 IO、无定时器。
"""

from __future__ import annotations

from datetime import datetime

from ..core import constants as C
from ..core.context import SvcCtx
from ..core.errors import NotEnoughItem, TargetMissing, WhaleError
from ..data import items as item_data
from ..ui.schema import (
    ART_BAG,
    ART_FISH,
    ART_MARRY,
    Card,
    card,
    kv,
    kv_block,
    stat,
    text_block,
)

_LOYAL_KIND = "loyal"

# 体验券 -> (加成文案, 补充说明)；kind 直接用 item.id，钓鱼侧按同名字符串读 buff
_TICKET_BONUS: dict[str, tuple[str, str]] = {
    "ticket_reservoir": (
        f"上钩率 +{C.TICKET_HOOK_BONUS:.0%}",
        "水库场，鱼获与物品品质小幅提升",
    ),
    "ticket_private": (
        f"上钩率 +{C.PRIVATE_POOL_HOOK_BONUS:.0%}",
        "私人放养塘，鱼获品质大幅提升",
    ),
    "ticket_contract": (
        f"上钩率 +{C.PRIVATE_POOL_HOOK_BONUS:.0%}，品质额外 +1 档",
        "私人承包塘，另有鲸币加成",
    ),
}

# 这三张卡不在本模块消耗，必须配合指令使用（指令侧会自动优先消耗卡）
_COMBAT_CARDS: dict[str, str] = {
    "card_wife": "娶群友 @TA",
    "card_good": "离婚 @TA",
    "card_green": "NTR @TA",
}
_COMBAT_HINT = (
    "这张卡要配合指令用："
    + " / ".join(_COMBAT_CARDS.values())
    + "（鲸币不够时会自动用卡，也可以加个「卡」字强制用卡）"
)

_RICE_HINT = "用「喂食 @TA」来喂饭，好感涨得比干吃快"


# ================================================================ 小工具
def _fmt_expire(expires_ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(expires_ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return "-"


async def _consume(ctx: SvcCtx, uid: str, item) -> None:
    """原子扣 1 个；失败抛 NotEnoughItem（message 用物品真名）。"""
    ok, _bag = await ctx.repo.consume_items(uid, {item.id: 1})
    if not ok:
        raise NotEnoughItem(item.name)


def _hint_card(item, title: str, hint: str, art: str = ART_BAG) -> Card:
    c = card(title, item.display, art=art, tone="warn")
    c.blocks.append(
        kv_block(
            "这件道具",
            [kv("道具", item.display), kv("说明", item.desc)],
            footer=f"回收价 {item.sell} 鲸币" if item.sell > 0 else "",
        )
    )
    c.blocks.append(text_block(hint))
    return c.validate()


async def _guard_active(ctx: SvcCtx, subject_uid: str, kind: str, group_id: str, item) -> None:
    """同类 buff 还在生效时挡住二次使用，防止把 24 小时券直接覆盖掉。"""
    buff = await ctx.repo.get_buff(subject_uid, kind, group_id)
    if buff is not None:
        raise WhaleError(
            f"{item.name}还在生效中，别浪费",
            hint=f"当前到期时间 {_fmt_expire(int(buff.get('expires_ts', 0)))}",
        )


# ================================================================ 分支实现
async def _use_ticket(ctx: SvcCtx, uid: str, item) -> Card:
    """体验券：扣 1 张 -> add_buff(uid, item.id, 24h, 群无关)，出到期卡片。"""
    group_id = ""
    await _guard_active(ctx, uid, item.id, group_id, item)
    await _consume(ctx, uid, item)

    expires = await ctx.repo.add_buff(uid, item.id, C.DAY_SECONDS, group_id, {"hours": 24})
    bonus, note = _TICKET_BONUS[item.id]

    c = card(
        "体验券已生效",
        f"{item.display} · 24 小时",
        art=ART_FISH,
        tone="success",
        badge="24h",
        stats=[stat("到期", _fmt_expire(expires)), stat("加成", bonus)],
    )
    c.blocks.append(
        kv_block(
            "生效详情",
            [
                kv("道具", item.display),
                kv("时长", f"24 小时（{C.DAY_SECONDS} 秒）"),
                kv("加成", bonus),
                kv("说明", note),
                kv("到期时间", _fmt_expire(expires)),
            ],
            footer="到期自动失效，无需任何操作",
        )
    )
    return c.validate()


async def _use_loyal(
    ctx: SvcCtx,
    uid: str,
    group_id: str,
    target_uid: str,
    target_name: str,
    item,
) -> Card:
    """钟情卡：必须 @ 目标，扣 1 张 -> 给目标挂 24 小时 loyal 护盾（本群内生效）。"""
    if not str(target_uid or "").strip():
        raise TargetMissing()
    label = (target_name or "").strip() or target_uid[-4:]

    await _guard_active(ctx, target_uid, _LOYAL_KIND, group_id, item)
    await _consume(ctx, uid, item)

    expires = await ctx.repo.add_buff(
        target_uid,
        _LOYAL_KIND,
        C.LOYAL_CARD_DURATION,
        group_id,
        {"hours": 24},  # payload 只允许 int 值（buffs.payload 走 parse_int_dict 读取）
    )

    c = card(
        "钟情护盾已挂上",
        f"给 {label} 挂上了 24 小时钟情护盾",
        art=ART_MARRY,
        tone="success",
        badge="loyal",
        stats=[stat("目标", label), stat("到期", _fmt_expire(expires))],
    )
    c.blocks.append(
        kv_block(
            "护盾详情",
            [
                kv("目标", label),
                kv("道具", item.display),
                kv("时长", "24 小时"),
                kv("到期时间", _fmt_expire(expires)),
                kv("效果", "期间不会被 NTR 走"),
            ],
            footer="只保护本群内的关系",
        )
    )
    c.blocks.append(text_block(f"{label} 现在挂着钟情卡，别人下手会直接被劝退。"))
    return c.validate()


# ================================================================ 入口
async def do_use(
    ctx: SvcCtx,
    uid: str,
    name: str,
    group_id: str,
    arg: str,
    target_uid: str = "",
    target_name: str = "",
    umo: str = "",
) -> Card:
    """使用道具：解析 -> 分派（补签/升塘/体验券/钟情卡/战斗卡/饭/饵）。

    * 只在真正需要扣道具的分支里扣，且只扣一次（补签卡与升塘卡由目标服务自己扣）。
    * umo 目前只做透传预留，本模块不发起任何 LLM / 网络请求。
    """
    item = item_data.resolve_item(arg)
    if item is None:
        raise WhaleError(
            f"没认出「{(arg or '').strip()[:12]}」是什么道具",
            hint="输入 /帮助 看看能做什么",
        )

    # ---- 复用已有服务（函数内延迟 import，避免 services 互相导入成环）
    if item.id == "card_makeup":
        from . import daily  # 内部自己扣卡，这里不再扣

        return await daily.do_makeup(ctx, uid, name, group_id)

    if item.id == "card_pond_up":
        from . import pond  # 内部自己扣卡，这里不再扣

        return await pond.do_use_pond_card(ctx, uid, name, group_id)

    # ---- 天气控制机（3 天天气）
    if item.id == "weather_controller":
        return await _use_weather_controller(ctx, uid, name, group_id, arg, item)

    # ---- 星空券：24h 鱼星 +1
    if item.id == "ticket_star":
        return await _use_star_ticket(ctx, uid, item)

    # ---- 体验券
    if item.id in _TICKET_BONUS:
        return await _use_ticket(ctx, uid, item)

    # ---- 钟情卡
    if item.id == "card_loyal":
        return await _use_loyal(ctx, uid, group_id, target_uid, target_name, item)

    # ---- 战斗卡：不在这里消耗，提示配合指令
    if item.id in _COMBAT_CARDS:
        return _hint_card(item, "这张卡要配合指令用", _COMBAT_HINT)

    # ---- 花园物品（种子/肥料/果实/碗盆）
    if item.category in (item_data.CAT_SEED,):
        return _hint_card(
            item,
            "种子要种进花园",
            f"用 /播种 {item.name} 种下；不带参数的 /播种 会列出背包里的种子",
            art=ART_BAG,
        )
    if item.category == item_data.CAT_FERT:
        return _hint_card(
            item,
            "肥料要施给植物",
            "用 /施肥 氮、/施肥 磷 或 /施肥 钾；每天最多 2 次",
            art=ART_BAG,
        )
    if item.category == item_data.CAT_CROP:
        return _hint_card(
            item,
            "果实可以喂鱼或卖钱",
            f"配一个空碗/空盆发 /喂食 {item.name}，或者 /卖鱼 果实 批量卖",
            art=ART_BAG,
        )
    if item.id in ("bowl_plain", "pot_plain"):
        return _hint_card(
            item,
            "碗和盆用来装果实",
            "先 /播种 收获果实，再发 /喂食 <果实名> 就会自动消耗一个碗/盆",
            art=ART_BAG,
        )

    # ---- 饭
    if item.id in ("rice_bowl", "rice_pot"):
        return _hint_card(item, "饭要喂给群友", _RICE_HINT)

    # ---- 鱼饵
    if item.category == item_data.CAT_BAIT:
        return _hint_card(
            item,
            "鱼饵要挂在钩上",
            f"钓鱼时带上它：/钓鱼 {item.name}（抛竿自动消耗 1 个）",
        )

    # ---- 其余非可用道具
    if not item.usable:
        return _hint_card(
            item,
            "这个用不了",
            f"只能卖钱：/卖鱼 {item.name}（回收价 {item.sell} 鲸币）",
        )

    return _hint_card(item, "暂时没有效果", "输入 /帮助 看看能做什么")


# ================================================================ 天气 / 星空券
_WEATHER_ALIASES: dict[str, str] = {
    "晴天": "sunny", "sunny": "sunny", "太阳": "sunny", "晴": "sunny",
    "多云": "cloudy", "cloudy": "cloudy", "阴天": "cloudy", "阴": "cloudy",
    "小雨": "rain", "rain": "rain", "下雨": "rain", "雨": "rain",
    "雷雨": "storm", "storm": "storm", "雷暴": "storm", "打雷": "storm",
    "大雾": "fog", "fog": "fog", "雾": "fog",
    "流星雨": "meteor", "meteor": "meteor", "流星": "meteor",
    "寒潮": "cold", "cold": "cold", "下雪": "cold", "雪": "cold",
}


async def _use_weather_controller(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str, item) -> Card:
    """天气控制机：把本群天气锁定 3 天；不带参数则随机一个天气。"""
    from ..core import weather as WX
    from ..core.time_utils import now_ts

    raw = (arg or "").strip()
    # `/使用 天气控制机 晴天`：先把物品名/别名从参数里剥掉，只留下天气名
    for cand in (item.name, *item.aliases, item.id):
        if cand and raw.startswith(cand):
            raw = raw[len(cand):].strip()
            break
    key = _WEATHER_ALIASES.get(raw)
    if key is None and not raw:
        key = str(WX.weather_at(group_id or uid, now_ts()).key)
    if key is None:
        raise WhaleError(
            f"没认出天气「{raw[:8]}」",
            hint="可用：晴天/多云/小雨/雷雨/大雾/流星雨/寒潮",
        )
    weather = WX.WEATHERS[key]
    now = int(now_ts())
    until = now + C.WEATHER_CONTROL_SECONDS
    await _consume(ctx, uid, item)
    await ctx.repo.set_pond_weather(group_id or f"private:{uid}", weather.key, until)
    c = card(
        "天气控制机启动",
        f"{name or '你'} 把本群天气改成了 {weather.emoji}{weather.name}",
        art=ART_FISH,
        accent=C.ACCENT_BLUE,
        badge="3 天",
        tone="legend",
        footer="3 天内本群钓鱼等待/上钩/品质都按这个天气走",
    )
    c.stats = [
        stat("天气", f"{weather.emoji}{weather.name}"),
        stat("持续", "3 天"),
        stat("到期", _fmt_expire(until)),
    ]
    c.blocks.append(
        kv_block(
            "控制结果",
            [
                kv("生效范围", "本群所有钓鱼"),
                kv("增益", weather.positive or "无"),
                kv("负面", weather.negative or "无"),
                kv("到期时间", _fmt_expire(until)),
            ],
        )
    )
    return c.validate()


async def _use_star_ticket(ctx: SvcCtx, uid: str, item) -> Card:
    """星空券：24 小时鱼星 +1（与天气、饵料叠加）。"""
    from ..core.time_utils import now_ts

    until = int(now_ts()) + 24 * 3600
    await _consume(ctx, uid, item)
    await ctx.repo.add_buff(uid, "ticket_star", 24 * 3600, "")
    c = card(
        "星空体验券生效",
        "24 小时内鱼星 +1",
        art=ART_FISH,
        accent=C.ACCENT_BLUE,
        badge="+1★",
        tone="success",
        footer="鱼星加成和天气/饵料叠加，去钓一条大的吧",
    )
    c.stats = [stat("持续", "24 小时"), stat("到期", _fmt_expire(until))]
    return c.validate()
