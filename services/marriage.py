"""娶群友 / 离婚 / NTR / 后宫榜 —— 社交主线（A4）。

设计要点（2 核 2G 硬约束）
    1. 零定时器：每日保底进度全靠 `settle_harem_day` 惰性清零，停机再久也不会漏重置。
    2. 零同步阻塞：只算数值 + 拼 `ui.schema.Card`（纯数据），不碰 PIL / 文件 / 网络。
    3. 单事务：对 harem 行的写全部塞进 `repo.mutate_harem` 的一次 BEGIN IMMEDIATE，
       两个人同时抢同一个目标时，后到者看到的是先到者已经写下的 owner。
    4. 随机一律走 `core/rng.py::chance`，可注入种子复现。
    5. 顺序固定：校验 -> 扣费 -> 事务内掷骰 -> 出卡。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core import constants as C
from ..core import rng as R
from ..core.context import SvcCtx, fmt_num, settle_harem_day
from ..core.errors import (
    AlreadyOwned,
    NotEnoughCoins,
    NotEnoughItem,
    NotInGroup,
    TargetMissing,
    WhaleError,
)
from ..core.models import HaremRow
from ..data import items as item_data
from ..ui.schema import (
    ART_MARRY,
    Block,
    Card,
    card,
    kv,
    kv_block,
    list_block,
    stat,
    text_block,
)

# 契约书里写死、但 constants 未提供的好感增减量（以具名常量表达，避免魔法数字）
MARRY_AFFECTION_GAIN = 5
NTR_AFFECTION_LOSS = 10

_WIFE_CARD = "card_wife"
_GOOD_CARD = "card_good"
_GREEN_CARD = "card_green"
_LOYAL_KIND = "loyal"

# 离婚步进：constants 只给了 DIVORCE_BASE_RATE + DIVORCE_PITY(2)，
# 即"每次失败 +50%，第 2 次必中"，等价于 STEP == BASE，故此处以 BASE 复用，不新增平衡数值。
_DIVORCE_STEP_RATE = C.DIVORCE_BASE_RATE

_MEDALS = ("🥇", "🥈", "🥉")
_LIST_CHUNK = 8  # ui.schema.Card.validate() 把 list 行截到 8 行，主动分块防吞条目
_EMPTY_PUBLIC = "群里还没有公共老婆，快去 @ 一个开始攻略吧"


# ================================================================ 小工具
def _pct(rate: float) -> str:
    """0.125 -> "12.5%"，整数百分比不带小数点。"""
    value = 100.0 * max(0.0, min(1.0, float(rate)))
    return f"{value:.0f}%" if abs(value - round(value)) < 0.05 else f"{value:.1f}%"


def _marry_rate(fail: int) -> float:
    """娶群友成功率：10% 起步，每次失败 +10%，第 5 次强制 100%（不看喂食好感）。"""
    fail = max(0, int(fail))
    if fail >= C.MARRY_PITY - 1:
        return 1.0
    return min(1.0, C.MARRY_BASE_RATE + C.MARRY_STEP_RATE * fail)


def _divorce_rate(fail: int) -> float:
    """离婚成功率：50% 起步，每次失败 +50%，第 2 次强制 100%。"""
    fail = max(0, int(fail))
    if fail >= C.DIVORCE_PITY - 1:
        return 1.0
    return min(1.0, C.DIVORCE_BASE_RATE + _DIVORCE_STEP_RATE * fail)


def _ntr_rate(fail: int) -> float:
    """NTR 成功率：5% 起步，每次失败 +5%，第 10 次强制 100%。"""
    fail = max(0, int(fail))
    if fail >= C.NTR_PITY - 1:
        return 1.0
    return min(1.0, C.NTR_BASE_RATE + C.NTR_STEP_RATE * fail)


def _days_since(created_at: str) -> int:
    """created_at(YYYY-MM-DD HH:MM:SS) -> 持续天数；坏数据一律按 0 天。"""
    try:
        start = datetime.strptime((created_at or "")[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 0
    return max(0, int((datetime.now() - start).days))


def _require_group(group_id: str) -> None:
    if not str(group_id or "").strip():
        raise NotInGroup()


def _require_target(target_uid: str, uid: str, message: str) -> None:
    if not str(target_uid or "").strip():
        raise TargetMissing()
    if target_uid == uid:
        raise WhaleError(message, hint="换一位群友试试")


def _name_or_tail(name: str, uid: str) -> str:
    return (name or "").strip() or uid[-4:]


def _list_blocks(title: str, entries: list[str], footer: str = "") -> list[Block]:
    """列表分块：validate() 会把单块 lines 截断到 8 行，这里主动切块避免条目被吞。"""
    if not entries:
        return [list_block(title, ["（暂无）"], footer=footer)]
    blocks: list[Block] = []
    for start in range(0, len(entries), _LIST_CHUNK):
        part = entries[start : start + _LIST_CHUNK]
        head = title if start == 0 else f"{title} 续"
        tail = start + _LIST_CHUNK >= len(entries)
        blocks.append(list_block(head, part, footer=footer if tail else ""))
    return blocks


async def _charge(
    ctx: SvcCtx,
    uid: str,
    name: str,
    card_id: str,
    cost: int,
    use_card: bool,
) -> str:
    """先校验后扣费，返回给用户看的"消耗"文案。

    卡券由调用方通过 use_card 决定；未显式用卡且鲸币不足时，自动兜底消耗同名卡券。
    扣费发生在 mutate_harem 之前；若事务内因状态变化抛错，本次消耗不退，
    这是契约书允许的"损失一次机会"，换来的是无跨表补偿逻辑、无二次事务。
    """
    card_name = item_data.name_of(card_id)
    if use_card:
        ok, _bag = await ctx.repo.consume_items(uid, {card_id: 1})
        if not ok:
            raise NotEnoughItem(card_name)
        return f"{card_name} x1"

    user = await ctx.repo.get_user(uid, name)
    if user.coins < cost:
        # 文案承诺「鲸币不够时会自动用卡」：余额不足时优先兜底用卡，不把人拦在门口。
        bag = await ctx.repo.get_bag(uid)
        if bag.count(card_id) > 0:
            ok, _bag = await ctx.repo.consume_items(uid, {card_id: 1})
            if ok:
                return f"{card_name} x1（鲸币不足，自动用卡）"
        raise NotEnoughCoins(cost, user.coins)
    if not await ctx.repo.spend_coins(uid, cost):
        latest = await ctx.repo.get_user(uid, name)  # 并发花掉了余额，取最新余额报错
        raise NotEnoughCoins(cost, latest.coins)
    return f"{cost} 鲸币"


# ================================================================ 娶群友
async def do_marry(
    ctx: SvcCtx,
    uid: str,
    name: str,
    group_id: str,
    target_uid: str,
    target_name: str,
    use_card: bool = False,
) -> Card:
    """娶群友：100 鲸币 或 娶群友卡 x1；初始 10%，每次失败 +10%，第 5 次必中。

    消耗：MARRY_COST 鲸币（use_card=False）/ card_wife x1（use_card=True）。
    保底：结算后 marry_fail >= MARRY_PITY-1 时强制 100%。
    成功：owner=uid、恩爱值 +5、chase_count +1；失败：marry_fail +1、chase_count +1。
    注意：成功率只与失败次数有关，与喂食好感无关（两个系统已解耦）。
    """
    _require_group(group_id)
    _require_target(target_uid, uid, "不能娶自己啦")
    display_name = _name_or_tail(name, uid)
    label = _name_or_tail(target_name, target_uid)

    row = await ctx.repo.get_harem(group_id, target_uid)
    if row is None:
        row = await ctx.repo.ensure_harem(group_id, target_uid, label)
    settle_harem_day(row)  # 仅为"扣费前"快速失败；权威结算在事务内重做一次
    if row.owner_uid == uid:
        raise WhaleError("她/他已经在你后宫了")
    if row.owner_uid:
        raise AlreadyOwned(row.owner_label)

    cost_text = await _charge(ctx, uid, display_name, _WIFE_CARD, C.MARRY_COST, use_card)

    out: dict[str, Any] = {}

    def _mut(entry: HaremRow) -> None:
        settle_harem_day(entry)
        if entry.owner_uid == uid:
            raise WhaleError("她/他已经在你后宫了")
        if entry.owner_uid:
            raise AlreadyOwned(entry.owner_label)
        fail = max(0, entry.marry_fail)
        rate = _marry_rate(fail)
        entry.chase_count += 1
        hit = R.chance(rate)
        if hit:
            entry.owner_uid = uid
            entry.owner_name = display_name
            # 换主人 -> 其他保底进度归零，避免新主人继承上一位的离婚/NTR 保底
            entry.marry_fail = 0
            entry.ntr_fail = 0
            entry.divorce_fail = 0
            entry.affection = max(0, entry.affection) + MARRY_AFFECTION_GAIN
        else:
            entry.marry_fail = fail + 1
        out.update(
            rate=rate,
            hit=hit,
            attempt=min(fail + 1, C.MARRY_PITY),
            next_rate=_marry_rate(entry.marry_fail),
        )

    entry = await ctx.repo.mutate_harem(group_id, target_uid, _mut, label)

    rate = float(out.get("rate", 0.0))
    attempt = int(out.get("attempt", 1))
    next_rate = float(out.get("next_rate", 0.0))
    pity_text = f"第 {attempt}/{C.MARRY_PITY} 次（{C.MARRY_PITY} 次内必中）"

    if out.get("hit"):
        c = card(
            "成婚！",
            f"{display_name} 把 {label} 娶回了后宫",
            art=ART_MARRY,
            tone="success",
            badge=f"恩爱 {entry.affection}",
            stats=[
                stat("本次成功率", _pct(rate)),
                stat("尝试", f"第 {attempt}/{C.MARRY_PITY} 次"),
                stat("消耗", cost_text),
            ],
        )
        c.blocks.append(
            kv_block(
                "求婚细节",
                [
                    kv("本次成功率", _pct(rate)),
                    kv("尝试次数", pity_text),
                    kv("消耗", cost_text),
                    kv("恩爱值", f"{entry.affection}（+{MARRY_AFFECTION_GAIN}）"),
                    kv("结果", "成婚！"),
                ],
            )
        )
        c.blocks.append(
            text_block(
                f"从今天起，{label} 就是你后宫的人了……",
                footer="每日凌晨 4 点惰性重置保底进度",
            )
        )
        return c.validate()

    c = card(
        "求婚失败",
        f"{label} 婉拒了 {display_name} 的求婚",
        art=ART_MARRY,
        tone="warn",
        stats=[
            stat("本次成功率", _pct(rate)),
            stat("下次成功率", _pct(next_rate)),
            stat("尝试", f"第 {attempt}/{C.MARRY_PITY} 次"),
        ],
    )
    c.blocks.append(
        kv_block(
            "求婚细节",
            [
                kv("本次成功率", _pct(rate)),
                kv("尝试次数", pity_text),
                kv("消耗", cost_text),
                kv("结果", "被拒绝了……"),
                kv("下次成功率", _pct(next_rate)),
            ],
            footer=f"下次成功率 {_pct(next_rate)}，第 {C.MARRY_PITY} 次必中",
        )
    )
    c.blocks.append(
        text_block(
            f"下次成功率 {_pct(next_rate)}，再试 {C.MARRY_PITY - attempt} 次内必中（成功率只与失败次数有关）。",
            footer="每天凌晨 4 点重置保底进度",
        )
    )
    return c.validate()


# ================================================================ 离婚
async def do_divorce(
    ctx: SvcCtx,
    uid: str,
    name: str,
    group_id: str,
    target_uid: str,
    target_name: str,
    use_card: bool = False,
) -> Card:
    """离婚：100 鲸币 或 好人卡 x1；50% 起步，第 2 次必中。

    消耗：DIVORCE_COST 鲸币（use_card=False）/ card_good x1（use_card=True）。
    限制：只能离自己后宫里的目标（owner_uid 必须是自己）。
    成功：owner 清空、divorce_fail 归零、好感腰斩，目标回到公共老婆池；
    失败：divorce_fail +1，提示下次成功率。
    """
    _require_group(group_id)
    _require_target(target_uid, uid, "不能和自己离婚啦")
    display_name = _name_or_tail(name, uid)
    label = _name_or_tail(target_name, target_uid)

    row = await ctx.repo.get_harem(group_id, target_uid)
    if row is None:
        raise WhaleError("这不是你的老婆", hint="先用「娶群友 @TA」把人娶回来")
    settle_harem_day(row)
    if row.owner_uid != uid:
        raise WhaleError("这不是你的老婆", hint="只能离自己后宫里的目标")

    cost_text = await _charge(ctx, uid, display_name, _GOOD_CARD, C.DIVORCE_COST, use_card)

    out: dict[str, Any] = {}

    def _mut(entry: HaremRow) -> None:
        settle_harem_day(entry)
        if entry.owner_uid != uid:
            raise WhaleError("这不是你的老婆", hint="只能离自己后宫里的目标")
        fail = max(0, entry.divorce_fail)
        rate = _divorce_rate(fail)
        hit = R.chance(rate)
        if hit:
            entry.owner_uid = ""
            entry.owner_name = ""
            entry.divorce_fail = 0
            entry.affection = max(0, entry.affection) // 2  # 好感腰斩
        else:
            entry.divorce_fail = fail + 1
        out.update(
            rate=rate,
            hit=hit,
            attempt=min(fail + 1, C.DIVORCE_PITY),
            next_rate=_divorce_rate(entry.divorce_fail),
        )

    entry = await ctx.repo.mutate_harem(group_id, target_uid, _mut, label)

    rate = float(out.get("rate", 0.0))
    attempt = int(out.get("attempt", 1))
    next_rate = float(out.get("next_rate", 0.0))
    pity_text = f"第 {attempt}/{C.DIVORCE_PITY} 次（{C.DIVORCE_PITY} 次内必中）"

    if out.get("hit"):
        c = card(
            "离婚成功",
            f"{display_name} 和 {label} 一拍两散",
            art=ART_MARRY,
            tone="normal",
            badge="单身了",
            stats=[
                stat("本次成功率", _pct(rate)),
                stat("尝试", f"第 {attempt}/{C.DIVORCE_PITY} 次"),
                stat("消耗", cost_text),
            ],
        )
        c.blocks.append(
            kv_block(
                "离婚细节",
                [
                    kv("本次成功率", _pct(rate)),
                    kv("尝试次数", pity_text),
                    kv("消耗", cost_text),
                    kv("好感度", f"{entry.affection}（腰斩）"),
                    kv("结果", f"{label} 回到公共老婆池"),
                ],
            )
        )
        c.blocks.append(
            text_block(
                f"{label} 现在是公共老婆了，小心别人下手。",
                footer="每日凌晨 4 点惰性重置保底进度",
            )
        )
        return c.validate()

    c = card(
        "离婚失败",
        f"{label} 还没同意和 {display_name} 分手",
        art=ART_MARRY,
        tone="warn",
        stats=[
            stat("本次成功率", _pct(rate)),
            stat("下次成功率", _pct(next_rate)),
            stat("尝试", f"第 {attempt}/{C.DIVORCE_PITY} 次"),
        ],
    )
    c.blocks.append(
        kv_block(
            "离婚细节",
            [
                kv("本次成功率", _pct(rate)),
                kv("尝试次数", pity_text),
                kv("消耗", cost_text),
                kv("结果", "没离成……"),
                kv("下次成功率", _pct(next_rate)),
            ],
            footer=f"下次成功率 {_pct(next_rate)}，第 {C.DIVORCE_PITY} 次必中",
        )
    )
    c.blocks.append(text_block(f"下次成功率 {_pct(next_rate)}，再试一次吧。"))
    return c.validate()


# ================================================================ NTR
async def do_ntr(
    ctx: SvcCtx,
    uid: str,
    name: str,
    group_id: str,
    target_uid: str,
    target_name: str,
    use_card: bool = False,
) -> Card:
    """NTR：125 鲸币 或 绿帽卡 x1；5% 起步，每次失败 +5%，第 10 次必中。

    消耗：NTR_COST 鲸币（use_card=False）/ card_green x1（use_card=True，用了就消耗，失败不退）。
    前置：目标必须已被别人娶走；目标挂着钟情卡（loyal buff）时直接拒绝且**不扣费**。
    成功：owner 变成自己、ntr_count +1、ntr_fail/marry_fail 归零、好感 -10（最低 0）；
    失败：ntr_fail +1。
    """
    _require_group(group_id)
    _require_target(target_uid, uid, "不能 NTR 自己啦")
    display_name = _name_or_tail(name, uid)
    label = _name_or_tail(target_name, target_uid)

    row = await ctx.repo.get_harem(group_id, target_uid)
    if row is None or not row.owner_uid:
        raise WhaleError("对方还是单身，直接娶吧", hint=f"娶群友 @{label} 更省钱")
    settle_harem_day(row)
    if row.owner_uid == uid:
        raise WhaleError("对方已经是你的人了，不用 NTR 啦")

    # 钟情卡护盾：命中即拒绝，且发生在扣费之前，保证"不扣费"
    if await ctx.repo.has_buff(target_uid, _LOYAL_KIND, group_id):
        raise WhaleError(
            "对方挂着钟情卡，今天下手不了",
            hint="钟情护盾 24 小时后自动过期，或被护盾挡下的这次不扣费",
        )

    cost_text = await _charge(ctx, uid, display_name, _GREEN_CARD, C.NTR_COST, use_card)

    out: dict[str, Any] = {}

    def _mut(entry: HaremRow) -> None:
        settle_harem_day(entry)
        if not entry.owner_uid:
            raise WhaleError("对方还是单身，直接娶吧")
        if entry.owner_uid == uid:
            raise WhaleError("对方已经是你的人了，不用 NTR 啦")
        prev_owner = entry.owner_label
        fail = max(0, entry.ntr_fail)
        rate = _ntr_rate(fail)
        hit = R.chance(rate)
        if hit:
            entry.owner_uid = uid
            entry.owner_name = display_name
            entry.ntr_count += 1
            entry.ntr_fail = 0
            entry.marry_fail = 0
            entry.divorce_fail = 0
            entry.affection = max(0, entry.affection - NTR_AFFECTION_LOSS)
        else:
            entry.ntr_fail = fail + 1
        out.update(
            rate=rate,
            hit=hit,
            prev_owner=prev_owner,
            attempt=min(fail + 1, C.NTR_PITY),
            next_rate=_ntr_rate(entry.ntr_fail),
        )

    entry = await ctx.repo.mutate_harem(group_id, target_uid, _mut, label)

    rate = float(out.get("rate", 0.0))
    attempt = int(out.get("attempt", 1))
    next_rate = float(out.get("next_rate", 0.0))
    prev_owner = str(out.get("prev_owner", "") or "对方")
    pity_text = f"第 {attempt}/{C.NTR_PITY} 次（{C.NTR_PITY} 次内必中）"

    if out.get("hit"):
        c = card(
            "NTR 成功！",
            f"{display_name} 从 {prev_owner} 手里抢走了 {label}",
            art=ART_MARRY,
            tone="success",
            badge=f"被抢 {entry.ntr_count} 次",
            stats=[
                stat("本次成功率", _pct(rate)),
                stat("尝试", f"第 {attempt}/{C.NTR_PITY} 次"),
                stat("消耗", cost_text),
            ],
        )
        c.blocks.append(
            kv_block(
                "抢人细节",
                [
                    kv("本次成功率", _pct(rate)),
                    kv("尝试次数", pity_text),
                    kv("消耗", cost_text),
                    kv("好感度", f"{entry.affection}（-{NTR_AFFECTION_LOSS}）"),
                    kv("前夫/前妻", prev_owner),
                    kv("结果", "得手！"),
                ],
            )
        )
        c.blocks.append(
            text_block(
                f"{label} 现在归你了，绿帽卡用掉就没有了。",
                footer="每日凌晨 4 点惰性重置保底进度",
            )
        )
        return c.validate()

    # 已经失败 attempt 次（ntr_fail == attempt），下一次是第 attempt+1 次；
    # 第 NTR_PITY 次必中 -> 还需失败 (NTR_PITY-1-attempt) 次，保底那一发就轮到 100%。
    left = max(0, C.NTR_PITY - 1 - attempt)
    pity_progress = "下一次必中（100%）" if left <= 0 else f"再失败 {left} 次必中"
    c = card(
        "NTR 失败",
        f"{prev_owner} 看得太紧，{display_name} 没能抢走 {label}",
        art=ART_MARRY,
        tone="warn",
        stats=[
            stat("本次成功率", _pct(rate)),
            stat("下次成功率", _pct(next_rate)),
            stat("尝试", f"第 {attempt}/{C.NTR_PITY} 次"),
        ],
    )
    c.blocks.append(
        kv_block(
            "抢人细节",
            [
                kv("本次成功率", _pct(rate)),
                kv("尝试次数", pity_text),
                kv("消耗", cost_text),
                kv("保底进度", pity_progress),
                kv("下次成功率", _pct(next_rate)),
                kv("结果", "被挡回来了"),
            ],
            footer="绿帽卡一旦使用就会消耗，失败不退",
        )
    )
    c.blocks.append(text_block(f"下次成功率 {_pct(next_rate)}，第 {C.NTR_PITY} 次必中。"))
    return c.validate()


# ================================================================ 我的后宫
async def do_harem(ctx: SvcCtx, uid: str, name: str, group_id: str) -> Card:
    """我的后宫：最近 N 条（默认 10），显示好感 / 被追求次数 / 持续天数 / 被 NTR 次数。"""
    _require_group(group_id)
    display_name = _name_or_tail(name, uid)
    rows = await ctx.repo.list_harem(group_id, uid, C.HAREM_DISPLAY_LIMIT)
    total = await ctx.repo.harem_count(group_id, uid)

    if not rows:
        c = card(
            "我的后宫",
            f"{display_name} 目前还没有后宫",
            art=ART_MARRY,
            badge="0 人",
            stats=[stat("后宫总数", 0)],
        )
        c.blocks.append(
            list_block(
                "后宫",
                ["空空如也，去 @ 一位群友试试「娶群友 @TA」吧"],
                footer=f"娶一次 {C.MARRY_COST} 鲸币，或用娶群友卡",
            )
        )
        return c.validate()

    entries: list[str] = []
    for row in rows:
        target = row.target_name or row.target_uid[-4:]
        entries.append(
            f"{target}｜恩爱 {row.affection}｜被追求 {row.chase_count} 次"
            f"｜{_days_since(row.created_at)} 天｜被 NTR {row.ntr_count} 次"
        )

    affection_sum = sum(max(0, row.affection) for row in rows)
    c = card(
        "我的后宫",
        f"{display_name} 的后宫（展示最近 {len(rows)} 位）",
        art=ART_MARRY,
        badge=f"{total} 人",
        stats=[
            stat("后宫总数", total),
            stat("恩爱合计", fmt_num(affection_sum)),
            stat("展示", f"{len(rows)}/{C.HAREM_DISPLAY_LIMIT}"),
        ],
    )
    c.blocks.extend(_list_blocks("后宫", entries, footer="每日凌晨 4 点惰性结算恩爱值增长与保底"))
    return c.validate()


# ================================================================ 恩爱榜
async def do_love_ranking(ctx: SvcCtx, group_id: str) -> Card:
    """恩爱榜：本群按好感度排序的前 10 对，前三名带奖牌。"""
    _require_group(group_id)
    rows = await ctx.repo.affection_ranking(group_id, C.HAREM_DISPLAY_LIMIT)

    if not rows:
        c = card("恩爱榜", "本群还没有人成婚", art=ART_MARRY, badge="0 对")
        c.blocks.append(
            list_block("恩爱榜", ["榜单空空的，去 @ 一位群友开始攻略吧"])
        )
        return c.validate()

    entries: list[str] = []
    for idx, row in enumerate(rows):
        medal = _MEDALS[idx] if idx < len(_MEDALS) else f"{idx + 1}."
        owner = row.owner_label or "?"
        target = row.target_name or row.target_uid[-4:]
        entries.append(
            f"{medal} {owner} → {target}｜恩爱 {row.affection}｜{_days_since(row.created_at)} 天"
        )

    c = card(
        "恩爱榜",
        f"本群恩爱榜 TOP {len(rows)}",
        art=ART_MARRY,
        badge="TOP 10",
        stats=[
            stat("上榜", f"{len(rows)} 对"),
            stat("恩爱合计", fmt_num(sum(max(0, r.affection) for r in rows))),
        ],
    )
    c.blocks.extend(_list_blocks("恩爱榜", entries, footer="持续天数从第一次成功娶到算起"))
    return c.validate()


# ================================================================ 公共老婆
async def do_public_wives(ctx: SvcCtx, group_id: str) -> Card:
    """公共老婆：还没被娶走且群里有记录的目标 + 被 NTR 最多的倒霉蛋。"""
    _require_group(group_id)
    public = await ctx.repo.public_targets(group_id, C.HAREM_DISPLAY_LIMIT)
    ntr = await ctx.repo.ntr_ranking(group_id, 5)

    c = card(
        "公共老婆",
        "还没被娶走的群友 · 以及被抢最多的倒霉蛋",
        art=ART_MARRY,
        badge=f"{len(public)} 位可攻略",
        stats=[stat("可攻略", f"{len(public)} 位"), stat("NTR 记录", f"{len(ntr)} 条")],
    )

    if public:
        lines = [
            f"{row.target_name or row.target_uid[-4:]}｜被追求 {row.chase_count} 次｜恩爱 {row.affection}"
            for row in public
        ]
        c.blocks.extend(_list_blocks("公共老婆", lines, footer="娶回家：娶群友 @TA"))
    else:
        c.blocks.append(list_block("公共老婆", [_EMPTY_PUBLIC]))

    if ntr:
        lines = [
            f"{row.target_name or row.target_uid[-4:]}｜被 NTR {row.ntr_count} 次"
            f"｜现属 {row.owner_label or '公共'}"
            for row in ntr
        ]
        c.blocks.extend(_list_blocks("NTR 榜", lines, footer="被抢次数越多，说明越抢手"))
    else:
        c.blocks.append(list_block("NTR 榜", ["本群还没人被 NTR 过，风气很正"]))

    return c.validate()