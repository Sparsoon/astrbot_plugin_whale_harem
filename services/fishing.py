"""钓鱼 / 鱼护 / 卖鱼 —— 全插件产出的发动机。

设计要点：
    * 一竿 = 1 次扣饵事务 + 1 次写背包事务 + 1 次加鲸币 + 1 次加经验；
      所有随机决策都在内存里算完，Alias 表从 data.drops 的缓存里取，绝不进事务；
    * 鱼护只存 {"sp","g","p","ts"} 四个短键，80 条约 3KB JSON，2G 机器毫无压力；
    * "空军"是设计的一部分（饵料照样消耗），只有"没饵 / 没东西可卖"才抛 WhaleError。
"""

from __future__ import annotations

from datetime import datetime

from ..core import constants as C
from ..core import weather as WX
from ..core.context import SvcCtx, fmt_num
from ..core.errors import NotEnoughItem, WhaleError
from ..core.models import Bag
from ..core.rng import chance, choice, randint
from ..core.time_utils import fmt_duration, now_ts
from ..data import drops as D
from ..data import fish as F
from ..data.items import (
    BAIT_IDS,
    CATEGORY_NAMES,
    CAT_COLLECT,
    CAT_CROP,
    get_item,
    is_bait,
    name_of,
    resolve_item,
)
from ..ui.schema import (
    ART_BAG,
    ART_FISH,
    card,
    grid_block,
    kv,
    kv_block,
    list_block,
    quote_block,
    stat,
    text_block,
)

FISH_EXP = 8           # 上钩成功一次的经验；空军不给经验（饵料已经白花了，不额外通胀）
AIR_LINES = (
    "浮漂像焊在水面上一样，半天没动。",
    "竿梢轻轻一颤，你猛地提竿——只是一截水草。",
    "水下好像有东西碰了一下饵，然后就跑了。",
    "今天的鱼大概都在开会，没空理你。",
)
_SELL_FISH_KEYS = ("", "鱼", "鱼护", "卖鱼", "全部鱼", "all")
_SELL_COLLECT_KEYS = ("杂物", "收藏", "收藏品", "垃圾", "杂货")
_SELL_CROP_KEYS = ("果实", "作物", "水果", "花", "花朵")


# ================================================================ 内部工具
def _select_bait(bag: Bag, bait_id: str | None) -> str:
    """选饵：指定了就校验，没指定就挑 tier 最低的一种（BAIT_IDS 已按 tier 升序）。"""
    if bait_id:
        item = get_item(bait_id) or resolve_item(bait_id)
        if item is None or not is_bait(item.id):
            raise WhaleError(
                f"「{bait_id}」不是饵料",
                hint="能用的饵料：普通饵料 / 蚯蚓 / 红虫 / 酒酿玉米 / 商品饵",
            )
        if bag.count(item.id) <= 0:
            raise NotEnoughItem(item.name)
        return item.id
    for item_id in BAIT_IDS:
        if bag.count(item_id) > 0:
            return item_id
    err = NotEnoughItem("饵料")
    err.hint = "去商店买「普通饵料」（8 鲸币），或者签到 / 每日喂食达标记领"
    raise err


async def _read_ticket(ctx: SvcCtx, uid: str, group_id: str) -> tuple[float, float, str]:
    """读体验券 BUFF，返回 (品质加成, 上钩率加成, 券种)；过期券由 repo.get_buff 惰性删除。"""
    for kind in D.TICKET_PRIORITY:
        payload = await ctx.repo.get_buff(uid, kind, group_id)
        if payload is None and group_id:
            # 兼容 items.py 可能把 buff 记在全局（group_id="") 的情况
            payload = await ctx.repo.get_buff(uid, kind, "")
        if payload is not None:
            return (
                D.TICKET_QUALITY_BOOST_BY_KIND.get(kind, 0.0),
                D.TICKET_HOOK_BONUS_BY_KIND.get(kind, 0.0),
                kind,
            )
    return 0.0, 0.0, ""


def _hook_rate(pond_level: int, ticket_bonus: float, bait_tier: int = 1, weather_delta: float = 0.0) -> float:
    """上钩率 = 基础 0.72 + 池塘 + 体验券 + 饵料 + 天气，夹在 [0.45, 0.96]。"""
    bait_bonus = max(0, int(bait_tier) - 1) * 0.01
    rate = (
        C.HOOK_BASE_RATE
        + D.pond_hook_bonus(pond_level)
        + max(0.0, float(ticket_bonus))
        + bait_bonus
        + float(weather_delta)
    )
    if rate < C.HOOK_RATE_FLOOR:
        return C.HOOK_RATE_FLOOR
    if rate > C.HOOK_RATE_CEIL:
        return C.HOOK_RATE_CEIL
    return rate


def _stats(balance: int, fish_count: int, limit: int, pond_level: int) -> list:
    return [
        stat("鲸币", fmt_num(balance)),
        stat("鱼护", f"{fish_count}/{limit}"),
        stat("池塘", f"Lv.{pond_level}"),
    ]


def _air_card(
    user,
    pond,
    bait_name: str,
    bait_tier: int,
    hook_rate: float,
    limit: int,
    bag: Bag,
    wait_min: int = 0,
    weather=None,
):
    """空军卡：不抛异常，消耗照样结算，文案安慰 + 明示概率。"""
    weather_name = f"{weather.emoji}{weather.name}" if weather is not None else "—"
    c = card(
        "空军了…",
        f"{user.name or '你'} 蹲在池边守了半天，浮漂一动不动",
        art=ART_FISH,
        tone="warn",
        badge=f"Lv.{pond.level}",
        footer="空军也消耗 1 个饵料，换个天气/换个饵，手气会回来的",
    )
    c.stats = _stats(int(user.coins), bag.fish_count(), limit, pond.level)
    c.blocks.append(
        kv_block(
            "这一竿",
            [
                kv("等待", f"{wait_min} 分钟" if wait_min else "—"),
                kv("饵料", f"{bait_name}（Tier {bait_tier}）"),
                kv("上钩率", f"{hook_rate:.0%}"),
                kv("天气", weather_name),
                kv("结果", "空军，饵料已消耗"),
            ],
        )
    )
    c.blocks.append(text_block(choice(AIR_LINES)))
    return c.validate()


def _empty_card(title: str, hint: str, fish_count: int):
    """友好提示卡（不是错误）：鱼护为空之类的正常状态，不该变成错误卡。"""
    c = card(
        title,
        hint,
        art=ART_BAG,
        tone="warn",
        badge=f"鱼护 {fish_count} 条",
        footer="钓到鱼就能卖钱啦",
    )
    c.stats = [stat("鱼护", f"{fish_count} 条")]
    return c.validate()


async def _read_pond_and_boost(ctx: SvcCtx, uid: str, group_id: str, bait_tier: int):
    """读齐 池塘 / 天气 / 体验券 / 上钩率。

    * 体验券池塘：按固定 Lv.5 计算上钩率与品质，**不吃实际池塘等级**（天气仍生效）；
    * 天气：等待/上钩/品质/星级/鲸币/材料 全部走 core.weather。
    """
    pond = await ctx.repo.get_pond(group_id)
    ticket_boost, ticket_hook, ticket_kind = await _read_ticket(ctx, uid, group_id)
    weather = WX.resolve(group_id, now_ts(), pond.weather, pond.weather_until)
    effective_level = C.TICKET_POND_LEVEL if ticket_kind else pond.level
    # 体验券池塘不叠加"实际池塘品质"，只按固定等级 + 券本身给品质
    boost = D.compose_boost(effective_level, bait_tier, ticket_boost)
    hook = _hook_rate(effective_level, ticket_hook, bait_tier, weather.hook_delta)
    return pond, boost, hook, weather, effective_level


# ================================================================ 钓鱼
async def do_fish(ctx: SvcCtx, uid: str, name: str, group_id: str, bait_id: str | None = None, umo: str = ""):
    """【钓鱼】抛竿 -> 等待(天气修正) -> **本人再发消息提杆**（两段式，零轮询）。

    流程（按需求）：
        1. 抛竿：消耗 1 个饵料；等待时长 = 5~60 分钟随机 × 天气倍率（钳 5~90 分钟）；
        2. 到点：不会自动结算，也**不再由任何群友触发**；
        3. 提杆：**抛竿本人**再发一条消息，65% 概率提杆；提杆时按
           池塘等级（体验券固定 Lv.5）+ 鱼饵品质 + 天气 计算上钩率；
        4. 空军也消耗饵料。
    """
    repo = ctx.repo
    cfg = ctx.cfg
    user = await repo.get_user(uid, name)
    now = now_ts()
    pond = await repo.get_pond(group_id)
    limit = max(1, int(cfg.fish_bag_limit))
    weather = WX.resolve(group_id, now, pond.weather, pond.weather_until)

    # ① 已经抛竿
    if user.fish_ready_ts:
        if user.fish_ready_ts > now:
            left = fmt_duration(user.fish_ready_ts - now)
            raise WhaleError(
                f"浮漂还在水里，还要等 {left}",
                hint="到点后你自己发一条消息就会提杆（65% 概率/条）",
            )
        raise WhaleError(
            "鱼已经到点了，等你提杆",
            hint="你自己发一条消息就会提杆（65% 概率/条）",
        )

    # ② 抛竿
    bag = await repo.get_bag(uid)
    bait_key = _select_bait(bag, bait_id)
    bait_def = get_item(bait_key)
    bait_name = bait_def.name if bait_def else name_of(bait_key)
    ok, _bag = await repo.consume_items(uid, {bait_key: 1})
    if not ok:
        raise NotEnoughItem(bait_name)

    lo = max(1, int(cfg.fish_wait_min))
    hi = max(lo, int(cfg.fish_wait_max))
    base_wait = int(randint(lo, hi))
    wait_min = max(5, min(90, int(round(base_wait * weather.wait_mult))))
    ready = now + wait_min * 60
    await repo.update_user(
        uid,
        fish_ready_ts=ready,
        fish_bait=bait_key,
        fish_wait_min=wait_min,
        fish_group=group_id,
        day_key=user.day_key,
        feed_day=user.feed_day,
        feed_count=user.feed_count,
    )

    c = card(
        "抛竿，等鱼上钩…",
        f"{name or '你'} · 池塘 Lv.{pond.level} · {weather.emoji}{weather.name}",
        art=ART_FISH,
        badge=f"{wait_min} 分钟",
        tone="normal",
        footer="到点后你自己发一条消息提杆（65% 概率/条，不用等别人）",
    )
    c.stats = [
        stat("等待", fmt_duration(wait_min * 60)),
        stat("可提杆", _clock(ready)),
        stat("饵料", bait_name),
    ]
    c.blocks.append(
        kv_block(
            "抛竿信息",
            [
                kv("饵料", f"{bait_name}（已消耗 1 个）"),
                kv("基础等待", f"{base_wait} 分钟（5~60 随机）"),
                kv("天气修正", f"×{weather.wait_mult:.2f} → {wait_min} 分钟"),
                kv("可提杆时间", _clock(ready)),
                kv("池塘", f"Lv.{pond.level}"),
            ],
        )
    )
    c.blocks.append(
        kv_block(
            "今日天气",
            [
                kv("天气", f"{weather.emoji} {weather.name}"),
                kv("增益", weather.positive or "无"),
                kv("负面", weather.negative or "无"),
                kv("下次变化", _clock(WX.next_change_ts(now))),
            ],
        )
    )
    c.blocks.append(text_block("到点后发条消息，自己把竿提起来喵。"))
    return c.validate()


async def reel_catch(ctx: SvcCtx, user) -> Card | None:
    """本人提杆：原子领取这条已到点的鱼并结算（未到点/没抢到返回 None）。"""
    now = now_ts()
    grabbed = await ctx.repo.take_fish_pending(user.uid, now)
    if grabbed is None:
        return None
    bait_key, wait_min, group_id = grabbed
    if not group_id:
        return None
    pond = await ctx.repo.get_pond(group_id)
    limit = max(1, int(ctx.cfg.fish_bag_limit))
    return await _reel(ctx, user, pond, limit, bait_key or None, wait_min, now)


def _clock(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


async def _reel(
    ctx: SvcCtx,
    user,
    pond,
    limit: int,
    bait_key: str | None,
    wait_min: int,
    now: int,
):
    """收竿结算：鱼/道具/物品三选一 + 必掉材料；清空等待状态。"""
    repo = ctx.repo
    uid = user.uid
    bag = await repo.get_bag(uid)
    if not bait_key:
        bait_key = _select_bait(bag, None)
    bait_def = get_item(bait_key)
    bait_name = bait_def.name if bait_def else name_of(bait_key)
    bait_tier = bait_def.bait_tier if bait_def else 1
    pond, boost, hook_rate, weather, effective_level = await _read_pond_and_boost(
        ctx, uid, pond.group_id, bait_tier
    )

    if not chance(hook_rate):
        # 等待状态已由 take_fish_pending 原子清空，这里不再重复写库
        return _air_card(user, pond, bait_name, bait_tier, hook_rate, limit, bag, wait_min, weather)

    # ------------------------------------------------------------ 上钩：先算结果
    category = str(D.CATEGORY_SAMPLER.pick())
    star = 0
    fish_def = None
    grams = 0
    price = 0
    record = None
    prop_id = ""
    item_id = ""
    coin_win = 0
    coin_label = ""

    if category == D.CATEGORY_FISH:
        star = D.roll_star(boost, weather.star_shift)
        try:
            if await repo.get_buff(uid, "ticket_star", pond.group_id) or await repo.get_buff(
                uid, "ticket_star", ""
            ):
                star = min(5, star + 1)
        except Exception:  # noqa: BLE001 - buff 读失败不影响这一竿
            pass
        fish_def = F.random_fish(star)
        grams = randint(fish_def.min_g, fish_def.max_g)
        price = F.sell_value(fish_def, grams)
        record = {"sp": fish_def.id, "g": int(grams), "p": int(price), "ts": now_ts()}
    elif category == D.CATEGORY_PROP:
        prop_id = str(D.PROP_SAMPLER.pick())
    else:
        rarity = D.roll_rarity(
            boost, D.min_rarity_rank(effective_level), weather.rarity_shift
        )
        pool = D.ITEM_POOL_BY_RARITY.get(rarity) or D.ITEM_POOL_BY_RARITY[C.RARITY_XIAOLAN]
        picked = str(choice(pool))
        if D.is_virtual_coin(picked):
            coin_win = int(round(D.roll_coin_bag(picked) * weather.coin_mult))
            coin_label = D.pool_item_name(picked)
        else:
            item_id = picked

    mat_id = str(D.material_sampler(pond.level).pick())
    mat_num = int(randint(C.MATERIAL_DROP_MIN, C.MATERIAL_DROP_MAX)) + int(weather.material_extra)

    # 花园种子：所有等级都可能钓到（果实不会从水里出来）
    seed_drop = D.roll_seed() if chance(D.SEED_DROP_CHANCE) else ""

    state = {"overflow": 0, "overflow_coins": 0}

    def _mut(target: Bag) -> None:
        if record is not None:
            overflow = target.add_fish(record, limit)
            state["overflow"] = len(overflow)
            state["overflow_coins"] = sum(int(r.get("p", 0) or 0) for r in overflow)
        if prop_id:
            target.add(prop_id, 1)
        if item_id:
            target.add(item_id, 1)
        if mat_id and mat_num > 0:
            target.add(mat_id, mat_num)
        if seed_drop:
            target.add(seed_drop, 1)

    bag = await repo.mutate_bag(uid, _mut)

    ov_n = int(state["overflow"])
    ov_coins = int(state["overflow_coins"])
    coin_total = coin_win + ov_coins
    balance = int(user.coins)
    if coin_total > 0:
        balance = int(await repo.add_coins(uid, coin_total))
    level, _exp, leveled = await repo.add_exp(uid, FISH_EXP)

    rows = [
        kv("等待", f"{wait_min} 分钟"),
        kv("饵料", f"{bait_name}（Tier {bait_tier}）"),
        kv("上钩率", f"{hook_rate:.0%}"),
        kv("品质加成", f"+{boost:.2f}"),
        kv("天气", f"{weather.emoji} {weather.name}（{weather.positive} / {weather.negative}）"),
        kv("保底品质", D.min_rarity_name(effective_level)),
    ]
    accent = C.ACCENT_BLUE
    badge = f"Lv.{effective_level}"
    tone = "normal"

    if record is not None and fish_def is not None:
        rows.append(kv("鱼获", f"{fish_def.name}（{F.STAR_NAMES.get(star, '★')} {F.STAR_LABEL.get(star, '普通')}）"))
        rows.append(kv("重量", f"{grams} g"))
        rows.append(kv("估价", f"{price} 鲸币"))
        title = f"收竿！{fish_def.name}"
        accent = fish_def.color
        badge = F.STAR_NAMES.get(star, "★")
        tone = "legend" if star >= 5 else ("success" if star >= 3 else "normal")
    elif prop_id:
        prop_def = get_item(prop_id)
        prop_name = prop_def.name if prop_def else name_of(prop_id)
        rows.append(kv("道具", prop_name))
        if prop_def is not None:
            rows.append(kv("说明", prop_def.desc))
            accent = prop_def.rarity_color
        title = "收竿：捞到一张道具卡！"
        badge = "道具"
        tone = "success"
    elif coin_win > 0:
        rows.append(kv("鲸币", f"{coin_label} +{coin_win}"))
        title = "收竿：钓上一袋鲸币！"
        accent = C.RARITY_COLORS.get(C.RARITY_XIAOJIN, C.ACCENT_BLUE)
        badge = "鲸币"
        tone = "success"
    else:
        item_def = get_item(item_id)
        item_name = item_def.name if item_def else name_of(item_id)
        rows.append(kv("物品", item_name))
        if item_def is not None:
            rows.append(kv("品质", item_def.rarity))
            accent = item_def.rarity_color
        rows.append(kv("售价参考", f"{item_def.sell if item_def else 0} 鲸币"))
        title = f"收竿：钓到了 {item_name}"
        badge = item_def.rarity if item_def else "物品"
        tone = "success"

    footer = f"经验 +{FISH_EXP} · 鱼护 {bag.fish_count()}/{limit}"
    if leveled:
        footer += f" · 升级啦 Lv.{level}"

    c = card(
        title,
        f"{user.name or '你'} · 池塘 Lv.{effective_level} · {weather.emoji}{weather.name}",
        art=ART_FISH,
        accent=accent,
        badge=badge,
        tone=tone,
        footer=footer,
    )
    c.stats = _stats(balance, bag.fish_count(), limit, effective_level)
    c.blocks.append(kv_block("这一竿", rows))
    c.blocks.append(text_block(f"{name_of(mat_id)} x{mat_num}", title="🪨 上钩必掉材料"))
    if seed_drop:
        seed_name = name_of(seed_drop)
        c.blocks.append(text_block(f"{seed_name} x1", title="🌱 顺手捞到的种子"))
    if ov_n > 0:
        c.blocks.append(text_block(f"鱼护满了，自动卖了 {ov_n} 条，+{ov_coins} 鲸币"))
    if fish_def is not None and star >= 4:
        c.blocks.append(quote_block(fish_def.desc, author=fish_def.display))
    return c.validate()


# ================================================================ 背包 / 鱼护
async def do_bag(ctx: SvcCtx, uid: str, name: str, group_id: str):
    """【鱼护 / 背包】物品按类别分组（只看数量最多的 12 种）+ 鱼护按星级统计 + 总估值。"""
    user = await ctx.repo.get_user(uid, name)
    bag = await ctx.repo.get_bag(uid)
    pond = await ctx.repo.get_pond(group_id)
    limit = max(1, int(ctx.cfg.fish_bag_limit))

    c = card(
        "鱼护与背包",
        f"{user.name or name} 的全部家当",
        art=ART_BAG,
        badge=f"Lv.{pond.level}",
        footer="钓鱼 → 卖鱼 → 买饵，别让鱼护塞满",
    )

    # ---------------- 物品：按类别分组，只取数量最多的 12 种
    if bag.items:
        top = sorted(bag.items.items(), key=lambda pair: -int(pair[1]))[:12]
        grouped: dict[str, list[str]] = {}
        for item_id, num in top:
            item = get_item(item_id)
            category = item.category if item else ""
            label = f"{item.name}x{num}" if item else f"{item_id}x{num}"
            grouped.setdefault(CATEGORY_NAMES.get(category, "📦 其他"), []).append(label)
        lines = [f"{title}：{'、'.join(names)}" for title, names in grouped.items()]
        c.blocks.append(list_block("物品（按类别 · 数量 Top12）", lines))
    else:
        c.blocks.append(text_block("背包空空如也，先去 /钓鱼 吧", title="物品"))

    # ---------------- 鱼护：按星级统计 + 总估值
    star_count: dict[int, int] = {star: 0 for star in F.stars()}
    total_value = 0
    for rec in bag.fish:
        total_value += int(rec.get("p", 0) or 0)
        fish_def = F.get_fish(str(rec.get("sp", "")))
        if fish_def is not None:
            star_count[fish_def.star] = star_count.get(fish_def.star, 0) + 1
    fish_rows = [
        kv(f"{F.STAR_NAMES[star]} {F.STAR_LABEL[star]}", f"{star_count[star]} 条")
        for star in F.stars()
        if star_count.get(star)
    ]
    if fish_rows:
        fish_title = f"鱼护 {bag.fish_count()}/{limit} · 估值 {total_value} 鲸币"
        c.blocks.append(grid_block(fish_title, fish_rows))
    else:
        c.blocks.append(text_block("鱼护还是空的，去 /钓鱼 甩两竿吧", title="鱼护"))

    collect_num = 0
    for item_id, num in bag.items.items():
        item = get_item(item_id)
        if item is not None and item.category == CAT_COLLECT:
            collect_num += int(num)

    c.stats = [
        stat("鲸币", fmt_num(int(user.coins))),
        stat("鱼护", f"{bag.fish_count()}/{limit}"),
        stat("池塘", f"Lv.{pond.level}"),
        stat("杂物", f"{collect_num} 件"),
    ]
    return c.validate()


# ================================================================ 卖东西
async def do_sell(ctx: SvcCtx, uid: str, name: str, group_id: str, arg: str = ""):
    """【卖东西】把鱼 / 杂物 / 某个物品换成鲸币。

    * 空参数 或 "鱼" => 卖掉鱼护里全部鱼，按记录里的 p 求和；
    * "杂物"        => 卖掉全部收藏品（CAT_COLLECT）；
    * 物品名        => 卖掉该物品的全部数量（按 data/items.py 的 sell 价）。
    实现上一定先"在事务里扣掉东西"，再 add_coins，绝不会出现"钱到手东西还在"。
    """
    key = (arg or "").strip()
    # 先确保 users 行存在：后面 add_coins 是 UPDATE，行不存在会静默丢钱
    await ctx.repo.get_user(uid, name)
    bag = await ctx.repo.get_bag(uid)
    if not bag.items and not bag.fish:
        raise WhaleError(
            "鱼护和背包都是空的，没东西可卖",
            hint="先去 /钓鱼 钓两条，或者 /签到 领点物资",
        )
    if key in _SELL_FISH_KEYS:
        return await _sell_fish(ctx, uid, name, group_id)
    if key in _SELL_COLLECT_KEYS:
        return await _sell_collect(ctx, uid, name, group_id)
    if key in _SELL_CROP_KEYS:
        return await _sell_crop(ctx, uid, name, group_id)
    return await _sell_item(ctx, uid, name, group_id, key)


async def _sell_fish(ctx: SvcCtx, uid: str, name: str, group_id: str):
    bag = await ctx.repo.get_bag(uid)
    if not bag.fish:
        # 鱼护空不属于"错"，给一张友好提示卡而不是错误卡（空背包仍由 do_sell 兜底抛错）
        return _empty_card("鱼护是空的", "先去 /钓鱼 甩两竿，鱼护里有鱼才能卖", bag.fish_count())

    state = {"n": 0, "coins": 0, "best": None}

    def _mut(target: Bag) -> None:
        fish = target.clear_fish()  # 先扣鱼护
        state["n"] = len(fish)
        state["coins"] = sum(int(r.get("p", 0) or 0) for r in fish)
        if fish:
            state["best"] = max(fish, key=lambda r: int(r.get("p", 0) or 0))

    await ctx.repo.mutate_bag(uid, _mut)
    n = int(state["n"])
    coins = int(state["coins"])
    if n <= 0 or coins <= 0:
        return _empty_card("鱼护里的记录坏了", "像是旧版本数据，去 /钓鱼 重新钓几条", 0)

    balance = int(await ctx.repo.add_coins(uid, coins))

    rows = [kv("卖出", f"{n} 条鱼"), kv("收入", f"+{coins} 鲸币")]
    best = state["best"]
    if best:
        best_def = F.get_fish(str(best.get("sp", "")))
        best_name = best_def.name if best_def else name_of(str(best.get("sp", "")))
        rows.append(kv("最贵那条", f"{best_name} {best.get('g', 0)}g · {best.get('p', 0)} 鲸币"))

    c = card(
        "鱼护清空，鲸币到账",
        f"卖了 {n} 条鱼，+{coins} 鲸币",
        art=ART_BAG,
        tone="success",
        badge=f"+{coins}",
        footer="鱼护已清空，接着钓吧",
    )
    c.stats = [
        stat("鲸币", fmt_num(balance)),
        stat("卖出", f"{n} 条"),
        stat("本次收入", f"+{coins}"),
    ]
    c.blocks.append(kv_block("本次卖鱼", rows))
    return c.validate()


async def _sell_crop(ctx: SvcCtx, uid: str, name: str, group_id: str):
    """批量卖花园果实/花朵（CAT_CROP），按各自 sell 价。"""
    state = {"n": 0, "coins": 0, "kinds": 0}

    def _mut(target: Bag) -> None:
        coins = 0
        num = 0
        kinds = 0
        for item_id in list(target.items.keys()):
            item = get_item(item_id)
            if item is None or item.category != CAT_CROP or item.sell <= 0:
                continue
            count = int(target.items.get(item_id, 0))
            if count <= 0:
                continue
            coins += item.sell * count
            num += count
            kinds += 1
            target.items.pop(item_id, None)
        state.update(coins=coins, n=num, kinds=kinds)

    bag = await ctx.repo.mutate_bag(uid, _mut)
    n = int(state["n"])
    coins = int(state["coins"])
    if n <= 0:
        raise WhaleError(
            "背包里没有可卖的果实",
            hint="去 /养植物 种一株，成熟后 /收获 就有果实了",
            commands=["/养植物"],
        )
    balance = int(await ctx.repo.add_coins(uid, coins))
    c = card(
        "果实出清，鲸币到账",
        f"卖了 {state['kinds']} 种、共 {n} 件果实/花朵，+{coins} 鲸币",
        art=ART_BAG,
        tone="success",
        badge=f"+{coins}",
    )
    c.stats = [stat("鲸币余额", balance), stat("卖出件数", n)]
    return c.validate()


async def _sell_collect(ctx: SvcCtx, uid: str, name: str, group_id: str):
    state = {"n": 0, "coins": 0, "kinds": 0}

    def _mut(target: Bag) -> None:
        coins = 0
        num = 0
        kinds = 0
        for item_id in list(target.items.keys()):
            item = get_item(item_id)
            if item is None or item.category != CAT_COLLECT or item.sell <= 0:
                continue
            count = int(target.items.get(item_id, 0))
            if count <= 0:
                continue
            coins += item.sell * count
            num += count
            kinds += 1
            target.items.pop(item_id, None)
        state["coins"] = coins
        state["n"] = num
        state["kinds"] = kinds

    bag = await ctx.repo.mutate_bag(uid, _mut)
    n = int(state["n"])
    coins = int(state["coins"])
    if n <= 0:
        raise WhaleError(
            "背包里没有可卖的杂物",
            hint="收藏品才算杂物：铁盆 / 鲸鱼抱枕 / 手办 / 毛绒玩偶 / DeepSeek之泪",
        )

    balance = int(await ctx.repo.add_coins(uid, coins))
    c = card(
        "杂物出清，鲸币到账",
        f"卖了 {state['kinds']} 种、共 {n} 件杂物，+{coins} 鲸币",
        art=ART_BAG,
        tone="success",
        badge=f"+{coins}",
    )
    c.stats = [
        stat("鲸币", fmt_num(balance)),
        stat("卖出", f"{n} 件"),
        stat("本次收入", f"+{coins}"),
        stat("鱼护", f"{bag.fish_count()} 条"),
    ]
    c.blocks.append(kv_block("本次卖杂物", [kv("件数", f"{n} 件"), kv("种类", f"{state['kinds']} 种")]))
    return c.validate()


async def _sell_item(ctx: SvcCtx, uid: str, name: str, group_id: str, key: str):
    item = resolve_item(key)
    if item is None:
        raise WhaleError(
            f"没找到「{key}」这种能卖的东西",
            hint="试试 /卖鱼、/卖杂物，或者 /卖 白米饭",
        )
    if item.sell <= 0:
        raise WhaleError(f"「{item.name}」不值钱，没人收", hint="留着自己用吧")

    state = {"n": 0, "coins": 0}

    def _mut(target: Bag) -> None:
        count = target.count(item.id)
        if count <= 0:
            return
        if target.remove(item.id, count):  # 先扣物品
            state["n"] = count
            state["coins"] = item.sell * count

    await ctx.repo.mutate_bag(uid, _mut)
    n = int(state["n"])
    coins = int(state["coins"])
    if n <= 0:
        raise NotEnoughItem(item.name)

    balance = int(await ctx.repo.add_coins(uid, coins))  # 再入账
    c = card(
        "卖出成功",
        f"{item.name} x{n}，+{coins} 鲸币",
        art=ART_BAG,
        tone="success",
        badge=f"+{coins}",
    )
    c.stats = [
        stat("鲸币", fmt_num(balance)),
        stat("卖出", f"x{n}"),
        stat("本次收入", f"+{coins}"),
    ]
    c.blocks.append(
        kv_block(
            "本次卖出",
            [
                kv("物品", item.name),
                kv("单价", f"{item.sell} 鲸币"),
                kv("数量", f"x{n}"),
            ],
        )
    )
    return c.validate()