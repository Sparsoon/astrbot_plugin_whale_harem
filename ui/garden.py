"""花园卡片（纯数据，Pillow 渲染交给 ``ui.renderer``）。

约定（GARDEN_SPEC §3.4 / §6）：
    * 所有花园卡片 ``accent = core.constants.GARDEN_ACCENT``（绿色）；
    * 浇水卡刻意保持最窄：只有标题 + 一行结果，**不加 blocks、不加 stats**，
      避免群里每次浇水都刷一张大图；
    * 这里只做"文案 + 排版"，成长值 / 突变值 / 天数等业务数字全部由
      ``services/garden.py`` 传进来，UI 层不硬编码数值；
    * 植物品质未公开时统一显示 ``???（发芽后揭晓）``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..core import constants as C
from .schema import (
    ART_FATFISH,
    Card,
    card,
    kv,
    kv_block,
    list_block,
    progress_block,
    stat,
    text_block,
)

# 花园卡片的统一 tone 使用约定：正常 normal / 有收获 success / 枯萎 warn
_TONE_WARN = "warn"
_TONE_SUCCESS = "success"
_TONE_NORMAL = "normal"
_TONE_LEGEND = "legend"


# ---------------------------------------------------------------- 视图数据
@dataclass(slots=True)
class SeedEntry:
    """背包里的一种种子（只用于列出，不做业务判断）。"""

    name: str
    count: int
    grade: str


@dataclass(slots=True)
class GardenView:
    """一张花园面板需要的全部展示字段（由 service 层填好）。"""

    owner: str = ""
    revealed: bool = False          # 品质是否已公开（发芽期）
    plant_name: str = ""
    grade: str = ""
    stage_name: str = ""
    stage_index: int = 0
    growth: int = 0
    growth_max: int = C.GARDEN_GROWTH_MAX
    mutation: int = 0
    mutation_cap: int = 0
    status_text: str = "健康"
    penalty_text: str = ""
    weather_text: str = ""
    sow_text: str = ""
    water_text: str = ""
    fert_text: str = ""
    sun_text: str = ""
    progress_text: str = ""
    dead: bool = False
    sun_start: int = C.GARDEN_SUN_START_HOUR
    sun_end: int = C.GARDEN_SUN_END_HOUR

    # 品质未公开时，任何地方都不许泄露植物名 / 品质
    @property
    def plant_label(self) -> str:
        if not self.revealed:
            return "???（发芽后揭晓）"
        name = self.plant_name or "未知植物"
        return f"{name}·{self.grade}" if self.grade else name

    def mutation_text(self) -> str:
        if self.revealed and self.mutation_cap > 0:
            shown = min(int(self.mutation), int(self.mutation_cap))
            return f"{shown}/{self.mutation_cap}"
        if self.revealed and self.mutation_cap == 0:
            return "已圆满"
        return str(self.mutation)


# ---------------------------------------------------------------- 工具
def _green_card(
    title: str,
    subtitle: str = "",
    *,
    badge: str = "",
    tone: str = _TONE_NORMAL,
    footer: str = "",
) -> Card:
    """所有花园卡片的唯一入口：强制绿色 accent + 统一插画。"""
    return card(
        title,
        subtitle,
        art=ART_FATFISH,
        accent=C.GARDEN_ACCENT,
        badge=badge,
        tone=tone,
        footer=footer,
    )


def _help_lines(view: GardenView) -> list[str]:
    """6 条固定指令帮助（太阳时段来自配置，不在 UI 里写死）。"""
    return [
        "/养植物：看花园状态（就是本面板）",
        "/播种 <种子名>：不带参数先看背包里的种子",
        "/浇水：每小时 1 次，稳定加成长与突变",
        "/施肥 氮|磷|钾：每天 2 次，成长与突变侧重不同",
        f"/晒太阳：每天 1 次，{view.sun_start}:00~{view.sun_end}:00 之间",
        "/收获：成长满后收果实 + 一颗种子",
    ]


def _status_rows(view: GardenView) -> list:
    return [
        kv("植物", view.plant_label),
        kv("状态", view.status_text or "健康"),
        kv("成长收益", view.penalty_text or "正常"),
        kv("天气", view.weather_text or "-"),
        kv("上次浇水", view.water_text or "从未"),
        kv("上次施肥", view.fert_text or "从未"),
        kv("上次晒太阳", view.sun_text or "从未"),
        kv("播种", view.sow_text or "-"),
    ]


# ---------------------------------------------------------------- 总览面板
def panel(view: GardenView, *, empty: bool = False) -> Card:
    """``/养植物``：帮助（6 条）+ 总体状态，空地和枯萎都用同一张卡。"""
    if empty:
        title = "花园 · 空地"
        subtitle = "这里还没有种东西"
        tone = _TONE_NORMAL
        badge = "空地"
    elif view.dead:
        title = "花园 · 枯萎"
        subtitle = "这株植物已经枯死，只能重新播种"
        tone = _TONE_WARN
        badge = "枯萎"
    else:
        title = f"花园 · {view.stage_name}" if view.stage_name else "花园"
        subtitle = f"{view.owner} 的小花园" if view.owner else "赛博鲸鱼馆的小花园"
        tone = _TONE_SUCCESS if view.stage_index >= 4 else _TONE_NORMAL
        badge = view.grade if view.revealed and view.grade else "???"

    c = _green_card(title, subtitle, badge=badge, tone=tone)
    c.owner = view.owner
    c.stats = [
        stat("阶段", "枯萎" if view.dead else (view.stage_name or "空地")),
        stat("成长", f"{view.growth}/{view.growth_max}"),
        stat("突变", view.mutation_text()),
    ]
    c.blocks.append(
        progress_block(
            "成长进度",
            view.growth,
            view.growth_max,
            text=view.progress_text or f"{view.growth}/{view.growth_max}",
        )
    )
    c.blocks.append(kv_block("植物状态", _status_rows(view)))
    c.blocks.append(list_block("可以做什么（6 条指令）", _help_lines(view)))
    if view.dead:
        c.footer = "枯萎后 /收获 只能拿到 0，重新 /播种 再来一株"
    else:
        c.footer = f"品质发芽后揭晓 · 成长到 {view.growth_max} 就能 /收获"
    return c.validate()


# ---------------------------------------------------------------- 播种
def seed_list_card(owner: str, entries: Sequence[SeedEntry]) -> Card:
    """``/播种`` 不带参数：列出背包里的种子（名称、数量、档位）。"""
    if not entries:
        c = _green_card(
            "播种 · 还没有种子",
            "背包里一颗种子都没有",
            badge="0",
            tone=_TONE_WARN,
            footer="买到种子后：/播种 <种子名>",
        )
        c.blocks.append(
            list_block(
                "怎么拿到种子",
                [
                    "在 /商店 买 D 级 / E 级种子（价格最低）",
                    "钓鱼、签到偶尔也能捡到",
                    "收获成熟植物会返还一颗种子",
                ],
            )
        )
        return c.validate()

    total = sum(int(e.count) for e in entries)
    c = _green_card(
        "播种 · 背包种子",
        f"{owner or '你'} 有 {len(entries)} 种种子",
        badge=f"{total}",
        tone=_TONE_SUCCESS,
        footer="用法：/播种 <种子名>，例如 /播种 E 级种子",
    )
    c.stats = [stat("种子种类", len(entries)), stat("种子总数", total)]
    c.blocks.append(
        list_block(
            "可以播种",
            [f"{e.name} x{e.count}（{e.grade} 级）" for e in entries],
            footer="种子种下后，成长到发芽期才会揭晓植物品质",
        )
    )
    return c.validate()


def sow_card(seed_name: str, grade: str, *, sun_start: int, sun_end: int) -> Card:
    """``/播种 <种子>`` 成功：不提前泄露植物品质。"""
    c = _green_card(
        f"播种 · {seed_name}",
        "已经埋进土里，等它发芽",
        badge="播种期",
        tone=_TONE_SUCCESS,
        footer=f"成长到 {C.GARDEN_STAGE_CAP}（发芽期）揭晓植物品质",
    )
    c.stats = [
        stat("阶段", "播种期"),
        stat("成长", f"0/{C.GARDEN_GROWTH_MAX}"),
        stat("种子档位", grade),
    ]
    c.blocks.append(
        list_block(
            "接下来",
            [
                "/浇水：每小时 1 次，成长 + 突变",
                "/施肥 氮|磷|钾：每天 2 次，侧重点不同",
                f"/晒太阳：每天 1 次，{sun_start}:00~{sun_end}:00",
                "/收获：成熟后收果实和种子",
            ],
        )
    )
    return c.validate()


# ---------------------------------------------------------------- 浇水（窄卡）
def water_card(
    *,
    growth: int,
    growth_max: int,
    growth_gain: int,
    mutation_gain: int,
    water_count: int,
    mutation_capped: bool = False,
) -> Card:
    """浇水面板必须窄：标题 + 一行结果，无 blocks、无 stats。"""
    if mutation_capped:
        subtitle = f"成长 +{growth_gain} · 突变已封顶 · 今日第 {water_count} 次"
    else:
        subtitle = f"成长 +{growth_gain} · 突变 +{mutation_gain} · 今日第 {water_count} 次"
    c = _green_card(
        "浇水",
        subtitle,
        badge=f"{growth}/{growth_max}",
        tone=_TONE_SUCCESS,
    )
    # 刻意不设置 stats / blocks：这张卡要尽量矮。
    return c.validate()


# ---------------------------------------------------------------- 施肥
def fertilize_card(
    *,
    fert_name: str,
    growth_gain: int,
    mutation_gain: int,
    used: int,
    limit: int,
    mutation_capped: bool = False,
) -> Card:
    mutation_text = "已封顶" if mutation_capped else f"+{mutation_gain}"
    c = _green_card(
        f"施肥 · {fert_name}",
        f"成长 +{growth_gain} · 突变 {mutation_text}",
        badge=f"{used}/{limit}",
        tone=_TONE_SUCCESS,
        footer="每天最多 2 次；氮长成长、磷长突变、钾更均衡",
    )
    c.stats = [
        stat("成长", f"+{growth_gain}"),
        stat("突变", mutation_text),
        stat("今日施肥", f"{used}/{limit}"),
    ]
    c.blocks.append(
        kv_block(
            "土壤记录",
            [
                kv("肥料", fert_name),
                kv("成长", f"+{growth_gain}"),
                kv("突变", mutation_text),
                kv("今日次数", f"{used}/{limit}"),
            ],
        )
    )
    return c.validate()


# ---------------------------------------------------------------- 晒太阳
def sun_card(
    *,
    weather_text: str,
    growth_gain: int,
    mutation_gain: int,
    mutation_capped: bool = False,
) -> Card:
    mutation_text = "已封顶" if mutation_capped else f"{mutation_gain:+d}"
    c = _green_card(
        f"晒太阳 · {weather_text}",
        f"成长 +{growth_gain} · 突变 {mutation_text}",
        badge="每日 1 次",
        tone=_TONE_SUCCESS,
        footer="天气越好收益越高，雷雨还会掉突变",
    )
    c.stats = [
        stat("天气", weather_text),
        stat("成长", f"+{growth_gain}"),
        stat("突变", mutation_text),
    ]
    c.blocks.append(
        text_block("晒太阳的收益由当天天气决定，同一个天气对所有人一样。")
    )
    return c.validate()


# ---------------------------------------------------------------- 收获
def harvest_card(
    *,
    plant_name: str,
    grade: str,
    fruit_name: str,
    fruit_count: int,
    seed_name: str,
    seed_grade: str,
    mutation: int,
) -> Card:
    tone = _TONE_LEGEND if seed_grade in ("S", "A") else _TONE_SUCCESS
    c = _green_card(
        f"收获 · {plant_name or '植物'}",
        f"{fruit_name} x{fruit_count} · {seed_name} x1",
        badge=seed_grade or "收获",
        tone=tone,
        footer="花园已清空，可以 /播种 下一株",
    )
    c.stats = [
        stat("果实", f"x{fruit_count}"),
        stat("种子档位", seed_grade or "-"),
        stat("突变", mutation),
    ]
    c.blocks.append(
        kv_block(
            "收获结算",
            [
                kv("植物", f"{plant_name}·{grade}" if grade else plant_name),
                kv("果实", f"{fruit_name} x{fruit_count}"),
                kv("种子", f"{seed_name}（{seed_grade} 级）"),
                kv("突变值", mutation),
            ],
        )
    )
    return c.validate()


def withered_card(*, plant_name: str = "", wither_days: int = 5) -> Card:
    """枯萎后 ``/收获``：收获 0，花园清空。"""
    c = _green_card(
        "收获 · 枯萎",
        f"{plant_name or '这株植物'} 已经枯死，收获 0",
        badge="0",
        tone=_TONE_WARN,
        footer="花园已清空，/播种 重新开始",
    )
    c.blocks.append(
        text_block(f"连续 {wither_days} 天没浇水就会枯萎，记得常回来看看。")
    )
    return c.validate()


__all__ = [
    "GardenView",
    "SeedEntry",
    "panel",
    "seed_list_card",
    "sow_card",
    "water_card",
    "fertilize_card",
    "sun_card",
    "harvest_card",
    "withered_card",
]
