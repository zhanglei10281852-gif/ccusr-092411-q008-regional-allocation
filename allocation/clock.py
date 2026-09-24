"""时间策略：领域合同要求 ISO 8601 且必须携带时区。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

CN_TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """系统时钟，默认按北京时间（+08:00）盖时间戳。"""

    def __init__(self, tz: timezone = CN_TZ) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    """测试用固定时钟。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("时间必须携带时区")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, hours: float = 0, minutes: float = 0) -> None:
        self._moment += timedelta(hours=hours, minutes=minutes)


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("时间必须携带时区（ISO 8601 with timezone）")
    return moment


def format_time(moment: datetime) -> str:
    return moment.astimezone().isoformat()
