"""个人面板：把散落在 4 张表里的信息合成一张图。

省资源要点：
    * 只做 4 次轻查询（user / harem count / pond / buffs），不做 N+1；
    * 鱼护只统计数量与估值，不把 80 条记录全塞进 Card（Card 有长度上限）；
    * 经验条用 progress_block，渲染层统一画。
"""

from __future__ import annotations

from ..core import constants as C
from ..core.context import SvcCtx, fmt_num, settle_user_day
from ..core.time_utils import day_key, fmt_duration, seconds_to_reset
from ..data.items import CATEGORY_NAMES, get_item
from ..ui.schema import (
    ART_FATFISH,
    card,
    kv,
    kv_block,
    list_block,
    progress_block,
    stat,
    text_block,
)

_BUFF_LABELS = {
    "ticket_reservoir": "水库体验券",
    "ticket_private": "私人放养塘券",
    "ticket_contract": "私人承包塘券",
    "loyal": "钟情护盾",
}


async def do_profile(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """个人面板。无消耗、幂等。"""
    repo = ctx.repo
    user = await repo.get_user(uid, name)
    if settle_user_day(user):
        await repo.update_user(
            uid,
            day_key=user.day_key,
            feed_day=user.feed_day,
            feed_count=user.feed_count,
        )

    group_count = await repo.harem_count(group_id, uid)
    total_count = await repo.total_harem(uid)
    pond = await repo.get_pond(group_id)
    bag = await repo.get_bag(uid)
    buffs = await repo.active_buffs(uid)

    fish_value = sum(int(f.get("p", 0) or 0) for f in bag.fish)
    item_kinds = len([1 for c in bag.items.values() if c > 0])
    buff_bits = []
    for buff in buffs:
        label = _BUFF_LABELS.get(str(buff.get("kind")), str(buff.get("kind")))
        left = max(0, int(buff.get("expires_ts", 0)) - _now())
        buff_bits.append(f"{label}（剩 {fmt_duration(left)}）")

    exp_need = 100 * max(1, user.level)
    c = card(
        "我的鲸鱼馆",
        f"{user.name or name} · 第 {user.level} 级",
        art="profile",
        badge=f"Lv.{user.level}",
        footer="签到/喂食/鱼护状态每日 4:00 刷新；本页数据实时读取",
    )
    c.stats.extend(
        [
            stat("鲸币", fmt_num(user.coins), C.RARITY_COLORS.get(C.RARITY_XIAOJIN, C.ACCENT_BLUE)),
            stat("本群后宫", group_count, C.RARITY_COLORS.get(C.RARITY_XIAOZI, C.ACCENT_BLUE)),
            stat("全部后宫", total_count, C.RARITY_COLORS.get(C.RARITY_XIAOZI, C.ACCENT_BLUE)),
            stat("池塘", f"Lv.{pond.level}", C.ACCENT_BLUE),
        ]
    )

    c.blocks.append(
        progress_block(
            f"经验 {user.exp} / {exp_need}",
            user.exp,
            exp_need,
            text=f"{user.exp}/{exp_need}",
        )
    )
    c.blocks.append(
        kv_block(
            "📊 状态",
            [
                kv("连续签到", f"{user.checkin_streak} 天"),
                kv("累计签到", f"{user.total_checkin} 次"),
                kv("今日喂食", f"{user.feed_count} 次" if user.feed_day == day_key() else "0 次"),
                kv("鱼护", f"{bag.fish_count()} / {ctx.cfg.fish_bag_limit} 条", note=f"估值 {fmt_num(fish_value)} 鲸币"),
                kv("物品", f"{item_kinds} 种"),
                kv("距离重置", fmt_duration(max(0, seconds_to_reset() // 60 * 60))),
            ],
        )
    )

    if buff_bits:
        c.blocks.append(list_block("✨ 生效中的加成", buff_bits[:4]))

    if bag.items:
        top = sorted(bag.items.items(), key=lambda kv_: kv_[1], reverse=True)[:6]
        entries = []
        for item_id, count in top:
            item = get_item(item_id)
            if not item:
                continue
            cat = CATEGORY_NAMES.get(item.category, "")
            entries.append(f"{item.name}·{item.rarity} ×{count} {cat}")
        if entries:
            c.blocks.append(list_block("🎒 背包精选", entries))

    c.blocks.append(
        text_block("想变强？签到 → 打工 → 买饵 → 钓鱼 → 卖鱼，一条龙循环。")
    )
    return c


def _now() -> int:
    from ..core.time_utils import now_ts

    return now_ts()