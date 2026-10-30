"""大肥鱼吉祥物与场景插画 —— 纯 ImageDraw 图元，零外部素材。

设计约束（2 核 2G，与 NapCat + AstrBot 同机）：
    * 只往调用方传入的 draw 上画：不 new Image、不返回 Image、不持有模块级位图。
      渲染器在 to_thread 里画完可以立刻释放底图，这里不会留下额外 3MB 副本。
    * 单只鱼约 25 次图元调用，带配件约 40 次，远低于 200 次上限；没有逐像素循环，
      "发光描边"用 2 层描边椭圆模拟，成本常数级。
    * 所有坐标都经 _clamp 收进 box：渲染器给的头部区域再小，也只是画得挤，
      不会糊到卡片别处，也不会越界抛错。
    * 不 import theme / cache / renderer（防循环依赖）；品牌色从 core.constants 取，
      取不到就用本文件兜底色。
"""

from __future__ import annotations

import math

try:  # 作为插件包内子模块加载（main.py: from .ui import Renderer）
    from ..core.constants import ACCENT_BLUE, DEEP_BLUE, RARITY_COLORS, RARITY_ORDER
except ImportError:  # 以顶层包加载（单测 / 独立预览）
    ACCENT_BLUE = "#4D6BFE"
    DEEP_BLUE = "#0B1330"
    RARITY_ORDER = ["小蓝", "大蓝", "小紫", "大紫", "小金", "大金", "小红", "大红"]
    RARITY_COLORS = dict(
        zip(
            RARITY_ORDER,
            [
                "#4D6BFE",
                "#2F4BD8",
                "#8B5CF6",
                "#6D28D9",
                "#F5B301",
                "#D98324",
                "#EF4444",
                "#B91C1C",
            ],
        )
    )

WHITE = "#FFFFFF"
GOLD = "#F5B301"
PINK = "#FF7EA8"
PINK_DEEP = "#F472B6"
TEAR = "#BFE3FF"

_ACCENT_RGB = (77, 107, 254)

__all__ = [
    "draw_fatfish",
    "draw_fish",
    "draw_pond_scene",
    "draw_error_face",
    "draw_bag",
    "draw_shop",
    "draw_calendar",
    "draw_hearts",
    "draw_sweat",
    "draw_plain",
]


# ---------------------------------------------------------------- 基础图元
def _box(box):
    """把外部 box 收敛成合法整数四元组；非法返回 None（调用方直接跳过）。"""
    try:
        x0, y0, x1, y1 = box
        x0, y0 = int(round(x0)), int(round(y0))
        x1, y1 = int(round(x1)), int(round(y1))
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _clamp(value, low, high):
    if high < low:
        low, high = high, low
    value = int(round(value))
    if value < low:
        return low
    if value > high:
        return high
    return value


def _rgb(value, default=None):
    """#RRGGBB / #RGB / (r,g,b) -> (r,g,b)；非法输入回退 default。"""
    if default is None:
        default = _ACCENT_RGB
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return (
                max(0, min(255, int(value[0]))),
                max(0, min(255, int(value[1]))),
                max(0, min(255, int(value[2]))),
            )
        except (TypeError, ValueError):
            return default
    s = str(value or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return default
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return default


def _hex_ok(value) -> bool:
    """接受 #RRGGBB / RRGGBB / (r,g,b)：renderer 传字符串或元组都能直接用。"""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return all(0 <= int(channel) <= 255 for channel in value[:3])
        except (TypeError, ValueError):
            return False
    s = str(value or "").strip()
    if s.startswith("#"):
        s = s[1:]
    return len(s) in (3, 6) and all(ch in "0123456789abcdefABCDEF" for ch in s)


def _mix(a, b, t):
    """线性混色：t=0 取 a，t=1 取 b。返回 (r,g,b)。"""
    ca, cb = _rgb(a), _rgb(b)
    try:
        k = max(0.0, min(1.0, float(t)))
    except (TypeError, ValueError):
        k = 0.5
    return (
        int(ca[0] + (cb[0] - ca[0]) * k),
        int(ca[1] + (cb[1] - ca[1]) * k),
        int(ca[2] + (cb[2] - ca[2]) * k),
    )


def _ellipse(draw, clip, rect, fill=None, outline=None, width=1):
    x0, y0, x1, y1 = clip
    left = _clamp(rect[0], x0, x1)
    top = _clamp(rect[1], y0, y1)
    right = _clamp(rect[2], x0, x1)
    bottom = _clamp(rect[3], y0, y1)
    if right - left < 1 or bottom - top < 1:
        return
    draw.ellipse((left, top, right, bottom), fill=fill, outline=outline, width=max(1, int(width)))


def _rect(draw, clip, rect, fill=None, outline=None):
    x0, y0, x1, y1 = clip
    left = _clamp(rect[0], x0, x1)
    top = _clamp(rect[1], y0, y1)
    right = _clamp(rect[2], x0, x1)
    bottom = _clamp(rect[3], y0, y1)
    if right - left < 1 or bottom - top < 1:
        return
    draw.rectangle((left, top, right, bottom), fill=fill, outline=outline)


def _polygon(draw, clip, points, fill=None, outline=None):
    x0, y0, x1, y1 = clip
    pts = []
    for point in points:
        try:
            px, py = point
        except (TypeError, ValueError):
            continue
        pts.append((_clamp(px, x0, x1), _clamp(py, y0, y1)))
    if len(pts) < 3:
        return
    draw.polygon(pts, fill=fill, outline=outline)


def _line(draw, clip, start, end, fill, width=2):
    x0, y0, x1, y1 = clip
    try:
        ax, ay = start
        bx, by = end
    except (TypeError, ValueError):
        return
    draw.line(
        (
            _clamp(ax, x0, x1),
            _clamp(ay, y0, y1),
            _clamp(bx, x0, x1),
            _clamp(by, y0, y1),
        ),
        fill=fill,
        width=max(1, int(width)),
    )


def _arc(draw, clip, rect, start, end, fill, width=2):
    x0, y0, x1, y1 = clip
    left = _clamp(rect[0], x0, x1)
    top = _clamp(rect[1], y0, y1)
    right = _clamp(rect[2], x0, x1)
    bottom = _clamp(rect[3], y0, y1)
    if right - left < 2 or bottom - top < 2:
        return
    draw.arc((left, top, right, bottom), start, end, fill=fill, width=max(1, int(width)))


def _round_rect(draw, clip, rect, radius, fill=None, outline=None, width=1):
    x0, y0, x1, y1 = clip
    left = _clamp(rect[0], x0, x1)
    top = _clamp(rect[1], y0, y1)
    right = _clamp(rect[2], x0, x1)
    bottom = _clamp(rect[3], y0, y1)
    if right - left < 2 or bottom - top < 2:
        return
    radius = max(0, min(int(round(radius)), (right - left) // 2, (bottom - top) // 2))
    try:
        draw.rounded_rectangle(
            (left, top, right, bottom),
            radius=radius,
            fill=fill,
            outline=outline,
            width=max(1, int(width)),
        )
    except (AttributeError, TypeError):  # Pillow < 8.2 兜底
        draw.rectangle((left, top, right, bottom), fill=fill, outline=outline, width=max(1, int(width)))


def _star(draw, clip, cx, cy, radius, color, points=5, inner=0.46):
    radius = max(2.0, float(radius))
    coords = []
    for index in range(points * 2):
        angle = math.radians(-90.0 + index * (180.0 / points))
        dist = radius if index % 2 == 0 else radius * inner
        coords.append((cx + dist * math.cos(angle), cy + dist * math.sin(angle)))
    _polygon(draw, clip, coords, fill=color)


def _heart(draw, clip, cx, cy, size, color):
    s = max(5.0, float(size))
    r = s * 0.27
    top = cy - s * 0.10
    _ellipse(draw, clip, (cx - 2 * r, top - r, cx, top + r), fill=color)
    _ellipse(draw, clip, (cx, top - r, cx + 2 * r, top + r), fill=color)
    _polygon(
        draw,
        clip,
        [(cx - 1.92 * r, top + r * 0.30), (cx + 1.92 * r, top + r * 0.30), (cx, top + 2.35 * r)],
        fill=color,
    )


def _tear(draw, clip, rect, color=TEAR):
    left = float(rect[0])
    top = float(rect[1])
    right = float(rect[2])
    bottom = float(rect[3])
    w = max(3.0, right - left)
    h = max(4.0, bottom - top)
    r = w / 2.0
    _ellipse(draw, clip, (left, bottom - 2 * r, right, bottom), fill=color, outline=_mix(WHITE, color, 0.55), width=1)
    _polygon(
        draw,
        clip,
        [(left + w * 0.5, top), (left + w * 0.12, bottom - r * 0.95), (left + w * 0.88, bottom - r * 0.95)],
        fill=color,
    )
    _ellipse(draw, clip, (left + w * 0.20, bottom - 1.55 * r, left + w * 0.52, bottom - 1.10 * r), fill=_mix(WHITE, color, 0.25))


def _rarity_color(star: int) -> str:
    try:
        index = max(1, min(8, int(star)))
    except (TypeError, ValueError):
        index = 1
    try:
        name = RARITY_ORDER[index - 1]
        return RARITY_COLORS.get(name, ACCENT_BLUE)
    except (IndexError, AttributeError, TypeError):
        return ACCENT_BLUE


def _norm_mood(mood) -> str:
    m = str(mood or "happy").strip().lower()
    if m in ("cry", "error", "tear"):
        return "cry"
    if m in ("sad", "unhappy", "down"):
        return "sad"
    if m in ("wink", "playful"):
        return "wink"
    if m in ("smug", "proud", "legend", "cool"):
        return "smug"
    if m in ("sleepy", "sleep", "tired"):
        return "sleepy"
    return "happy"


# ---------------------------------------------------------------- 大肥鱼本体
def _fatfish(draw, clip, box, mood, accent, stars=0) -> None:
    """在 box 里画一只胖鱼；clip 是整张插画区域的硬边界（防止越界）。"""
    if box is None:
        return
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if w < 28 or h < 22:
        return

    base_rgb = _rgb(accent)
    light = _mix(accent, WHITE, 0.30)
    belly = _mix(accent, WHITE, 0.66)
    dark = _mix(accent, DEEP_BLUE, 0.42)
    edge = _mix(accent, WHITE, 0.52)
    halo = _mix(accent, DEEP_BLUE, 0.74)
    norm = _norm_mood(mood)

    cx = x0 + w * 0.46
    cy = y0 + h * 0.58
    rx = w * 0.32
    ry = h * 0.25

    # 发光描边：两层半透明感轮廓
    _ellipse(draw, clip, (cx - rx - 6, cy - ry - 6, cx + rx + 6, cy + ry + 6), outline=halo, width=5)
    _ellipse(draw, clip, (cx - rx - 2, cy - ry - 2, cx + rx + 2, cy + ry + 2), outline=_mix(accent, WHITE, 0.20), width=2)

    # 尾鳍（右侧扇形）
    _polygon(
        draw,
        clip,
        [
            (cx + rx * 0.72, cy - ry * 0.10),
            (x0 + w * 0.98, cy - h * 0.20),
            (x0 + w * 0.90, cy),
            (x0 + w * 0.98, cy + h * 0.20),
            (cx + rx * 0.72, cy + ry * 0.10),
        ],
        fill=light,
        outline=edge,
    )
    # 背鳍
    _polygon(
        draw,
        clip,
        [
            (cx - rx * 0.30, cy - ry * 0.88),
            (cx + rx * 0.02, cy - ry * 1.55),
            (cx + rx * 0.46, cy - ry * 0.66),
        ],
        fill=_mix(accent, WHITE, 0.16),
        outline=edge,
    )
    # 胸鳍
    _polygon(
        draw,
        clip,
        [
            (cx - rx * 0.26, cy + ry * 0.38),
            (cx - rx * 0.82, cy + ry * 0.78),
            (cx - rx * 0.16, cy + ry * 0.86),
        ],
        fill=light,
        outline=edge,
    )
    # 圆滚滚的身体 + 肚皮 + 描边
    _ellipse(draw, clip, (cx - rx, cy - ry, cx + rx, cy + ry), fill=base_rgb)
    _ellipse(draw, clip, (cx - rx * 0.72, cy - ry * 0.02, cx + rx * 0.72, cy + ry * 0.92), fill=belly)
    _ellipse(draw, clip, (cx - rx, cy - ry, cx + rx, cy + ry), outline=edge, width=2)

    # 头顶的 DeepSeek 蓝小灯
    lamp_r = max(3.0, min(w, h) * 0.055)
    lamp_x = cx - rx * 0.12
    lamp_base_y = cy - ry * 1.04
    lamp_y = lamp_base_y - h * 0.14
    _line(
        draw,
        clip,
        (lamp_x, lamp_base_y),
        (lamp_x, lamp_y + lamp_r * 0.5),
        fill=_mix(accent, WHITE, 0.42),
        width=max(2, int(round(lamp_r * 0.45))),
    )
    _ellipse(
        draw,
        clip,
        (lamp_x - lamp_r - 3, lamp_y - lamp_r - 3, lamp_x + lamp_r + 3, lamp_y + lamp_r + 3),
        outline=_mix(ACCENT_BLUE, DEEP_BLUE, 0.45),
        width=2,
    )
    _ellipse(
        draw,
        clip,
        (lamp_x - lamp_r, lamp_y - lamp_r, lamp_x + lamp_r, lamp_y + lamp_r),
        fill=_mix("#9FB2FF", WHITE, 0.18),
    )
    _ellipse(
        draw,
        clip,
        (lamp_x - lamp_r * 0.45, lamp_y - lamp_r * 0.55, lamp_x + lamp_r * 0.05, lamp_y - lamp_r * 0.02),
        fill=WHITE,
    )

    # 表情
    eye_dx = rx * 0.30
    eye_y = cy - ry * 0.16
    er = max(2.5, min(w, h) * 0.062)
    for sign in (-1, 1):
        ex = cx + sign * eye_dx
        if norm == "wink" and sign > 0:
            _arc(draw, clip, (ex - er, eye_y - er, ex + er, eye_y + er), 200, 340, fill=dark, width=2)
            continue
        if norm == "sleepy":
            _arc(draw, clip, (ex - er, eye_y - er * 0.7, ex + er, eye_y + er * 0.7), 200, 340, fill=dark, width=2)
            continue
        if norm == "smug":
            _arc(draw, clip, (ex - er, eye_y - er * 0.4, ex + er, eye_y + er * 1.1), 200, 340, fill=dark, width=2)
            continue
        if norm == "cry":
            _line(draw, clip, (ex - er, eye_y - er), (ex + er, eye_y + er), fill=dark, width=2)
            _line(draw, clip, (ex - er, eye_y + er), (ex + er, eye_y - er), fill=dark, width=2)
            continue
        _ellipse(draw, clip, (ex - er, eye_y - er, ex + er, eye_y + er), fill=WHITE)
        pupil = er * 0.55
        _ellipse(
            draw,
            clip,
            (ex - pupil + er * 0.12, eye_y - pupil + er * 0.10, ex + pupil + er * 0.12, eye_y + pupil + er * 0.10),
            fill=dark,
        )
        _ellipse(
            draw,
            clip,
            (ex - pupil * 0.40, eye_y - pupil * 0.62, ex + pupil * 0.10, eye_y - pupil * 0.12),
            fill=WHITE,
        )

    # 眉毛（难过时压下来）
    if norm in ("sad", "cry"):
        for sign in (-1, 1):
            ex = cx + sign * eye_dx
            inner = ex + sign * er * 0.90
            outer = ex - sign * er * 0.90
            _line(
                draw,
                clip,
                (outer, eye_y - er * 1.90),
                (inner, eye_y - er * 1.15),
                fill=dark,
                width=max(2, int(round(er * 0.40))),
            )

    # 腮红
    blush = _mix(PINK, accent, 0.48)
    for sign in (-1, 1):
        bx = cx + sign * rx * 0.60
        by = cy + ry * 0.22
        _ellipse(
            draw,
            clip,
            (bx - w * 0.055, by - h * 0.028, bx + w * 0.055, by + h * 0.028),
            fill=blush,
        )

    # 嘴
    mw = max(3.0, rx * 0.20)
    mh = max(2.0, ry * 0.16)
    mx = cx
    my = cy + ry * 0.44
    if norm in ("sad", "cry"):
        _arc(draw, clip, (mx - mw, my, mx + mw, my + mh * 1.9), 200, 340, fill=dark, width=2)
    elif norm == "sleepy":
        _ellipse(draw, clip, (mx - mw * 0.5, my, mx + mw * 0.5, my + mh), fill=_mix(dark, WHITE, 0.20))
    elif norm == "smug":
        _arc(draw, clip, (mx - mw * 0.55, my - mh, mx + mw * 0.75, my + mh), 20, 110, fill=dark, width=2)
    else:
        _arc(draw, clip, (mx - mw, my - mh, mx + mw, my + mh * 1.7), 20, 160, fill=dark, width=2)

    # 星级装饰
    try:
        count = max(0, min(5, int(stars or 0)))
    except (TypeError, ValueError):
        count = 0
    if count:
        star_r = max(3.0, min(w, h) * 0.062)
        for index in range(count):
            if count == 1:
                sx = x0 + w * 0.50
            else:
                sx = x0 + w * (0.16 + index * (0.68 / (count - 1)))
            _star(draw, clip, sx, y0 + h * 0.10, star_r, GOLD)


# ---------------------------------------------------------------- 对外：各 art 变体
def draw_fatfish(img, draw, box, *, mood="happy", accent=ACCENT_BLUE, with_stars=0) -> None:
    """默认吉祥物：一只圆滚滚的大肥鱼，脚下有柔光。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 24 or h < 20:
        return
    _ellipse(draw, b, (x0 + w * 0.18, y1 - h * 0.10, x1 - w * 0.18, y1 - h * 0.01), fill=_mix(accent, DEEP_BLUE, 0.80))
    _fatfish(draw, b, (x0 + w * 0.02, y0 + h * 0.06, x1 - w * 0.02, y1 - h * 0.05), mood, accent, with_stars)


def draw_fish(img, draw, box, star: int, accent: str = "") -> None:
    """图鉴/钓获用：按星级换配色的侧身鱼，头顶挂小星星。"""
    b = _box(box)
    if b is None:
        return
    try:
        star_value = max(1, min(8, int(star)))
    except (TypeError, ValueError):
        star_value = 3
    color = accent if _hex_ok(accent) else _rarity_color(star_value)
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 32 or h < 26:
        return

    light = _mix(color, WHITE, 0.35)
    belly = _mix(color, WHITE, 0.70)
    edge = _mix(color, WHITE, 0.52)
    dark = _mix(color, DEEP_BLUE, 0.45)
    cx = x0 + w * 0.45
    cy = y0 + h * 0.58
    rx = w * 0.29
    ry = h * 0.185

    _ellipse(draw, b, (cx - rx - 5, cy - ry - 5, cx + rx + 5, cy + ry + 5), outline=_mix(color, DEEP_BLUE, 0.74), width=4)
    _polygon(
        draw,
        b,
        [
            (cx + rx * 0.74, cy),
            (x0 + w * 0.99, cy - h * 0.17),
            (x0 + w * 0.91, cy),
            (x0 + w * 0.99, cy + h * 0.17),
        ],
        fill=light,
        outline=edge,
    )
    _polygon(
        draw,
        b,
        [
            (cx - rx * 0.38, cy - ry * 0.82),
            (cx + rx * 0.02, cy - ry * 1.52),
            (cx + rx * 0.44, cy - ry * 0.62),
        ],
        fill=_mix(color, WHITE, 0.20),
        outline=edge,
    )
    _ellipse(draw, b, (cx - rx, cy - ry, cx + rx, cy + ry), fill=_rgb(color))
    _ellipse(draw, b, (cx - rx * 0.70, cy - ry * 0.05, cx + rx * 0.70, cy + ry * 0.82), fill=belly)
    _ellipse(draw, b, (cx - rx, cy - ry, cx + rx, cy + ry), outline=edge, width=2)
    _arc(draw, b, (cx - rx * 0.52, cy - ry * 0.72, cx + rx * 0.10, cy + ry * 0.72), 300, 60, fill=_mix(edge, color, 0.35), width=2)
    _arc(draw, b, (cx + rx * 0.58, cy + ry * 0.02, cx + rx * 0.94, cy + ry * 0.42), 20, 160, fill=dark, width=2)

    er = max(2.5, min(w, h) * 0.058)
    ex = cx + rx * 0.38
    ey = cy - ry * 0.30
    _ellipse(draw, b, (ex - er, ey - er, ex + er, ey + er), fill=WHITE)
    _ellipse(draw, b, (ex - er * 0.5, ey - er * 0.5, ex + er * 0.5, ey + er * 0.5), fill=dark)
    _ellipse(draw, b, (ex - er * 0.30, ey - er * 0.52, ex + er * 0.10, ey - er * 0.18), fill=WHITE)

    star_count = min(5, star_value)
    for index in range(star_count):
        if star_count == 1:
            sx = cx
        else:
            sx = x0 + w * (0.15 + index * (0.70 / (star_count - 1)))
        _star(draw, b, sx, y0 + h * 0.11, max(3.0, min(w, h) * 0.058), GOLD)
    for index in range(max(0, star_value - 5)):
        dx = x0 + w * (0.32 + index * 0.13)
        _ellipse(draw, b, (dx - 2, y0 + h * 0.235 - 2, dx + 2, y0 + h * 0.235 + 2), fill=GOLD)

    # 气泡点缀
    for index in range(3):
        bx = x0 + w * (0.10 + index * 0.07)
        by = y0 + h * (0.70 - index * 0.13)
        br = max(2.0, min(w, h) * (0.018 + index * 0.006))
        _ellipse(draw, b, (bx - br, by - br, bx + br, by + br), outline=_mix(WHITE, color, 0.55), width=1)


def draw_pond_scene(img, draw, box, level: int) -> None:
    """池塘：水面、波纹、荷叶荷花、小鱼剪影、等级星标。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 36:
        return
    try:
        lv = max(1, min(10, int(level)))
    except (TypeError, ValueError):
        lv = 1

    panel = _mix("#16234F", DEEP_BLUE, 0.12)
    water = _mix("#2EA8D8", DEEP_BLUE, 0.55)
    water_top = y0 + h * 0.36

    _round_rect(draw, b, (x0, y0, x1, y1), max(8, int(h * 0.10)), fill=panel, outline=_mix(ACCENT_BLUE, DEEP_BLUE, 0.55), width=2)
    _round_rect(draw, b, (x0 + 2, water_top, x1 - 2, y1 - 2), max(6, int(h * 0.09)), fill=water)
    _arc(
        draw,
        b,
        (x0 + w * 0.10, water_top - h * 0.05, x1 - w * 0.10, water_top + h * 0.10),
        200,
        340,
        fill=_mix("#D8F5FF", water, 0.40),
        width=2,
    )

    # 波纹
    for index in range(3):
        rwid = w * (0.22 + index * 0.13)
        rht = h * (0.038 + index * 0.010)
        cyy = y1 - h * (0.20 + index * 0.11)
        _arc(
            draw,
            b,
            (x0 + w * 0.5 - rwid, cyy - rht, x0 + w * 0.5 + rwid, cyy + rht),
            20,
            160,
            fill=_mix("#D8F5FF", water, 0.58),
            width=2,
        )

    # 荷叶 + 荷花
    lx = x0 + w * 0.24
    ly = y1 - h * 0.24
    lrx = w * 0.15
    lry = h * 0.055
    _ellipse(draw, b, (lx - lrx, ly - lry, lx + lrx, ly + lry), fill=_mix("#3FBF7F", DEEP_BLUE, 0.28))
    _polygon(
        draw,
        b,
        [(lx, ly), (lx + lrx * 0.90, ly - lry * 0.60), (lx + lrx * 0.98, ly + lry * 0.25)],
        fill=water,
    )
    _ellipse(draw, b, (lx - lrx * 0.20, ly - lry * 1.70, lx + lrx * 0.20, ly - lry * 0.35), fill=_mix(PINK, DEEP_BLUE, 0.10))
    _ellipse(draw, b, (lx - lrx * 0.40, ly - lry * 1.35, lx - lrx * 0.02, ly - lry * 0.25), fill=_mix(PINK, DEEP_BLUE, 0.22))
    _ellipse(draw, b, (lx + lrx * 0.02, ly - lry * 1.35, lx + lrx * 0.40, ly - lry * 0.25), fill=_mix(PINK, DEEP_BLUE, 0.22))

    # 小鱼剪影
    fx = x0 + w * 0.66
    fy = y1 - h * 0.17
    frx = w * 0.085
    fry = h * 0.040
    fish_col = _mix("#BFEFFF", water, 0.30)
    _ellipse(draw, b, (fx - frx, fy - fry, fx + frx, fy + fry), fill=fish_col)
    _polygon(
        draw,
        b,
        [(fx + frx * 0.80, fy), (fx + frx * 1.55, fy - fry * 1.0), (fx + frx * 1.55, fy + fry * 1.0)],
        fill=fish_col,
    )
    _ellipse(draw, b, (fx + frx * 0.32, fy - fry * 0.48, fx + frx * 0.54, fy - fry * 0.20), fill=_mix(DEEP_BLUE, water, 0.25))

    # 等级星标（5 级以内直接星数，超出用小点补）
    stars = min(5, lv)
    for index in range(stars):
        sx = x0 + w * 0.16 + index * (w * 0.145)
        _star(draw, b, sx, y0 + h * 0.18, max(3.0, min(w, h) * 0.052), GOLD)
    for index in range(max(0, lv - 5)):
        dx = x0 + w * 0.16 + index * (w * 0.145)
        _ellipse(draw, b, (dx - 2, y0 + h * 0.275 - 2, dx + 2, y0 + h * 0.275 + 2), fill=GOLD)


def draw_error_face(img, draw, box) -> None:
    """错误卡：哭脸胖鱼 + 两行眼泪。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0 + w * 0.02, y0 + h * 0.08, x1 - w * 0.02, y1 - h * 0.03), "cry", "#EF4444", 0)
    tear_w = max(4.0, w * 0.045)
    tear_h = max(8.0, h * 0.16)
    for fx in (0.365, 0.565):
        _tear(draw, b, (x0 + w * fx, y0 + h * 0.60, x0 + w * fx + tear_w, y0 + h * 0.60 + tear_h))


def draw_bag(img, draw, box) -> None:
    """背包：胖鱼 + 小背包。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0, y0 + h * 0.12, x0 + w * 0.74, y1 - h * 0.02), "happy", ACCENT_BLUE, 0)
    _backpack(draw, b, (x0 + w * 0.56, y0 + h * 0.40, x0 + w * 0.98, y0 + h * 0.90))


def draw_shop(img, draw, box) -> None:
    """商店：胖鱼 + 价签 + 两枚鲸币。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0, y0 + h * 0.12, x0 + w * 0.74, y1 - h * 0.02), "happy", ACCENT_BLUE, 0)
    _price_tag(draw, b, (x0 + w * 0.58, y0 + h * 0.20, x0 + w * 0.98, y0 + h * 0.48))
    coin_r = max(3.0, min(w, h) * 0.048)
    for fx, fy in ((0.66, 0.68), (0.82, 0.80)):
        coin_x = x0 + w * fx
        coin_y = y0 + h * fy
        _ellipse(
            draw,
            b,
            (coin_x - coin_r, coin_y - coin_r, coin_x + coin_r, coin_y + coin_r),
            fill=_mix(GOLD, DEEP_BLUE, 0.15),
            outline=_mix(GOLD, WHITE, 0.35),
            width=1,
        )


def draw_calendar(img, draw, box) -> None:
    """签到：胖鱼 + 小日历。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0, y0 + h * 0.12, x0 + w * 0.69, y1 - h * 0.02), "happy", ACCENT_BLUE, 0)
    _calendar_icon(draw, b, (x0 + w * 0.64, y0 + h * 0.24, x0 + w * 0.95, y0 + h * 0.82))


def draw_hearts(img, draw, box) -> None:
    """婚姻：粉色胖鱼 + 爱心。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0 + w * 0.02, y0 + h * 0.12, x1 - w * 0.02, y1 - h * 0.02), "happy", PINK_DEEP, 0)
    _heart(draw, b, x0 + w * 0.80, y0 + h * 0.18, max(8.0, min(w, h) * 0.24), PINK)
    _heart(draw, b, x0 + w * 0.90, y0 + h * 0.40, max(6.0, min(w, h) * 0.16), _mix(PINK, WHITE, 0.25))
    _heart(draw, b, x0 + w * 0.66, y0 + h * 0.10, max(5.0, min(w, h) * 0.11), _mix(PINK, WHITE, 0.40))


def draw_sweat(img, draw, box) -> None:
    """打工：胖鱼 + 汗滴 + 动态线。"""
    b = _box(box)
    if b is None:
        return
    x0, y0, x1, y1 = b
    w = x1 - x0
    h = y1 - y0
    if w < 40 or h < 32:
        return
    _fatfish(draw, b, (x0, y0 + h * 0.10, x0 + w * 0.80, y1 - h * 0.02), "wink", ACCENT_BLUE, 0)
    _tear(draw, b, (x0 + w * 0.74, y0 + h * 0.26, x0 + w * 0.84, y0 + h * 0.46), TEAR)
    for index in range(2):
        _arc(
            draw,
            b,
            (x1 - w * 0.20, y0 + h * (0.12 + index * 0.12), x1 - w * 0.02, y0 + h * (0.30 + index * 0.12)),
            300,
            60,
            fill=_mix(WHITE, ACCENT_BLUE, 0.35),
            width=2,
        )


def draw_plain(img, draw, box) -> None:
    """ART_NONE：不画插画（卡片走纯排版）。"""
    return None


# ---------------------------------------------------------------- 小配件
def _backpack(draw, clip, rect) -> None:
    left, top, right, bottom = (int(round(v)) for v in rect)
    w = right - left
    h = bottom - top
    if w < 12 or h < 12:
        return
    body = _mix(ACCENT_BLUE, WHITE, 0.20)
    edge = _mix(ACCENT_BLUE, WHITE, 0.55)
    _round_rect(draw, clip, (left, top + h * 0.20, right, bottom), max(3, int(w * 0.26)), fill=body, outline=edge, width=2)
    _round_rect(
        draw,
        clip,
        (left + w * 0.08, top + h * 0.10, right - w * 0.08, top + h * 0.56),
        max(3, int(w * 0.22)),
        fill=_mix(ACCENT_BLUE, WHITE, 0.34),
        outline=edge,
        width=2,
    )
    _arc(draw, clip, (left + w * 0.30, top, right - w * 0.30, top + h * 0.24), 180, 360, fill=edge, width=2)
    _round_rect(draw, clip, (left + w * 0.42, top + h * 0.44, right - w * 0.42, top + h * 0.58), max(1, int(w * 0.05)), fill=GOLD)
    _line(draw, clip, (left + w * 0.12, top + h * 0.58), (left + w * 0.12, bottom - h * 0.08), fill=edge, width=2)
    _line(draw, clip, (right - w * 0.12, top + h * 0.58), (right - w * 0.12, bottom - h * 0.08), fill=edge, width=2)


def _price_tag(draw, clip, rect) -> None:
    left, top, right, bottom = (int(round(v)) for v in rect)
    w = right - left
    h = bottom - top
    if w < 12 or h < 8:
        return
    edge = _mix(GOLD, DEEP_BLUE, 0.28)
    _round_rect(
        draw,
        clip,
        (left, top + h * 0.10, right, bottom - h * 0.10),
        max(2, min(int(h * 0.32), int(w * 0.20))),
        fill=GOLD,
        outline=edge,
        width=2,
    )
    hole_r = max(2.0, h * 0.09)
    hole_x = left + w * 0.16
    hole_y = top + h * 0.50
    _ellipse(draw, clip, (hole_x - hole_r, hole_y - hole_r, hole_x + hole_r, hole_y + hole_r), fill=_mix(DEEP_BLUE, GOLD, 0.12))
    _line(draw, clip, (hole_x, hole_y - hole_r), (hole_x - w * 0.10, top), fill=_mix(GOLD, WHITE, 0.40), width=2)
    for index in range(2):
        line_y = top + h * (0.36 + index * 0.24)
        _line(draw, clip, (left + w * 0.34, line_y), (right - w * 0.12, line_y), fill=_mix(WHITE, GOLD, 0.35), width=2)


def _calendar_icon(draw, clip, rect) -> None:
    left, top, right, bottom = (int(round(v)) for v in rect)
    w = right - left
    h = bottom - top
    if w < 14 or h < 14:
        return
    body = _mix(WHITE, ACCENT_BLUE, 0.06)
    edge = _mix(ACCENT_BLUE, DEEP_BLUE, 0.30)
    _round_rect(draw, clip, (left, top, right, bottom), max(3, int(w * 0.16)), fill=body, outline=edge, width=2)
    head_h = int(h * 0.30)
    _round_rect(
        draw,
        clip,
        (left, top, right, top + head_h + max(3, int(w * 0.10))),
        max(3, int(w * 0.16)),
        fill=_rgb(ACCENT_BLUE),
    )
    _rect(draw, clip, (left + 1, top + head_h, right - 1, top + head_h + 2), fill=_rgb(ACCENT_BLUE))
    for fx in (0.30, 0.70):
        ring_x = left + w * fx
        _rect(draw, clip, (ring_x - 1, top + 2, ring_x + 1, top + head_h * 0.55), fill=WHITE)
    dot_r = max(1.5, w * 0.045)
    for row in range(2):
        for col in range(3):
            dot_x = left + w * (0.22 + col * 0.28)
            dot_y = top + head_h + h * (0.20 + row * 0.26)
            if row == 1 and col == 1:
                _ellipse(
                    draw,
                    clip,
                    (dot_x - dot_r * 1.7, dot_y - dot_r * 1.7, dot_x + dot_r * 1.7, dot_y + dot_r * 1.7),
                    fill=_rgb(ACCENT_BLUE),
                )
            else:
                _ellipse(draw, clip, (dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=_mix(ACCENT_BLUE, WHITE, 0.55))