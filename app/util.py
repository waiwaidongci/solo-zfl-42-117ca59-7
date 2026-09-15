"""时间工具：统一使用 UTC、ISO-8601（秒级，尾缀 Z），便于比较与测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def add_months_iso(value: str, months: int) -> str:
    """在给定 ISO 时间上增加若干个公历月，保持时区与秒级精度。"""
    dt = parse_iso(value)
    month_index = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    # 处理 31 日落到短月的情况（如 1/31 + 1 月）。
    day = dt.day
    while True:
        try:
            moved = dt.replace(year=year, month=month, day=day)
            break
        except ValueError:
            day -= 1
    return moved.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_expired(valid_until: str, at: str | None = None) -> bool:
    return valid_until <= (at or now_iso())
