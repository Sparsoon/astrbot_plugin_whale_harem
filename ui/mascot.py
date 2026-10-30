"""DeepSeek 社区形象（Q 版 / 2D 立绘）的加载与绘制。

设计要点（2 核 2G）：
    * 图片只在 `preload()` 时解码一次，缩到 ≤220×260 后缓存（每张约 150KB 内存）；
    * 渲染线程只做 `thumbnail` + `paste`，不解码大图、不读磁盘；
    * 资源缺失时 `get()` 返回 None，渲染器自动回退到手绘大肥鱼，不会崩。

素材来源：官方社区形象（Q 版 / 2D），放在插件 `assets/` 目录：
    assets/mascot_q.jpg    全身 Q 版（白底）
    assets/mascot_2d.jpg   2D 立绘头像（白底）
"""

from __future__ import annotations

import threading
from pathlib import Path

from PIL import Image

from ..core.log import log
from ..core.paths import PLUGIN_DIR
from . import theme as T

ASSETS_DIR = PLUGIN_DIR / "assets"
MAX_SIZE = (220, 260)

_LOCK = threading.Lock()
_CACHE: dict[str, Image.Image | None] = {}
_WARNED: set[str] = set()

# 白底素材放进白色圆角"贴纸"里，既不需要抠图，也不会和深蓝卡片打架
CARD_FILL = "#FFFFFF"
CARD_EDGE = "#DDE6FF"


def _load(kind: str) -> Image.Image | None:
    name = "mascot_q.jpg" if kind == "q" else "mascot_2d.jpg"
    path = ASSETS_DIR / name
    if not path.is_file():
        if kind not in _WARNED:
            log.warning(f"形象素材缺失: {path}（回退手绘大肥鱼）")
            _WARNED.add(kind)
        return None
    try:
        with Image.open(path) as raw:
            img = raw.convert("RGB")
            img.thumbnail(MAX_SIZE, Image.Resampling.LANCZOS)
            return img.copy()
    except Exception as exc:  # noqa: BLE001 - 素材损坏不能让渲染崩
        if kind not in _WARNED:
            log.warning(f"形象素材加载失败 {path}: {exc!r}")
            _WARNED.add(kind)
        return None


def preload() -> None:
    """启动时解码一次（在 Renderer.__init__ 里调用，之后只读缓存）。"""
    with _LOCK:
        for kind in ("q", "2d"):
            if kind not in _CACHE:
                _CACHE[kind] = _load(kind)


def get(kind: str) -> Image.Image | None:
    with _LOCK:
        if kind not in _CACHE:
            _CACHE[kind] = _load(kind)
        return _CACHE[kind]


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


def draw_sticker(
    img: Image.Image,
    draw,
    box: tuple[int, int, int, int],
    kind: str,
    accent: str,
) -> bool:
    """把形象画成圆角贴纸卡片；成功返回 True，素材缺失返回 False（调用方回退）。"""
    source = get(kind)
    if source is None:
        return False
    x0, y0, x1, y1 = box
    if x1 - x0 < 24 or y1 - y0 < 24:
        return False

    # 斜投影 + 白底贴纸 + 主色描边
    T.rounded_rect(draw, (x0 + 2, y0 + 3, x1 + 1, y1 + 2), 16, fill=T.mix(T.PANEL, T.BG_TOP, 0.25))
    T.rounded_rect(draw, (x0, y0, x1, y1), 16, fill=CARD_FILL, outline=T.mix(accent, CARD_EDGE, 0.35), width=2)

    pad = 4
    max_w = max(8, (x1 - x0) - pad * 2)
    max_h = max(8, (y1 - y0) - pad * 2)
    thumb = source.copy()
    thumb.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    px = x0 + ((x1 - x0) - thumb.width) // 2
    py = y0 + ((y1 - y0) - thumb.height) // 2
    img.paste(thumb, (px, py))
    del thumb
    return True