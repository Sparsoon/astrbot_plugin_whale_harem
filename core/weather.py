"""随机天气系统（纯函数 + 惰性时间桶，零定时器）。

设计：
    * 天气按 (作用域, 半天/12 小时时间桶) 用稳定哈希决定，不落库、不轮询；
    * 每个 12 小时桶内天气固定，跨桶自动变化（"随机天气"，每天早晚各换一次）；
    * 天气控制机可以覆盖 3 天：ponds.weather / ponds.weather_until；
    * 每种天气都有 1 个增益 + 1 个负面（见 WEATHERS），并同时影响：
        - 等待时长  wait_mult
        - 上钩率    hook_delta
        - 物品品质  rarity_shift（-1 ~ +1 档）
        - 鱼星      star_shift（-1 ~ +1 星）
        - 鲸币袋    coin_mult
        - 材料      material_extra（额外必掉数量）

数值平衡（与池塘/体验券/鱼饵叠加后仍在上钩率 [0.45, 0.96] 内）：
    晴天：等待 -15%，上钩 +5%，品质 0      —— 稳定
    多云：等待 0，   上钩 0，  品质 +1 档   —— 平稳偏产出
    小雨：等待 +20%，上钩 +8%，品质 -1 档   —— 鱼多但杂
    雷雨：等待 +35%，上钩 -6%，鱼星 +1      —— 高风险高星
    大雾：等待 +10%，上钩 -3%，品质 +1      —— 慢但精
    流星雨：等待 -10%，上钩 -10%，品质 +1、鱼星 +1、鲸币 ×1.3 —— 欧皇天气
    寒潮：等待 -25%，上钩 +10%，品质 -2、材料 +1 —— 速刷材料
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

WEATHER_BUCKET_SECONDS = 12 * 3600


@dataclass(frozen=True, slots=True)
class Weather:
    key: str
    name: str
    emoji: str
    desc: str
    wait_mult: float = 1.0      # 等待时长倍率
    hook_delta: float = 0.0     # 上钩率增减
    rarity_shift: int = 0       # 物品品质档位偏移
    star_shift: int = 0         # 鱼星偏移
    coin_mult: float = 1.0      # 鲸币袋倍率
    material_extra: int = 0     # 额外材料
    positive: str = ""
    negative: str = ""


WEATHERS: dict[str, Weather] = {
    "sunny": Weather(
        "sunny", "晴天", "☀️", "阳光把水面照得发亮，鱼也懒洋洋的。",
        wait_mult=0.85, hook_delta=0.05, rarity_shift=0, star_shift=0,
        positive="等待 -15%", negative="无额外加成",
    ),
    "cloudy": Weather(
        "cloudy", "多云", "⛅", "云层压着太阳，水色深沉，鱼口一般。",
        wait_mult=1.0, hook_delta=0.0, rarity_shift=1, star_shift=0,
        positive="物品品质 +1 档", negative="无其他加成",
    ),
    "rain": Weather(
        "rain", "小雨", "🌧️", "细雨点在水面，鱼群活跃，但好东西不多。",
        wait_mult=1.2, hook_delta=0.08, rarity_shift=-1, star_shift=0,
        positive="上钩 +8%", negative="物品品质 -1 档、等待 +20%",
    ),
    "storm": Weather(
        "storm", "雷雨", "⛈️", "雷声滚过，水面翻涌，容易惊到大家伙。",
        wait_mult=1.35, hook_delta=-0.06, rarity_shift=0, star_shift=1,
        positive="鱼星 +1", negative="上钩 -6%、等待 +35%",
    ),
    "fog": Weather(
        "fog", "大雾", "🌫️", "雾里看不清浮漂，但水下的东西更精。",
        wait_mult=1.1, hook_delta=-0.03, rarity_shift=1, star_shift=0,
        positive="物品品质 +1 档", negative="上钩 -3%、等待 +10%",
    ),
    "meteor": Weather(
        "meteor", "流星雨", "🌠", "星屑落进池塘，今晚什么都可能钓上来。",
        wait_mult=0.9, hook_delta=-0.10, rarity_shift=1, star_shift=1,
        coin_mult=1.3,
        positive="品质 +1、鱼星 +1、鲸币 ×1.3", negative="上钩 -10%",
    ),
    "cold": Weather(
        "cold", "寒潮", "❄️", "水面结了一层薄冰，鱼都挤在浅水区，但品质一般。",
        wait_mult=0.75, hook_delta=0.10, rarity_shift=-2, star_shift=0,
        material_extra=1,
        positive="上钩 +10%、材料 +1", negative="物品品质 -2 档、等待 -25%",
    ),
}

DEFAULT_WEATHER = "cloudy"


def _bucket(ts: int) -> int:
    return int(ts) // WEATHER_BUCKET_SECONDS


def next_change_ts(ts: int) -> int:
    """下一次天气变化的 unix 时间戳（当前桶结束）。"""
    return (_bucket(ts) + 1) * WEATHER_BUCKET_SECONDS


def weather_at(scope: str, ts: int) -> Weather:
    """按 (作用域, 时间桶) 稳定取天气；同一桶内所有人看到同一天气。"""
    digest = hashlib.blake2s(f"{scope}:{_bucket(ts)}".encode("utf-8"), digest_size=4).digest()
    idx = int.from_bytes(digest, "big") % len(WEATHERS)
    return tuple(WEATHERS.values())[idx]


def resolve(scope: str, ts: int, override: str = "", until: int = 0) -> Weather:
    """优先使用天气控制机的覆盖（未过期时），否则走自然天气。"""
    if override and int(until) > int(ts):
        weather = WEATHERS.get(str(override))
        if weather is not None:
            return weather
    return weather_at(scope, ts)


def describe(weather: Weather) -> str:
    return f"{weather.emoji} {weather.name}"


def effect_lines(weather: Weather) -> tuple[str, str]:
    return weather.positive, weather.negative
