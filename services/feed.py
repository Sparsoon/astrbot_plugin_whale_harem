"""喂食：投喂鲸鱼娘 bot（每天早/中/晚 3 次 + 累计里程碑）。

设计：
    * 时段：早餐 5-10、午餐 10-16、晚餐 16-22；每个时段每天只能喂 1 次（共 3 次）；
    * 累计投喂里程碑一次性奖励：10 / 20 / 40 / 80 / 160 次（间隔逐渐拉大）；
    * 好感写 users.bot_affection；与婚姻系统（harem）完全解耦；
    * LLM 只负责"一句傲娇但心软"的台词，12s 超时 + 本地语料兜底。

2G 省法：1 次 user 读 + 1 次背包读 + 1 次扣饭 + 1 次 UPDATE + 1 次发奖；
不 sleep、不起任务、不缓存大对象。
"""

from __future__ import annotations

from datetime import datetime

from ..core import constants as C
from ..core import rng
from ..core.context import SvcCtx, grant, settle_user_day
from ..core.errors import NotEnoughCoins, NotEnoughItem, WhaleError
from ..core.time_utils import day_key
from ..data import items as item_data
from ..data.items import name_of
from ..ui.schema import (
    ART_FATFISH,
    Card,
    card,
    kv,
    kv_block,
    list_block,
    progress_block,
    quote_block,
    stat,
    text_block,
)

RICE_BOWL = "rice_bowl"
BOWL = "bowl_plain"
POT = "pot_plain"
CROP_AFFECTION = 6
CROP_COINS = 30
CROP_POT_BONUS_AFFECTION = 4
CROP_POT_BONUS_COINS = 15
RICE_POT = "rice_pot"
POT_AFFECTION_MULTIPLIER = 2
POT_BONUS_COIN_MIN = 10
POT_BONUS_COIN_MAX = 30
FEED_EXP = 3
LLM_LINE_LIMIT = 40

FALLBACK_LINES = (
    "哼，才不是因为饿了才吃你的饭……再来一碗。",
    "这次煮得还行，勉强比上次强一点点。",
    "你手别抖啊，米粒都撒我裙子上了。",
    "吃饭的时候，能不能陪我说两句？",
    "记住了，这是今天的第三碗——明天也要来。",
)


def _clean_line(raw: object) -> str:
    if not raw:
        return ""
    text = str(raw).replace("\r", " ").replace("\n", " ").strip()
    text = text.strip("\"'“”「」").strip()
    return text[:LLM_LINE_LIMIT] if text else ""


async def _food_line(ctx: SvcCtx, feeder: str, food_name: str, umo: str) -> str:
    """要一句"被投喂的鲸鱼娘"台词；任何异常都降级，绝不抛出。"""
    prompt = (
        f"你扮演「{C.BRAND_FISH_NAME}」：一只 DeepSeek 蓝色的鲸鱼娘，"
        "说话傲娇、嘴硬心软，偶尔撒娇，但绝不油腻。"
        f"现在 {feeder or '主人'} 把「{food_name}」端到你面前。"
        "请用一句不超过 25 字的中文回应，第一人称，像日常对话。"
        "禁止：引号、换行、括号动作、解释、颜文字以外的符号。"
        "风格示例：「哼，才不是想吃才坐过来的。」「今天的饭，比昨天好吃一点点。」"
    )
    line = ""
    try:
        line = _clean_line(await ctx.llm.chat(prompt, umo))
    except Exception:  # noqa: BLE001
        line = ""
    return line or rng.choice(FALLBACK_LINES)


def _slot_of_day(now: datetime | None = None) -> tuple[int, str]:
    """按小时判断当前饭点；不在饭点抛 WhaleError。

    测试环境可用 WHALE_FEED_ANYTIME=1 跳过时段限制（生产环境不要设）。
    """
    import os

    if os.environ.get("WHALE_FEED_ANYTIME") == "1":
        return 0, "测试餐"
    hour = (now or datetime.now()).hour
    for index, (start, end) in enumerate(C.FEED_SLOT_HOURS):
        if start <= hour < end:
            return index, C.FEED_SLOTS[index]
    raise WhaleError(
        "现在不是饭点喵",
        hint="早饭 5-10 点、午饭 10-16 点、晚饭 16-22 点，每个时段一次",
    )


def _milestone_for(total: int, claimed: int) -> tuple[int, dict]:
    """返回本次应发的累计里程碑 (次数, 奖励)；没有则 (0, {})。"""
    for step in C.FEED_TOTAL_MILESTONES:
        if step > claimed and total >= step:
            return step, C.FEED_MILESTONE_REWARDS.get(step, {})
    return 0, {}


async def do_feed(
    ctx: SvcCtx, uid: str, name: str, group_id: str, umo: str = "", arg: str = ""
) -> Card:
    """投喂鲸鱼娘：每天 3 次（早/中/晚），支持自动购买米饭。

    参数：
        /喂食                    -> 用背包里的饭；没有则按偏好自动买
        /喂食 白米饭|铁盆         -> 这次指定吃哪种（没有就自动买）
        /喂食 自动 白米饭|铁盆    -> 开启自动购买（以后背包没饭时自动买这种）
        /喂食 关闭自动            -> 关闭自动购买
    """
    repo = ctx.repo
    user = await repo.get_user(uid, name)
    settle_user_day(user)
    today = day_key()

    text = (arg or "").strip()
    auto_pref = user.feed_auto_food
    chosen = ""

    if text.startswith("自动"):
        rest = text[2:].strip()
        if not rest or rest in ("关闭", "关", "off", "取消"):
            await repo.update_user(uid, feed_auto_food="")
            return _auto_setting_card(name, "", disabled=True)
        item = item_data.resolve_item(rest)
        if item is None or item.id not in (RICE_BOWL, RICE_POT):
            raise WhaleError(
                "自动购买只能选白米饭或铁盆饭",
                hint="例：/喂食 自动 白米饭；/喂食 关闭自动",
            )
        await repo.update_user(uid, feed_auto_food=item.id)
        return _auto_setting_card(name, item.id, disabled=False)
    elif text in ("关闭自动", "关闭喂养自动", "取消自动"):
        await repo.update_user(uid, feed_auto_food="")
        return _auto_setting_card(name, "", disabled=True)
    elif text:
        item = item_data.resolve_item(text)
        if item is not None and item.category == item_data.CAT_CROP:
            _, crop_slot = _slot_of_day()
            return await _feed_crop(
                ctx, repo, user, uid, name, group_id, crop_slot, text, umo
            )
        if item is None or item.id not in (RICE_BOWL, RICE_POT):
            raise WhaleError(
                "喂食只能喂白米饭、铁盆饭，或花园收获的果实",
                hint="例：/喂食 白米饭；/喂食 自动 铁盆饭；/喂食 苹果",
            )
        chosen = item.id

    slot_index, slot_name = _slot_of_day()

    used: set[str] = set()
    if user.feed_day == today and user.feed_slots:
        used = {part for part in str(user.feed_slots).split(",") if part}
    if str(slot_index) in used:
        raise WhaleError(
            f"{slot_name}已经喂过啦",
            hint="等下一个饭点再来，或者明天再来",
        )

    bag = await repo.get_bag(uid)
    order = [chosen] if chosen else [auto_pref, RICE_BOWL, RICE_POT]
    order = [x for x in order if x in (RICE_BOWL, RICE_POT)]
    food_id = next((x for x in order if bag.count(x) > 0), "")
    bought = 0
    if not food_id:
        # 背包里没有：按指定 / 偏好自动购买
        food_id = chosen or auto_pref
        if not food_id:
            raise NotEnoughItem(
                name_of(RICE_BOWL),
                1,
                hint="可以 /购买 白米饭，或 /喂食 自动 白米饭 开启自动购买",
            )
        food_def = item_data.get_item(food_id)
        price = int(food_def.price) if food_def else 0
        if price <= 0:
            raise NotEnoughItem(name_of(food_id), 1)
        latest = await repo.get_user(uid, name)
        if latest.coins < price:
            raise NotEnoughCoins(price, latest.coins)
        if not await repo.spend_coins(uid, price):
            latest = await repo.get_user(uid, name)
            raise NotEnoughCoins(price, latest.coins)
        bag = await repo.add_items(uid, {food_id: 1})
        bought = price

    pot = food_id == RICE_POT
    food_name = name_of(food_id)
    ok, bag = await repo.consume_items(uid, {food_id: 1})
    if not ok:
        raise NotEnoughItem(food_name, 1)

    gain = rng.randint(C.FEED_AFFECTION_MIN, C.FEED_AFFECTION_MAX)
    if pot:
        gain *= POT_AFFECTION_MULTIPLIER
    coins = rng.randint(C.FEED_COIN_MIN, C.FEED_COIN_MAX)
    if pot:
        coins += rng.randint(POT_BONUS_COIN_MIN, POT_BONUS_COIN_MAX)

    total = int(user.feed_total) + 1
    step, reward = _milestone_for(total, int(user.feed_milestone))
    reward_items: dict[str, int] = {}
    if reward:
        coins += int(reward.get("coins", 0))
        reward_items = {k: int(v) for k, v in (reward.get("items") or {}).items()}

    used.add(str(slot_index))
    await repo.update_user(
        uid,
        feed_day=today,
        feed_slots=",".join(sorted(used, key=int)),
        feed_count=len(used),
        feed_total=total,
        feed_milestone=(step or int(user.feed_milestone)),
        day_key=user.day_key,
    )
    affection = await repo.add_bot_affection(uid, gain)
    await grant(ctx, uid, coins=coins, items=reward_items, exp=FEED_EXP)

    line = await _food_line(ctx, name, food_name, umo)
    c = card(
        f"{slot_name} · {food_name}",
        f"{name or '你'} 投喂了 {C.BRAND_FISH_NAME}",
        art=ART_FATFISH,
        accent=C.ACCENT_BLUE,
        badge=f"今日 {len(used)}/{C.FEED_DAILY_LIMIT}",
        tone="legend" if reward else ("success" if pot else "normal"),
        footer="每天早/中/晚各一次；/喂食 自动 白米饭 可开启自动购买",
    )
    c.stats = [
        stat("本轮好感", f"+{gain}", C.RARITY_COLORS[C.RARITY_XIAOHONG]),
        stat("鲸鱼娘好感", affection, C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("累计投喂", f"{total} 次", C.RARITY_COLORS[C.RARITY_XIAOLAN]),
        stat("鲸币余额", int(user.coins) + coins, C.ACCENT_BLUE),
    ]
    c.blocks.append(quote_block(line, author=f"—— {C.BRAND_FISH_NAME}"))
    source = f"自动购买 -{bought} 鲸币" if bought else "来自背包"
    c.blocks.append(
        list_block(
            "结算明细",
            [
                f"食物：{food_name} x1（{source}，剩余 {bag.count(food_id)}）",
                f"好感 +{gain}" + ("（铁盆 ×2）" if pot else ""),
                f"鲸币 +{coins}（含铁盆/里程碑奖励）",
                f"经验 +{FEED_EXP}",
            ],
        )
    )
    if step:
        c.blocks.append(
            text_block(f"累计 {step} 次里程碑达成！奖励已发放（下次间隔更大）", title="🎁 里程碑")
        )

    next_step = next(
        (s for s in C.FEED_TOTAL_MILESTONES if s > max(int(user.feed_milestone), step)), 0
    )
    if next_step:
        c.blocks.append(
            progress_block(
                f"累计投喂里程碑（下一次 {next_step} 次）",
                min(total, next_step),
                next_step,
                text=f"已投喂 {total} 次，还差 {max(0, next_step - total)} 次",
            )
        )
    return c.validate()


def _auto_setting_card(name: str, food_id: str, *, disabled: bool) -> Card:
    """自动购买设置结果卡（不消耗饭点）。"""
    c = card(
        "喂食自动购买已更新",
        f"{name or '你'} · " + ("已关闭" if disabled else f"缺饭时自动买 {name_of(food_id)}"),
        art=ART_FATFISH,
        tone="success" if not disabled else "warn",
        badge="OFF" if disabled else "AUTO",
        footer="/喂食 自动 白米饭 开启；/喂食 关闭自动 关闭",
    )
    c.blocks.append(
        text_block(
            "关闭后，背包没饭时会提示你自己去买。"
            if disabled
            else f"以后背包里没有 {name_of(food_id)} 时，会自动花 {item_data.get_item(food_id).price} 鲸币买 1 份再喂。"
        )
    )
    return c.validate()

async def _feed_crop(
    ctx: SvcCtx,
    repo,
    user,
    uid: str,
    name: str,
    group_id: str,
    slot_name: str,
    text: str,
    umo: str,
) -> Card:
    """果实餐：1 份果实 + 1 个空碗/空盆，好感和鲸币都更高。"""
    crop = item_data.resolve_item(text)
    if crop is None or crop.category != item_data.CAT_CROP:
        raise WhaleError("这不是能吃的果实", hint="用 /播种 种出来的果实试试")

    bag = await repo.get_bag(uid)
    if bag.count(crop.id) <= 0:
        raise NotEnoughItem(crop.name, 1)
    if bag.count(POT) > 0:
        vessel, bonus = POT, True
    elif bag.count(BOWL) > 0:
        vessel, bonus = BOWL, False
    else:
        raise NotEnoughItem(
            "空碗",
            1,
            hint="空碗/空盆可以 /购买，也可能在钓鱼时捞到",
        )

    today = day_key()
    slot_index, _ = _slot_of_day()
    used = set()
    if user.feed_day == today and user.feed_slots:
        used = {part for part in str(user.feed_slots).split(",") if part}
    if str(slot_index) in used:
        raise WhaleError(f"{slot_name}已经喂过啦", hint="等下一个饭点再来")

    ok, bag = await repo.consume_items(uid, {crop.id: 1, vessel: 1})
    if not ok:
        raise NotEnoughItem(f"{crop.name} + 空碗", 1)

    gain = CROP_AFFECTION + (CROP_POT_BONUS_AFFECTION if bonus else 0)
    coins = CROP_COINS + (CROP_POT_BONUS_COINS if bonus else 0)
    used.add(str(slot_index))

    total = int(user.feed_total) + 1
    step, reward = _milestone_for(total, int(user.feed_milestone))
    reward_items: dict[str, int] = {}
    if reward:
        coins += int(reward.get("coins", 0))
        reward_items = {k: int(v) for k, v in (reward.get("items") or {}).items()}

    await repo.update_user(
        uid,
        feed_day=today,
        feed_slots=",".join(sorted(used, key=int)),
        feed_count=len(used),
        feed_total=total,
        feed_milestone=(step or int(user.feed_milestone)),
        day_key=user.day_key,
    )
    affection = await repo.add_bot_affection(uid, gain)
    await grant(ctx, uid, coins=coins, items=reward_items, exp=FEED_EXP)

    line = await _food_line(ctx, name, crop.name, umo)
    c = card(
        f"{slot_name} · 果实餐",
        f"{name or '你'} 用 {crop.name} 喂了 {C.BRAND_FISH_NAME}",
        art=ART_FATFISH,
        accent=C.GARDEN_ACCENT,
        badge=f"今日 {len(used)}/{C.FEED_DAILY_LIMIT}",
        tone="legend" if step else "success",
        footer="果实餐 = 1 份果实 + 1 个空碗/空盆",
    )
    c.stats = [
        stat("本轮好感", f"+{gain}", C.RARITY_COLORS[C.RARITY_XIAOHONG]),
        stat("鲸鱼娘好感", affection, C.RARITY_COLORS[C.RARITY_XIAOJIN]),
        stat("累计投喂", f"{total} 次", C.RARITY_COLORS[C.RARITY_XIAOLAN]),
        stat("鲸币", coins, C.ACCENT_BLUE),
    ]
    c.blocks.append(quote_block(line, author=f"—— {C.BRAND_FISH_NAME}"))
    c.blocks.append(
        list_block(
            "结算明细",
            [
                f"消耗：{crop.name} x1 + {name_of(vessel)} x1",
                f"好感 +{gain}" + ("（空盆加成）" if bonus else ""),
                f"鲸币 +{coins}（含里程碑奖励）",
            ],
        )
    )
    if step:
        c.blocks.append(text_block(f"累计 {step} 次里程碑达成！", title="🎁 里程碑"))
    return c.validate()
