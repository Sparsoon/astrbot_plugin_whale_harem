#!/usr/bin/env python3
"""经济数值蒙特卡洛验证：用真实采样器跑 N 次钓鱼，核对 docs/BALANCE.md 的期望收益表。

用法：
    python scripts/sim_economy.py --n 200000
    python scripts/sim_economy.py --n 50000 --level 1 --tier 1

不连数据库、不渲染，纯内存抽样（Alias O(1)），几秒钟出结果。
"""

from __future__ import annotations

import argparse
import importlib
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap() -> str:
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    names: list[str] = []
    for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("name:"):
            names.append(line.split(":", 1)[1].strip().strip("'\""))
            break
    names.extend([ROOT.name, "astrbot_plugin_whale_harem"])
    last: Exception | None = None
    for name in names:
        if not name or not name.isidentifier():
            continue
        try:
            importlib.import_module(f"{name}.data.drops")
            return name
        except ImportError as exc:
            last = exc
    raise SystemExit(f"无法导入插件包：{last!r}")


PKG = _bootstrap()
D = importlib.import_module(f"{PKG}.data.drops")
F = importlib.import_module(f"{PKG}.data.fish")
I = importlib.import_module(f"{PKG}.data.items")
C = importlib.import_module(f"{PKG}.core.constants")


def simulate(n: int, level: int, tier: int, ticket_boost: float, ticket_hook: float, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    hook = min(C.HOOK_RATE_CEIL, C.HOOK_BASE_RATE + D.pond_hook_bonus(level) + ticket_hook)
    boost = D.compose_boost(level, tier, ticket_boost)
    star_table = D.star_sampler(boost)
    quality_table = D.quality_sampler(boost)

    fish_value = 0.0
    prop_value = 0.0
    item_value = 0.0
    mat_value = 0.0
    hooked = 0
    stars = {i: 0 for i in range(1, 6)}
    rarities = {name: 0 for name in C.RARITY_ORDER}
    cat_count = {"fish": 0, "prop": 0, "item": 0}

    for _ in range(n):
        if rnd.random() >= hook:
            continue
        hooked += 1
        category = D.CATEGORY_SAMPLER.pick(rnd)
        cat_count[category] = cat_count.get(category, 0) + 1
        if category == "fish":
            star = int(star_table.pick(rnd))
            stars[star] = stars.get(star, 0) + 1
            fish_def = F.random_fish(star, rnd)
            grams = rnd.randint(fish_def.min_g, fish_def.max_g)
            fish_value += F.sell_value(fish_def, grams)
        elif category == "prop":
            prop_id = D.PROP_SAMPLER.pick(rnd)
            prop_value += I.sell_price(prop_id)
        else:
            rarity = quality_table.pick(rnd)
            rarities[rarity] = rarities.get(rarity, 0) + 1
            picked = rnd.choice(D.ITEM_POOL_BY_RARITY[rarity])
            if D.is_virtual_coin(picked):
                item_value += D.roll_coin_bag(picked, rnd)
            else:
                item_value += I.sell_price(picked)
        # 上钩必掉材料 1~2 个
        mat_id = D.material_sampler(level).pick(rnd)
        mat_value += I.sell_price(mat_id) * rnd.randint(C.MATERIAL_DROP_MIN, C.MATERIAL_DROP_MAX)

    per_cast = (fish_value + prop_value + item_value + mat_value) / n
    return {
        "n": n,
        "hook_rate": hook,
        "boost": boost,
        "hooked": hooked,
        "per_cast": per_cast,
        "fish_per_cast": fish_value / n,
        "prop_per_cast": prop_value / n,
        "item_per_cast": item_value / n,
        "mat_per_cast": mat_value / n,
        "stars": {k: v / max(1, hooked) for k, v in stars.items()},
        "cat": {k: v / max(1, hooked) for k, v in cat_count.items()},
        "rarities": {k: v / max(1, sum(rarities.values()) or 1) for k, v in rarities.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200000)
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--ticket", choices=["none", "reservoir", "private"], default="none")
    args = parser.parse_args()

    ticket_boost = {"none": 0.0, "reservoir": D.TICKET_QUALITY_BOOST_BY_KIND["ticket_reservoir"],
                    "private": D.TICKET_QUALITY_BOOST_BY_KIND["ticket_private"]}[args.ticket]
    ticket_hook = {"none": 0.0, "reservoir": C.TICKET_HOOK_BONUS,
                   "private": C.PRIVATE_POOL_HOOK_BONUS}[args.ticket]

    r = simulate(args.n, args.level, args.tier, ticket_boost, ticket_hook)
    print(f"模拟 {r['n']} 竿 · 池塘 Lv.{args.level} · 饵料 tier {args.tier} · 券 {args.ticket}")
    print(f"  上钩率 {r['hook_rate']:.0%} · boost {r['boost']:.2f} · 实际上钩 {r['hooked']} 次")
    print(f"  每竿期望（不含材料）: {r['per_cast'] - r['mat_per_cast']:.2f} 鲸币")
    print(f"    鱼 {r['fish_per_cast']:.2f} / 道具 {r['prop_per_cast']:.2f} / 物品 {r['item_per_cast']:.2f}")
    print(f"  每竿期望（含材料）  : {r['per_cast']:.2f} 鲸币")
    print(f"  大类（占上钩）: " + " / ".join(f"{k} {v:.1%}" for k, v in r["cat"].items()))
    print("  星级分布: " + " / ".join(f"{k}★ {v:.1%}" for k, v in r["stars"].items()))
    print("  品质分布(物品): " + " / ".join(f"{k} {v:.1%}" for k, v in r["rarities"].items()))
    print("\n对照 docs/BALANCE.md：Lv1 每竿 50.0（不含材料）；含材料约 +2.2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())