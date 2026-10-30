"""日志薄封装：优先用 AstrBot 的 logger，脱离框架时退化为标准 logging。"""

from __future__ import annotations

import logging

try:  # pragma: no cover - 仅在 AstrBot 环境成立
    from astrbot.api import logger as log  # type: ignore
except Exception:  # pragma: no cover
    log = logging.getLogger("astrbot_plugin_whale_harem")
    if not log.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("[鲸鱼馆] %(levelname)s %(message)s"))
        log.addHandler(_h)
        log.setLevel(logging.INFO)


def debug(msg: str) -> None:
    try:
        log.debug(msg)
    except Exception:  # noqa: BLE001
        pass