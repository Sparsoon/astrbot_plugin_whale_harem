"""service 层共享上下文与通用助手。

所有 service 都是纯函数式：入参 = (SvcCtx, uid, ...)，出参 = ui.schema.Card。
这样既方便并行开发，也方便以后把玩法拆成独立进程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .config import PluginConfig
from .constants import PLUGIN_NAME
from .db.repos import Repo
from .llm import LLMClient
from .models import Bag, User
from .time_utils import day_key


@dataclass(slots=True)
class SvcCtx:
    repo: Repo
    cfg: PluginConfig
    llm: LLMClient
    plugin_name: str = PLUGIN_NAME

    @property
    def db(self):  # 便捷透传，避免 service 里写 ctx.repo.db
        return self.repo.db


# ---------------------------------------------------------------- 惰性日常结算
def settle_user_day(user: User) -> bool:
    """把用户身上所有"按天"的计数字段就地结算到当前游戏日。

    返回 True 表示有字段被改动，调用方需要落库。
    这是全插件唯一的"每日重置"实现：没有定时器，没有轮询，
    哪怕机器人停机三天，下次交互时依旧会正确结算。
    """
    today = day_key()
    changed = False
    if user.feed_day != today:
        user.feed_day = today
        user.feed_count = 0
        changed = True
    if user.day_key != today:
        user.day_key = today
        changed = True
    return changed


def settle_harem_day(row) -> bool:
    """群友关系上的每日计数（娶/离/NTR 的保底进度）惰性清零。

    同时结算"恩爱值"的自然增长：每在一起一天 +HAREM_AFFECTION_PER_DAY（封顶 999）。
    喂食已改为喂 bot，不再影响这里的数值，两个系统彻底解耦。
    """
    today = day_key()
    if row.day_key == today:
        return False
    from .constants import HAREM_AFFECTION_PER_DAY
    from .time_utils import days_between

    gap = max(0, days_between(row.day_key, today)) if row.day_key else 1
    row.marry_fail = 0
    row.ntr_fail = 0
    row.divorce_fail = 0
    growth = int(gap * HAREM_AFFECTION_PER_DAY)
    if growth > 0:
        row.affection = min(999, max(0, row.affection) + growth)
    row.day_key = today
    return True


async def grant(
    ctx: SvcCtx,
    uid: str,
    *,
    coins: int = 0,
    items: Mapping[str, int] | None = None,
    exp: int = 0,
) -> Bag:
    """统一发奖入口：鲸币 / 物品 / 经验一次到位，减少事务次数。"""
    bag: Bag | None = None
    if items:
        bag = await ctx.repo.add_items(uid, items)
    if coins:
        await ctx.repo.add_coins(uid, coins)
    if exp:
        await ctx.repo.add_exp(uid, exp)
    if bag is None:
        bag = await ctx.repo.get_bag(uid)
    return bag


def fmt_num(value: int | float) -> str:
    """大数字友好显示：12800 -> 1.28w。"""
    v = int(value)
    if abs(v) < 10000:
        return str(v)
    return f"{v / 10000:.2f}w"