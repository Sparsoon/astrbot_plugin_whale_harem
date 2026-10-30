"""数据模型：数据库行 <-> Python 对象的唯一映射，附带 JSON 字段的防御式解析。

注意：这里全部是轻量 dataclass，不做任何 IO，
因此可以被 service 层随意持有/丢弃，GC 压力极小。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import constants as C
from .log import log


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


# ------------------------------------------------------------------ 用户
@dataclass(slots=True)
class User:
    uid: str
    name: str = ""
    coins: int = 0
    level: int = 1
    exp: int = 0
    checkin_streak: int = 0
    last_checkin_day: str = ""
    total_checkin: int = 0
    feed_day: str = ""
    feed_count: int = 0
    feed_reward_day: str = ""
    feed_slots: str = ""
    feed_total: int = 0
    feed_milestone: int = 0
    feed_auto_food: str = ""
    work_ready_ts: int = 0
    work_started_ts: int = 0
    work_minutes: int = 0
    work_pending_coins: int = 0
    fish_ready_ts: int = 0
    fish_bait: str = ""
    fish_wait_min: int = 0
    fish_group: str = ""
    bot_affection: int = 0
    total_earned: int = 0
    total_spent: int = 0
    day_key: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "User":
        if row is None:
            raise ValueError("empty user row")
        keys = row.keys() if hasattr(row, "keys") else []

        def g(name: str, default: Any = None) -> Any:
            try:
                return row[name]
            except (IndexError, KeyError, TypeError):
                return default

        return cls(
            uid=_to_str(g("uid")),
            name=_to_str(g("name")),
            coins=_to_int(g("coins")),
            level=_to_int(g("level"), 1),
            exp=_to_int(g("exp")),
            checkin_streak=_to_int(g("checkin_streak")),
            last_checkin_day=_to_str(g("last_checkin_day")),
            total_checkin=_to_int(g("total_checkin")),
            feed_day=_to_str(g("feed_day")),
            feed_count=_to_int(g("feed_count")),
            feed_reward_day=_to_str(g("feed_reward_day")),
            feed_slots=_to_str(g("feed_slots")),
            feed_total=_to_int(g("feed_total")),
            feed_milestone=_to_int(g("feed_milestone")),
            feed_auto_food=_to_str(g("feed_auto_food")),
            work_ready_ts=_to_int(g("work_ready_ts")),
            work_started_ts=_to_int(g("work_started_ts")),
            work_minutes=_to_int(g("work_minutes")),
            work_pending_coins=_to_int(g("work_pending_coins")),
            fish_ready_ts=_to_int(g("fish_ready_ts")),
            fish_bait=_to_str(g("fish_bait")),
            fish_wait_min=_to_int(g("fish_wait_min")),
            fish_group=_to_str(g("fish_group")),
            bot_affection=_to_int(g("bot_affection")),
            total_earned=_to_int(g("total_earned")),
            total_spent=_to_int(g("total_spent")),
            day_key=_to_str(g("day_key")),
            created_at=_to_str(g("created_at")),
            updated_at=_to_str(g("updated_at")),
        )


# ------------------------------------------------------------------ 池塘
@dataclass(slots=True)
class Pond:
    group_id: str
    level: int = C.POND_BASE_LEVEL
    materials: dict[str, int] = field(default_factory=dict)
    contribution: dict[str, int] = field(default_factory=dict)
    weather: str = ""
    weather_until: int = 0
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any, group_id: str = "") -> "Pond":
        if row is None:
            return cls(group_id=group_id)
        try:
            materials = row["materials"]
        except (IndexError, KeyError, TypeError):
            materials = {}
        try:
            contribution = row["contribution"]
        except (IndexError, KeyError, TypeError):
            contribution = {}
        return cls(
            group_id=group_id,
            level=max(C.POND_BASE_LEVEL, min(C.POND_MAX_LEVEL, _to_int(row["level"], 1))),
            materials=parse_int_dict(materials),
            contribution=parse_int_dict(contribution),
            weather=_to_str(row["weather"]),
            weather_until=_to_int(row["weather_until"]),
            updated_at=_to_str(row["updated_at"]),
        )

    def add_material(self, item_id: str, num: int) -> None:
        if num <= 0:
            return
        self.materials[item_id] = self.materials.get(item_id, 0) + num


# ------------------------------------------------------------------ 后宫
@dataclass(slots=True)
class HaremRow:
    """一个 (群, 目标) 的关系记录。

    * owner_uid 为空 => 公共老婆（可攻略，还没被娶走）
    * marry_fail / ntr_fail / divorce_fail 都是"当日"计数，靠 day_key 惰性清零
    """

    group_id: str
    target_uid: str
    target_name: str = ""
    owner_uid: str = ""
    owner_name: str = ""
    affection: int = 0
    marry_fail: int = 0
    ntr_fail: int = 0
    divorce_fail: int = 0
    ntr_count: int = 0
    chase_count: int = 0
    day_key: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Any, group_id: str = "") -> "HaremRow":
        return cls(
            group_id=group_id or _to_str(_get(row, "group_id")),
            target_uid=_to_str(_get(row, "target_uid")),
            target_name=_to_str(_get(row, "target_name")),
            owner_uid=_to_str(_get(row, "owner_uid")),
            owner_name=_to_str(_get(row, "owner_name")),
            affection=_to_int(_get(row, "affection")),
            marry_fail=_to_int(_get(row, "marry_fail")),
            ntr_fail=_to_int(_get(row, "ntr_fail")),
            divorce_fail=_to_int(_get(row, "divorce_fail")),
            ntr_count=_to_int(_get(row, "ntr_count")),
            chase_count=_to_int(_get(row, "chase_count")),
            day_key=_to_str(_get(row, "day_key")),
            created_at=_to_str(_get(row, "created_at")),
        )

    @property
    def owner_label(self) -> str:
        return self.owner_name or (self.owner_uid[-4:] if self.owner_uid else "")

    @property
    def is_public(self) -> bool:
        return not self.owner_uid


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


# ------------------------------------------------------------------ 背包
@dataclass(slots=True)
class Bag:
    """背包：物品用 {id: 数量}，鱼护用轻量 dict 列表（不建对象，省内存）。"""

    items: dict[str, int] = field(default_factory=dict)
    fish: list[dict[str, Any]] = field(default_factory=list)

    # ---------------------------------------------------------- 物品
    def count(self, item_id: str) -> int:
        return int(self.items.get(item_id, 0))

    def add(self, item_id: str, num: int = 1) -> None:
        if num <= 0:
            return
        total = self.items.get(item_id, 0) + num
        self.items[item_id] = min(C.ITEM_STACK_LIMIT, total)

    def remove(self, item_id: str, num: int = 1) -> bool:
        have = self.items.get(item_id, 0)
        if have < num:
            return False
        left = have - num
        if left <= 0:
            self.items.pop(item_id, None)
        else:
            self.items[item_id] = left
        return True

    # ---------------------------------------------------------- 鱼护
    def fish_count(self) -> int:
        return len(self.fish)

    def add_fish(self, record: dict[str, Any], limit: int = C.DEFAULT_FISH_BAG_LIMIT) -> list[dict[str, Any]]:
        """返回被挤出的鱼（调用方负责折算鲸币），避免背包无限膨胀。"""
        self.fish.append(record)
        overflow: list[dict[str, Any]] = []
        limit = max(1, limit)
        if len(self.fish) > limit:
            overflow = self.fish[: len(self.fish) - limit]
            self.fish = self.fish[len(self.fish) - limit :]
        return overflow

    def clear_fish(self) -> list[dict[str, Any]]:
        out = self.fish
        self.fish = []
        return out

    # ---------------------------------------------------------- 序列化
    def to_json(self) -> str:
        return json.dumps(
            {"items": self.items, "fish": self.fish},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def parse_int_dict(raw: Any) -> dict[str, int]:
    """把 DB 里的 JSON 字段解析成 {str: int}，坏数据一律丢弃而不是抛异常。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("JSON 字段解析失败，已忽略")
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out[str(k)] = iv
    return out


def parse_bag(items_raw: Any, fish_raw: Any, limit: int = C.DEFAULT_FISH_BAG_LIMIT) -> Bag:
    items = parse_int_dict(items_raw)
    fish: list[dict[str, Any]] = []
    if fish_raw:
        try:
            data = json.loads(fish_raw) if not isinstance(fish_raw, list) else fish_raw
        except (ValueError, TypeError):
            data = []
        if isinstance(data, list):
            for rec in data[-max(1, limit) :]:
                if isinstance(rec, dict) and rec.get("sp"):
                    fish.append(rec)
    return Bag(items=items, fish=fish)

# ------------------------------------------------------------------ 花园
@dataclass(slots=True)
class Garden:
    group_id: str
    uid: str
    seed_id: str = ""
    plant_id: str = ""
    planted_ts: int = 0
    growth: int = 0
    mutation: int = 0
    stage: int = 0
    watered_ts: int = 0
    watered_day: str = ""
    water_count: int = 0
    fert_day: str = ""
    fert_count: int = 0
    sun_day: str = ""
    sun_ts: int = 0
    last_n: int = 0
    last_p: int = 0
    last_k: int = 0
    dead: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any, group_id: str = "", uid: str = "") -> "Garden":
        if row is None:
            return cls(group_id=group_id, uid=uid)
        return cls(
            group_id=_to_str(row["group_id"]) or group_id,
            uid=_to_str(row["uid"]) or uid,
            seed_id=_to_str(row["seed_id"]),
            plant_id=_to_str(row["plant_id"]),
            planted_ts=_to_int(row["planted_ts"]),
            growth=_to_int(row["growth"]),
            mutation=_to_int(row["mutation"]),
            stage=_to_int(row["stage"]),
            watered_ts=_to_int(row["watered_ts"]),
            watered_day=_to_str(row["watered_day"]),
            water_count=_to_int(row["water_count"]),
            fert_day=_to_str(row["fert_day"]),
            fert_count=_to_int(row["fert_count"]),
            sun_day=_to_str(row["sun_day"]),
            sun_ts=_to_int(row["sun_ts"]),
            last_n=_to_int(row["last_n"]),
            last_p=_to_int(row["last_p"]),
            last_k=_to_int(row["last_k"]),
            dead=_to_int(row["dead"]),
            created_at=_to_str(row["created_at"]),
            updated_at=_to_str(row["updated_at"]),
        )

    def is_empty(self) -> bool:
        return not self.seed_id and not self.plant_id
