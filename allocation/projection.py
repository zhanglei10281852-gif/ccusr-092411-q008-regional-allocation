"""事件投影：把不可变事件日志重放成当前运营态。

进程重启后只需重新加载事件日志，未决轮次、批准链、保留池台账、补偿队列
全部恢复，不需要额外的快照文件（事件即事实）。
"""
from __future__ import annotations

from datetime import datetime

from .clock import parse_time
from .models import (
    Approval,
    Compensation,
    CompensationStatus,
    Plan,
    PlanLine,
    PlanStatus,
    Region,
    RequestLine,
    Round,
    RoundStatus,
    Transfer,
    TransferStatus,
)
from .models import InTransit


class State:
    def __init__(self) -> None:
        self.rounds: dict[str, Round] = {}
        self.regions: dict[str, Region] = {}
        self.requests: dict[str, list[RequestLine]] = {}
        self.plans: dict[str, Plan] = {}
        self.transfers: dict[str, Transfer] = {}
        self.compensations: dict[str, Compensation] = {}
        self.approvals: dict[str, list[Approval]] = {}

    # -- 查询辅助 ----------------------------------------------------------

    def round_active_plan(self, round_id: str) -> Plan | None:
        active = self.rounds[round_id].active_plan_id
        return None if active is None else self.plans[active]

    def round_transfers(self, round_id: str, *, include_cancelled: bool = False) -> list[Transfer]:
        result = [t for t in self.transfers.values() if t.round_id == round_id]
        if not include_cancelled:
            result = [t for t in result if t.status != TransferStatus.CANCELLED]
        return sorted(result, key=lambda t: t.transfer_id)

    def plan_transfers(self, plan_id: str) -> list[Transfer]:
        return sorted(
            (t for t in self.transfers.values() if t.plan_id == plan_id),
            key=lambda t: t.transfer_id,
        )

    def compensation_queue(self, round_id: str) -> list[Compensation]:
        pending = [
            c for c in self.compensations.values()
            if c.round_id == round_id and c.status == CompensationStatus.PENDING
        ]
        pending.sort(key=lambda c: (-c.priority_score, c.created_at, c.compensation_id))
        for index, comp in enumerate(pending, start=1):
            comp.rank = index
        return pending

    def arrived_net_by_region(self, round_id: str) -> dict[str, float]:
        totals: dict[str, float] = {}
        for transfer in self.round_transfers(round_id):
            if transfer.arrived_qty > 0:
                net = transfer.arrived_qty * (1.0 - transfer.loss_rate)
                totals[transfer.region_id] = totals.get(transfer.region_id, 0.0) + net
        return {k: round(v, 6) for k, v in totals.items()}


def _line_key(region_id: str, customer_id: str) -> str:
    return f"{region_id}|{customer_id}"


def replay(events: list[dict]) -> State:
    state = State()
    for event in events:
        _apply(state, event)
    return state


def _apply(state: State, event: dict) -> None:
    etype = event["event_type"]
    p = event["payload"]
    at = parse_time(event["occurred_at"])
    handler = _HANDLERS.get(etype)
    if handler is not None:
        handler(state, p, at)


def _on_region_registered(s: State, p: dict, at: datetime) -> None:
    s.regions[p["region_id"]] = Region(
        region_id=p["region_id"],
        name=p["name"],
        baseline=float(p["baseline"]),
        priority=int(p.get("priority", 100)),
        historical_share=float(p.get("historical_share", 0.0)),
    )


def _on_round_opened(s: State, p: dict, at: datetime) -> None:
    round_id = p["round_id"]
    s.rounds[round_id] = Round(
        round_id=round_id,
        opened_at=at,
        deadline=parse_time(p["deadline"]),
        status=RoundStatus.OPEN,
        supply=float(p["supply"]),
        reserve=float(p["reserve"]),
        regions=dict(s.regions),
    )


def _on_request_submitted(s: State, p: dict, at: datetime) -> None:
    rnd = s.rounds[p["round_id"]]
    data = p["request"]
    line = RequestLine(
        customer_id=data["customer_id"],
        region_id=data["region_id"],
        contract_qty=float(data["contract_qty"]),
        urgent_qty=float(data["urgent_qty"]),
        emergency=bool(p.get("emergency", False)),
        transit_before_deadline=float(data.get("transit_before_deadline", 0.0)),
        loss_rate=float(data.get("loss_rate", 0.0)),
        lead_time_h=float(data.get("lead_time_h", 0.0)),
    )
    rnd.requests.append(line)
    s.requests.setdefault(p["round_id"], []).append(line)


def _on_intransit_registered(s: State, p: dict, at: datetime) -> None:
    rnd = s.rounds[p["round_id"]]
    item = InTransit(
        source=p["source"],
        region_id=p["region_id"],
        qty=float(p["qty"]),
        eta=parse_time(p["eta"]),
    )
    item.counted = item.eta <= rnd.deadline
    rnd.in_transit.append(item)


def _on_round_closed(s: State, p: dict, at: datetime) -> None:
    rnd = s.rounds[p["round_id"]]
    rnd.status = RoundStatus.CLOSED
    rnd.closed_at = at


def _on_round_finalized(s: State, p: dict, at: datetime) -> None:
    rnd = s.rounds[p["round_id"]]
    rnd.status = RoundStatus.FINALIZED
    rnd.finalized_at = at


def _restore_plan(p: dict) -> Plan:
    lines = {
        key: PlanLine(
            region_id=val["region_id"],
            customer_id=val["customer_id"],
            loss_rate=float(val.get("loss_rate", 0.0)),
            baseline_qty=float(val.get("baseline_qty", 0.0)),
            contract_qty=float(val.get("contract_qty", 0.0)),
            share_qty=float(val.get("share_qty", 0.0)),
            emergency_qty=float(val.get("emergency_qty", 0.0)),
        )
        for key, val in p.get("lines", {}).items()
    }
    return Plan(
        plan_id=p["plan_id"],
        round_id=p["round_id"],
        version=int(p.get("version", 0)),
        status=PlanStatus(p["status"]),
        supply=float(p["supply"]),
        reserve=float(p.get("reserve", 0.0)),
        deadline=parse_time(p["deadline"]),
        lines=lines,
        simulation_of=p.get("simulation_of"),
        supersedes=p.get("supersedes"),
        required_approvals=int(p.get("required_approvals", 2)),
        rejected=bool(p.get("rejected", False)),
        created_at=parse_time(p["created_at"]) if p.get("created_at") else None,
        published_at=parse_time(p["published_at"]) if p.get("published_at") else None,
    )


def _plan_payload(plan: Plan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "round_id": plan.round_id,
        "version": plan.version,
        "status": plan.status.value,
        "supply": plan.supply,
        "reserve": plan.reserve,
        "deadline": plan.deadline.isoformat(),
        "simulation_of": plan.simulation_of,
        "supersedes": plan.supersedes,
        "required_approvals": plan.required_approvals,
        "rejected": plan.rejected,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "published_at": plan.published_at.isoformat() if plan.published_at else None,
        "lines": {
            key: {
                "region_id": line.region_id,
                "customer_id": line.customer_id,
                "loss_rate": line.loss_rate,
                "baseline_qty": line.baseline_qty,
                "contract_qty": line.contract_qty,
                "share_qty": line.share_qty,
                "emergency_qty": line.emergency_qty,
            }
            for key, line in plan.lines.items()
        },
    }


def _on_plan_created(s: State, p: dict, at: datetime) -> None:
    plan = _restore_plan(p)
    s.plans[plan.plan_id] = plan
    if plan.simulation_of is not None:
        s.rounds[plan.round_id].simulations[plan.simulation_of] = plan.plan_id


def _on_plan_rejected(s: State, p: dict, at: datetime) -> None:
    plan = s.plans[p["plan_id"]]
    plan.rejected = True


def _on_plan_published(s: State, p: dict, at: datetime) -> None:
    plan = _restore_plan(p)
    plan.status = PlanStatus.PUBLISHED
    plan.published_at = at
    s.plans[plan.plan_id] = plan
    rnd = s.rounds[plan.round_id]
    if rnd.active_plan_id and rnd.active_plan_id != plan.plan_id:
        previous = s.plans.get(rnd.active_plan_id)
        if previous is not None:
            previous.status = PlanStatus.SUPERSEDED
    rnd.active_plan_id = plan.plan_id


def _on_transfer_created(s: State, p: dict, at: datetime) -> None:
    transfer = Transfer(
        transfer_id=p["transfer_id"],
        round_id=p["round_id"],
        plan_id=p["plan_id"],
        region_id=p["region_id"],
        customer_id=p["customer_id"],
        gross_qty=float(p["gross_qty"]),
        net_qty=float(p["net_qty"]),
        loss_rate=float(p.get("loss_rate", 0.0)),
        kind=p.get("kind", "regular"),
        status=TransferStatus.PLANNED,
        created_at=at,
        source=p.get("source"),
    )
    s.transfers[transfer.transfer_id] = transfer
    if transfer.kind in ("emergency", "regular"):
        plan = s.plans.get(transfer.plan_id)
        if plan is not None:
            key = _line_key(transfer.region_id, transfer.customer_id)
            line = plan.lines.get(key)
            if line is None:
                line = PlanLine(transfer.region_id, transfer.customer_id, transfer.loss_rate)
                plan.lines[key] = line
            if transfer.kind == "emergency":
                line.emergency_qty = round(line.emergency_qty + transfer.net_qty, 6)


def _on_transfer_cancelled(s: State, p: dict, at: datetime) -> None:
    transfer = s.transfers[p["transfer_id"]]
    transfer.status = TransferStatus.CANCELLED


def _recompute_lifecycle(transfer: Transfer) -> None:
    """生命周期阶段由回执事实推导；无任何回执但被挤占过才挂 compensating。"""
    if transfer.status == TransferStatus.CANCELLED:
        return
    if transfer.arrived_qty > 1e-6:
        transfer.status = TransferStatus.ARRIVED
    elif transfer.dispatched_qty > 1e-6:
        transfer.status = TransferStatus.DISPATCHED
    elif transfer.confirmed_qty > 1e-6:
        transfer.status = TransferStatus.CONFIRMED
    elif transfer.diverted_gross > 1e-6:
        transfer.status = TransferStatus.COMPENSATING
    else:
        transfer.status = TransferStatus.PLANNED


def _on_transfer_diverted(s: State, p: dict, at: datetime) -> None:
    transfer = s.transfers[p["transfer_id"]]
    qty = float(p["qty_gross"])
    transfer.diverted_gross = round(transfer.diverted_gross + qty, 6)
    comp_id = p["compensation_id"]
    existing = transfer.source or {}
    comps = list(existing.get("compensation_ids", ()))
    if transfer.compensation_id and transfer.compensation_id not in comps:
        comps.append(transfer.compensation_id)
    comps.append(comp_id)
    transfer.compensation_id = comp_id
    transfer.source = {**existing, "compensation_ids": comps}
    _recompute_lifecycle(transfer)


def _on_compensation_created(s: State, p: dict, at: datetime) -> None:
    comp = Compensation(
        compensation_id=p["compensation_id"],
        round_id=p["round_id"],
        donor_transfer_id=p["donor_transfer_id"],
        donor_region_id=p["donor_region_id"],
        donor_customer_id=p["donor_customer_id"],
        qty=float(p["qty"]),
        reason=p["reason"],
        priority_score=float(p["priority_score"]),
        rank=int(p.get("rank", 0)),
        created_at=at,
    )
    s.compensations[comp.compensation_id] = comp


def _on_compensation_settled(s: State, p: dict, at: datetime) -> None:
    comp = s.compensations[p["compensation_id"]]
    comp.settled_qty = round(comp.settled_qty + float(p["qty_settled"]), 6)
    comp.settled_from = p.get("settled_from")
    if p.get("fully", comp.settled_qty + 1e-6 >= comp.qty):
        comp.status = CompensationStatus.SETTLED
        comp.settled_at = at
    # 部分清偿：保持 PENDING，继续占用补偿队列中的优先级位次。


def _on_reserve_used(s: State, p: dict, at: datetime) -> None:
    s.rounds[p["round_id"]].reserve_used_gross = round(
        s.rounds[p["round_id"]].reserve_used_gross + float(p["qty_gross"]), 6
    )


def _on_reserve_refunded(s: State, p: dict, at: datetime) -> None:
    s.rounds[p["round_id"]].reserve_used_gross = round(
        s.rounds[p["round_id"]].reserve_used_gross - float(p["qty_gross"]), 6
    )


def _on_reserve_replenished(s: State, p: dict, at: datetime) -> None:
    s.rounds[p["round_id"]].reserve_added_gross = round(
        s.rounds[p["round_id"]].reserve_added_gross + float(p["qty_gross"]), 6
    )


def _on_approval(s: State, p: dict, at: datetime) -> None:
    s.approvals.setdefault(p["plan_id"], []).append(
        Approval(
            plan_id=p["plan_id"],
            level=int(p["level"]),
            approver=p["approver"],
            decided_at=at,
            approved=bool(p["approved"]),
            comment=p.get("comment", ""),
        )
    )



def _on_receipt(s: State, p: dict, at: datetime) -> None:
    transfer = s.transfers[p["transfer_id"]]
    if transfer.status == TransferStatus.CANCELLED:
        return
    stage = p["stage"]
    qty = float(p["qty"])
    happened_at = parse_time(p["happened_at"])
    if stage == "confirmed":
        transfer.confirmed_qty = max(transfer.confirmed_qty, qty)
        if transfer.confirmed_at is None or happened_at < transfer.confirmed_at:
            transfer.confirmed_at = happened_at
    elif stage == "dispatched":
        transfer.dispatched_qty = max(transfer.dispatched_qty, qty)
        if transfer.dispatched_at is None or happened_at < transfer.dispatched_at:
            transfer.dispatched_at = happened_at
    else:
        transfer.arrived_qty = max(transfer.arrived_qty, qty)
        if transfer.arrived_at is None or happened_at < transfer.arrived_at:
            transfer.arrived_at = happened_at
    # 状态按回执事实重算：确认->装运->到达 单调推进，迟到回执不令状态倒退。
    _recompute_lifecycle(transfer)


_HANDLERS = {
    "region.registered": _on_region_registered,
    "round.opened": _on_round_opened,
    "request.submitted": _on_request_submitted,
    "intransit.registered": _on_intransit_registered,
    "round.closed": _on_round_closed,
    "round.finalized": _on_round_finalized,
    "allocation.simulated": _on_plan_created,
    "allocation.drafted": _on_plan_created,
    "allocation.approved": _on_approval,
    "allocation.rejected": lambda s, p, at: (_on_approval(s, {**p, "approved": False}, at),
                                             _on_plan_rejected(s, p, at)),
    "allocation.published": _on_plan_published,
    "transfer.created": _on_transfer_created,
    "transfer.cancelled": _on_transfer_cancelled,
    "transfer.diverted": _on_transfer_diverted,
    "compensation.created": _on_compensation_created,
    "compensation.settled": _on_compensation_settled,
    "reserve.used": _on_reserve_used,
    "reserve.refunded": _on_reserve_refunded,
    "reserve.replenished": _on_reserve_replenished,
    "receipt.recorded": _on_receipt,
}
