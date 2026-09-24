"""分轮分配引擎。

决策口径：
- 货源 ``supply`` 按可发运毛吨计；底线、合同、申请量均按送达净吨计。
  某条申请线损耗率为 l，要交付净 q，需占用毛额 q / (1 - l)，损耗直接参与决策。
- 在途补给若预计在截止线前抵达，先冲减区域底线缺口。
- 运输时效：方案时点 + 时效晚于截止线的申请线不可行，不参与本轮分配，
  其未满足量进入 ``baseline_gaps`` / ``unmet`` 供指挥员决策。
- 三轮顺序，每轮都是水位上升（water-filling）：
  1. 底线轮：按区域底线覆盖率抬水位，覆盖率相同的区域共同抬升，
     谁也不能一口拿满；区域内优先使用低损耗线路（同样净需求占用更少毛额）。
  2. 合同轮：按各申请线合同兑现率抬水位，已获得的底线量计入合同兑现。
  3. 历史份额轮：剩余货源按“额外兑现量 / 历史兑现份额”抬水位配平。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Region, RequestLine

EPS = 1e-6


def q(value: float) -> float:
    """统一六位小数，避免浮点尾差污染台账。"""
    return round(value + 0.0, 6)


def gross_for_net(net: float, loss_rate: float) -> float:
    if not 0 <= loss_rate < 1:
        raise ValueError(f"损耗率必须位于 [0, 1)：{loss_rate}")
    if net <= EPS:
        return 0.0
    return q(net / (1.0 - loss_rate))


@dataclass
class EngineLine:
    region_id: str
    customer_id: str
    contract_qty: float
    urgent_qty: float
    loss_rate: float
    lead_time_h: float
    feasible: bool
    baseline_qty: float = 0.0
    contract_qty_alloc: float = 0.0
    share_qty: float = 0.0
    locked_net: float = 0.0       # 已锁定（确认/发运/挤占后存活）的既有净量，不再重新分配

    @property
    def used_net(self) -> float:
        """本轮新分净量。"""
        return self.baseline_qty + self.contract_qty_alloc + self.share_qty

    @property
    def committed_net(self) -> float:
        """含既有锁定在内的总承诺净量。"""
        return self.locked_net + self.used_net

    @property
    def spare_capacity(self) -> float:
        return max(0.0, self.urgent_qty - self.committed_net)

    @property
    def gross(self) -> float:
        net = self.used_net
        return gross_for_net(net, self.loss_rate) if net > EPS else 0.0


@dataclass
class RegionGap:
    region_id: str
    baseline: float
    covered_by_intransit: float
    allocated_net: float
    gap_net: float
    reason: str


@dataclass
class PlanResult:
    lines: list[EngineLine] = field(default_factory=list)
    gross_used: float = 0.0
    reserve_gross: float = 0.0
    pool_gross: float = 0.0
    baseline_gaps: list[RegionGap] = field(default_factory=list)
    unmet_net: float = 0.0
    intransit_by_region: dict[str, float] = field(default_factory=dict)

    def line(self, region_id: str, customer_id: str) -> EngineLine | None:
        for line in self.lines:
            if line.region_id == region_id and line.customer_id == customer_id:
                return line
        return None


def merge_lines(requests: list[RequestLine], deadline: datetime, as_of: datetime,
                locked_net_by_line: dict[tuple[str, str], float] | None = None) -> list[EngineLine]:
    """同一区域+客户的多条申请合并；时效取最差（最大）值，损耗按加急量加权。

    ``locked_net_by_line`` 给出各申请行已锁定、不可再分配的既有净量。
    """
    locked_net_by_line = locked_net_by_line or {}
    merged: dict[tuple[str, str], EngineLine] = {}
    weights: dict[tuple[str, str], float] = {}
    for req in requests:
        key = (req.region_id, req.customer_id)
        feasible = as_of + timedelta(hours=req.lead_time_h) <= deadline
        if key not in merged:
            merged[key] = EngineLine(
                region_id=req.region_id,
                customer_id=req.customer_id,
                contract_qty=req.contract_qty,
                urgent_qty=req.urgent_qty,
                loss_rate=req.loss_rate,
                lead_time_h=req.lead_time_h,
                feasible=feasible,
                locked_net=q(locked_net_by_line.get(key, 0.0)),
            )
            weights[key] = max(req.urgent_qty, EPS)
        else:
            line = merged[key]
            total_weight = weights[key] + req.urgent_qty
            line.loss_rate = (
                line.loss_rate * weights[key] + req.loss_rate * req.urgent_qty
            ) / max(total_weight, EPS)
            line.contract_qty += req.contract_qty
            line.urgent_qty += req.urgent_qty
            line.lead_time_h = max(line.lead_time_h, req.lead_time_h)
            line.feasible = feasible and line.feasible
            weights[key] = total_weight
    return list(merged.values())


def _affordable_net(pool_gross: float, loss_rate: float) -> float:
    return max(0.0, pool_gross * (1.0 - loss_rate))


class _Cursor:
    """水填游标。

    ratio 是该分配单位的“满足率”，scale 是满足率每升 1 对应的净量；
    一次只经过一条边际线路（区域内按损耗率从低到高），容量耗尽后自动推进。
    """

    def __init__(self, key: str, ratio0: float, scale: float,
                 lines: list[EngineLine], bucket: str, cap_fn,
                 tie_break: tuple = ()) -> None:
        self.key = key
        self.lines = [ln for ln in lines if ln.feasible]
        self.bucket = bucket
        self._cap_fn = cap_fn
        self.scale = max(scale, EPS)
        self._ratio0 = min(ratio0, 1.0) if bucket != "share" else ratio0
        self.given = 0.0
        self.tie_break = tie_break

    def ratio(self) -> float:
        return self._ratio0 + self.given / self.scale

    def _marginal(self) -> EngineLine | None:
        for line in self.lines:
            if self._cap_fn(self, line) > EPS:
                return line
        return None

    def has_capacity(self) -> bool:
        return self._marginal() is not None

    def marginal_cap_ratio(self) -> float:
        """当前边际线路还能把满足率推高多少。"""
        line = self._marginal()
        if line is None:
            return 0.0
        return self._cap_fn(self, line) / self.scale

    def allocate_ratio(self, delta_ratio: float, pool: float) -> float:
        line = self._marginal()
        if line is None or delta_ratio <= EPS:
            return 0.0
        want = q(delta_ratio * self.scale)
        want = min(want, self._cap_fn(self, line))
        got = q(min(want, _affordable_net(pool, line.loss_rate)))
        if got <= EPS:
            return 0.0
        if self.bucket == "baseline":
            line.baseline_qty = q(line.baseline_qty + got)
        elif self.bucket == "contract":
            line.contract_qty_alloc = q(line.contract_qty_alloc + got)
        else:
            line.share_qty = q(line.share_qty + got)
        self.given = q(self.given + got)
        return got


def _waterfill(pool_gross: float, cursors: list[_Cursor]) -> float:
    """分组水位上升。

    满足率最低的一组游标共同抬升，直到：组内某边际线路装满 / 与下一档齐平 /
    预算耗尽。返回剩余毛额。
    """
    pool = q(pool_gross)
    while pool > EPS:
        active = [c for c in cursors if c.has_capacity()]
        if not active:
            break
        active.sort(key=lambda c: (c.ratio(), c.key))
        lowest = active[0].ratio()
        group = [c for c in active if c.ratio() <= lowest + 1e-9]
        rest = [c for c in active if c.ratio() > lowest + 1e-9]
        next_level = min((c.ratio() for c in rest), default=None)

        delta = float("inf")
        for c in group:
            room = c.marginal_cap_ratio()
            if next_level is not None:
                room = min(room, max(0.0, next_level - c.ratio()))
            delta = min(delta, room)
        if delta <= EPS:
            break

        # 组内各游标净增 scale·δ，按各自边际损耗折算毛成本，共同受预算约束。
        cost_per_ratio = 0.0
        for c in group:
            line = c._marginal()
            cost_per_ratio += c.scale / (1.0 - line.loss_rate)
        delta = min(delta, pool / max(cost_per_ratio, EPS))
        if delta <= EPS:
            break

        for c in group:
            line = c._marginal()
            gross_cost = q(delta * c.scale / (1.0 - line.loss_rate))
            got = c.allocate_ratio(delta, min(pool, gross_cost + EPS))
            pool = q(pool - gross_for_net(got, line.loss_rate))
    return pool


def run_allocation(
    *,
    supply_gross: float,
    reserve_gross: float,
    deadline: datetime,
    as_of: datetime,
    regions: dict[str, Region],
    requests: list[RequestLine],
    intransit_net_by_region: dict[str, float] | None = None,
    prior_coverage_net_by_region: dict[str, float] | None = None,
    locked_net_by_line: dict[tuple[str, str], float] | None = None,
) -> PlanResult:
    """三轮水填。

    ``prior_coverage_net_by_region`` 用于发布新版本时计入已到货/锁定的区域覆盖；
    ``locked_net_by_line`` 给出各申请行已锁定净量，再版不会把同一行重复分配。
    """
    intransit_net_by_region = intransit_net_by_region or {}
    prior_coverage = prior_coverage_net_by_region or {}
    locked_net_by_line = locked_net_by_line or {}
    result = PlanResult(reserve_gross=q(reserve_gross))
    pool = q(supply_gross - reserve_gross)
    if pool < -EPS:
        raise ValueError("保留池不能大于总货源")
    pool = q(max(pool, 0.0))
    result.pool_gross = pool

    lines = merge_lines(requests, deadline, as_of, locked_net_by_line)
    result.lines = lines
    feasible = [line for line in lines if line.feasible]

    by_region: dict[str, list[EngineLine]] = {}
    for line in feasible:
        by_region.setdefault(line.region_id, []).append(line)
    for region_lines in by_region.values():
        # 区域内始终优先低损耗线路。
        region_lines.sort(key=lambda x: (x.loss_rate, x.customer_id))

    covered: dict[str, float] = {}
    for region_id, region in regions.items():
        it = intransit_net_by_region.get(region_id, 0.0) + prior_coverage.get(region_id, 0.0)
        result.intransit_by_region[region_id] = q(intransit_net_by_region.get(region_id, 0.0))
        covered[region_id] = min(region.baseline, it)

    # -- 第一轮：区域底线（覆盖率水填，同档区域共同抬升）-------------------
    baseline_cursors: list[_Cursor] = []
    for region_id, region in regions.items():
        region_lines = by_region.get(region_id, [])
        if not region_lines:
            continue
        start_ratio = covered[region_id] / max(region.baseline, EPS)

        def baseline_cap(c: "_Cursor", line: EngineLine, _r=region) -> float:
            region_room = max(0.0, _r.baseline * (1.0 - c.ratio()))
            return min(line.spare_capacity, region_room)

        baseline_cursors.append(_Cursor(
            key=region_id, ratio0=start_ratio, scale=region.baseline,
            lines=region_lines, bucket="baseline", cap_fn=baseline_cap,
            tie_break=(region.priority, region_id),
        ))
    pool = _waterfill(pool, baseline_cursors)
    for c in baseline_cursors:
        covered[c.key] = q(min(regions[c.key].baseline, covered[c.key] + c.given))

    # -- 第二轮：客户合同（按合同兑现率水填）-------------------------------
    contract_cursors: list[_Cursor] = []
    for line in feasible:
        if line.contract_qty <= EPS:
            continue

        def contract_cap(c: "_Cursor", ln: EngineLine = line) -> float:
            return min(max(0.0, ln.contract_qty - ln.committed_net), ln.spare_capacity)

        contract_cursors.append(_Cursor(
            key=f"{line.region_id}:{line.customer_id}",
            ratio0=min(1.0, line.committed_net / max(line.contract_qty, EPS)),
            scale=line.contract_qty, lines=[line],
            bucket="contract", cap_fn=contract_cap,
            tie_break=(regions[line.region_id].priority, line.region_id, line.customer_id),
        ))
    pool = _waterfill(pool, contract_cursors)

    # -- 第三轮：历史兑现份额配平（额外量/历史份额 水填）-------------------
    weights = {rid: max(region.historical_share, 0.0) for rid, region in regions.items()}
    if not any(w > EPS for w in weights.values()):
        # 没有历史数据则区域等权。
        weights = {rid: 1.0 for rid in regions}
    share_cursors: list[_Cursor] = []
    for region_id, region in regions.items():
        region_lines = by_region.get(region_id, [])
        if not region_lines or weights[region_id] <= EPS:
            continue
        share_cursors.append(_Cursor(
            key=region_id, ratio0=0.0, scale=weights[region_id],
            lines=region_lines, bucket="share",
            cap_fn=lambda c, ln: ln.spare_capacity,
            tie_break=(region_id,),
        ))
    pool = _waterfill(pool, share_cursors)

    result.gross_used = q(result.pool_gross - pool)
    result.unmet_net = q(sum(max(0.0, line.urgent_qty - line.committed_net) for line in lines))
    for region_id, region in regions.items():
        allocated = q(sum(line.used_net for line in feasible if line.region_id == region_id))
        it = intransit_net_by_region.get(region_id, 0.0) + prior_coverage.get(region_id, 0.0)
        gap = q(max(0.0, (region.baseline - it) - allocated))
        if gap > EPS:
            has_line = any(line.region_id == region_id for line in lines)
            if not has_line:
                reason = "该区域无在案申请线"
            elif not any(line.feasible and line.spare_capacity > EPS
                         for line in lines if line.region_id == region_id):
                reason = "受运输时效/货源限制，新增发运无法在截止线前补足"
            else:
                reason = "总货源不足"
            result.baseline_gaps.append(
                RegionGap(
                    region_id=region_id,
                    baseline=region.baseline,
                    covered_by_intransit=q(min(region.baseline, it)),
                    allocated_net=allocated,
                    gap_net=gap,
                    reason=reason,
                )
            )
    return result
