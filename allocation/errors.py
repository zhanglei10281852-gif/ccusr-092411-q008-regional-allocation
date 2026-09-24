"""领域错误与错误码。"""
from __future__ import annotations


class AllocationError(Exception):
    """所有领域规则冲突的基类。"""

    code = "domain_error"


class ValidationFailed(AllocationError):
    code = "validation_failed"


class RoundClosed(AllocationError):
    """常规轮次已结束：紧急需求只能走保留池或授权挤占。"""

    code = "round_closed"


class ReserveExhausted(AllocationError):
    code = "reserve_exhausted"


class NothingToDivert(AllocationError):
    code = "nothing_to_divert"


class PlanNotUsable(AllocationError):
    code = "plan_not_usable"


class TransferStateConflict(AllocationError):
    """确认/装运/到达顺序被破坏，或数量倒退。"""

    code = "state_conflict"


class ReceiptLate(AllocationError):
    """迟到回执：按真实发生时间归档，但不允许令状态倒退。"""

    code = "receipt_late"


class QuotaAlreadyDeducted(AllocationError):
    """幂等命令重复到达：配额只扣一次。"""

    code = "idempotent_replay"


class ApprovalConflict(AllocationError):
    code = "approval_conflict"


class SimulationConflict(AllocationError):
    code = "simulation_conflict"


class ReconciliationMismatch(AllocationError):
    code = "reconciliation_mismatch"
