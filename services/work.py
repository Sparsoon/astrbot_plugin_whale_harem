"""打工（上工 -> 等待 -> 收工，两段式；惰性时间戳，绝不 sleep）。

规则（需求）：
    * 上工随机时长 t ∈ [配置 60,1440] 分钟（1 小时 ~ 1 天）；等待期间不能再次打工；
    * **收益与等待时长成正比**：coins = t × 0.35/分钟 × 浮动 ±25%，12% 概率暴击 ×1.8；
    * 到点后本人发一条消息（或再发 /打工）收工，工资一次性到账；
    * 钓鱼与打工互不阻塞：可以一边等鱼一边上班。

2G 机器省法：
    1. 上工只写 4 个整数列，收工只读一次 user + 两个写操作；
    2. 收益在上工时算好并落库（`work_pending_coins`），收工时不再掷骰，避免重开刷钱；
    3. 随机全走 core.rng，不建临时列表、不排序。
"""

from __future__ import annotations

from datetime import datetime

from ..core import constants as C
from ..core import rng
from ..core.context import SvcCtx, fmt_num, settle_user_day
from ..core.errors import WhaleError
from ..core.time_utils import fmt_duration, now_ts
from ..ui.schema import ART_WORK, Card, card, kv, kv_block, stat, text_block

WORK_COIN_FLOOR = 24   # 单次打工鲸币保底
WORK_EXP = 5           # 每次收工经验


def _duration_text(minutes: int) -> str:
    """把分钟数写成"3 小时 20 分"，短班保留分钟。"""
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, left = divmod(minutes, 60)
    return f"{hours} 小时 {left} 分" if left else f"{hours} 小时"


def _fmt_clock(ts: int) -> str:
    """时间戳 -> "18:40"（只用于文案，异常时退化为 -）。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


def _roll_pay(minutes: int) -> tuple[int, bool]:
    """按等待时长算工资：时长 × 0.35/分钟 × 浮动 ±25%，12% 暴击 ×1.8。"""
    spread = max(0.0, float(C.WORK_RANDOM_SPREAD))
    raw = minutes * float(C.WORK_COIN_PER_MINUTE) * rng.uniform(1.0 - spread, 1.0 + spread)
    base = max(WORK_COIN_FLOOR, int(raw))
    crit = rng.chance(C.WORK_BONUS_CHANCE)
    coins = max(WORK_COIN_FLOOR, int(round(base * float(C.WORK_BONUS_RATE)))) if crit else base
    return coins, crit


async def do_work(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """打工：上工 / 收工两段式。

    消耗：无鲸币/道具，只占用时间（等待期内不能钓鱼与再次打工）。
    产出：收工时按上工随机时长结算鲸币，经验 +5。
    """
    repo = ctx.repo
    cfg = ctx.cfg
    user = await repo.get_user(uid, name)
    settle_user_day(user)
    now = now_ts()

    # 钓鱼与打工互不阻塞：可以一边等鱼一边上班（v1.2 起解耦）

    # 已有未结算的工作
    if user.work_pending_coins > 0 and user.work_ready_ts:
        if user.work_ready_ts > now:
            raise WhaleError(
                f"你还在上班，剩 {fmt_duration(user.work_ready_ts - now)}",
                hint=f"{_fmt_clock(user.work_ready_ts)} 收工（再发一次 /打工）",
            )
        return await _settle(ctx, user, now)

    # 上工
    lo = max(1, int(cfg.work_cooldown_min))
    hi = max(lo, int(cfg.work_cooldown_max))
    minutes = int(rng.randint(lo, hi))
    coins, crit = _roll_pay(minutes)
    ready = now + minutes * 60
    await repo.update_user(
        uid,
        work_started_ts=now,
        work_minutes=minutes,
        work_pending_coins=coins,
        work_ready_ts=ready,
        day_key=user.day_key,
        feed_day=user.feed_day,
        feed_count=user.feed_count,
    )

    c = card(
        "上工了，慢慢干…",
        f"本次班 {_duration_text(minutes)} · 工资随等待时长涨",
        art=ART_WORK,
        accent=C.RARITY_COLORS[C.RARITY_DAJIN] if crit else C.ACCENT_BLUE,
        badge=_duration_text(minutes),
        tone="legend" if crit else "normal",
        footer="到点后再发一次 /打工，或随便发条消息自动收工",
    )
    c.stats = [
        stat("等待", fmt_duration(minutes * 60)),
        stat("预计工资", f"{coins} 鲸币", C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("收工时间", _fmt_clock(ready)),
    ]
    c.blocks.append(
        kv_block(
            "上工信息",
            [
                kv("本次时长", f"{_duration_text(minutes)}（{lo}~{hi} 分钟随机）"),
                kv("工资公式", f"{minutes} × {C.WORK_COIN_PER_MINUTE} × 浮动 ±{int(C.WORK_RANDOM_SPREAD * 100)}%"),
                kv("摸鱼暴击", f"是，×{C.WORK_BONUS_RATE}（{C.WORK_BONUS_CHANCE:.0%} 概率）" if crit else f"没有（{C.WORK_BONUS_CHANCE:.0%} 概率）"),
                kv("预计到手", f"{coins} 鲸币"),
                kv("收工时间", _fmt_clock(ready)),
            ],
        )
    )
    c.blocks.append(text_block("老板说干满这段时间才有钱，别中途跑路喵。"))
    return c.validate()


async def settle_work(ctx: SvcCtx, user) -> Card | None:
    """群消息触发收工：到点才结算；没到点/没待领工资返回 None。

    由 main.py 的群消息监听调用——"到时间后发消息才触发"。
    """
    now = now_ts()
    if not user.work_pending_coins or not user.work_ready_ts or user.work_ready_ts > now:
        return None
    return await _settle(ctx, user, now)


async def _settle(ctx: SvcCtx, user, now: int) -> Card:
    """收工：原子领取上工时算好的工资并发放（监听器与指令同时触发也只发一次）。"""
    repo = ctx.repo
    grabbed = await repo.take_work_pending(user.uid, now)
    if grabbed is None:
        c = card("没有待领的工资", "这一班已经结过啦", art=ART_WORK, tone="warn")
        c.blocks.append(text_block("再发一次 /打工 就能开新的一班喵。"))
        return c.validate()
    coins, minutes = grabbed
    uid = user.uid

    balance = int(await repo.add_coins(uid, coins))
    level, _exp, leveled = await repo.add_exp(uid, WORK_EXP)

    footer = "休息一下，下一班再来"
    if leveled:
        footer += f" · 升级啦 Lv.{level}"
    c = card(
        "收工，领工资！",
        f"干了 {_duration_text(minutes)}，到手 {coins} 鲸币",
        art=ART_WORK,
        accent=C.ACCENT_BLUE,
        badge=f"+{coins}",
        tone="success",
        footer=footer,
    )
    c.stats = [
        stat("到手鲸币", coins, C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("当前余额", fmt_num(balance), C.ACCENT_BLUE),
        stat("本次时长", _duration_text(minutes)),
    ]
    c.blocks.append(
        kv_block(
            "结算",
            [
                kv("本次时长", _duration_text(minutes)),
                kv("到手工资", f"{coins} 鲸币"),
                kv("经验", f"+{WORK_EXP}"),
                kv("当前余额", f"{balance} 鲸币"),
            ],
        )
    )
    c.blocks.append(text_block("工资和等待时长挂钩，想多赚就多上会儿班喵。"))
    return c.validate()
