#!/usr/bin/env python3
r"""出图预览：不需要 AstrBot / QQ，直接把几张典型 Card 渲染成 PNG，用来肉眼验收美术与排版。

用法：
    python scripts/render_demo.py            # 输出到 docs/preview/
    python scripts/render_demo.py --out D:\\tmp\\pv

产物：
    checkin.png / fish.png / pond.png / marry.png / store.png / profile.png / help.png / error.png

注意：渲染器关闭时会清空自己的缓存目录，所以这里先把图复制到 preview 目录再 close()。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_package() -> str:
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    candidates: list[str] = []
    try:
        for line in (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                candidates.append(line.split(":", 1)[1].strip().strip("'\""))
                break
    except OSError:
        pass
    candidates.extend([ROOT.name, "astrbot_plugin_whale_harem"])
    for name in candidates:
        if not name or not name.isidentifier():
            continue
        try:
            importlib.import_module(f"{name}.ui.renderer")
            return name
        except ImportError:
            continue
    raise SystemExit("无法导入插件包，请在 <插件目录>/scripts/ 下运行")


PKG = _bootstrap_package()
_config = importlib.import_module(f"{PKG}.core.config")
_schema = importlib.import_module(f"{PKG}.ui.schema")
_renderer = importlib.import_module(f"{PKG}.ui.renderer")

PluginConfig = _config.PluginConfig
Renderer = _renderer.Renderer
card = _schema.card
stat = _schema.stat
kv = _schema.kv
kv_block = _schema.kv_block
list_block = _schema.list_block
grid_block = _schema.grid_block
progress_block = _schema.progress_block
quote_block = _schema.quote_block
text_block = _schema.text_block
error_card = _schema.error_card


def build_cards() -> list[tuple[str, object]]:
    checkin = card(
        "签到成功 · 连续 3 天",
        "10月25日 · 累计签到 12 次",
        art="checkin",
        accent="#4D6BFE",
        badge="+80",
        tone="success",
        footer="明天 4 点刷新 · 断签了记得用补签卡",
    )
    checkin.stats = [
        stat("鲸币余额", 1260, "#4D6BFE"),
        stat("连续签到", "3 天", "#F5B301"),
        stat("今日状态", "已签到", "#4D6BFE"),
    ]
    checkin.blocks.append(
        list_block(
            "奖励明细",
            [
                "鲸币 +80（基础 50 + 加成 30）",
                "连续 3 天，加成 +30（7 天封顶）",
                "经验 +10",
                "普通饵料 x2",
                "一碗白米饭 x1（35% 概率，现有 4）",
            ],
        )
    )
    checkin.blocks.append(progress_block("连续签到加成（7 天封顶）", 3, 7, text="3/7 天 · 再签 4 天拿满 +70"))

    fish = card(
        "收竿！蓝鳍金枪鱼",
        "赛博鲸鱼馆 · 池塘 Lv.5",
        art="fish:5",
        accent="#EF4444",
        badge="★★★★★",
        tone="legend",
        footer="经验 +8 · 鱼护 12/80 · 升级啦 Lv.7",
    )
    fish.stats = [stat("鲸币", "2.41w"), stat("鱼护", "12/80"), stat("池塘", "Lv.5")]
    fish.blocks.append(
        kv_block(
            "这一竿",
            [
                kv("饵料", "中级商品饵（Tier 4）"),
                kv("上钩率", "82%"),
                kv("品质加成", "+0.48"),
                kv("鱼获", "蓝鳍金枪鱼（★★★★★ 传说）"),
                kv("重量", "31200 g"),
                kv("估价", "712 鲸币"),
            ],
        )
    )
    fish.blocks.append(text_block("深海结晶 x2", title="🪨 上钩必掉材料"))
    fish.blocks.append(quote_block("深海里的蓝色闪电，一条顶你半年饵料钱。", author="蓝鳍金枪鱼·★★★★★"))

    pond = card("集团池塘", "赛博鲸鱼馆 · 全群共享的钓鱼宝地", art="pond", badge="Lv.4", footer="材料只能靠钓鱼获得，商店买不到")
    pond.stats = [
        stat("池塘等级", "Lv.4/10", "#4D6BFE"),
        stat("上钩加成", "+8%", "#22C55E"),
        stat("品质加成", "+32%", "#8B5CF6"),
        stat("我的贡献", "36 点", "#F5B301"),
    ]
    pond.blocks.append(
        grid_block("当前材料", [kv("池塘基石", "x12"), kv("珊瑚簇", "x7"), kv("鲸骨", "x3"), kv("深海结晶", "x0")])
    )
    pond.blocks.append(progress_block("升到 Lv.5", 0.44, 1.0, text="珊瑚簇 7/16（差 9）"))
    pond.blocks.append(list_block("群贡献榜 Top5", ["1. 群友…1024 — 42 点", "2. 群友…7788 — 21 点", "3. 群友…0001 — 9 点"]))

    marry = card("成婚！", "煤球 把 测试老婆 娶回了后宫", art="marry", tone="success", badge="好感 23")
    marry.stats = [stat("本次成功率", "30%"), stat("尝试", "第 3/5 次"), stat("消耗", "150 鲸币")]
    marry.blocks.append(
        kv_block(
            "求婚细节",
            [
                kv("本次成功率", "30%"),
                kv("好感度加成", "+11.5%"),
                kv("尝试次数", "第 3/5 次（5 次内必中）"),
                kv("消耗", "150 鲸币"),
                kv("好感度", "23（+5）"),
                kv("结果", "成婚！"),
            ],
        )
    )
    marry.blocks.append(text_block("从今天起，测试老婆 就是你后宫的人了……", footer="每日凌晨 4 点惰性重置保底进度"))

    store = card("赛博鲸鱼馆 · 商店", "钓鱼攒鲸币，鲸币换装备", art="shop", badge="14 件在售", footer="下单：/鲸鱼购买 红虫 3")
    store.stats = [stat("鲸币", "1260"), stat("今日鱼护", "12/80")]
    store.blocks.append(grid_block("🍚 饭", [kv("一碗白米饭", "小蓝 · 15 鲸币"), kv("一铁盆白米饭", "大蓝 · 40 鲸币")]))
    store.blocks.append(
        grid_block(
            "🪱 饵料",
            [
                kv("普通饵料", "大蓝 · 8 鲸币"),
                kv("蚯蚓", "小紫 · 14 鲸币"),
                kv("红虫", "小紫 · 18 鲸币"),
                kv("酒酿玉米", "小紫 · 22 鲸币"),
                kv("初级商品饵", "大紫 · 45 鲸币"),
                kv("中级商品饵", "小金 · 90 鲸币"),
            ],
        )
    )
    store.blocks.append(text_block("鲸币不够？去钓鱼 / 卖鱼 / 签到打工，一次最多买 99 个。"))

    profile = card("我的鲸鱼馆", "煤球 · 第 6 级", art="profile", badge="Lv.6", footer="每天凌晨 4:00 重置签到/喂食/保底进度")
    profile.stats = [
        stat("鲸币", "1260", "#F5B301"),
        stat("本群后宫", 3, "#8B5CF6"),
        stat("全部后宫", 7, "#8B5CF6"),
        stat("池塘", "Lv.4", "#4D6BFE"),
    ]
    profile.blocks.append(progress_block("经验 40 / 600", 40, 600, text="40/600"))
    profile.blocks.append(
        kv_block(
            "📊 状态",
            [
                kv("连续签到", "3 天"),
                kv("累计签到", "12 次"),
                kv("今日喂食", "5 次"),
                kv("鱼护", "12 / 80 条", note="估值 1260 鲸币"),
                kv("物品", "9 种"),
                kv("距离重置", "6 小时"),
            ],
        )
    )
    profile.blocks.append(list_block("✨ 生效中的加成", ["水库体验券（剩 18 小时）", "钟情护盾（剩 6 小时）"]))
    profile.blocks.append(list_block("🎒 背包精选", ["普通饵料·大蓝 ×12 🪱 饵料", "一碗白米饭·小蓝 ×4 🍚 饭", "珊瑚簇·大蓝 ×7 🪨 材料"]))

    help_card = card("赛博鲸鱼馆 · 指令簿", "在深蓝的水里，钓一条属于你的鱼", art="help", footer="所有指令都会返回一张图喵 | 每日凌晨 4:00 重置")
    help_card.stats = [stat("鲸币", "通用货币"), stat("池塘", "群共享"), stat("鱼护", "上限80")]
    help_card.blocks.append(
        list_block(
            "🐟 基础",
            [
                "签到 —— 基础 50 + 连续加成（7 天封顶），送随机饵料",
                "补签 —— 消耗补签卡，补回漏签的一天",
                "喂食 @TA —— 消耗白米饭，涨好感 + 随机鲸币",
                "打工 —— 随机时长换鲸币，冷却=本次时长",
            ],
        )
    )
    help_card.blocks.append(
        list_block(
            "🎣 钓鱼",
            [
                "钓鱼 [饵料] —— 必须用饵；50%鱼/25%道具/25%物品",
                "背包 —— 看鱼护与物品",
                "卖鱼 / 卖鱼 杂物 —— 鱼按星级卖鲸币",
                "池塘 —— 群共享池塘等级与升级进度",
                "捐献 [材料|all] —— 群友共同凑材料升级",
            ],
        )
    )
    help_card.blocks.append(list_block("💘 社交", ["娶群友 @TA [卡] —— 150 鲸币，10% 起，5 次必中", "我的后宫 / 恩爱榜 / 公共老婆"]))

    err = error_card("这条鱼太重了，拉不上来", "操作超时，请稍后再试")

    cast = card(
        "抛竿，等鱼上钩…",
        "赛博鲸鱼馆 · 池塘 Lv.3",
        art="fish",
        badge="27 分钟",
        footer="到点后群里有人聊天，鱼会随机咬钩（不用再发指令）",
    )
    cast.stats = [stat("等待", "27 分钟"), stat("可咬钩", "21:47"), stat("饵料", "红虫")]
    cast.blocks.append(
        kv_block(
            "抛竿信息",
            [
                kv("饵料", "红虫（已消耗 1 个）"),
                kv("等待时长", "27 分钟（5~60 分钟随机，与渔获无关）"),
                kv("可咬钩时间", "21:47"),
                kv("池塘", "Lv.3"),
            ],
        )
    )
    cast.blocks.append(text_block("鱼在水下慢慢咬钩，别急着提竿喵。"))

    work_start = card(
        "上工了，慢慢干…",
        "本次班 46 分钟 · 工资随等待时长涨",
        art="work",
        badge="46 分钟",
        footer="到点后随便发条群消息自动收工（等待期间不能钓鱼/打工）",
    )
    work_start.stats = [stat("等待", "46 分钟"), stat("预计工资", "51 鲸币"), stat("收工时间", "21:58")]
    work_start.blocks.append(
        kv_block(
            "上工信息",
            [
                kv("本次时长", "46 分钟（60~1440 分钟随机）"),
                kv("工资公式", "46 × 0.9 × 浮动 ±25%"),
                kv("摸鱼暴击", "没有（12% 概率）"),
                kv("预计到手", "51 鲸币"),
            ],
        )
    )

    work_claim = card(
        "收工，领工资！",
        "干了 46 分钟，到手 51 鲸币",
        art="work",
        badge="+51",
        tone="success",
        footer="休息一下，下一班再来",
    )
    work_claim.stats = [stat("到手鲸币", 51, "#F5B301"), stat("当前余额", "1311"), stat("本次时长", "46 分钟")]
    work_claim.blocks.append(kv_block("结算", [kv("本次时长", "46 分钟"), kv("到手工资", "51 鲸币"), kv("经验", "+5")]))

    storehouse = card(
        "我的仓库",
        "煤球 的家当 · 共 43 件",
        art="warehouse",
        badge="估值 918",
        footer="/卖鱼 杂物 可回收收藏品，材料和卡券建议留着用",
    )
    storehouse.stats = [stat("物品种类", "9 种"), stat("总件数", "43 件"), stat("回收估值", "918 鲸币"), stat("鱼护", "12 条")]
    storehouse.blocks.append(list_block("🪱 饵料", ["普通饵料·大蓝 ×12（估值 36）", "红虫·小紫 ×4（估值 20）", "酒酿玉米·小紫 ×2（估值 12）"]))
    storehouse.blocks.append(list_block("🍚 饭", ["一碗白米饭·小蓝 ×7（估值 28）", "一铁盆白米饭·大蓝 ×2（估值 28）"]))
    storehouse.blocks.append(list_block("🪨 材料", ["池塘基石·小蓝 ×18（估值 36）", "珊瑚簇·大蓝 ×6（估值 36）"]))
    storehouse.blocks.append(list_block("🧸 收藏", ["鲸鱼抱枕·大蓝 ×1（估值 60）", "铁盆·大蓝 ×2（估值 50）"]))

    return [
        ("checkin", checkin),
        ("cast", cast),
        ("fish", fish),
        ("work_start", work_start),
        ("work_claim", work_claim),
        ("warehouse", storehouse),
        ("pond", pond),
        ("marry", marry),
        ("store", store),
        ("profile", profile),
        ("help", help_card),
        ("error", err),
    ]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs" / "preview"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "_cache"

    cfg = PluginConfig()
    cfg.llm_enabled = False
    renderer = Renderer(cfg, cache, PKG)
    try:
        for name, c in build_cards():
            try:
                path = Path(await renderer.render(c))
                target = out / f"{name}.png"
                shutil.copyfile(path, target)
                print(f"[ok] {name:8s} -> {target}  ({target.stat().st_size // 1024} KB)")
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] {name}: {exc!r}")
                return 1
        print("\n渲染统计:", renderer.stats())
    finally:
        await renderer.close()
    print(f"\n预览图目录: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))