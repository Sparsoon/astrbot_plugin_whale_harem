"""数据仓库层：所有 SQL 只出现在这里。

约定
    1. 每个方法自己拿一次连接（或一个事务），绝不嵌套借用第二条连接。
    2. 写操作走 `db.run_tx` / `db.transaction`，天然串行，杜绝并发写坏 JSON。
    3. 所有返回值都是轻量 dataclass，调用方不需要处理 sqlite3.Row。
    4. 惰性重置：涉及"每日"的字段一律带 day_key，读到期就顺便结算，不跑定时器。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

import aiosqlite

from ..config import PluginConfig
from ..constants import POND_MAX_LEVEL, POND_UPGRADE_REQUIREMENTS
from ..errors import WhaleError
from ..log import log
from ..models import Bag, Garden, HaremRow, Pond, User, parse_bag, parse_int_dict
from ..time_utils import day_key, now_ts
from .database import Database

EXPIRE_FMT = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    return datetime.now().strftime(EXPIRE_FMT)


def _row_to_user(row: Any) -> User:
    return User.from_row(row)


def _clean_items(items: Mapping[str, Any] | None) -> dict[str, int]:
    """把外部传入的 {item_id: 数量} 洗成 {str: 正整数}。

    防御三类脏输入：None / 非数字数量 / 非正数量 —— 它们只会让事务白跑一趟
    甚至触发 ValueError 回滚，这里直接丢弃并返回干净字典。
    """
    if not items:
        return {}
    out: dict[str, int] = {}
    for item_id, num in items.items():
        try:
            count = int(num)
        except (TypeError, ValueError):
            log.debug(f"忽略非法物品数量: {item_id}={num!r}")
            continue
        if count > 0:
            out[str(item_id)] = count
    return out


def _bag_from_row(row: Any, fish_limit: int) -> Bag:
    """把 bags 行解析成 Bag；列缺失 / JSON 损坏时退化为空背包而不是抛异常。"""
    try:
        items_raw = row["items"]
        fish_raw = row["fish"]
    except (IndexError, KeyError, TypeError):
        log.warning("bags 行列缺失（结构不一致？），本次按空背包处理")
        return Bag()
    return parse_bag(items_raw, fish_raw, fish_limit)


class Repo:
    """聚合仓库。service 层只依赖这个门面。"""

    __slots__ = ("db", "cfg")

    def __init__(self, db: Database, cfg: PluginConfig) -> None:
        self.db = db
        self.cfg = cfg

    # ================================================================ 用户
    async def get_user(self, uid: str, name: str = "") -> User:
        uid = str(uid or "").strip()
        name = str(name or "")
        row = await self.db.fetch_one("SELECT * FROM users WHERE uid=?", (uid,))
        if row is None:
            ts = now_str()
            await self.db.execute(
                "INSERT OR IGNORE INTO users(uid, name, day_key, created_at, updated_at) VALUES(?,?,?,?,?)",
                (uid, name, day_key(), ts, ts),
            )
            row = await self.db.fetch_one("SELECT * FROM users WHERE uid=?", (uid,))
        if row is None:
            # 理论上只有磁盘满 / 表被删才会走到这里；转成业务异常而不是 ValueError，
            # 让指令层能回一张提示卡。
            log.error(f"users 表读取失败 uid={uid!r}")
            raise WhaleError("用户数据读写出错，请稍后重试")
        user = _row_to_user(row)
        if name and user.name != name:
            await self.db.execute("UPDATE users SET name=? WHERE uid=?", (name, uid))
            user.name = name
        return user

    async def update_user(self, uid: str, **fields: Any) -> None:
        allowed = {
            "name",
            "coins",
            "level",
            "exp",
            "checkin_streak",
            "last_checkin_day",
            "total_checkin",
            "feed_day",
            "feed_count",
            "feed_reward_day",
            "feed_slots",
            "feed_total",
            "feed_milestone",
            "feed_auto_food",
            "work_ready_ts",
            "work_started_ts",
            "work_minutes",
            "work_pending_coins",
            "fish_ready_ts",
            "fish_bait",
            "fish_wait_min",
            "fish_group",
            "bot_affection",
            "total_earned",
            "total_spent",
            "day_key",
        }
        # 所有列都是 NOT NULL：value=None 会直接触发 IntegrityError 把事务打回，
        # 所以在进入 SQL 之前就过滤掉（正常调用不会传 None，纯防御）。
        pairs = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
        dropped = len([k for k in fields if k in allowed]) - len(pairs)
        if dropped > 0:
            log.debug(f"update_user 忽略了 {dropped} 个 None 字段 uid={uid}")
        if not pairs:
            return
        sets = ", ".join(f"{k}=?" for k, _ in pairs)
        params = [v for _, v in pairs]
        params.append(now_str())
        params.append(uid)
        await self.db.execute(f"UPDATE users SET {sets}, updated_at=? WHERE uid=?", tuple(params))

    async def add_bot_affection(self, uid: str, delta: int) -> int:
        """原子增加"对鲸鱼娘的好感"；返回最新值（喂食专用）。"""
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            delta = 0
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE users SET bot_affection=bot_affection+?, updated_at=? WHERE uid=?",
                (delta, now_str(), uid),
            )
            await cursor.close()
            cursor = await conn.execute("SELECT bot_affection FROM users WHERE uid=?", (uid,))
            row = await cursor.fetchone()
            await cursor.close()
            return int(row["bot_affection"] or 0) if row is not None else 0

    async def add_coins(self, uid: str, delta: int, *, count_as_earn: bool = True) -> int:
        """原子加减鲸币；返回最新余额。负数请用 spend_coins。"""
        try:
            delta = int(delta)
        except (TypeError, ValueError):
            log.warning(f"add_coins 收到非法 delta={delta!r}，按 0 处理 uid={uid}")
            delta = 0
        if delta == 0:
            value = await self.db.fetch_value("SELECT coins FROM users WHERE uid=?", (uid,), 0)
            return int(value or 0)
        ts = now_str()
        # UPDATE + SELECT 放进同一个事务：返回的余额一定等于本次修改后的值，
        # 不会被别的连接在同一瞬间插队改掉（少一次"读到旧值覆盖"的机会）。
        async with self.db.transaction() as conn:
            if delta > 0:
                col = "total_earned" if count_as_earn else "total_spent"
                cursor = await conn.execute(
                    f"UPDATE users SET coins=coins+?, {col}={col}+?, updated_at=? WHERE uid=?",
                    (delta, delta, ts, uid),
                )
            else:
                cursor = await conn.execute(
                    "UPDATE users SET coins=coins+?, total_spent=total_spent+?, updated_at=? WHERE uid=?",
                    (delta, abs(delta), ts, uid),
                )
            changed = cursor.rowcount or 0
            await cursor.close()
            cursor = await conn.execute("SELECT coins FROM users WHERE uid=?", (uid,))
            row = await cursor.fetchone()
            await cursor.close()
        if changed == 0:
            # 没有 users 行：金币加了等于没加。宁可留一条告警也不静默吞钱。
            log.warning(f"add_coins 未命中用户 {uid}（delta={delta}），请确认调用前已 get_user")
        if row is None:
            return 0
        return int(row["coins"] or 0)

    async def spend_coins(self, uid: str, amount: int) -> bool:
        """条件扣费：余额不足时不做任何修改（CAS 语义）。"""
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            log.warning(f"spend_coins 收到非法 amount={amount!r}，按扣费失败处理 uid={uid}")
            return False
        if amount <= 0:
            return True
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE users SET coins=coins-?, total_spent=total_spent+?, updated_at=? "
                "WHERE uid=? AND coins>=?",
                (amount, amount, now_str(), uid, amount),
            )
            changed = cursor.rowcount or 0
            await cursor.close()
        return changed > 0

    async def add_exp(self, uid: str, amount: int) -> tuple[int, int, bool]:
        """返回 (等级, 当前经验, 是否升级)。等级经验曲线 100*level，无上限但收益封顶。"""
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            row = await self.db.fetch_one("SELECT level, exp FROM users WHERE uid=?", (uid,))
            if row is None:
                return 1, 0, False
            return int(row["level"]), int(row["exp"]), False
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT level, exp FROM users WHERE uid=?", (uid,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                log.warning(f"add_exp 未命中用户 {uid}，经验未发放")
                return 1, 0, False
            level = max(1, int(row["level"]))
            exp = int(row["exp"]) + amount
            leveled = False
            while exp >= 100 * level and level < 99:
                exp -= 100 * level
                level += 1
                leveled = True
            await conn.execute(
                "UPDATE users SET level=?, exp=?, updated_at=? WHERE uid=?",
                (level, exp, now_str(), uid),
            )
        return level, exp, leveled

    async def take_fish_pending(self, uid: str, now: int) -> tuple[str, int, str] | None:
        """原子领取"已到点"的一竿：返回 (饵料, 等待分钟, 群号)，没有则 None。

        用 UPDATE ... WHERE 条件保证并发下只有一个人能领到，监听器与指令不会重复结算。
        """
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT fish_bait, fish_wait_min, fish_group, fish_ready_ts FROM users WHERE uid=?",
                (uid,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            ready = int(row["fish_ready_ts"] or 0)
            if ready <= 0 or ready > int(now):
                return None
            cursor = await conn.execute(
                "UPDATE users SET fish_ready_ts=0, fish_bait='', fish_wait_min=0, fish_group='' "
                "WHERE uid=? AND fish_ready_ts=?",
                (uid, ready),
            )
            changed = cursor.rowcount or 0
            await cursor.close()
            if changed <= 0:
                return None
            return (str(row["fish_bait"] or ""), int(row["fish_wait_min"] or 0), str(row["fish_group"] or ""))

    async def take_work_pending(self, uid: str, now: int) -> tuple[int, int] | None:
        """原子领取"已到点"的工资：返回 (工资, 分钟)，没有则 None。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT work_pending_coins, work_minutes, work_ready_ts FROM users WHERE uid=?",
                (uid,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            coins = int(row["work_pending_coins"] or 0)
            ready = int(row["work_ready_ts"] or 0)
            if coins <= 0 or ready <= 0 or ready > int(now):
                return None
            cursor = await conn.execute(
                "UPDATE users SET work_pending_coins=0, work_minutes=0, work_started_ts=0, work_ready_ts=0 "
                "WHERE uid=? AND work_pending_coins=?",
                (uid, coins),
            )
            changed = cursor.rowcount or 0
            await cursor.close()
            if changed <= 0:
                return None
            return (coins, int(row["work_minutes"] or 0))

    async def peek_user(self, uid: str) -> User | None:
        """只读用户，不存在返回 None（不建行）——群消息监听器用，避免给潜水员建行。"""
        row = await self.db.fetch_one("SELECT * FROM users WHERE uid=?", (uid,))
        return User.from_row(row) if row is not None else None

    # ================================================================ 花园
    async def get_garden(self, group_id: str, uid: str) -> Garden | None:
        row = await self.db.fetch_one(
            "SELECT * FROM gardens WHERE group_id=? AND uid=?", (group_id, uid)
        )
        return Garden.from_row(row, group_id, uid) if row is not None else None

    async def save_garden(self, garden: Garden) -> None:
        await self.db.execute(
            "INSERT INTO gardens(group_id, uid, seed_id, plant_id, planted_ts, growth, mutation, stage, "
            "watered_ts, watered_day, water_count, fert_day, fert_count, sun_day, sun_ts, "
            "last_n, last_p, last_k, dead, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id, uid) DO UPDATE SET "
            "seed_id=excluded.seed_id, plant_id=excluded.plant_id, planted_ts=excluded.planted_ts, "
            "growth=excluded.growth, mutation=excluded.mutation, stage=excluded.stage, "
            "watered_ts=excluded.watered_ts, watered_day=excluded.watered_day, "
            "water_count=excluded.water_count, fert_day=excluded.fert_day, "
            "fert_count=excluded.fert_count, sun_day=excluded.sun_day, sun_ts=excluded.sun_ts, "
            "last_n=excluded.last_n, last_p=excluded.last_p, last_k=excluded.last_k, "
            "dead=excluded.dead, created_at=excluded.created_at, updated_at=excluded.updated_at",
            (
                garden.group_id, garden.uid, garden.seed_id, garden.plant_id, garden.planted_ts,
                garden.growth, garden.mutation, garden.stage, garden.watered_ts, garden.watered_day,
                garden.water_count, garden.fert_day, garden.fert_count, garden.sun_day, garden.sun_ts,
                garden.last_n, garden.last_p, garden.last_k, garden.dead,
                garden.created_at or now_str(), now_str(),
            ),
        )

    async def delete_garden(self, group_id: str, uid: str) -> None:
        await self.db.execute("DELETE FROM gardens WHERE group_id=? AND uid=?", (group_id, uid))

    # ================================================================ 咬钩
    async def pending_fish(self, group_id: str, now: int, limit: int = 20) -> list[User]:
        """本群"已可收竿但还没被咬钩"的用户（咬钩由群消息随机触发）。

        只在群消息到达时调用，走 `idx_users_fish(group_id, fish_ready_ts)`；
        没有待收竿的鱼时是一条空查询，成本可忽略。
        """
        rows = await self.db.fetch_all(
            "SELECT * FROM users WHERE fish_group=? AND fish_ready_ts>0 AND fish_ready_ts<=? LIMIT ?",
            (group_id, int(now), max(1, int(limit))),
        )
        return [User.from_row(row) for row in rows]

    # ================================================================ 背包
    async def get_bag(self, uid: str) -> Bag:
        row = await self.db.fetch_one("SELECT items, fish FROM bags WHERE uid=?", (uid,))
        if row is None:
            return Bag()
        return _bag_from_row(row, self.cfg.fish_bag_limit)

    async def save_bag(self, uid: str, bag: Bag) -> None:
        await self.db.execute(
            "INSERT INTO bags(uid, items, fish, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET items=excluded.items, fish=excluded.fish, updated_at=excluded.updated_at",
            (uid, json.dumps(bag.items, ensure_ascii=False), json.dumps(bag.fish, ensure_ascii=False), now_str()),
        )

    async def mutate_bag(self, uid: str, mutator: Callable[[Bag], Any]) -> Bag:
        """在单个事务里读-改-写背包，并发安全。mutator 为同步函数。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT items, fish FROM bags WHERE uid=?", (uid,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                bag = Bag()
            else:
                bag = _bag_from_row(row, self.cfg.fish_bag_limit)
            mutator(bag)
            await conn.execute(
                "INSERT INTO bags(uid, items, fish, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET items=excluded.items, fish=excluded.fish, updated_at=excluded.updated_at",
                (
                    uid,
                    json.dumps(bag.items, ensure_ascii=False),
                    json.dumps(bag.fish, ensure_ascii=False),
                    now_str(),
                ),
            )
        return bag

    async def add_items(self, uid: str, items: Mapping[str, int]) -> Bag:
        clean = _clean_items(items)

        def _mut(bag: Bag) -> None:
            for item_id, num in clean.items():
                bag.add(item_id, num)

        return await self.mutate_bag(uid, _mut)

    async def consume_items(self, uid: str, items: Mapping[str, int]) -> tuple[bool, Bag]:
        """全有或全无地扣物品，返回 (是否成功, 最新背包)。"""
        result = {"ok": False}
        clean = _clean_items(items)

        def _mut(bag: Bag) -> None:
            for item_id, num in clean.items():
                if bag.count(item_id) < num:
                    return
            for item_id, num in clean.items():
                bag.remove(item_id, num)
            result["ok"] = True

        bag = await self.mutate_bag(uid, _mut)
        return bool(result["ok"]), bag

    # ================================================================ 池塘
    async def get_pond(self, group_id: str) -> Pond:
        row = await self.db.fetch_one("SELECT * FROM ponds WHERE group_id=?", (group_id,))
        if row is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO ponds(group_id, level, materials, contribution, updated_at) VALUES(?,?,?,?,?)",
                (group_id, 1, "{}", "{}", now_str()),
            )
            row = await self.db.fetch_one("SELECT * FROM ponds WHERE group_id=?", (group_id,))
        return Pond.from_row(row, group_id)

    async def save_pond(self, pond: Pond) -> None:
        await self.db.execute(
            "INSERT INTO ponds(group_id, level, materials, contribution, weather, weather_until, updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET level=excluded.level, materials=excluded.materials, "
            "contribution=excluded.contribution, weather=excluded.weather, "
            "weather_until=excluded.weather_until, updated_at=excluded.updated_at",
            (
                pond.group_id,
                pond.level,
                json.dumps(pond.materials, ensure_ascii=False),
                json.dumps(pond.contribution, ensure_ascii=False),
                pond.weather,
                pond.weather_until,
                now_str(),
            ),
        )

    async def set_pond_weather(self, group_id: str, weather: str, until: int) -> None:
        """天气控制机：把本群天气锁定到 until（unix 秒）。"""
        await self.db.execute(
            "INSERT INTO ponds(group_id, level, materials, contribution, weather, weather_until, updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET weather=excluded.weather, "
            "weather_until=excluded.weather_until, updated_at=excluded.updated_at",
            (group_id, 1, "{}", "{}", weather, int(until), now_str()),
        )

    async def donate_materials(self, group_id: str, uid: str, items: Mapping[str, int]) -> Pond:
        """群友捐献：材料进池子，同时记录个人贡献度。"""
        clean = _clean_items(items)
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM ponds WHERE group_id=?", (group_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                pond = Pond(group_id=group_id)
            else:
                pond = Pond.from_row(row, group_id)
            for item_id, num in clean.items():
                pond.add_material(item_id, num)
                pond.contribution[uid] = pond.contribution.get(uid, 0) + num
            await conn.execute(
                "INSERT INTO ponds(group_id, level, materials, contribution, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET level=excluded.level, materials=excluded.materials, "
                "contribution=excluded.contribution, updated_at=excluded.updated_at",
                (
                    pond.group_id,
                    pond.level,
                    json.dumps(pond.materials, ensure_ascii=False),
                    json.dumps(pond.contribution, ensure_ascii=False),
                    now_str(),
                ),
            )
        return pond

    async def take_pond_upgrade(self, group_id: str) -> tuple[Pond, bool, dict[str, int]]:
        """材料凑够就自动升一级（每次最多一级）。返回 (池塘, 是否升级, 本次消耗)。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("SELECT * FROM ponds WHERE group_id=?", (group_id,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                pond = Pond(group_id=group_id)
            else:
                pond = Pond.from_row(row, group_id)
            if pond.level >= POND_MAX_LEVEL:
                return pond, False, {}
            req = POND_UPGRADE_REQUIREMENTS.get(pond.level + 1) or {}
            if not req:
                return pond, False, {}
            if any(pond.materials.get(k, 0) < v for k, v in req.items()):
                return pond, False, {}
            for k, v in req.items():
                pond.materials[k] = pond.materials.get(k, 0) - v
                if pond.materials[k] <= 0:
                    pond.materials.pop(k, None)
            pond.level += 1
            await conn.execute(
                "INSERT INTO ponds(group_id, level, materials, contribution, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET level=excluded.level, materials=excluded.materials, "
                "contribution=excluded.contribution, updated_at=excluded.updated_at",
                (
                    pond.group_id,
                    pond.level,
                    json.dumps(pond.materials, ensure_ascii=False),
                    json.dumps(pond.contribution, ensure_ascii=False),
                    now_str(),
                ),
            )
        log.info(f"池塘 {group_id} 升级到 Lv.{pond.level}")
        return pond, True, dict(req)

    # ================================================================ 后宫
    async def get_harem(self, group_id: str, target_uid: str) -> HaremRow | None:
        row = await self.db.fetch_one(
            "SELECT * FROM harem WHERE group_id=? AND target_uid=?", (group_id, target_uid)
        )
        if row is None:
            return None
        return HaremRow.from_row(row, group_id)

    async def ensure_harem(self, group_id: str, target_uid: str, target_name: str = "") -> HaremRow:
        row = await self.get_harem(group_id, target_uid)
        if row is not None:
            if target_name and row.target_name != target_name:
                await self.db.execute(
                    "UPDATE harem SET target_name=? WHERE group_id=? AND target_uid=?",
                    (target_name, group_id, target_uid),
                )
                row.target_name = target_name
            return row
        await self.db.execute(
            "INSERT OR IGNORE INTO harem(group_id, target_uid, target_name, day_key, created_at) VALUES(?,?,?,?,?)",
            (group_id, target_uid, target_name, day_key(), now_str()),
        )
        row = await self.get_harem(group_id, target_uid)
        if row is None:
            # 不是 RuntimeError：指令层只认 WhaleError，否则用户那边是"没回复"
            log.error(f"harem 记录创建失败 group={group_id} target={target_uid}")
            raise WhaleError("关系数据初始化失败，请稍后重试")
        return row

    async def save_harem(self, row: HaremRow) -> None:
        await self.db.execute(
            "UPDATE harem SET target_name=?, owner_uid=?, owner_name=?, affection=?, marry_fail=?, "
            "ntr_fail=?, divorce_fail=?, ntr_count=?, chase_count=?, day_key=? "
            "WHERE group_id=? AND target_uid=?",
            (
                row.target_name,
                row.owner_uid,
                row.owner_name,
                row.affection,
                row.marry_fail,
                row.ntr_fail,
                row.divorce_fail,
                row.ntr_count,
                row.chase_count,
                row.day_key,
                row.group_id,
                row.target_uid,
            ),
        )

    async def mutate_harem(
        self, group_id: str, target_uid: str, mutator: Callable[[HaremRow], Any], target_name: str = ""
    ) -> HaremRow:
        """事务内读-改-写，避免两个群友同时抢同一个目标时数据打架。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM harem WHERE group_id=? AND target_uid=?", (group_id, target_uid)
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await conn.execute(
                    "INSERT OR IGNORE INTO harem(group_id, target_uid, target_name, day_key, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (group_id, target_uid, target_name, day_key(), now_str()),
                )
                cursor = await conn.execute(
                    "SELECT * FROM harem WHERE group_id=? AND target_uid=?", (group_id, target_uid)
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None:
                # 极罕见（表被删/磁盘只读）：不能拿空行继续 mutate，否则会写一条
                # target_uid='' 的幽灵记录，关系数据静默丢失。
                log.error(f"harem 记录读取失败 group={group_id} target={target_uid}")
                raise WhaleError("关系数据初始化失败，请稍后重试")
            entry = HaremRow.from_row(row, group_id)
            if target_name and entry.target_name != target_name:
                entry.target_name = target_name
            mutator(entry)
            await conn.execute(
                "UPDATE harem SET target_name=?, owner_uid=?, owner_name=?, affection=?, marry_fail=?, "
                "ntr_fail=?, divorce_fail=?, ntr_count=?, chase_count=?, day_key=? "
                "WHERE group_id=? AND target_uid=?",
                (
                    entry.target_name,
                    entry.owner_uid,
                    entry.owner_name,
                    entry.affection,
                    entry.marry_fail,
                    entry.ntr_fail,
                    entry.divorce_fail,
                    entry.ntr_count,
                    entry.chase_count,
                    entry.day_key,
                    entry.group_id,
                    entry.target_uid,
                ),
            )
        return entry

    async def list_harem(self, group_id: str, owner_uid: str, limit: int = 10) -> list[HaremRow]:
        rows = await self.db.fetch_all(
            "SELECT * FROM harem WHERE group_id=? AND owner_uid=? ORDER BY created_at DESC LIMIT ?",
            (group_id, owner_uid, max(1, limit)),
        )
        return [HaremRow.from_row(r, group_id) for r in rows]

    async def harem_count(self, group_id: str, owner_uid: str) -> int:
        value = await self.db.fetch_value(
            "SELECT COUNT(1) FROM harem WHERE group_id=? AND owner_uid=?",
            (group_id, owner_uid),
            0,
        )
        return int(value or 0)

    async def total_harem(self, owner_uid: str) -> int:
        value = await self.db.fetch_value("SELECT COUNT(1) FROM harem WHERE owner_uid=?", (owner_uid,), 0)
        return int(value or 0)

    async def affection_ranking(self, group_id: str, limit: int = 10) -> list[HaremRow]:
        rows = await self.db.fetch_all(
            "SELECT * FROM harem WHERE group_id=? AND owner_uid<>'' ORDER BY affection DESC, created_at ASC LIMIT ?",
            (group_id, max(1, limit)),
        )
        return [HaremRow.from_row(r, group_id) for r in rows]

    async def public_targets(self, group_id: str, limit: int = 10) -> list[HaremRow]:
        rows = await self.db.fetch_all(
            "SELECT * FROM harem WHERE group_id=? AND owner_uid='' ORDER BY chase_count DESC, created_at ASC LIMIT ?",
            (group_id, max(1, limit)),
        )
        return [HaremRow.from_row(r, group_id) for r in rows]

    async def ntr_ranking(self, group_id: str, limit: int = 10) -> list[HaremRow]:
        rows = await self.db.fetch_all(
            "SELECT * FROM harem WHERE group_id=? AND ntr_count>0 ORDER BY ntr_count DESC LIMIT ?",
            (group_id, max(1, limit)),
        )
        return [HaremRow.from_row(r, group_id) for r in rows]

    async def married_targets_of(self, group_id: str, target_uid: str) -> HaremRow | None:
        return await self.get_harem(group_id, target_uid)

    # ================================================================ BUFF
    async def add_buff(
        self,
        subject_uid: str,
        kind: str,
        duration_sec: int,
        group_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        expires = now_ts() + max(1, int(duration_sec))
        await self.db.execute(
            "INSERT INTO buffs(subject_uid, kind, group_id, expires_ts, payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(subject_uid, kind, group_id) DO UPDATE SET "
            "expires_ts=excluded.expires_ts, payload=excluded.payload",
            (
                subject_uid,
                kind,
                group_id,
                expires,
                json.dumps(dict(payload or {}), ensure_ascii=False),
            ),
        )
        return expires

    async def get_buff(self, subject_uid: str, kind: str, group_id: str = "") -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT expires_ts, payload FROM buffs WHERE subject_uid=? AND kind=? AND group_id=?",
            (subject_uid, kind, group_id),
        )
        if row is None:
            return None
        if int(row["expires_ts"] or 0) <= now_ts():
            # 惰性清理：不跑定时器，读到过期就地删除
            await self.db.execute(
                "DELETE FROM buffs WHERE subject_uid=? AND kind=? AND group_id=?",
                (subject_uid, kind, group_id),
            )
            return None
        payload = parse_int_dict(row["payload"])
        payload["expires_ts"] = int(row["expires_ts"] or 0)
        return payload

    async def has_buff(self, subject_uid: str, kind: str, group_id: str = "") -> bool:
        return await self.get_buff(subject_uid, kind, group_id) is not None

    async def clear_buff(self, subject_uid: str, kind: str, group_id: str = "") -> None:
        await self.db.execute(
            "DELETE FROM buffs WHERE subject_uid=? AND kind=? AND group_id=?",
            (subject_uid, kind, group_id),
        )

    async def active_buffs(self, subject_uid: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT kind, group_id, expires_ts FROM buffs WHERE subject_uid=? AND expires_ts>? ORDER BY expires_ts",
            (subject_uid, now_ts()),
        )
        return [
            {"kind": r["kind"], "group_id": r["group_id"], "expires_ts": int(r["expires_ts"])}
            for r in rows
        ]

    async def expire_buffs(self) -> int:
        """只在插件启动时调用一次，清掉历史残留（不是轮询）。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM buffs WHERE expires_ts<=?", (now_ts(),))
            count = cursor.rowcount or 0
            await cursor.close()
        if count:
            log.info(f"清理过期 buff {count} 条")
        return int(count)

    # ================================================================ KV
    async def kv_get(self, key: str, default: str = "") -> str:
        value = await self.db.fetch_value("SELECT v FROM kv WHERE k=?", (key,), default)
        return str(value)

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )

    # ================================================================ 统计
    async def group_overview(self, group_id: str) -> dict[str, Any]:
        """一次查询拿全群概况，避免 N 次查询。"""
        row = await self.db.fetch_one(
            "SELECT COUNT(1) AS married, COALESCE(SUM(affection),0) AS affection "
            "FROM harem WHERE group_id=? AND owner_uid<>''",
            (group_id,),
        )
        members = await self.db.fetch_value(
            "SELECT COUNT(DISTINCT owner_uid) FROM harem WHERE group_id=? AND owner_uid<>''",
            (group_id,),
            0,
        )
        pond = await self.get_pond(group_id)
        return {
            "married": int(row["married"]) if row else 0,
            "affection": int(row["affection"]) if row else 0,
            "members": int(members or 0),
            "pond_level": pond.level,
        }

    async def delete_all_of_user(self, uid: str) -> None:
        """数据擦除（合规用）。口径（队长确认）：

        * 用户资料 / 背包 / 随身 buff 直接删除；
        * 该用户**拥有的**后宫关系转为公共（owner 清空），不删目标行——
          目标是别人的数据，删了会连带抹掉别人群里的关系记录；
        * 该用户**作为目标**的行保留关系结构，只清空昵称（避免残留可识别信息）。
        """
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE harem SET owner_uid='', owner_name='' WHERE owner_uid=?", (uid,)
            )
            await conn.execute(
                "UPDATE harem SET target_name='' WHERE target_uid=?", (uid,)
            )
            for sql in (
                "DELETE FROM users WHERE uid=?",
                "DELETE FROM bags WHERE uid=?",
                "DELETE FROM buffs WHERE subject_uid=?",
            ):
                await conn.execute(sql, (uid,))