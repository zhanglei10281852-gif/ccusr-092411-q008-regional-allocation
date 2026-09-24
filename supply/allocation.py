"""分轮保供分配算法（纯函数，无副作用）。

三轮决策，依次消耗可分货源：

1. 底线轮：先补齐每个区域的民生底线。在途补给视为即将到货，冲减缺口；
   损耗率把"到货量"换算成"发运量"；运输时效越慢的区域越先拿货，
   避免远途区域在货源耗尽时连底线都无法满足。
2. 合同轮：按客户合同保量缺口分配，缺口大者优先。
3. 份额轮：剩余货源按历史兑现份额加权分配，并在需求封顶后把余量
   再分给仍有需求的方，直到货源耗尽或全部满足。

所有数量均为发运量（毛量）；净到货量 = 发运量 * (1 - loss_rate)。
区域底线与在途补给以净到货量计。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .errors import AllocationError, AuthorizationRequired

EPSILON = 1e-9


@dataclass(frozen=True)
class Lane:
    region: str
    floor: float = 0.0                 # 民生底线（净到货量）
    loss_rate: float = 0.0            # 运输损耗率，0~1
    transit_days: float = 0.0         # 运输时效，越大越慢
    historical_share: float = 0.0     # 历史兑现份额权重


@dataclass(frozen=True)
class Request:
    customer: str
    region: str
    quantity: float                    # 申报需求量（发运量）
    contract_minimum: float = 0.0      # 合同保量（净到货量）
    urgent: bool = False


@dataclass
class Allocation:
    customer: str
    region: str
    quantity: float = 0.0              # 总分得发运量
    by_round: dict[str, float] = field(default_factory=dict)

    def add(self, round_name: str, qty: float) -> None:
        if qty <= EPSILON:
            return
        self.by_round[round_name] = self.by_round.get(round_name, 0.0) + qty
        self.quantity += qty


@dataclass
class Plan:
    allocations: dict[str, Allocation]
    pool: float                        # 本轮可分货源（不含保留池）
    reserve: float                     # 保留池
    used: float
    floor_shortfall: dict[str, float]  # 底线仍未覆盖的净到货缺口
    unmet: dict[str, float]            # 客户未满足的申报需求

    @property
    def leftover(self) -> float:
        return self.pool - self.used


def _net(qty_shipped: float, loss_rate: float) -> float:
    return qty_shipped * (1.0 - loss_rate)


def _gross(qty_net: float, loss_rate: float) -> float:
    if qty_net <= EPSILON:
        return 0.0
    factor = max(EPSILON, 1.0 - loss_rate)
    return qty_net / factor


def proportional_allocate(pool: float, claimants: Sequence[tuple[str, float, float]]) -> dict[str, float]:
    """按权重在封顶需求内分配 pool。

    claimants: (key, weight, max_qty)。达到封顶的 claimant 退出，
    余量按剩余权重再分，直到分完或全部封顶。返回 key -> 数量。
    """
    result = {key: 0.0 for key, _, _ in claimants}
    active = {key: [max(weight, 0.0), cap] for key, weight, cap in claimants if cap > EPSILON}
    remaining = pool
    while remaining > EPSILON and active:
        total_weight = sum(w for w, _ in active.values())
        if total_weight <= EPSILON:
            # 无权重信息时平均分给仍有需求者
            weighted = {key: remaining / len(active) for key in active}
        else:
            weighted = {key: remaining * w / total_weight for key, (w, _) in active.items()}
        for key, give in list(weighted.items()):
            weight, cap = active[key]
            if give + EPSILON >= cap:
                result[key] += cap
                remaining -= cap
                del active[key]
            else:
                result[key] += give
                remaining -= give
                active[key][1] = cap - give
    return result


def plan_rounds(
    supply: float,
    lanes: Iterable[Lane],
    requests: Iterable[Request],
    in_transit: dict[str, float] | None = None,
    reserve: float = 0.0,
) -> Plan:
    """执行一轮常规分轮分配。

    supply 为总货源，其中 reserve 进入保留池不参与常规分配；
    in_transit 以区域为键、净到货量为值，表示在途补给。
    """
    if supply < -EPSILON:
        raise ValueError("货源不能为负")
    if reserve < -EPSILON or reserve > supply + EPSILON:
        raise ValueError("保留池必须介于 0 与总货源之间")
    in_transit = dict(in_transit or {})
    lanes_by_region = {lane.region: lane for lane in lanes}
    pool = supply - reserve

    reqs = sorted(requests, key=lambda r: (r.region, r.customer))
    for req in reqs:
        if req.quantity < -EPSILON or req.contract_minimum < -EPSILON:
            raise ValueError(f"需求与合同保量不能为负：{req.customer}")
        if req.region not in lanes_by_region:
            raise ValueError(f"请求的区域未登记：{req.region}")

    allocations = {
        req.customer: Allocation(req.customer, req.region)
        for req in reqs
    }
    remaining_by_customer = {req.customer: max(0.0, req.quantity) for req in reqs}
    used = 0.0
    floor_shortfall: dict[str, float] = {}

    # ---- 第一轮：区域民生底线（在途冲减，远途优先）----
    region_order = sorted(
        lanes_by_region.values(),
        key=lambda lane: (-lane.transit_days, lane.region),
    )
    for lane in region_order:
        net_intransit = max(0.0, in_transit.get(lane.region, 0.0))
        net_deficit = max(0.0, lane.floor - net_intransit)
        if net_deficit <= EPSILON or pool - used <= EPSILON:
            floor_shortfall[lane.region] = net_deficit
            continue
        used_before = used
        needed_gross = _gross(net_deficit, lane.loss_rate)
        region_reqs = [r for r in reqs if r.region == lane.region and remaining_by_customer[r.customer] > EPSILON]
        if not region_reqs:
            floor_shortfall[lane.region] = net_deficit
            continue
        # 底线货量在区域内按申报需求比例落到客户，受申报量封顶
        total_demand = sum(remaining_by_customer[r.customer] for r in region_reqs)
        claimants = [
            (r.customer, remaining_by_customer[r.customer],
             min(remaining_by_customer[r.customer], needed_gross * remaining_by_customer[r.customer] / total_demand))
            for r in region_reqs
        ] if total_demand > EPSILON else []
        give_map = proportional_allocate(min(needed_gross, pool - used), claimants)
        for customer, give in give_map.items():
            allocations[customer].add("floor", give)
            remaining_by_customer[customer] -= give
            used += give
        covered_net = _net(used - used_before, lane.loss_rate)
        floor_shortfall[lane.region] = max(0.0, net_deficit - covered_net)

    # ---- 第二轮：客户合同保量 ----
    def contract_gap(req: Request) -> float:
        lane = lanes_by_region[req.region]
        got_net = _net(allocations[req.customer].quantity, lane.loss_rate)
        return max(0.0, req.contract_minimum - got_net)

    contract_order = sorted(reqs, key=lambda r: (-contract_gap(r), r.region, r.customer))
    for req in contract_order:
        gap_net = contract_gap(req)
        if gap_net <= EPSILON:
            continue
        lane = lanes_by_region[req.region]
        want = min(_gross(gap_net, lane.loss_rate), remaining_by_customer[req.customer])
        give = min(want, pool - used)
        if give > EPSILON:
            allocations[req.customer].add("contract", give)
            remaining_by_customer[req.customer] -= give
            used += give

    # ---- 第三轮：历史兑现份额加权，区域内按剩余需求承接 ----
    residual = {r.customer: remaining_by_customer[r.customer] for r in reqs if remaining_by_customer[r.customer] > EPSILON}
    if pool - used > EPSILON and residual:
        claimants: list[tuple[str, float, float]] = []
        for req in reqs:
            if req.customer not in residual:
                continue
            lane = lanes_by_region[req.region]
            claimants.append((req.customer, lane.historical_share, residual[req.customer]))
        give_map = proportional_allocate(pool - used, claimants)
        for customer, give in give_map.items():
            if give > EPSILON:
                allocations[customer].add("share", give)
                remaining_by_customer[customer] -= give
                used += give

    # 数值清理
    for allocation in allocations.values():
        allocation.quantity = round(allocation.quantity, 9)
        allocation.by_round = {k: round(v, 9) for k, v in allocation.by_round.items() if v > EPSILON}

    unmet = {c: round(q, 9) for c, q in remaining_by_customer.items() if q > EPSILON}
    plan = Plan(
        allocations=allocations,
        pool=round(pool, 9),
        reserve=round(reserve, 9),
        used=round(used, 9),
        floor_shortfall={r: round(v, 9) for r, v in floor_shortfall.items() if v > EPSILON},
        unmet=unmet,
    )
    return plan


def emergency_need(
    plan: Plan,
    lanes: Iterable[Lane],
    customer: str,
    quantity: float,
    unshipped: dict[str, float] | None = None,
    authorization: str | None = None,
) -> tuple[str, float, list[tuple[str, float, int]]]:
    """常规轮次结束后的紧急需求处理（纯决策，不落库）。

    返回 (来源, 满足量, 挤占明细)。挤占明细元素为
    (被挤占客户, 数量, 补偿层级)；层级 0 表示挤占触及底线份额，补偿优先级最高，
    层级 1 表示仅挤占合同/份额轮额度。来源为 ``reserve`` / ``preempted`` /
    ``reserve+preempted``；保留池不足且无授权时抛出 AuthorizationRequired。
    unshipped 给出各客户当前尚未发运的可挤占额度；缺省视为全部额度未发运。
    """
    if quantity <= EPSILON:
        raise ValueError("紧急需求量必须为正")
    if customer not in plan.allocations:
        raise ValueError(f"紧急需求客户不在本轮请求中：{customer}")

    outstanding = quantity
    from_reserve = min(quantity, plan.reserve)
    outstanding -= from_reserve

    preempted: list[tuple[str, float, int]] = []
    if outstanding > EPSILON:
        if not authorization:
            raise AuthorizationRequired(
                f"保留池仅剩 {plan.reserve:.3f}，缺口 {outstanding:.3f} 需授权挤占未发运额度"
            )
        lanes_by_region = {lane.region: lane for lane in lanes}

        # 优先挤占：时效快（让出影响小）、历史份额低者；同一受害者先动
        # 合同/份额轮额度（tier 1），不够再动底线份额（tier 0）。
        def preempt_key(alloc: Allocation) -> tuple[float, float, str]:
            lane = lanes_by_region[alloc.region]
            return (lane.transit_days, lane.historical_share, alloc.customer)

        candidates = [a for a in plan.allocations.values() if a.customer != customer]
        for alloc in sorted(candidates, key=preempt_key):
            if outstanding <= EPSILON:
                break
            total_available = alloc.quantity if unshipped is None else unshipped.get(alloc.customer, 0.0)
            non_floor = alloc.by_round.get("share", 0.0) + alloc.by_round.get("contract", 0.0)
            tier1_cap = min(total_available, non_floor)
            give1 = min(tier1_cap, outstanding)
            if give1 > EPSILON:
                preempted.append((alloc.customer, round(give1, 9), 1))
                outstanding -= give1
            if outstanding > EPSILON:
                floor_cap = max(0.0, total_available - tier1_cap)
                give0 = min(floor_cap, outstanding)
                if give0 > EPSILON:
                    preempted.append((alloc.customer, round(give0, 9), 0))
                    outstanding -= give0
        if outstanding > EPSILON:
            raise AllocationError(
                f"保留池与可挤占额度仍不足，缺口 {outstanding:.3f}"
            )

    source = "reserve+preempted" if preempted and from_reserve > EPSILON else (
        "preempted" if preempted else "reserve"
    )
    return source, round(quantity - max(0.0, outstanding), 9), preempted


def ceil_qty(value: float) -> float:
    """数量向上取整到 3 位小数，避免分数单位。"""
    return math.ceil(value * 1000) / 1000
