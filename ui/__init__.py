"""Pillow 渲染层（DeepSeek 蓝大肥鱼风）。

对外唯一入口：
    from .ui import Renderer
    renderer = Renderer(cfg, cache_dir, plugin_name)
    await renderer.render(card)     # -> 本地 PNG 绝对路径
    await renderer.close()
"""

from __future__ import annotations

from .schema import (
    ART_BAG,
    ART_CHECKIN,
    ART_ERROR,
    ART_FATFISH,
    ART_FISH,
    ART_HELP,
    ART_MARRY,
    ART_NONE,
    ART_POND,
    ART_PROFILE,
    ART_SHOP,
    ART_STATS,
    ART_WAREHOUSE,
    ART_WORK,
    Block,
    Card,
    Row,
    Stat,
    card,
    divider_block,
    error_card,
    grid_block,
    kv,
    kv_block,
    list_block,
    progress_block,
    quote_block,
    stat,
    text_block,
)

try:
    from .renderer import Renderer
except Exception as _exc:  # noqa: BLE001 - Pillow 缺失/损坏时仍要让插件能加载
    _PIL_ERROR = _exc

    class Renderer:  # type: ignore[no-redef]
        """占位渲染器：初始化即失败，main.py 会捕获并降级为纯文本回复。"""

        def __init__(self, *args, **kwargs) -> None:
            from ..core.errors import RenderError

            raise RenderError(f"Pillow 不可用，已降级为文字模式: {_PIL_ERROR!r}")

        async def render(self, card_obj) -> str:  # pragma: no cover
            from ..core.errors import RenderError

            raise RenderError("Pillow 不可用")

        async def close(self) -> None:  # pragma: no cover
            return None

        def stats(self) -> dict:  # pragma: no cover
            return {"entries": 0, "rendered": 0, "failures": 0}

__all__ = [
    "Renderer",
    "Card",
    "Block",
    "Row",
    "Stat",
    "card",
    "stat",
    "kv",
    "text_block",
    "kv_block",
    "list_block",
    "quote_block",
    "progress_block",
    "grid_block",
    "divider_block",
    "error_card",
    "ART_FATFISH",
    "ART_FISH",
    "ART_POND",
    "ART_MARRY",
    "ART_SHOP",
    "ART_WORK",
    "ART_CHECKIN",
    "ART_BAG",
    "ART_WAREHOUSE",
    "ART_PROFILE",
    "ART_HELP",
    "ART_STATS",
    "ART_ERROR",
    "ART_NONE",
]