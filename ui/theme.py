"""Pillow 主题层 —— 配色、字体探测、圆角、渐变、中文折行。

2 核 2G 上的三条纪律：
    1. 字体只解析一次（ttc 解析在 2 核上要几十毫秒），按字号缓存在模块字典里，几十 KB；
    2. 渐变不逐像素画（720×1600 = 115 万次 Python 循环，会卡死），用 1×H 小图拉伸；
    3. 所有绘图函数只操作传入的 Image/Draw，绝不 new 大图、绝不持有引用。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..core.log import log

# ---------------------------------------------------------------- 版式
CARD_WIDTH = 720
PAD = 28
INNER_WIDTH = CARD_WIDTH - PAD * 2
RADIUS = 20
LINE_GAP = 6
MAX_HEIGHT = 1600

# ---------------------------------------------------------------- 配色
BG_TOP = "#0B1330"
BG_BOTTOM = "#151E42"
PANEL = "#18214A"
PANEL_ALT = "#1E2857"
PANEL_SOFT = "#141C3A"
STROKE = "#2B376E"
ACCENT = "#4D6BFE"
ACCENT_SOFT = "#7C93FF"
TEXT = "#E8ECFF"
TEXT_DIM = "#8C96C8"
WHITE = "#FFFFFF"
GOLD = "#F5B301"

TONE_COLORS = {
    "normal": ACCENT,
    "success": "#22C55E",
    "warn": "#F59E0B",
    "error": "#EF4444",
    "legend": GOLD,
}

# 品质色（与 core.constants 保持一致，渲染层不 import core 以免循环依赖）
RARITY_COLORS = {
    "小蓝": "#4D6BFE",
    "大蓝": "#2F4BD8",
    "小紫": "#8B5CF6",
    "大紫": "#6D28D9",
    "小金": "#F5B301",
    "大金": "#D98324",
    "小红": "#EF4444",
    "大红": "#B91C1C",
}

# ---------------------------------------------------------------- 字体
_FONT_CANDIDATES = (
    # Linux（Debian/Ubuntu 常见）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)

_font_path: str | None = None
_font_probed = False
_font_cache: dict[int, "ImageFont.FreeTypeFont | ImageFont.ImageFont"] = {}
_default_warned = False


def resolve_font_path(preferred: str = "") -> str | None:
    """按 配置 -> 系统候选 的顺序找第一个可用的中文字体。只探测一次。"""
    global _font_path, _font_probed
    if _font_probed:
        return _font_path
    _font_probed = True

    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    env_font = os.environ.get("WHALE_FONT_PATH", "")
    if env_font:
        candidates.append(env_font)
    candidates.extend(_FONT_CANDIDATES)

    for path in candidates:
        try:
            if path and Path(path).is_file():
                # 真解析一次，确认 Pillow 能读（ttc 可能缺子字体）
                probe = ImageFont.truetype(path, 16)
                probe.getbbox("鲸")
                _font_path = path
                log.info(f"鲸鱼馆中文字体: {path}")
                return _font_path
        except OSError:
            continue
    _font_path = None
    log.warning("未找到中文字体，图片里的中文会变成方块；请在配置 render_font_path 指定 ttf/otf/ttc")
    return None


def get_font(size: int, preferred: str = ""):
    """按字号取字体（缓存）。找不到中文字体时退化为 Pillow 内置位图字体。"""
    global _default_warned
    size = max(10, int(size))
    cached = _font_cache.get(size)
    if cached is not None:
        return cached
    path = resolve_font_path(preferred)
    font = None
    if path:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            font = None
    if font is None:
        if not _default_warned:
            log.warning("Pillow 内置字体不支持中文，请安装 fonts-noto-cjk")
            _default_warned = True
        try:
            font = ImageFont.load_default(size)  # Pillow >= 10.1
        except TypeError:
            font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def clear_font_cache() -> None:
    _font_cache.clear()


# ---------------------------------------------------------------- 颜色
def hex_rgb(color: str) -> tuple[int, int, int]:
    value = (color or ACCENT).lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return (77, 107, 254)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (77, 107, 254)


def mix(color_a: str, color_b: str, t: float) -> tuple[int, int, int]:
    """线性混色，t=0 取 a，t=1 取 b。"""
    t = max(0.0, min(1.0, float(t)))
    a = hex_rgb(color_a)
    b = hex_rgb(color_b)
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def with_alpha(color: str, alpha: int) -> tuple[int, int, int, int]:
    r, g, b = hex_rgb(color)
    return (r, g, b, max(0, min(255, int(alpha))))


def normalize_color(value: object, default: str = ACCENT) -> object:
    """把 service 可能传来的"品质中文名/元组/十六进制"统一成 Pillow 能吃的颜色。

    services 里出现过 `stat("鲸币", ..., C.RARITY_XIAOJIN)`（值是"小金"），
    直接丢给 draw.text 会 ValueError；这里做一层收敛，渲染层永不因颜色崩。
    """
    if isinstance(value, str):
        key = value.strip()
        if not key:
            return default
        if key.startswith("#"):
            return key
        mapped = RARITY_COLORS.get(key)
        if mapped:
            return mapped
        # 允许 "4D6BFE" 这种不带 # 的写法
        if len(key) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in key):
            return f"#{key}"
        return default
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return tuple(int(v) for v in value[:3])
    return default


# ---------------------------------------------------------------- 图元
def rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int = RADIUS,
    fill: object = None,
    outline: object = None,
    width: int = 1,
) -> None:
    """兼容 Pillow < 8.2，并把 radius 夹紧到半个短边以内。

    Pillow 10 的 `rounded_rectangle` 对 `radius > min(w,h)/2` 会抛 ValueError，
    而本项目的"顶部高光条"只有 5px 高、radius=3 —— 必须夹紧，否则每张卡都会渲染失败。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if w <= 0 or h <= 0:
        return
    safe_radius = max(0, min(int(radius), w // 2, h // 2))
    fill = normalize_color(fill) if fill is not None else None
    outline = normalize_color(outline) if outline is not None else None
    try:
        draw.rounded_rectangle(box, radius=safe_radius, fill=fill, outline=outline, width=width)
    except (AttributeError, TypeError, ValueError):
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def vertical_gradient(width: int, height: int, top: str, bottom: str) -> Image.Image:
    """1×H 小图逐行上色再拉伸 —— 比 Python 逐像素快两个数量级。"""
    height = max(1, int(height))
    width = max(1, int(width))
    strip = Image.new("RGB", (1, height))
    px = strip.load()
    for y in range(height):
        px[0, y] = mix(top, bottom, y / max(1, height - 1))
    return strip.resize((width, height), Image.Resampling.BILINEAR)


# ---------------------------------------------------------------- 文字清洗
# Pillow 只加载一个字体文件，而 CJK 字体基本不含彩色 emoji 字形，直接画会变成"豆腐块"。
# 这里在**测量与绘制之前**统一剥掉 emoji 与变体选择符，保证排版宽度和实际绘制一致。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\uFE0F\u200D\u2B00-\u2BFF]"
)
# CJK 字体通常自带这些装饰符号，保留它们（★ 用于鱼星级展示）
_SYMBOL_KEEP = frozenset("★☆♥♦♣♠※")


def sanitize(text: str) -> str:
    """去掉 emoji / 变体选择符，保留中文、英文、数字、★ 与常用标点。"""
    if not text:
        return ""
    cleaned = _EMOJI_RE.sub("", str(text))
    # U+2600~U+27BF 里混杂着 ✅✨⚙ 这类字体不支持的符号，逐字符过滤但保留 ★☆♥
    cleaned = "".join(
        ch for ch in cleaned if not ("\u2600" <= ch <= "\u27BF") or ch in _SYMBOL_KEEP
    )
    return cleaned.replace("  ", " ").strip()


def text_size(text: str, font) -> tuple[int, int]:
    """跨 Pillow 版本取文字尺寸。"""
    text = sanitize(text) if text else ""
    if not text:
        return (0, 0)
    try:
        left, top, right, bottom = font.getbbox(text)
        return (max(0, right - left), max(0, bottom - top))
    except (AttributeError, ValueError):
        try:
            return font.getsize(text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return (len(text) * 10, 16)


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font,
    fill: object,
    *,
    bold: bool = False,
    anchor: str = "la",
) -> None:
    """统一入口：bold 用 stroke_width 模拟（Pillow 无法给 ttc 加粗）。

颜色一律先过 `normalize_color`：service 层可能把品质中文名（"小金"）当 accent 传进来，
直接交给 Pillow 会 ValueError；这里收敛成十六进制，且第二次重试也必须是合法颜色。
"""
    text = sanitize(text)
    if not text:
        return
    color = normalize_color(fill, TEXT)
    try:
        draw.text(xy, text, font=font, fill=color, anchor=anchor, stroke_width=1 if bold else 0,
                  stroke_fill=color if bold else None)
    except (TypeError, ValueError):
        try:
            draw.text(xy, text, font=font, fill=color)
        except (TypeError, ValueError):  # 连 fill 都不认：退回主文字色，保证不抛
            draw.text(xy, text, font=font, fill=TEXT)


def wrap_text(text: str, font, max_width: int) -> list[str]:
    """中文按字符折行、英文/数字按单词折行，保证不超宽。"""
    text = sanitize(text)
    if not text:
        return []
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        word = ""
        for ch in paragraph:
            if ch.isspace():
                candidate = current + word + ch
                if text_size(candidate, font)[0] <= max_width:
                    current, word = candidate, ""
                else:
                    lines.append((current + word).rstrip())
                    current, word = "", ""
                continue
            if "\u2e80" <= ch <= "\u9fff" or ch in "，。！？、；：“”‘’（）【】《》…—":
                if word:
                    current += word
                    word = ""
                candidate = current + ch
                if text_size(candidate, font)[0] <= max_width:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = ch
            else:
                word += ch
                if text_size(current + word, font)[0] > max_width:
                    if word[:-1]:
                        current += word[:-1]
                    if current:
                        lines.append(current)
                    current, word = "", ch
        tail = (current + word).rstrip()
        if tail:
            lines.append(tail)
    return lines or [""]


def draw_text_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    box: tuple[int, int, int, int],
    font,
    fill: object,
    line_spacing: int = LINE_GAP,
    max_lines: int = 0,
) -> int:
    """在 box 内折行绘制，返回消耗的高度。超出 max_lines 时截断并加省略号。"""
    x0, y0, x1, _y1 = box
    lines = wrap_text(text, font, max(10, x1 - x0))
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    line_h = text_size("国", font)[1] + line_spacing
    y = y0
    for line in lines:
        draw_text(draw, (x0, y), line, font, fill)
        y += line_h
    return max(0, y - y0)


def ellipsis(text: str, font, max_width: int) -> str:
    """单行截断（用于 badge / 标题 / label）。"""
    text = str(text or "")
    if text_size(text, font)[0] <= max_width:
        return text
    while text and text_size(text + "…", font)[0] > max_width:
        text = text[:-1]
    return text + "…" if text else "…"