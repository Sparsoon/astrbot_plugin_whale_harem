"""插件配置：把 AstrBot 传来的 dict 收敛成类型安全的 dataclass。

所有值都有安全默认值 —— 即使 `_conf_schema.json` 缺失、用户手改坏，
插件也必须能启动（配置错误不应该让整个 AstrBot 崩掉）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from . import constants as C


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "y", "是"):
            return True
        if v in ("0", "false", "no", "off", "n", "否", ""):
            return False
    return default


def _as_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        out = default
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


@dataclass(slots=True)
class PluginConfig:
    llm_enabled: bool = True
    llm_timeout_sec: int = 12
    render_font_path: str = ""
    image_cache_size: int = 24
    image_cache_ttl_sec: int = 300
    render_concurrency: int = 1
    command_debounce_ms: int = 1500
    db_path: str = ""
    fish_bag_limit: int = C.DEFAULT_FISH_BAG_LIMIT
    checkin_base_coins: int = C.CHECKIN_BASE_COINS
    checkin_streak_step: int = 0  # 兼容旧配置，长线公式已不用
    checkin_streak_cap: int = C.CHECKIN_MAX_STREAK_DAYS
    work_cooldown_min: int = C.WORK_MIN_MINUTES
    work_cooldown_max: int = C.WORK_MAX_MINUTES
    fish_wait_min: int = C.FISH_WAIT_MIN
    fish_wait_max: int = C.FISH_WAIT_MAX
    marry_daily_clear: bool = True
    garden_sun_start: int = C.GARDEN_SUN_START_HOUR
    garden_sun_end: int = C.GARDEN_SUN_END_HOUR
    garden_timezone: str = C.GARDEN_TIMEZONE
    enabled_groups: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_dict(cls, raw: Any) -> "PluginConfig":
        cfg = cls()
        if raw is None:
            return cfg
        if not isinstance(raw, dict):
            # AstrBot 可能传 AstrBotConfig（dict 子类）或其它 Mapping，统一转成 dict
            try:
                raw = dict(raw)
            except (TypeError, ValueError):
                return cfg

        def get(key: str, default: Any) -> Any:
            # AstrBot 可能把配置包在 "config" / 插件名层级里，做一次浅兼容
            if key in raw:
                return raw.get(key)
            for wrapper in ("config", "options", "settings"):
                inner = raw.get(wrapper)
                if isinstance(inner, dict) and key in inner:
                    return inner.get(key)
            return default

        cfg.llm_enabled = _as_bool(get("llm_enabled", cfg.llm_enabled), cfg.llm_enabled)
        cfg.llm_timeout_sec = _as_int(get("llm_timeout_sec", cfg.llm_timeout_sec), cfg.llm_timeout_sec, 1, 60)
        cfg.render_font_path = str(get("render_font_path", cfg.render_font_path) or "").strip()
        cfg.image_cache_size = _as_int(get("image_cache_size", cfg.image_cache_size), cfg.image_cache_size, 0, 128)
        cfg.image_cache_ttl_sec = _as_int(get("image_cache_ttl_sec", cfg.image_cache_ttl_sec), cfg.image_cache_ttl_sec, 0, 3600)
        cfg.render_concurrency = _as_int(get("render_concurrency", cfg.render_concurrency), cfg.render_concurrency, 1, 4)
        cfg.command_debounce_ms = _as_int(
            get("command_debounce_ms", cfg.command_debounce_ms), cfg.command_debounce_ms, 0, 10000
        )
        cfg.db_path = str(get("db_path", cfg.db_path) or "").strip()
        cfg.fish_bag_limit = _as_int(get("fish_bag_limit", cfg.fish_bag_limit), cfg.fish_bag_limit, 20, C.FISH_BAG_HARD_LIMIT)
        cfg.checkin_base_coins = _as_int(
            get("checkin_base_coins", cfg.checkin_base_coins), cfg.checkin_base_coins, 0, 1000
        )
        cfg.checkin_streak_step = _as_int(
            get("checkin_streak_step", cfg.checkin_streak_step), cfg.checkin_streak_step, 0, 1000
        )
        cfg.checkin_streak_cap = _as_int(
            get("checkin_streak_cap", cfg.checkin_streak_cap), cfg.checkin_streak_cap, 1, 60
        )
        cfg.work_cooldown_min = _as_int(
            get("work_cooldown_min", cfg.work_cooldown_min), cfg.work_cooldown_min, 1, 1440
        )
        cfg.work_cooldown_max = _as_int(
            get("work_cooldown_max", cfg.work_cooldown_max), cfg.work_cooldown_max, 1, 1440
        )
        # 旧版默认值是 20~180，升级后配置文件里会残留；按新规则迁移成 1 小时~24 小时
        if cfg.work_cooldown_min == 20 and cfg.work_cooldown_max == 180:
            cfg.work_cooldown_min, cfg.work_cooldown_max = 60, 1440
        if cfg.work_cooldown_min < 60:
            cfg.work_cooldown_min = 60
        if cfg.work_cooldown_max < cfg.work_cooldown_min:
            cfg.work_cooldown_min, cfg.work_cooldown_max = cfg.work_cooldown_max, cfg.work_cooldown_min
        cfg.fish_wait_min = _as_int(get("fish_wait_min", cfg.fish_wait_min), cfg.fish_wait_min, 1, 180)
        cfg.fish_wait_max = _as_int(get("fish_wait_max", cfg.fish_wait_max), cfg.fish_wait_max, 1, 180)
        if cfg.fish_wait_max < cfg.fish_wait_min:
            cfg.fish_wait_min, cfg.fish_wait_max = cfg.fish_wait_max, cfg.fish_wait_min

        cfg.marry_daily_clear = _as_bool(
            get("marry_daily_clear", cfg.marry_daily_clear), cfg.marry_daily_clear
        )
        cfg.garden_sun_start = _as_int(
            get("garden_sun_start", cfg.garden_sun_start), cfg.garden_sun_start, 0, 23
        )
        cfg.garden_sun_end = _as_int(
            get("garden_sun_end", cfg.garden_sun_end), cfg.garden_sun_end, 1, 24
        )
        if cfg.garden_sun_end <= cfg.garden_sun_start:
            cfg.garden_sun_start, cfg.garden_sun_end = C.GARDEN_SUN_START_HOUR, C.GARDEN_SUN_END_HOUR
        cfg.garden_timezone = (
            str(get("garden_timezone", cfg.garden_timezone) or "").strip() or C.GARDEN_TIMEZONE
        )

        groups = get("enabled_groups", cfg.enabled_groups)
        if isinstance(groups, str):
            groups = [g.strip() for g in groups.replace("，", ",").split(",") if g.strip()]
        elif isinstance(groups, (list, tuple)):
            groups = [str(g).strip() for g in groups if str(g).strip()]
        else:
            groups = []
        cfg.enabled_groups = groups
        return cfg

    # ------------------------------------------------------------ 工具
    def group_enabled(self, group_id: str) -> bool:
        if not self.enabled_groups:
            return True
        return str(group_id) in self.enabled_groups

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def safe_summary(self) -> str:
        return (
            f"llm={self.llm_enabled} cache={self.image_cache_size} "
            f"render={self.render_concurrency} debounce={self.command_debounce_ms}ms"
        )