"""路径解析：所有可变数据都放 AstrBot 的 plugin_data 目录，插件目录保持只读。"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _astrbot_data_root() -> Path | None:
    try:  # AstrBot >= 4.9.2
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore

        return Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001
        pass
    env = os.environ.get("ASTRBOT_DATA_PATH") or os.environ.get("ASTRBOT_ROOT")
    if env:
        return Path(env) / "data"
    return None


def plugin_data_dir(plugin_name: str) -> Path:
    """返回并创建插件数据目录（DB、缓存图、字体缓存都放这里）。"""
    root = _astrbot_data_root()
    if root is not None:
        base = root / "plugin_data" / plugin_name
    else:  # 脱离 AstrBot 跑单测时的兜底
        base = Path.cwd() / "data" / "plugin_data" / plugin_name
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path.cwd() / "data" / "plugin_data" / plugin_name
        base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir(plugin_name: str) -> Path:
    path = plugin_data_dir(plugin_name) / "imgcache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_file(plugin_name: str, override: str = "") -> Path:
    if override:
        p = Path(override).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return plugin_data_dir(plugin_name) / "whale.db"