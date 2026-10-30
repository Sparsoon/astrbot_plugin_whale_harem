"""签到 / 补签（惰性日结，零定时器）。

2G 机器省法：
1. 只读 1 次 users 行；奖励合并成 1 次 update_user + 1 次 grant，全程 <6 条 SQL。
2. 连续/断签判断只用整数天序号比较，不解析时间串、不建 datetime 对象。
3. 4 项饵料权重直接 pick_weighted（O(n), n=4），比建 Alias 表更省。
4. 补签卡"全有或全无"由 consume_items 单事务保证，不存在半成品状态。
5. 唯一"每日"逻辑是 day_key 比对，没有任何定时器 / 轮询协程。
"""

from __future__ import annotations

from ..core import constants as C
from ..core import rng
from ..core.context import SvcCtx, grant, settle_user_day
from ..core.errors import NotEnoughItem, WhaleError
from ..core.time_utils import day_key, days_between, fmt_date, fmt_duration, now_ts, seconds_to_reset
from ..data.items import name_of
from ..ui.schema import ART_CHECKIN, Card, card, list_block, progress_block, stat, text_block

BAIT_WEIGHTS: dict[str, int] = {
    "bait_normal": 60,
    "bait_worm": 20,
    "bait_bloodworm": 12,
    "bait_corn": 8,
}
CHECKIN_EXP = 10        # 签到经验
MAKEUP_EXP = 5          # 补签经验
MAKEUP_CARD_ID = "card_makeup"
RICE_ID = "rice_bowl"
MAKEUP_BREAK_GAP = 2    # today - last > 2 视为断签，连续数重置为 1 再 +1


# ---------------------------------------------------------------- 内部工具
def _streak_bonus(streak: int) -> int:
    """长线签到加成：第 1 天 0，第 20 天 +50（总收益 100），之后封顶。

    线性但温和（每天约 +2.63），第 20 天达到 100 鲸币/天。
    """
    days = max(1, min(int(streak), C.CHECKIN_MAX_STREAK_DAYS))
    if days <= 1:
        return 0
    span = max(1, C.CHECKIN_MAX_STREAK_DAYS - 1)
    bonus = round((C.CHECKIN_MAX_COINS - C.CHECKIN_BASE_COINS) * (days - 1) / span)
    return max(0, min(C.CHECKIN_MAX_COINS - C.CHECKIN_BASE_COINS, bonus))


def _apply_settled(user, **fields) -> dict:
    """把 settle_user_day 就地改动的"按天"字段一并带进 update_user，保证惰性结算落库。"""
    fields.setdefault("day_key", user.day_key)
    fields.setdefault("feed_day", user.feed_day)
    fields.setdefault("feed_count", user.feed_count)
    fields.setdefault("feed_slots", getattr(user, "feed_slots", ""))
    return fields


def _bonus_progress_block(streak: int, bonus: int):
    """连续加成进度条（20 天长线，满额 100 鲸币/天）。"""
    max_days = max(1, C.CHECKIN_MAX_STREAK_DAYS)
    days = min(max(1, streak), max_days)
    left = max(0, max_days - days)
    if left:
        text = f"{days}/{max_days} 天 · 再连签 {left} 天满额"
    else:
        text = f"{days}/{max_days} 天 · 加成已满 +{bonus}（{C.CHECKIN_MAX_COINS} 鲸币/天）"
    return progress_block(f"连签加成（{max_days} 天满额）", days, max_days, text=text)


# ---------------------------------------------------------------- 签到
async def do_checkin(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """每日签到：基础 50 + 连签加成（第 20 天封顶 100），送 1~3 个饵料。

    幂等：同一游戏日（凌晨 4 点切换）重复签到抛 WhaleError。
    """
    repo = ctx.repo
    cfg = ctx.cfg
    user = await repo.get_user(uid, name)
    settle_user_day(user)
    today = day_key()

    if user.last_checkin_day == today:
        raise WhaleError(
            "今天已经签到过了喵",
            hint=f"凌晨 4 点刷新，还有 {fmt_duration(seconds_to_reset())}",
        )

    if user.last_checkin_day and days_between(user.last_checkin_day, today) == 1:
        streak = max(1, user.checkin_streak) + 1
    else:
        streak = 1

    base = max(0, int(cfg.checkin_base_coins))
    bonus = _streak_bonus(streak)
    coins = min(C.CHECKIN_MAX_COINS, base + bonus)

    drops: dict[str, int] = {}
    for _ in range(rng.randint(C.CHECKIN_BAIT_MIN, C.CHECKIN_BAIT_MAX)):
        bait_id = rng.pick_weighted(BAIT_WEIGHTS)
        drops[bait_id] = drops.get(bait_id, 0) + 1
    if rng.chance(C.CHECKIN_RICE_CHANCE):
        drops[RICE_ID] = drops.get(RICE_ID, 0) + 1

    await repo.update_user(
        uid,
        last_checkin_day=today,
        checkin_streak=streak,
        total_checkin=max(0, user.total_checkin) + 1,
        **_apply_settled(user),
    )
    bag = await grant(ctx, uid, coins=coins, items=drops, exp=CHECKIN_EXP)
    balance = user.coins + coins

    c = card(
        f"签到成功 · 连续 {streak} 天",
        f"{fmt_date(today)} · 累计签到 {max(0, user.total_checkin) + 1} 天",
        art=ART_CHECKIN,
        accent=C.ACCENT_BLUE,
        badge=f"+{coins}",
        tone="success",
        footer="每天凌晨 4 点刷新 · 断签会重置连签",
    )
    c.stats = [
        stat("鲸币余额", balance, C.ACCENT_BLUE),
        stat("连续签到", f"{streak} 天", C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("今日奖励", f"+{coins}", C.RARITY_COLORS[C.RARITY_XIAOLAN]),
    ]

    entries = [
        f"鲸币 +{coins}（基础 {base} + 连签 {bonus}）",
        f"连续 {streak} 天，20 天满额 {C.CHECKIN_MAX_COINS} 鲸币/天",
        f"经验 +{CHECKIN_EXP}",
    ]
    for item_id, num in drops.items():
        if item_id == RICE_ID:
            entries.append(
                f"额外掉落：{name_of(RICE_ID)} x{num}"
                f"（{C.CHECKIN_RICE_CHANCE:.0%} 概率，现有 {bag.count(RICE_ID)}）"
            )
        else:
            entries.append(f"{name_of(item_id)} x{num}")
    c.blocks.append(list_block("奖励明细", entries))
    c.blocks.append(_bonus_progress_block(streak, bonus))
    c.blocks.append(text_block("连签越长收益越高，断一天会重头再来喵。"))
    return c.validate()


# ---------------------------------------------------------------- 补签
async def do_makeup(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """补签：消耗 1 张补签卡，补回最近漏掉的一天。

    * 今天已签 / 没有漏签 -> WhaleError；
    * 断签超过 2 天：连续数重置为 1；
    * 只补一天（昨天的位置），不补更早的。
    """
    repo = ctx.repo
    user = await repo.get_user(uid, name)
    settle_user_day(user)
    today = day_key()

    if not user.last_checkin_day:
        raise WhaleError("还没有签到记录喵", hint="先 /签到 一次，再来补签")

    gap = days_between(user.last_checkin_day, today)
    if gap <= 1 and user.last_checkin_day == day_key(now_ts() - 86400):
        raise WhaleError("昨天已经签过啦", hint="没有漏签，不需要补签卡")
    if gap <= 0:
        raise WhaleError("今天已经签过啦", hint="明天再来吧")

    ok, bag = await repo.consume_items(uid, {MAKEUP_CARD_ID: 1})
    if not ok:
        raise NotEnoughItem(name_of(MAKEUP_CARD_ID), 1)

    streak = max(1, user.checkin_streak) + 1 if gap <= MAKEUP_BREAK_GAP else 1
    yesterday = day_key(now_ts() - 86400)
    await repo.update_user(
        uid,
        last_checkin_day=yesterday,
        checkin_streak=streak,
        total_checkin=max(0, user.total_checkin) + 1,
        **_apply_settled(user),
    )
    await grant(ctx, uid, exp=MAKEUP_EXP)

    c = card(
        "补签成功",
        f"{fmt_date(yesterday)} 已补上 · 连续 {streak} 天",
        art=ART_CHECKIN,
        accent=C.ACCENT_BLUE,
        badge="补签",
        tone="success",
        footer="补签卡已消耗 1 张",
    )
    c.stats = [
        stat("连续签到", f"{streak} 天", C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("补签卡", f"剩 {bag.count(MAKEUP_CARD_ID)} 张", C.RARITY_COLORS[C.RARITY_XIAOLAN]),
    ]
    c.blocks.append(
        text_block(f"经验 +{MAKEUP_EXP}，明天记得准时来喵。", title="补签完成")
    )
    return c.validate()
