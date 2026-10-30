"""时间工具 —— 惰性每日重置的核心。

设计要点：
    游戏日以本地时间凌晨 4:00 为分界，`day_key()` 是唯一的"重置开关"。
    所有玩法在读/写数据时对比 `user.day_key` / `row.day_key`，不一致就就地结算。
    因此进程里 **没有任何定时器 / 轮询协程**，休眠一个月再启动也不会漏重置。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .constants import RESET_HOUR


def now_ts() -> int:
    return int(time.time())


def now_dt() -> datetime:
    return datetime.now()


def day_key(ts: int | None = None) -> str:
    """返回游戏日标识，形如 ``2026-10-24``（凌晨 4 点前算前一天）。"""
    if ts is None:
        dt = now_dt()
    else:
        dt = datetime.fromtimestamp(ts)
    if dt.hour < RESET_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def day_index(key: str) -> int:
    """把 ``YYYY-MM-DD`` 转成整数天序号，用于连续天数比较。"""
    try:
        d = datetime.strptime(key, "%Y-%m-%d")
    except (ValueError, TypeError):
        return -1
    return d.toordinal()


def days_between(old_key: str, new_key: str) -> int:
    """两个 day_key 之间相隔的自然天数（可能为负）。"""
    return day_index(new_key) - day_index(old_key)


def is_same_day(a: str, b: str) -> bool:
    return bool(a) and a == b


def next_reset_ts(ts: int | None = None) -> int:
    """下一次凌晨 4 点的时间戳（纯计算，不注册任何任务）。"""
    dt = now_dt() if ts is None else datetime.fromtimestamp(ts)
    target = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt >= target:
        target = target + timedelta(days=1)
    return int(target.timestamp())


def seconds_to_reset(ts: int | None = None) -> int:
    base = now_ts() if ts is None else ts
    return max(0, next_reset_ts(base) - base)


def fmt_duration(seconds: int | float) -> str:
    """把秒数格式化成人类可读的中文时长。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def fmt_date(key: str) -> str:
    """``2026-10-24`` -> ``10月24日``。"""
    try:
        d = datetime.strptime(key, "%Y-%m-%d")
    except (ValueError, TypeError):
        return key or "-"
    return f"{d.month}月{d.day}日"