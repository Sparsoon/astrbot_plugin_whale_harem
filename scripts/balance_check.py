"""数值体检：天气 × 池塘 × 体验券 × 饵料 的上钩率/品质/等待，以及签到/喂食曲线。

运行：python scripts/balance_check.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

# 插件目录名即包名（上传 GitHub 后可能是任意名字），这里按真实目录名导入
PKG = ROOT.name
C = importlib.import_module(f"{PKG}.core.constants")
WX = importlib.import_module(f"{PKG}.core.weather")
D = importlib.import_module(f"{PKG}.data.drops")


def hook(level: int, bait: int, weather: WX.Weather, ticket: float = 0.0) -> float:
    rate = C.HOOK_BASE_RATE + D.pond_hook_bonus(level) + max(0.0, ticket) + (bait - 1) * 0.01 + weather.hook_delta
    return max(C.HOOK_RATE_FLOOR, min(C.HOOK_RATE_CEIL, rate))


def rarity_dist(level: int, bait: int, weather: WX.Weather, n: int = 200_000) -> dict[str, float]:
    boost = D.compose_boost(level, bait, 0.0)
    floor = D.min_rarity_rank(level)
    counts = {name: 0 for name in C.RARITY_ORDER}
    for _ in range(n):
        counts[D.roll_rarity(boost, floor, weather.rarity_shift)] += 1
    return {k: v * 100.0 / n for k, v in counts.items()}


def main() -> int:
    print("== 天气效果 ==")
    for w in WX.WEATHERS.values():
        print(f"{w.emoji} {w.name:3s} wait×{w.wait_mult:.2f} hook{w.hook_delta:+.0%} "
              f"rarity{w.rarity_shift:+d} star{w.star_shift:+d} coin×{w.coin_mult:.2f} mat+{w.material_extra}")

    print("\n== 上钩率（Lv1/5/10，饵料 T1/T3/T5，晴天/雷雨） ==")
    for level in (1, 5, 10):
        for bait in (1, 3, 5):
            sunny = hook(level, bait, WX.WEATHERS["sunny"])
            storm = hook(level, bait, WX.WEATHERS["storm"])
            ticket = hook(C.TICKET_POND_LEVEL, bait, WX.WEATHERS["sunny"], C.TICKET_HOOK_BONUS)
            print(f"Lv{level:2d} T{bait}  晴 {sunny:.0%} / 雷雨 {storm:.0%} / 体验券 {ticket:.0%}")

    print("\n== 品质分布（200k 次，天气=多云/晴天/寒潮） ==")
    for label, w in (("多云", WX.WEATHERS["cloudy"]), ("晴天", WX.WEATHERS["sunny"]), ("寒潮", WX.WEATHERS["cold"])):
        dist = rarity_dist(5, 3, w)
        print(f"{label}: " + " ".join(f"{k}{v:.2f}%" for k, v in dist.items()))

    print("\n== 鱼星分布（Lv5/T3，流星雨 vs 普通） ==")
    for label, w in (("普通", WX.WEATHERS["sunny"]), ("流星雨", WX.WEATHERS["meteor"])):
        boost = D.compose_boost(5, 3, 0.0)
        counts = {i: 0 for i in range(1, 6)}
        for _ in range(100_000):
            counts[D.roll_star(boost, w.star_shift)] += 1
        print(label + ": " + " ".join(f"{i}★{counts[i] / 1000:.2f}%" for i in counts))

    print("\n== 等待时长（5~60 基准 × 天气，钳 5~90） ==")
    for w in WX.WEATHERS.values():
        lo = max(5, min(90, round(5 * w.wait_mult)))
        hi = max(5, min(90, round(60 * w.wait_mult)))
        print(f"{w.name}: {lo}~{hi} 分钟")

    print("\n== 签到曲线（连签 1~22 天） ==")
    for streak in (1, 2, 5, 7, 10, 15, 20, 22):
        days = max(1, min(streak, C.CHECKIN_MAX_STREAK_DAYS))
        bonus = 0 if days <= 1 else round((C.CHECKIN_MAX_COINS - C.CHECKIN_BASE_COINS) * (days - 1) / (C.CHECKIN_MAX_STREAK_DAYS - 1))
        print(f"第{streak:2d}天: {min(C.CHECKIN_MAX_COINS, C.CHECKIN_BASE_COINS + bonus)} 鲸币")

    print("\n== 喂食里程碑 ==")
    for step in C.FEED_TOTAL_MILESTONES:
        print(f"{step:3d} 次: {C.FEED_MILESTONE_REWARDS.get(step)}")
    print(f"每日上限 {C.FEED_DAILY_LIMIT} 次，时段 {C.FEED_SLOT_HOURS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
