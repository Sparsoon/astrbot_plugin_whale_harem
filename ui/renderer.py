"""Pillow 渲染器 —— Card（纯数据）-> 一张 720px 宽的 PNG。

管线（每一步都为 2 核 2G 考虑过）：
    1. `card.validate()` 先裁剪超长字段，渲染层永远不会被脏数据打爆；
    2. 先算总高、再建图 —— 绝不"先建 1600px 大图再裁"（那会白吃 3.5MB）；
    3. 命中缓存直接返回路径（0 次绘制、0 次分配）；
    4. 未命中：并发闸门 -> `asyncio.to_thread` 里画 -> 存 PNG -> 登记缓存；
    5. `finally` 里 del 掉 Image/Draw，再调有节流的 gc.collect()。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw  # noqa: F401  (ImageDraw 用于类型注解)

from ..core.async_utils import LimitedRunner, collect_now
from ..core.config import PluginConfig
from ..core.errors import RenderError
from ..core.log import log
from . import mascot
from . import theme as T
from .cache import ImageCache
from .schema import ART_FATFISH, Block, Card

# ---------------------------------------------------------------- 版式常量
HEADER_H = 132
STATS_H = 78
FOOTER_H = 38
GAP = 12
BLOCK_PAD = 16
ART_BOX = 108
STAT_MIN_H = 44

FONTS = {
    "title": 30,
    "subtitle": 16,
    "badge": 14,
    "stat_value": 24,
    "stat_label": 13,
    "block_title": 17,
    "body": 16,
    "small": 13,
}

_ART_ALIASES = {
    "fatfish": "fatfish",
    "fish": "fish",
    "pond": "pond",
    "marry": "marry",
    "shop": "shop",
    "work": "work",
    "checkin": "checkin",
    "bag": "bag",
    "warehouse": "warehouse",
    "profile": "profile",
    "help": "help",
    "stats": "stats",
    "error": "error",
    "none": "none",
}


class Renderer:
    """所有指令的出图入口。实例在插件生命周期内只创建一次。"""

    def __init__(
        self,
        cfg: PluginConfig,
        cache_dir: str | Path,
        plugin_name: str = "astrbot_plugin_whale_harem",
    ) -> None:
        self._cfg = cfg
        self._plugin_name = plugin_name
        self._cache_dir = Path(cache_dir)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 只读文件系统：退化为临时目录
            log.warning(f"缓存目录不可用，改用临时目录: {exc!r}")
            import tempfile

            self._cache_dir = Path(tempfile.mkdtemp(prefix="whale_img_"))
        self._font_hint = getattr(cfg, "render_font_path", "") or ""
        # 提前探测一次字体：让启动日志就能看到"有没有中文字体"
        T.resolve_font_path(self._font_hint)
        # 官方形象素材：启动时解码一次（之后渲染线程只做缩放+粘贴）
        mascot.preload()
        self._cache = ImageCache(
            max_entries=int(getattr(cfg, "image_cache_size", 24) or 0),
            ttl_sec=int(getattr(cfg, "image_cache_ttl_sec", 300) or 0),
            cache_dir=self._cache_dir,
        )
        # 启动时清一次上个进程留下的缓存文件（不是轮询，只执行一次）
        try:
            self._cache.prune()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"缓存清理失败（忽略）: {exc!r}")
        self._runner = LimitedRunner(max(1, int(getattr(cfg, "render_concurrency", 1) or 1)), name="render")
        self._closed = False
        self._rendered = 0
        self._failures = 0
        self._last_ms = 0

    # ================================================================ 对外
    async def render(self, card: Card) -> str:
        """返回 PNG 的绝对路径。失败抛 RenderError（main.py 会降级成文字）。"""
        if self._closed:
            raise RenderError("渲染器已关闭")
        try:
            card.validate()
        except Exception as exc:  # noqa: BLE001 - validate 不该炸，但绝不能让脏卡崩插件
            raise RenderError(f"卡片数据非法: {exc}") from exc

        try:
            key = card.cache_key()
        except Exception:  # noqa: BLE001
            key = ""
        if key:
            # 命中判定含 is_file()（一次同步 stat），丢线程池避免慢盘时抖事件循环
            cached = await asyncio.to_thread(self._cache.get, key)
            if cached is not None:
                return str(cached)

        try:
            async with self._runner.slot():
                path = await asyncio.to_thread(self._render_sync, card, key)
        except RenderError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            raise RenderError(f"渲染失败: {type(exc).__name__}") from exc
        return str(path)

    async def render_bytes(self, card: Card) -> bytes:
        """直接拿 PNG 字节（测试/兜底用，不写缓存）。"""
        card.validate()
        return await asyncio.to_thread(self._render_bytes_sync, card)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._cache.clear()
        except Exception:  # noqa: BLE001
            pass
        T.clear_font_cache()
        log.info(f"渲染器已关闭: 出图 {self._rendered} 张, 失败 {self._failures} 次")

    def stats(self) -> dict[str, Any]:
        data = self._cache.stats()
        data.update({"rendered": self._rendered, "failures": self._failures, "last_ms": self._last_ms})
        return data

    # ================================================================ 同步实现
    def _render_bytes_sync(self, card: Card) -> bytes:
        img = self._compose(card)
        try:
            buffer = io.BytesIO()
            img.save(buffer, "PNG", compress_level=6)
            return buffer.getvalue()
        finally:
            del img
            collect_now(force=False)

    def _render_sync(self, card: Card, key: str) -> Path:
        started = time.monotonic()
        img = None
        try:
            img = self._compose(card)
            name = f"{key or int(started * 1000)}.png"
            path = self._cache_dir / name
            # 原子落盘：先写唯一临时名再 os.replace，避免并发同 key 时把半张图发给 QQ
            tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}.tmp")
            try:
                img.save(tmp, "PNG", compress_level=6)
                os.replace(tmp, path)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            self._rendered += 1
            self._last_ms = int((time.monotonic() - started) * 1000)
            if key:
                self._cache.put(key, path)
            return path
        except OSError as exc:
            raise RenderError(f"图片写入失败: {exc}") from exc
        finally:
            del img
            collect_now(force=False)

    # ---------------------------------------------------------------- 组装
    def _compose(self, card: Card) -> Image.Image:
        fonts = {
            name: T.get_font(size, self._font_hint)
            for name, size in FONTS.items()
        }
        accent = card.accent or T.ACCENT
        tone_color = T.TONE_COLORS.get(card.tone, accent)

        # ---- 第一遍：量高（不建图）
        blocks = list(card.blocks)
        truncated = False
        while True:
            content_heights = [self._block_height(b, fonts, accent) for b in blocks]
            raw_total = self._total_height(card, content_heights)
            if raw_total <= T_MAX or len(blocks) <= 1:
                break
            blocks = blocks[:-1]
            truncated = True
        content_heights = [self._block_height(b, fonts, accent) for b in blocks]
        raw_total = self._total_height(card, content_heights)
        # 只有"单块就超过 1600"这种极端情况才真正裁剪；正常路径 raw_total 就是画布高度
        total = min(raw_total, T_MAX)
        truncated = truncated or raw_total > T_MAX

        # ---- 第二遍：建图并绘制
        img = T.vertical_gradient(T.CARD_WIDTH, total, T.BG_TOP, T.BG_BOTTOM)
        draw = ImageDraw.Draw(img)
        try:
            self._decorate_background(draw, total)
            y = T.PAD
            y = self._draw_header(draw, card, fonts, accent, tone_color, y, img)
            y += GAP
            y = self._draw_stats(draw, card, fonts, y)
            for block in blocks:
                y = self._draw_block(draw, block, fonts, accent, y)
                y += GAP  # 与 _total_height 的统计口径保持一致
            if truncated:
                self._draw_note(draw, "内容过长，已折叠部分条目", fonts, T.PAD, min(y + 2, total - FOOTER_H - 20))
            self._draw_footer(draw, card, fonts, total)
        finally:
            del draw
        return img

    def _total_height(self, card: Card, content_heights: list[int]) -> int:
        """返回"真实需要的高度"（不裁剪）。

        必须返回原始值：如果这里返回 min(total, 1600)，`_compose` 的裁剪循环
        条件 `total <= T_MAX` 永远成立，超长卡片就会被画到图外（页脚压内容）。
        """
        total = T.PAD * 2 + HEADER_H + GAP
        if card.stats:
            total += STATS_H + GAP
        for height in content_heights:
            total += max(STAT_MIN_H, height) + GAP
        total += FOOTER_H
        return total

    # ---------------------------------------------------------------- 背景
    @staticmethod
    def _decorate_background(draw: "ImageDraw.ImageDraw", height: int) -> None:
        """几个淡淡的同心圆当气泡：固定 6 次绘制，不随卡片高度线性增长。"""
        ring = T.mix(T.PANEL, T.BG_TOP, 0.35)
        spots = (
            (T.CARD_WIDTH - 40, 60, 120),
            (T.CARD_WIDTH - 10, 190, 80),
            (60, height - 80, 150),
            (T.CARD_WIDTH - 120, height - 50, 110),
            (200, 40, 60),
            (T.CARD_WIDTH - 200, height - 150, 70),
        )
        for cx, cy, radius in spots:
            box = (cx - radius, cy - radius, cx + radius, cy + radius)
            try:
                draw.ellipse(box, outline=ring, width=2)
            except (TypeError, ValueError):
                draw.ellipse(box, outline=ring)

    # ---------------------------------------------------------------- 头部
    def _draw_header(
        self,
        draw: "ImageDraw.ImageDraw",
        card: Card,
        fonts: dict,
        accent: str,
        tone_color: str,
        y: int,
        img: Any = None,
    ) -> int:
        x0, x1 = T.PAD, T.CARD_WIDTH - T.PAD
        box = (x0, y, x1, y + HEADER_H)
        T.rounded_rect(draw, box, T.RADIUS, fill=T.mix(T.PANEL, accent, 0.16), outline=T.STROKE, width=1)
        # 顶部主色高光条
        T.rounded_rect(draw, (x0, y, x1, y + 5), 3, fill=tone_color)
        # 左：吉祥物插画
        art_box = (x0 + 12, y + 12, x0 + 12 + ART_BOX, y + HEADER_H - 12)
        self._draw_art(draw, card, art_box, accent, img)

        text_x = art_box[2] + 18
        badge_text = (card.badge or "").strip()
        badge_w = 0
        if badge_text:
            badge_font = fonts["badge"]
            label = T.ellipsis(badge_text, badge_font, 110)
            badge_w = T.text_size(label, badge_font)[0] + 22
            bx1 = x1 - 14
            bx0 = bx1 - badge_w
            T.rounded_rect(draw, (bx0, y + 14, bx1, y + 14 + 26), 13, fill=T.mix(accent, T.BG_TOP, 0.25))
            T.draw_text(draw, ((bx0 + bx1) // 2, y + 20), label, badge_font, T.WHITE, anchor="ma")

        title_font = fonts["title"]
        max_title_w = x1 - text_x - 16 - (badge_w + 8 if badge_w else 0)
        title = T.ellipsis(card.title, title_font, max(60, max_title_w))
        T.draw_text(draw, (text_x, y + 34), title, title_font, T.TEXT, bold=True)

        subtitle_font = fonts["subtitle"]
        subtitle = T.ellipsis(card.subtitle or "", subtitle_font, x1 - text_x - 18)
        if subtitle:
            T.draw_text(draw, (text_x, y + 78), subtitle, subtitle_font, T.TEXT_DIM)
        if card.owner:
            owner_font = fonts["small"]
            T.draw_text(
                draw,
                (text_x, y + 106),
                T.ellipsis(f"群友 · {card.owner}", owner_font, x1 - text_x - 16),
                owner_font,
                T.mix(accent, T.TEXT, 0.35),
            )
        return y + HEADER_H

    def _draw_art(
        self,
        draw: "ImageDraw.ImageDraw",
        card: Card,
        box: tuple[int, int, int, int],
        accent: str,
        img: Any = None,
    ) -> None:
        """形象优先级：官方素材贴纸 > fatfish 手绘 > 保底图形。"""
        kind = card.art or ART_FATFISH
        param = ""
        if ":" in kind:
            kind, param = kind.split(":", 1)
        kind = _ART_ALIASES.get(kind.strip().lower(), kind.strip().lower())
        if kind == "none":
            return
        # 官方形象：资料/帮助/统计/池塘用 2D 立绘，其余面板用 Q 版
        if img is not None:
            sticker = "2d" if kind in ("profile", "help", "stats", "pond") else "q"
            if mascot.draw_sticker(img, draw, box, sticker, accent):
                return
        kind = _ART_ALIASES.get(kind, "fatfish")
        if kind == "none":
            return
        try:
            from . import fatfish

            if kind == "fish":
                try:
                    star = max(1, min(5, int(param or 3)))
                except ValueError:
                    star = 3
                fatfish.draw_fish(img, draw, box, star, accent)
                return
            if kind == "pond":
                level = 1
                matched = re.search(r"\d+", card.badge or "")
                if matched:
                    level = int(matched.group())
                fatfish.draw_pond_scene(img, draw, box, level)
                return
            if kind == "marry":
                fatfish.draw_hearts(img, draw, box)
                return
            if kind == "shop":
                fatfish.draw_shop(img, draw, box)
                return
            if kind == "work":
                fatfish.draw_sweat(img, draw, box)
                return
            if kind == "checkin":
                fatfish.draw_calendar(img, draw, box)
                return
            if kind == "bag":
                fatfish.draw_bag(img, draw, box)
                return
            if kind == "error":
                fatfish.draw_error_face(img, draw, box)
                return
            fatfish.draw_fatfish(img, draw, box, mood="happy", accent=accent)
            return
        except Exception as exc:  # noqa: BLE001 - 插画失败不能拖垮整张卡
            log.debug(f"插画绘制失败({kind})，使用保底图形: {exc!r}")
            self._fallback_fish(draw, box, accent)

    @staticmethod
    def _fallback_fish(draw: "ImageDraw.ImageDraw", box: tuple[int, int, int, int], accent: str) -> None:
        x0, y0, x1, y1 = box
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        w = min(x1 - x0, y1 - y0) // 2
        body = (cx - w, cy - int(w * 0.72), cx + int(w * 0.62), cy + int(w * 0.72))
        draw.ellipse(body, fill=T.mix(accent, T.BG_TOP, 0.15), outline=T.ACCENT_SOFT)
        tail = ((cx + int(w * 0.5), cy), (cx + int(w * 1.02), cy - int(w * 0.5)), (cx + int(w * 1.02), cy + int(w * 0.5)))
        draw.polygon(tail, fill=T.mix(accent, T.BG_TOP, 0.35))
        eye_x = cx + int(w * 0.24)
        draw.ellipse((eye_x, cy - 12, eye_x + 12, cy), fill=T.WHITE)
        draw.ellipse((eye_x + 4, cy - 10, eye_x + 10, cy - 4), fill=T.BG_TOP)

    # ---------------------------------------------------------------- 数据胶囊
    def _draw_stats(self, draw: "ImageDraw.ImageDraw", card: Card, fonts: dict, y: int) -> int:
        if not card.stats:
            return y
        x0, x1 = T.PAD, T.CARD_WIDTH - T.PAD
        box = (x0, y, x1, y + STATS_H)
        T.rounded_rect(draw, box, T.RADIUS, fill=T.PANEL_SOFT, outline=T.STROKE, width=1)
        count = max(1, len(card.stats[:4]))
        cell_w = (x1 - x0) / count
        for index, item in enumerate(card.stats[:4]):
            cx = x0 + cell_w * index + cell_w / 2
            if index:
                sep_x = x0 + cell_w * index
                draw.line((sep_x, y + 16, sep_x, y + STATS_H - 16), fill=T.mix(T.STROKE, T.PANEL_SOFT, 0.3), width=1)
            T.draw_text(draw, (cx, y + 16), T.ellipsis(str(item.label), fonts["stat_label"], int(cell_w) - 10),
                        fonts["stat_label"], T.TEXT_DIM, anchor="ma")
            color = item.accent or T.ACCENT_SOFT
            T.draw_text(draw, (cx, y + 36), T.ellipsis(str(item.value), fonts["stat_value"], int(cell_w) - 10),
                        fonts["stat_value"], color, bold=True, anchor="ma")
        return y + STATS_H

    # ---------------------------------------------------------------- 内容块
    def _block_height(self, block: Block, fonts: dict, accent: str) -> int:
        content = self._paint_block(block, None, T.PAD, 0, T.INNER_WIDTH, fonts, accent)
        return max(STAT_MIN_H, content + BLOCK_PAD * 2)

    def _draw_block(self, draw: "ImageDraw.ImageDraw", block: Block, fonts: dict, accent: str, y: int) -> int:
        height = self._block_height(block, fonts, accent)
        x0, x1 = T.PAD, T.CARD_WIDTH - T.PAD
        if block.kind != "divider":
            T.rounded_rect(draw, (x0, y, x1, y + height), T.RADIUS, fill=T.PANEL, outline=T.STROKE, width=1)
            self._paint_block(block, draw, x0, y + BLOCK_PAD, x1 - x0, fonts, accent)
        else:
            mid = y + height // 2
            draw.line((x0 + 8, mid, x1 - 8, mid), fill=T.mix(T.STROKE, T.PANEL, 0.2), width=1)
        return y + height

    def _paint_block(
        self,
        block: Block,
        draw: "ImageDraw.ImageDraw | None",
        x: int,
        y: int,
        width: int,
        fonts: dict,
        accent: str,
    ) -> int:
        """统一测量+绘制：draw=None 时只算高度。返回消耗的高度。"""
        if block.kind == "divider":
            return 18
        inner_x = x + BLOCK_PAD
        inner_w = max(40, width - BLOCK_PAD * 2)
        cursor = y
        title_font = fonts["block_title"]
        body_font = fonts["body"]
        small_font = fonts["small"]

        if block.title:
            if draw is not None:
                T.draw_text(draw, (inner_x, cursor), T.ellipsis(block.title, title_font, inner_w),
                            title_font, T.ACCENT_SOFT, bold=True)
            cursor += 26

        kind = block.kind
        if kind in ("text", "list", "quote"):
            quote_color = T.mix(T.PANEL, accent, 0.22) if kind == "quote" else None
            for index, line in enumerate(block.lines or []):
                indent = 14 if kind == "list" else 0
                # list 会在行首加 "· "，预留 18px，避免折行后顶到面板边框
                wrap_width = inner_w - indent - (18 if kind == "list" else 0)
                wrapped = T.wrap_text(str(line), body_font, max(40, wrap_width))
                for sub_index, text_line in enumerate(wrapped):
                    if draw is not None:
                        if kind == "quote" and index == 0 and sub_index == 0:
                            T.rounded_rect(draw, (inner_x, cursor + 2, inner_x + 4, cursor + 22), 2, fill=accent)
                        text_x = inner_x + indent
                        prefix = "· " if (kind == "list" and sub_index == 0) else ""
                        color = T.TEXT if kind != "quote" else T.mix(T.TEXT, accent, 0.18)
                        T.draw_text(draw, (text_x, cursor), prefix + text_line, body_font, color)
                    cursor += 24
                if kind == "quote":
                    cursor += 2
            if block.footer:
                if draw is not None:
                    T.draw_text(draw, (inner_x, cursor + 2), T.ellipsis(block.footer, small_font, inner_w),
                                small_font, T.TEXT_DIM)
                cursor += 22
            return cursor - y

        if kind == "kv":
            column = int(inner_w * 0.46)
            for row in block.rows:
                label = T.ellipsis(str(row.label), body_font, column - 8)
                value_w = inner_w - column - 8
                value_lines = T.wrap_text(str(row.value), body_font, value_w) or [""]
                if draw is not None:
                    T.draw_text(draw, (inner_x, cursor), label, body_font, T.TEXT_DIM)
                    color = row.accent or T.TEXT
                    for i, value_line in enumerate(value_lines):
                        T.draw_text(draw, (inner_x + column, cursor + i * 24), value_line, body_font, color)
                    if row.note:
                        T.draw_text(draw, (inner_x + column, cursor + len(value_lines) * 24),
                                    T.ellipsis(row.note, small_font, value_w), small_font, T.TEXT_DIM)
                cursor += 24 * len(value_lines) + (22 if row.note else 0) + 4
            if block.footer:
                if draw is not None:
                    T.draw_text(draw, (inner_x, cursor + 2), T.ellipsis(block.footer, small_font, inner_w),
                                small_font, T.TEXT_DIM)
                cursor += 22
            return cursor - y

        if kind == "progress":
            if draw is not None:
                bar = (inner_x, cursor + 2, inner_x + inner_w, cursor + 16)
                T.rounded_rect(draw, bar, 7, fill=T.mix(T.BG_TOP, T.STROKE, 0.5))
                filled_w = int(inner_w * max(0.0, min(1.0, float(block.progress))))
                if filled_w > 4:
                    T.rounded_rect(draw, (inner_x, cursor + 2, inner_x + filled_w, cursor + 16), 7, fill=accent)
                if block.progress_text:
                    T.draw_text(draw, (inner_x, cursor + 22), T.ellipsis(block.progress_text, small_font, inner_w),
                                small_font, T.TEXT_DIM)
            cursor += 24 + (22 if block.progress_text else 0)
            return cursor - y

        if kind == "grid":
            columns = 2
            cell_w = (inner_w - 10) // columns
            for index, row in enumerate(block.rows):
                if index % columns == 0:
                    row_cursor = cursor
                col = index % columns
                cx = inner_x + col * (cell_w + 10)
                if draw is not None:
                    T.rounded_rect(draw, (cx, row_cursor, cx + cell_w, row_cursor + 40), 10,
                                   fill=T.PANEL_ALT, outline=T.STROKE, width=1)
                    T.draw_text(draw, (cx + 10, row_cursor + 5), T.ellipsis(str(row.label), small_font, cell_w - 20),
                                small_font, T.TEXT_DIM)
                    T.draw_text(draw, (cx + 10, row_cursor + 21), T.ellipsis(str(row.value), body_font, cell_w - 20),
                                body_font, row.accent or T.TEXT, bold=True)
                if col == columns - 1 or index == len(block.rows) - 1:
                    cursor = row_cursor + 46
            if block.footer:
                if draw is not None:
                    T.draw_text(draw, (inner_x, cursor), T.ellipsis(block.footer, small_font, inner_w),
                                small_font, T.TEXT_DIM)
                cursor += 22
            return cursor - y

        # 未知 kind：退化成纯文本
        for line in block.lines or []:
            for text_line in T.wrap_text(str(line), body_font, inner_w):
                if draw is not None:
                    T.draw_text(draw, (inner_x, cursor), text_line, body_font, T.TEXT)
                cursor += 24
        return cursor - y

    # ---------------------------------------------------------------- 页脚
    def _draw_note(self, draw: "ImageDraw.ImageDraw", text: str, fonts: dict, x: int, y: int) -> None:
        T.draw_text(draw, (x, y), T.ellipsis(text, fonts["small"], T.INNER_WIDTH), fonts["small"], T.TEXT_DIM)

    @staticmethod
    def _draw_footer(draw: "ImageDraw.ImageDraw", card: Card, fonts: dict, total: int) -> None:
        footer = (card.footer or "").strip()
        if not footer:
            return
        y = total - FOOTER_H + 6
        draw.line((T.PAD, y - 6, T.CARD_WIDTH - T.PAD, y - 6), fill=T.mix(T.STROKE, T.BG_BOTTOM, 0.35), width=1)
        T.draw_text(
            draw,
            (T.CARD_WIDTH // 2, y + 4),
            T.ellipsis(footer, fonts["small"], T.INNER_WIDTH),
            fonts["small"],
            T.TEXT_DIM,
            anchor="ma",
        )


# 卡片高度上限（超过就折叠尾部条目，避免 5000px 长图把内存顶爆）
T_MAX = T.MAX_HEIGHT