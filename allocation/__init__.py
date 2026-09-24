"""跨区域保供调拨系统。

事件溯源的领域内核：所有状态变更都是不可变事件，服务随时可从事件日志重放恢复。
"""
from __future__ import annotations

from .clock import Clock, SystemClock, parse_time
from .models import (
    Approval,
    Compensation,
    InTransit,
    Plan,
    PlanLine,
    Region,
    RequestLine,
    Round,
    Transfer,
)
from .service import AllocationService

__all__ = [
    "AllocationService",
    "Approval",
    "Clock",
    "Compensation",
    "InTransit",
    "Plan",
    "PlanLine",
    "Region",
    "RequestLine",
    "Round",
    "SystemClock",
    "Transfer",
    "parse_time",
]
