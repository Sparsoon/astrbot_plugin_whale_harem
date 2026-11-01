"""UI 数据协议 —— service 层与 Pillow 渲染层之间唯一的接口。

为什么要有这一层：
    * service 只负责算数值 + 拼卡片，完全不碰 PIL，永远不会有"业务代码里 new 图片"导致的 OOM；
    * 渲染层拿到的是纯数据，可以安全地丢进线程池、可以做缓存键、可以单测；
    * 任何指令的返回都是 `Card`，指令层统一 `await ui.render(card)` 返回图片。

风格：DeepSeek 蓝 + 圆角面板 + 一只大肥鱼。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

# 预制插画：(胖鱼, 鱼, 池塘, 婚姻, 商店, 打工, 签到, 背包, 错误, 无)
ART_FATFISH = "fatfish"
ART_FISH = "fish"
ART_POND = "pond"
ART_MARRY = "marry"
ART_SHOP = "shop"
ART_WORK = "work"
ART_CHECKIN = "checkin"
ART_BAG = "bag"
ART_WAREHOUSE = "warehouse"
ART_PROFILE = "profile"
ART_HELP = "help"
ART_STATS = "stats"
ART_ERROR = "error"
ART_NONE = "none"

KNOWN_ARTS = {
    ART_FATFISH,
    ART_FISH,
    ART_POND,
    ART_MARRY,
    ART_SHOP,
    ART_WORK,
    ART_CHECKIN,
    ART_BAG,
    ART_WAREHOUSE,
    ART_PROFILE,
    ART_HELP,
    ART_STATS,
    ART_ERROR,
    ART_NONE,
}


@dataclass(slots=True)
class Stat:
    """顶部大数字：比如"鲸币 1280"、"池塘 Lv.4"。"""

    label: str
    value: str
    accent: str = ""


@dataclass(slots=True)
class Row:
    """两列键值行。value 可以很长（会自动折行）。"""

    label: str
    value: str
    note: str = ""
    accent: str = ""


@dataclass(slots=True)
class Block:
    """卡片内容块。kind 决定渲染样式，未知 kind 一律退化为纯文本块。"""

    kind: str = "text"
    title: str = ""
    lines: list[str] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    footer: str = ""
    progress: float = 0.0
    progress_text: str = ""


@dataclass(slots=True)
class Card:
    """一张完整图片的纯数据描述。"""

    title: str
    subtitle: str = ""
    accent: str = ""          # 主色，空则用 DeepSeek 蓝
    art: str = ART_FATFISH   # 插画类型，支持 "fish:4" 这种带参数的写法
    badge: str = ""           # 右上角小标签，如 "Lv.3"
    stats: list[Stat] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    footer: str = ""
    owner: str = ""           # 群友名称：渲染时显示在标题下方，避免多群混淆
    tone: str = "normal"      # normal / success / warn / error / legend

    # -------------------------------------------------------------- 缓存键
    def cache_key(self) -> str:
        payload = {
            "t": self.title,
            "s": self.subtitle,
            "a": self.accent,
            "art": self.art,
            "b": self.badge,
            "o": self.owner,
            "st": [(s.label, s.value, s.accent) for s in self.stats],
            "bl": [
                (
                    b.kind,
                    b.title,
                    b.lines,
                    [(r.label, r.value, r.note, r.accent) for r in b.rows],
                    b.footer,
                    round(b.progress, 3),
                    b.progress_text,
                )
                for b in self.blocks
            ],
            "f": self.footer,
            "tone": self.tone,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # -------------------------------------------------------------- 兜底
    def compact(self) -> "Card":
        """渲染超时/失败时的紧凑版：只保留标题、前 2 个统计和前 2 块。"""
        small = replace(
            self,
            subtitle=(self.subtitle or "")[:32],
            stats=self.stats[:2],
            blocks=self.blocks[:2],
            footer=(self.footer or "")[:40],
        )
        return small.validate()

    def to_text(self, limit: int = 900) -> str:
        """图片渲染失败时的纯文本兜底，保证用户永远有回复。"""
        out: list[str] = [f"【{self.title}】"]
        if self.subtitle:
            out.append(self.subtitle)
        if self.stats:
            out.append(" | ".join(f"{s.label}:{s.value}" for s in self.stats))
        for block in self.blocks:
            if block.title:
                out.append(f"· {block.title}")
            for line in block.lines:
                out.append(f"  {line}")
            for row in block.rows:
                note = f"（{row.note}）" if row.note else ""
                out.append(f"  {row.label}：{row.value}{note}")
            if block.progress_text:
                out.append(f"  {block.progress_text}")
            if block.footer:
                out.append(f"  {block.footer}")
        if self.footer:
            out.append(self.footer)
        text = "\n".join(out)
        if limit and len(text) > limit:
            text = text[:limit].rsplit("\n", 1)[0] + "\n…（内容太长已截断，发 /帮助 查看指令）"
        return text

    def validate(self) -> "Card":
        """防止 service 层塞进脏数据把渲染器搞崩：就地裁剪超长内容。"""
        self.title = (self.title or "赛博鲸鱼馆")[:24]
        self.subtitle = (self.subtitle or "")[:48]
        self.badge = (self.badge or "")[:10]
        self.footer = (self.footer or "")[:80]
        if self.art.split(":", 1)[0] not in KNOWN_ARTS:
            self.art = ART_FATFISH
        self.stats = self.stats[:4]
        for s in self.stats:
            s.label = (s.label or "")[:8]
            s.value = (str(s.value) or "")[:12]
        self.blocks = self.blocks[:8]
        for block in self.blocks:
            block.title = (block.title or "")[:20]
            block.lines = [str(x)[:64] for x in block.lines[:8]]
            block.rows = block.rows[:12]
            for row in block.rows:
                row.label = (row.label or "")[:12]
                row.value = str(row.value)[:40]
                row.note = (row.note or "")[:24]
            block.footer = (block.footer or "")[:64]
        return self


# ---------------------------------------------------------------- 构造器
def card(
    title: str,
    subtitle: str = "",
    *,
    art: str = ART_FATFISH,
    accent: str = "",
    badge: str = "",
    tone: str = "normal",
    footer: str = "",
    stats: list[Stat] | None = None,
) -> Card:
    return Card(
        title=title,
        subtitle=subtitle,
        accent=accent,
        art=art,
        badge=badge,
        tone=tone,
        footer=footer,
        stats=list(stats or []),
    )


def stat(label: str, value: Any, accent: str = "") -> Stat:
    return Stat(label=str(label), value=str(value), accent=accent)


def kv(label: str, value: Any, *, note: str = "", accent: str = "") -> Row:
    return Row(label=str(label), value=str(value), note=note, accent=accent)


def text_block(*lines: str, title: str = "", footer: str = "") -> Block:
    return Block(kind="text", title=title, lines=[str(x) for x in lines], footer=footer)


def kv_block(title: str, rows: list[Row], *, footer: str = "") -> Block:
    return Block(kind="kv", title=title, rows=rows, footer=footer)


def list_block(title: str, entries: list[str], *, footer: str = "") -> Block:
    return Block(kind="list", title=title, lines=entries, footer=footer)


def quote_block(text: str, *, author: str = "") -> Block:
    return Block(kind="quote", title="", lines=[text], footer=author)


def progress_block(title: str, value: float, maximum: float, *, text: str = "") -> Block:
    ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, float(value) / float(maximum)))
    return Block(kind="progress", title=title, progress=ratio, progress_text=text)


def grid_block(title: str, rows: list[Row], *, footer: str = "") -> Block:
    return Block(kind="grid", title=title, rows=rows, footer=footer)


def divider_block() -> Block:
    return Block(kind="divider")


def error_card(
    message: str,
    hint: str = "",
    commands: Sequence[str] = (),
    *,
    title: str = "这条指令没走通",
) -> Card:
    """统一错误卡：把"发生了什么 / 怎么办 / 示例"分三层讲清楚。

    * 副标题只放原因，不重复 hint；
    * hint 与示例放在正文块，用户一眼知道下一步做什么；
    * footer 固定指向 /帮助。
    """
    c = card(
        title,
        message or "没有拿到结果",
        art=ART_ERROR,
        tone="error",
        badge="出错了",
        footer="输入 /帮助 查看全部指令和用法",
    )
    tips: list[str] = []
    if hint:
        tips.append(hint)
    if commands:
        tips.append("可以直接试：" + "  ".join(dict.fromkeys(commands)))
    if tips:
        c.blocks.append(list_block("下一步可以这样", tips))
    return c.validate()
