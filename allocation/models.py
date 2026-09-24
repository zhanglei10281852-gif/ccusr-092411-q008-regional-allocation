"""领域模型：区域、轮次、保留池、方案、调拨、在途、补偿、审批。

数量字段统一为吨（float），时间统一为带时区的 datetime。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .clock import parse_time


class RoundStatus(str, Enum):
    OPEN = "open"              # 常规轮次进行中，接受申请
    CLOSED = "closed"          # 常规轮次结束，只接受紧急需求
    FINALIZED = "finalized"    # 所有需求（含紧急）处理完毕，轮次封账


class PlanStatus(str, Enum):
    SIMULATED = "simulated"    # 隔离模拟，不占用运营态资源
    DRAFT = "draft"            # 正式方案草案，等待批准链
    PUBLISHED = "published"    # 已发布，可确认发运
    SUPERSEDED = "superseded"  # 被更新版本替代


class TransferStatus(str, Enum):
    PLANNED = "planned"
    CONFIRMED = "confirmed"
    DISPATCHED = "dispatched"
    ARRIVED = "arrived"
    COMPENSATING = "compensating"  # 额度曾被挤占，等待补偿清偿
    CANCELLED = "cancelled"


# 状态只能沿此顺序前进；迟到回执不能令状态倒退。
STATUS_ORDER: dict[TransferStatus, int] = {
    TransferStatus.PLANNED: 0,
    TransferStatus.CONFIRMED: 1,
    TransferStatus.DISPATCHED: 2,
    TransferStatus.ARRIVED: 3,
    TransferStatus.COMPENSATING: 2,  # 与发运同级：货已在途，只是挂补偿
    TransferStatus.CANCELLED: 99,
}


class CompensationStatus(str, Enum):
    PENDING = "pending"      # 已排队，等待保留池补充
    SETTLED = "settled"      # 已补偿
    CANCELLED = "cancelled"


@dataclass
class Region:
    region_id: str
    name: str
    baseline: float                       # 民生底线（净重）
    priority: int = 100                   # 同档排序用，越小越优先
    historical_share: float = 0.0         # 历史兑现份额（吨），用于第三轮配平
    active: bool = True


@dataclass
class RequestLine:
    """客户加急申请（去重后的有效需求行）。"""

    customer_id: str
    region_id: str
    contract_qty: float        # 合同约定量
    urgent_qty: float          # 本次加急申请量
    emergency: bool = False    # 常规轮次结束后到达 = 紧急需求
    transit_before_deadline: float = 0.0  # 截止线前可到达的在途量
    loss_rate: float = 0.0     # 本线预估损耗率
    lead_time_h: float = 0.0   # 运输时效（小时）


@dataclass
class InTransit:
    """在途补给：尚未到达但预计在截止线前抵达的货源。"""

    source: str
    region_id: str
    qty: float
    eta: datetime
    counted: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "InTransit":
        return cls(
            source=data["source"],
            region_id=data["region_id"],
            qty=float(data["qty"]),
            eta=parse_time(data["eta"]),
            counted=bool(data.get("counted", False)),
        )


@dataclass
class PlanLine:
    region_id: str
    customer_id: str
    loss_rate: float = 0.0
    baseline_qty: float = 0.0   # 第一轮：底线保障（净重）
    contract_qty: float = 0.0   # 第二轮：合同满足（净重）
    share_qty: float = 0.0      # 第三轮：历史份额配平（净重）
    emergency_qty: float = 0.0  # 保留池/挤占满足的紧急量（净重）

    @property
    def regular_net(self) -> float:
        return self.baseline_qty + self.contract_qty + self.share_qty

    @property
    def net_total(self) -> float:
        return self.regular_net + self.emergency_qty


@dataclass
class Plan:
    plan_id: str
    round_id: str
    version: int
    status: PlanStatus
    supply: float                       # 本次可供货源（毛重口径的总池）
    reserve: float                      # 锁定的保留池数量
    deadline: datetime                  # 民生保障截止线
    lines: dict[str, PlanLine] = field(default_factory=dict)
    simulation_of: str | None = None    # 若为模拟版，记录缩减场景名
    supersedes: str | None = None
    required_approvals: int = 2         # 批准链条数
    rejected: bool = False
    created_at: datetime | None = None
    published_at: datetime | None = None

    def all_lines(self) -> list[PlanLine]:
        return list(self.lines.values())

    def region_net(self, region_id: str) -> float:
        return sum(
            line.net_total for line in self.lines.values() if line.region_id == region_id
        )


@dataclass
class Round:
    round_id: str
    opened_at: datetime
    deadline: datetime
    status: RoundStatus = RoundStatus.OPEN
    supply: float = 0.0
    reserve: float = 0.0
    active_plan_id: str | None = None
    closed_at: datetime | None = None
    finalized_at: datetime | None = None
    regions: dict[str, Region] = field(default_factory=dict)
    requests: list[RequestLine] = field(default_factory=list)
    in_transit: list[InTransit] = field(default_factory=list)
    simulations: dict[str, str] = field(default_factory=dict)  # 场景 -> 模拟方案id
    reserve_used_gross: float = 0.0     # 紧急需求已动用的保留池（毛额）
    reserve_added_gross: float = 0.0    # 保留池累计补充（毛额）

    @property
    def reserve_balance_gross(self) -> float:
        return round(self.reserve + self.reserve_added_gross - self.reserve_used_gross, 6)


@dataclass
class Transfer:
    transfer_id: str
    round_id: str
    plan_id: str
    region_id: str
    customer_id: str
    gross_qty: float                 # 发运毛额（含损耗补偿）
    net_qty: float                   # 计入配额的净额
    loss_rate: float
    kind: str = "regular"            # regular | emergency | compensation
    status: TransferStatus = TransferStatus.PLANNED
    created_at: datetime | None = None
    confirmed_at: datetime | None = None
    dispatched_at: datetime | None = None
    arrived_at: datetime | None = None
    # 回执数量（物理毛吨）：同一阶段可补报，只许单调不减；迟到回执归入真实发生时间。
    confirmed_qty: float = 0.0
    dispatched_qty: float = 0.0
    arrived_qty: float = 0.0
    diverted_gross: float = 0.0      # 被紧急需求挤占的物理毛吨（仅限未发运额度）
    compensation_id: str | None = None
    source: dict | None = None       # 紧急/补偿调拨的来源追溯

    @property
    def effective_gross(self) -> float:
        """挤占后仍归属本客户的毛额。"""
        return round(self.gross_qty - self.diverted_gross, 6)

    @property
    def divertable_gross(self) -> float:
        """可被授权挤占的额度：未发运、且尚未被挤占的部分。"""
        return max(0.0, round(self.gross_qty - self.diverted_gross - self.dispatched_qty, 6))

    @property
    def shipped_qty(self) -> float:
        """已实际发运数量（不可再被挤占）。"""
        return self.dispatched_qty


@dataclass
class Compensation:
    compensation_id: str
    round_id: str
    donor_transfer_id: str
    donor_region_id: str
    donor_customer_id: str
    qty: float
    reason: str
    priority_score: float            # 越大越优先
    rank: int = 0
    status: CompensationStatus = CompensationStatus.PENDING
    settled_qty: float = 0.0         # 已补偿毛吨（允许分批部分清偿）
    created_at: datetime | None = None
    settled_at: datetime | None = None
    settled_from: str | None = None  # 清偿来源（保留池补充批次等）


@dataclass
class Approval:
    plan_id: str
    level: int
    approver: str
    decided_at: datetime
    approved: bool
    comment: str = ""


@dataclass
class CommandRecord:
    """幂等记录：同一 command_id 的重试直接回放首次结果，不再扣配额。"""

    command_id: str
    command_type: str
    result_type: str
    result_id: str
    accepted_at: datetime
