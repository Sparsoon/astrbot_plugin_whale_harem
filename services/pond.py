"""集团池塘 —— 捐献 / 自动升级 / 升塘卡，全群共享的一口塘。

设计要点：
    * 所有写操作都走 repo 的事务方法（consume_items / donate_materials / take_pond_upgrade），
      绝不做"读出来改一改再整行 save"，两个群友同时捐也不会互相覆盖；
    * 升级判定交给 repo.take_pond_upgrade（材料够就升，一次最多一级），捐款与升级在同一流程里连续完成；
    * 文案固定带"材料只能靠钓鱼获得"，避免玩家去商店里找材料。
"""

from __future__ import annotations

import re
from datetime import datetime

from ..core import constants as C
from ..core.context import SvcCtx, fmt_num
from ..core.errors import NotEnoughItem, WhaleError
from ..data import drops as D
from ..data.items import is_material, name_of, resolve_item
from ..ui.schema import (
    ART_POND,
    card,
    grid_block,
    kv,
    kv_block,
    list_block,
    progress_block,
    stat,
    text_block,
)

CARD_POND_UP = "card_pond_up"
RANKING_LIMIT = 5
MATERIAL_HINT = "材料只能靠钓鱼获得，商店买不到"


# ---------------------------------------------------------------- 小工具
def _nick(uid: str) -> str:
    """群友显示名：拿不到昵称时用 uid 后 4 位，避免为了显示去写 users 表。"""
    label = (uid or "").strip()
    if len(label) > 4:
        return f"群友…{label[-4:]}"
    return label or "匿名群友"


def _gap_text(pond, req: dict) -> tuple[float, str]:
    """返回 (瓶颈完成度 0~1, "基石 5/8（差 3） · 珊瑚 2/10（差 8）")。"""
    if not req:
        return 1.0, ""
    ratio = 1.0
    parts: list[str] = []
    for item_id, need in req.items():
        need = max(1, int(need))
        have = int(pond.materials.get(item_id, 0))
        ratio = min(ratio, min(1.0, have / need))
        gap = max(0, need - have)
        tail = f"（差 {gap}）" if gap else "（已凑齐）"
        parts.append(f"{name_of(item_id)} {have}/{need}{tail}")
    return ratio, " · ".join(parts)


_COUNT_RE = re.compile(r"^(?P<name>.*?)\s*(?:[xX×*]\s*)?(?P<num>\d+)$")
_ALL_KEYS = ("all", "全部", "所有", "全捐", "全给")


def _donation_plan(bag, arg: str) -> dict[str, int]:
    """把指令参数解析成 {材料id: 数量}。支持 全部 / 材料名 / 材料名x数量。"""
    text = (arg or "").strip()
    if not text:
        raise WhaleError("要捐什么材料呢", hint="例：/捐材料 基石x5 或 /捐材料 全部")
    if text.lower() in _ALL_KEYS:
        plan: dict[str, int] = {}
        for item_id, num in bag.items.items():
            if is_material(item_id) and int(num) > 0:
                plan[item_id] = int(num)
        return plan

    name_part = text
    num = 0
    matched = _COUNT_RE.match(text)
    if matched:
        name_part = (matched.group("name") or "").strip()
        num = int(matched.group("num"))
    item = resolve_item(name_part)
    if item is None:
        raise WhaleError(f"没找到「{name_part}」这种材料", hint="可捐：池塘基石 / 珊瑚簇 / 鲸骨 / 深海结晶")
    if not is_material(item.id):
        raise WhaleError(f"「{item.name}」不是材料，捐不进池塘", hint=MATERIAL_HINT)
    have = bag.count(item.id)
    if have <= 0:
        raise NotEnoughItem(item.name)
    if num <= 0:  # 没写数量 = 有多少捐多少
        return {item.id: have}
    return {item.id: min(num, have)}


# ---------------------------------------------------------------- 看池塘
async def do_pond(ctx: SvcCtx, uid: str, name: str, group_id: str):
    """【池塘面板】等级 / 上钩加成 / 品质加成 / 当前材料 / 下一级进度 / 贡献榜 Top5。

    只读，不消耗任何东西。
    """
    pond = await ctx.repo.get_pond(group_id)
    level = pond.level
    hook = D.pond_hook_bonus(level)
    quality = D.pond_quality_boost(level)
    from ..core import weather as WX
    from ..core.time_utils import now_ts

    weather = WX.resolve(group_id, now_ts(), pond.weather, pond.weather_until)

    c = card(
        "集团池塘",
        f"{C.BRAND_NAME} · 全群共享的钓鱼宝地 · {weather.emoji}{weather.name}",
        art=ART_POND,
        badge=f"Lv.{level}",
        footer=MATERIAL_HINT,
    )
    c.stats = [
        stat("池塘等级", f"Lv.{level}/{C.POND_MAX_LEVEL}"),
        stat("上钩加成", f"+{hook:.0%}"),
        stat("品质加成", f"+{quality:.0%}"),
        stat("今日天气", f"{weather.emoji}{weather.name}", C.ACCENT_BLUE),
    ]
    c.blocks.append(
        kv_block(
            "天气",
            [
                kv("当前", f"{weather.emoji} {weather.name}"),
                kv("增益", weather.positive or "无"),
                kv("负面", weather.negative or "无"),
                kv("下次变化", datetime.fromtimestamp(WX.next_change_ts(now_ts())).strftime("%H:%M")),
            ],
        )
    )

    # 当前材料
    if pond.materials:
        rows = [
            kv(name_of(item_id), f"x{num}")
            for item_id, num in sorted(pond.materials.items(), key=lambda pair: -int(pair[1]))[:12]
        ]
        c.blocks.append(grid_block("当前材料", rows))
    else:
        c.blocks.append(text_block("池子里还空着，第一铲土等你来填", title="当前材料"))

    # 下一级进度（按最缺的那种材料算进度条，杜绝"看起来快满了其实卡死"）
    req = C.POND_UPGRADE_REQUIREMENTS.get(level + 1) or {}
    if req:
        ratio, gap = _gap_text(pond, req)
        c.blocks.append(progress_block(f"升到 Lv.{level + 1}", ratio, 1.0, text=gap))
    else:
        c.blocks.append(
            text_block(
                f"已经满级 Lv.{C.POND_MAX_LEVEL}，全群享受最高上钩率与品质加成",
                title="升级进度",
            )
        )

    # 群贡献榜
    ranking = sorted(pond.contribution.items(), key=lambda pair: (-int(pair[1]), pair[0]))[:RANKING_LIMIT]
    if ranking:
        lines = [f"{idx}. {_nick(member)} — {int(score)} 点" for idx, (member, score) in enumerate(ranking, 1)]
        c.blocks.append(list_block(f"群贡献榜 Top{RANKING_LIMIT}", lines))
    else:
        c.blocks.append(text_block("还没有人捐过材料，第一个捐的人就是池塘元老"))

    return c.validate()


# ---------------------------------------------------------------- 捐材料
async def do_donate(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str):
    """【捐材料】把背包里的材料沉进本群池塘，凑够自动升一级。

    消耗：背包里的材料（材料只能靠钓鱼获得）。
    产出：池塘材料 + 个人贡献度；材料达到下一级需求瞬间自动升级（一次最多一级，
          多余的进度留在池子里）。捐献失败不扣任何东西。
    """
    bag = await ctx.repo.get_bag(uid)
    plan = _donation_plan(bag, arg)
    total = sum(plan.values())
    if total <= 0:
        raise WhaleError("没有可捐的材料", hint=MATERIAL_HINT)

    ok, _bag = await ctx.repo.consume_items(uid, plan)
    if not ok:
        raise WhaleError(
            "材料好像刚被别处用掉了，再试一次",
            hint="捐献是全有或全无，不会扣一半；实在没了就去 /钓鱼 再掉一点",
        )

    # 先入池（事务内 +贡献度），再尝试升级；两步都是 repo 的事务方法，不会丢并发捐献
    pond = await ctx.repo.donate_materials(group_id, uid, plan)
    before_level = pond.level
    pond, upgraded, cost = await ctx.repo.take_pond_upgrade(group_id)
    level = pond.level

    my_total = int(pond.contribution.get(uid, 0))
    group_total = sum(int(v) for v in pond.contribution.values())
    plan_text = "、".join(f"{name_of(item_id)} x{num}" for item_id, num in plan.items())

    if upgraded:
        title = f"池塘升级！Lv.{level}"
        subtitle = f"全群凑齐材料，池塘从 Lv.{before_level} 抬到 Lv.{level}"
        tone = "legend"
    else:
        title = "捐献成功"
        subtitle = f"{plan_text} 已经沉进池底"
        tone = "success"

    c = card(title, subtitle, art=ART_POND, badge=f"Lv.{level}", tone=tone, footer=MATERIAL_HINT)
    c.stats = [
        stat("本次贡献", f"+{total} 点"),
        stat("我的累计", f"{my_total} 点"),
        stat("全群贡献", f"{fmt_num(group_total)} 点"),
        stat("池塘等级", f"Lv.{level}/{C.POND_MAX_LEVEL}"),
    ]

    rows = [kv(name_of(item_id), f"x{num}", note="已入池") for item_id, num in plan.items()]
    if upgraded and cost:
        rows.append(kv("升级消耗", "、".join(f"{name_of(k)} x{v}" for k, v in cost.items())))
    c.blocks.append(kv_block("本次捐献", rows))

    req = C.POND_UPGRADE_REQUIREMENTS.get(level + 1) or {}
    if req:
        ratio, gap = _gap_text(pond, req)
        c.blocks.append(progress_block(f"升到 Lv.{level + 1}", ratio, 1.0, text=gap))
        c.blocks.append(text_block(f"全群总进度：{group_total} 点贡献 · 下一级缺口见上", title="进度"))
    else:
        c.blocks.append(
            text_block(f"池塘已满级 Lv.{C.POND_MAX_LEVEL}，材料先攒着等新版本开放", title="升级进度")
        )

    return c.validate()


# ---------------------------------------------------------------- 升塘卡
async def do_use_pond_card(ctx: SvcCtx, uid: str, name: str, group_id: str):
    """【升塘卡】消耗 1 张「池塘一键升一级卡」，无视材料直接升一级。

    满级时抛 WhaleError 且**不扣卡**；扣卡之后若被别人抢先升满，卡会自动退回背包。
    """
    pond = await ctx.repo.get_pond(group_id)
    if pond.level >= C.POND_MAX_LEVEL:
        raise WhaleError(
            f"本群池塘已经满级 Lv.{C.POND_MAX_LEVEL} 了",
            hint="升塘卡先留着，等开放新等级再用",
        )

    bag = await ctx.repo.get_bag(uid)
    if bag.count(CARD_POND_UP) <= 0:
        raise NotEnoughItem("池塘一键升一级卡")

    ok, bag = await ctx.repo.consume_items(uid, {CARD_POND_UP: 1})
    if not ok:
        raise NotEnoughItem("池塘一键升一级卡")

    # 重新读一次，把"扣卡到写库"之间的并发窗口压到最小；
    # 注意 save_pond 是整行写：这里读完立刻写、中间没有别的 await，窗口只有一次 IO。
    pond = await ctx.repo.get_pond(group_id)
    if pond.level >= C.POND_MAX_LEVEL:
        await ctx.repo.add_items(uid, {CARD_POND_UP: 1})  # 补偿：把卡退回去
        raise WhaleError(
            "刚刚池塘被别人升到满级了，升塘卡已自动退回",
            hint="换个群用，或者留着等新等级",
        )
    pond.level += 1
    await ctx.repo.save_pond(pond)

    level = pond.level
    c = card(
        "升塘卡生效！",
        f"无视材料，{C.BRAND_NAME}的池塘直接抬到 Lv.{level}",
        art=ART_POND,
        badge=f"Lv.{level}",
        tone="legend",
        footer=MATERIAL_HINT,
    )
    c.stats = [
        stat("池塘等级", f"Lv.{level}/{C.POND_MAX_LEVEL}"),
        stat("上钩加成", f"+{D.pond_hook_bonus(level):.0%}"),
        stat("品质加成", f"+{D.pond_quality_boost(level):.0%}"),
        stat("剩余升塘卡", f"x{bag.count(CARD_POND_UP)}"),
    ]

    req = C.POND_UPGRADE_REQUIREMENTS.get(level + 1) or {}
    if req:
        ratio, gap = _gap_text(pond, req)
        c.blocks.append(progress_block(f"升到 Lv.{level + 1}", ratio, 1.0, text=gap))
    else:
        c.blocks.append(
            text_block(f"已经满级 Lv.{C.POND_MAX_LEVEL}，全群享受最高加成", title="升级进度")
        )
    c.blocks.append(text_block("下一级继续靠钓鱼掉材料，或者再撕一张升塘卡", title="接下来"))
    return c.validate()