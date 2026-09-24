"""应用服务：跨区域保供调拨的全部用例。

约定：
- 每个写用例必须带 ``command_id``；同 command_id 重试直接回放首次结果，配额只扣一次。
- 每个用例在单事务内重放状态 -> 校验 -> 追加事件 -> 登记命令，提交后生效。
- 事件 ``occurred_at`` 是系统受理时间；回执另存 ``happened_at`` 为真实发生时间。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .clock import Clock, SystemClock, parse_time
from .engine import PlanResult, gross_for_net, q, run_allocation
from .errors import *
from .models import (
    Plan,
    PlanLine,
    PlanStatus,
    RoundStatus,
    Transfer,
    TransferStatus,
)
from .projection import State, replay, _line_key, _plan_payload
from .store import EventStore

EPS = 1e-6


@dataclass
class CommandResult:
    command_id: str
    result_type: str
    result_id: str
    replayed: bool = False
    extra: dict | None = None


class AllocationService:
    def __init__(self, store: EventStore | str = ":memory:", clock: Clock | None = None) -> None:
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        self.clock = clock or SystemClock()

    # -- 内部基础设施 --------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    def _load(self, connection) -> State:
        return replay(self.store.load_events(connection))

    def _seq(self, connection) -> int:
        return self.store.next_seq(connection)

    def _append(self, connection, event_type: str, aggregate_type: str, aggregate_id: str,
                payload: dict[str, Any], *, command_id: str | None = None,
                occurred_at: datetime | None = None) -> int:
        seq = self._seq(connection)
        self.store.append(
            connection, event_type, aggregate_type, aggregate_id,
            occurred_at or self._now(), payload, seq=seq, command_id=command_id,
        )
        return seq

    def _finish(self, connection, command_id: str, command_type: str, result_type: str,
                result_id: str, extra: dict | None = None) -> CommandResult:
        self.store.record_command(
            connection, command_id, command_type, result_type, result_id,
            self._now().isoformat(),
        )
        connection.commit()
        return CommandResult(command_id, result_type, result_id, False, extra)

    def _replay(self, connection, row, *, extra: dict | None = None) -> CommandResult:
        connection.commit()
        return CommandResult(row["command_id"], row["result_type"], row["result_id"], True, extra)

    def _dedup(self, connection, command_id: str, command_type: str):
        row = self.store.command_result(connection, command_id)
        if row is not None and row["command_type"] != command_type:
            raise QuotaAlreadyDeducted(
                f"command_id {command_id} 已用于 {row['command_type']}，"
                f"不能复用于 {command_type}"
            )
        return connection, row

    @staticmethod
    def _get_round(state: State, round_id: str):
        rnd = state.rounds.get(round_id)
        if rnd is None:
            raise ValidationFailed(f"轮次不存在：{round_id}")
        return rnd

    @staticmethod
    def _get_plan(state: State, plan_id: str) -> Plan:
        plan = state.plans.get(plan_id)
        if plan is None:
            raise ValidationFailed(f"方案不存在：{plan_id}")
        return plan

    @staticmethod
    def _get_transfer(state: State, transfer_id: str) -> Transfer:
        transfer = state.transfers.get(transfer_id)
        if transfer is None:
            raise ValidationFailed(f"调拨不存在：{transfer_id}")
        return transfer

    # -- 主数据 --------------------------------------------------------------

    def register_region(self, *, region_id: str, name: str, baseline: float,
                        priority: int = 100, historical_share: float = 0.0,
                        command_id: str | None = None) -> CommandResult:
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "register_region")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            if baseline < 0 or historical_share < 0:
                raise ValidationFailed("底线与历史份额不能为负")
            if region_id in state.regions:
                raise ValidationFailed(f"区域已存在：{region_id}")
            self._append(conn, "region.registered", "region", region_id, {
                "region_id": region_id, "name": name, "baseline": q(baseline),
                "priority": priority, "historical_share": q(historical_share),
            }, command_id=command_id)
            return self._finish(conn, command_id, "register_region", "region", region_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 轮次与申请 ----------------------------------------------------------

    def open_round(self, *, round_id: str, supply_gross: float, reserve_gross: float,
                   deadline: str, command_id: str | None = None) -> CommandResult:
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "open_round")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            if round_id in state.rounds:
                raise ValidationFailed(f"轮次已存在：{round_id}")
            if not state.regions:
                raise ValidationFailed("尚未登记任何区域")
            if supply_gross < 0 or reserve_gross < 0 or reserve_gross > supply_gross + EPS:
                raise ValidationFailed("货源与保留池数量非法")
            deadline_dt = parse_time(deadline)
            if deadline_dt <= self._now():
                raise ValidationFailed("截止线必须晚于当前时间")
            self._append(conn, "round.opened", "round", round_id, {
                "round_id": round_id,
                "supply": q(supply_gross),
                "reserve": q(reserve_gross),
                "deadline": deadline_dt.isoformat(),
            }, command_id=command_id)
            return self._finish(conn, command_id, "open_round", "round", round_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def submit_request(self, *, round_id: str, customer_id: str, region_id: str,
                       contract_qty: float, urgent_qty: float,
                       loss_rate: float = 0.0, lead_time_h: float = 0.0,
                       transit_before_deadline: float = 0.0,
                       command_id: str | None = None) -> CommandResult:
        """常规轮次进行中提交的为常规申请；关轮后到达的只能走紧急通道。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "submit_request")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if region_id not in rnd.regions:
                raise ValidationFailed(f"区域不在本轮范围：{region_id}")
            if rnd.status == RoundStatus.FINALIZED:
                raise RoundClosed("轮次已封账，不再接受申请")
            if not 0 <= loss_rate < 1:
                raise ValidationFailed("损耗率必须位于 [0, 1)")
            if urgent_qty < 0 or contract_qty < 0 or lead_time_h < 0:
                raise ValidationFailed("申请量与时效不能为负")
            if rnd.status == RoundStatus.CLOSED:
                raise RoundClosed(
                    "常规轮次已结束，紧急需求请使用 request_emergency（保留池/授权挤占）"
                )
            self._append(conn, "request.submitted", "round", round_id, {
                "round_id": round_id,
                "emergency": False,
                "request": {
                    "customer_id": customer_id,
                    "region_id": region_id,
                    "contract_qty": q(contract_qty),
                    "urgent_qty": q(urgent_qty),
                    "loss_rate": loss_rate,
                    "lead_time_h": lead_time_h,
                    "transit_before_deadline": q(transit_before_deadline),
                },
            }, command_id=command_id)
            return self._finish(conn, command_id, "submit_request", "request",
                                f"{round_id}:{region_id}:{customer_id}")
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def register_intransit(self, *, round_id: str, source: str, region_id: str,
                           qty_net: float, eta: str,
                           command_id: str | None = None) -> CommandResult:
        """登记在途补给；ETA 早于截止线才计入本轮冲减。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "register_intransit")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if region_id not in rnd.regions:
                raise ValidationFailed(f"区域不在本轮范围：{region_id}")
            if rnd.status == RoundStatus.FINALIZED:
                raise RoundClosed("轮次已封账")
            eta_dt = parse_time(eta)
            self._append(conn, "intransit.registered", "round", round_id, {
                "round_id": round_id, "source": source, "region_id": region_id,
                "qty": q(qty_net), "eta": eta_dt.isoformat(),
                "counts_for_round": eta_dt <= rnd.deadline,
            }, command_id=command_id)
            return self._finish(conn, command_id, "register_intransit", "intransit", source)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def close_round(self, *, round_id: str, command_id: str | None = None) -> CommandResult:
        """常规轮次结束；此后的需求一律按紧急需求处理。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "close_round")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if rnd.status != RoundStatus.OPEN:
                raise RoundClosed("轮次不在进行中状态")
            self._append(conn, "round.closed", "round", round_id,
                         {"round_id": round_id}, command_id=command_id)
            return self._finish(conn, command_id, "close_round", "round", round_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 分配计算（供模拟与正式草案共用）-------------------------------------

    def _regular_requests(self, state: State, round_id: str):
        rnd = state.rounds[round_id]
        return [req for req in rnd.requests if not req.emergency]

    def _intransit_net(self, state: State, round_id: str) -> dict[str, float]:
        rnd = state.rounds[round_id]
        totals: dict[str, float] = {}
        for item in rnd.in_transit:
            if item.eta <= rnd.deadline:
                totals[item.region_id] = totals.get(item.region_id, 0.0) + item.qty
        return {k: q(v) for k, v in totals.items()}

    def _coverage_net(self, state: State, round_id: str,
                      *, exclude_plan_id: str | None = None) -> dict[str, float]:
        """既有调拨对各区域的净覆盖（与再版锁定集合口径一致）。"""
        totals: dict[str, float] = {}
        active = state.rounds[round_id].active_plan_id
        for t in state.round_transfers(round_id):
            if exclude_plan_id and t.plan_id == exclude_plan_id:
                continue
            if self._is_reclaimable(t, active):
                continue
            physical = t.effective_gross
            if physical <= EPS:
                continue
            totals[t.region_id] = totals.get(t.region_id, 0.0) + physical * (1 - t.loss_rate)
        return {k: q(v) for k, v in totals.items()}

    def _locked_net_by_line(self, state: State, round_id: str) -> dict[tuple[str, str], float]:
        """各申请行（区域+客户）已锁定、不可再分配的既有净量。"""
        active = state.rounds[round_id].active_plan_id
        totals: dict[tuple[str, str], float] = {}
        for t in state.round_transfers(round_id):
            if self._is_reclaimable(t, active):
                continue
            physical = t.effective_gross
            if physical <= EPS:
                continue
            key = (t.region_id, t.customer_id)
            totals[key] = totals.get(key, 0.0) + physical * (1 - t.loss_rate)
        return {k: q(v) for k, v in totals.items()}

    def _is_reclaimable(self, t: Transfer, active_plan_id: str | None) -> bool:
        """再版发布时将被取消、毛额可重新分配的常规调拨：
        当前版本下仍停留在 PLANNED、未确认、未被挤占过的额度。"""
        return (
            t.kind == "regular"
            and t.plan_id == active_plan_id
            and t.status == TransferStatus.PLANNED
            and t.confirmed_qty <= EPS
            and t.diverted_gross <= EPS
        )

    def _compute_plan(self, state: State, round_id: str, *, supply_override: float | None,
                      as_of: datetime) -> PlanResult:
        rnd = state.rounds[round_id]
        supply = rnd.supply if supply_override is None else supply_override
        if rnd.active_plan_id is not None:
            # 正式再版：已确认/已发运/已挤占的承诺锁定，其余（含未动的自由池
            # 与即将取消的未发运额度）可重新分配。
            locked = 0.0
            for t in state.round_transfers(round_id):
                if t.kind != "regular":
                    continue
                if self._is_reclaimable(t, rnd.active_plan_id):
                    continue
                locked += t.effective_gross + t.diverted_gross
            locked = q(locked)
            # 常规自由池 = 初始常规池(S-初始保留池) - 已锁定常规承诺。
            # 保留池动用/补充不改变常规池；保留池余额单独留给后续紧急需求，
            # 不进入常规三轮分配。
            available_regular = q(supply - rnd.reserve - locked)
            if available_regular < -EPS:
                raise ReconciliationMismatch(
                    f"既有锁定承诺 {locked} 已超过常规货源 {supply - rnd.reserve}"
                )
            reserve_for_engine = 0.0
            supply_for_engine = available_regular
            # 仅锁定承诺的净覆盖冲减底线；可回收的 PLANNED 不计入。
            prior = self._coverage_net(state, round_id)
            locked_by_line = self._locked_net_by_line(state, round_id)
        else:
            reserve_for_engine = min(rnd.reserve, supply)
            supply_for_engine = supply
            available_regular = q(supply - reserve_for_engine)
            prior = {}
            locked_by_line = {}
        result = run_allocation(
            supply_gross=supply_for_engine,
            reserve_gross=reserve_for_engine,
            deadline=rnd.deadline,
            as_of=as_of,
            regions=rnd.regions,
            requests=self._regular_requests(state, round_id),
            intransit_net_by_region=self._intransit_net(state, round_id),
            prior_coverage_net_by_region=prior,
            locked_net_by_line=locked_by_line,
        )
        result.pool_gross = available_regular
        return result

    def _build_plan(self, *, plan_id: str, round_id: str, version: int, status: PlanStatus,
                    simulation_of: str | None, supersedes: str | None,
                    result: PlanResult, supply: float, reserve: float,
                    deadline: datetime, required_approvals: int, now: datetime) -> Plan:
        plan = Plan(
            plan_id=plan_id, round_id=round_id, version=version, status=status,
            supply=q(supply), reserve=q(reserve), deadline=deadline,
            simulation_of=simulation_of, supersedes=supersedes,
            required_approvals=required_approvals, created_at=now,
        )
        for line in result.lines:
            plan.lines[_line_key(line.region_id, line.customer_id)] = PlanLine(
                region_id=line.region_id,
                customer_id=line.customer_id,
                loss_rate=line.loss_rate,
                baseline_qty=q(line.baseline_qty),
                contract_qty=q(line.contract_qty_alloc),
                share_qty=q(line.share_qty),
            )
        return plan

    # -- 隔离模拟 ------------------------------------------------------------

    def simulate_supply_reduction(self, *, round_id: str, scenario: str,
                                  reduced_supply_gross: float,
                                  command_id: str | None = None) -> CommandResult:
        """在隔离沙盘中模拟供应缩减：只读运营态，只落 simulated 事件，绝不占资源。

        模拟方案没有任何调拨、不扣保留池、不进入批准链；指挥员确认后再走正式草案。
        """
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "simulate_supply_reduction")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if scenario in rnd.simulations:
                raise SimulationConflict(f"场景已模拟过：{scenario}")
            if reduced_supply_gross < 0:
                raise ValidationFailed("缩减后货源不能为负")
            if reduced_supply_gross >= rnd.supply:
                raise ValidationFailed("供应缩减模拟必须小于当前货源")
            now = self._now()
            result = run_allocation(
                supply_gross=reduced_supply_gross,
                reserve_gross=min(rnd.reserve, reduced_supply_gross),
                deadline=rnd.deadline,
                as_of=now,
                regions=rnd.regions,
                requests=self._regular_requests(state, round_id),
                intransit_net_by_region=self._intransit_net(state, round_id),
            )
            plan_id = f"sim-{round_id}-{len(rnd.simulations) + 1:02d}"
            plan = self._build_plan(
                plan_id=plan_id, round_id=round_id, version=0,
                status=PlanStatus.SIMULATED, simulation_of=scenario, supersedes=None,
                result=result, supply=reduced_supply_gross,
                reserve=min(rnd.reserve, reduced_supply_gross),
                deadline=rnd.deadline, required_approvals=0, now=now,
            )
            self._append(conn, "allocation.simulated", "plan", plan_id,
                         _plan_payload(plan), command_id=command_id, occurred_at=now)
            extra = self._plan_preview(result, plan)
            return self._finish(conn, command_id, "simulate_supply_reduction",
                                "simulation", plan_id, extra)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def _plan_preview(self, result: PlanResult, plan: Plan) -> dict:
        return {
            "plan_id": plan.plan_id,
            "supply_gross": plan.supply,
            "reserve_gross": plan.reserve,
            "gross_used": result.gross_used,
            "unmet_net": result.unmet_net,
            "baseline_gaps": [
                {
                    "region_id": g.region_id, "baseline": g.baseline,
                    "covered_by_intransit": g.covered_by_intransit,
                    "allocated_net": g.allocated_net, "gap_net": g.gap_net,
                    "reason": g.reason,
                }
                for g in result.baseline_gaps
            ],
            "lines": [
                {
                    "region_id": line.region_id, "customer_id": line.customer_id,
                    "feasible": line.feasible, "loss_rate": line.loss_rate,
                    "baseline_qty": line.baseline_qty,
                    "contract_qty": line.contract_qty_alloc,
                    "share_qty": line.share_qty,
                    "gross_qty": line.gross,
                }
                for line in result.lines
            ],
        }

    def review_simulation(self, round_id: str, scenario: str) -> dict:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            plan_id = rnd.simulations.get(scenario)
            if plan_id is None:
                raise ValidationFailed(f"未找到模拟场景：{scenario}")
            plan = state.plans[plan_id]
            return {
                "scenario": scenario,
                "plan_id": plan.plan_id,
                "status": plan.status.value,
                "supply_gross": plan.supply,
                "reserve_gross": plan.reserve,
                "lines": [
                    {
                        "region_id": line.region_id, "customer_id": line.customer_id,
                        "baseline_qty": line.baseline_qty,
                        "contract_qty": line.contract_qty,
                        "share_qty": line.share_qty,
                        "net_total": line.regular_net,
                        "gross_qty": gross_for_net(line.regular_net, line.loss_rate),
                    }
                    for line in plan.lines.values()
                ],
            }
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 正式草案与批准链 ----------------------------------------------------

    def create_draft(self, *, round_id: str,
                     from_simulation: str | None = None,
                     required_approvals: int = 2,
                     command_id: str | None = None) -> CommandResult:
        """确认模拟后生成正式草案；不带模拟则按当前货源出草案。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "create_draft")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if rnd.status == RoundStatus.FINALIZED:
                raise RoundClosed("轮次已封账")
            supply_override = None
            if from_simulation is not None:
                sim_id = rnd.simulations.get(from_simulation)
                if sim_id is None:
                    raise ValidationFailed(f"未找到模拟场景：{from_simulation}")
                supply_override = state.plans[sim_id].supply
            now = self._now()
            result = self._compute_plan(state, round_id, supply_override=supply_override,
                                        as_of=now)
            version = 1 + max(
                (p.version for p in state.plans.values()
                 if p.round_id == round_id and p.status != PlanStatus.SIMULATED),
                default=0,
            )
            plan_id = f"plan-{round_id}-v{version}"
            if plan_id in state.plans:
                raise ValidationFailed(f"方案版本已存在：{plan_id}")
            plan_supply = rnd.supply if supply_override is None else supply_override
            plan = self._build_plan(
                plan_id=plan_id, round_id=round_id, version=version,
                status=PlanStatus.DRAFT, simulation_of=None,
                supersedes=rnd.active_plan_id, result=result,
                supply=plan_supply,
                reserve=rnd.reserve_balance_gross if rnd.active_plan_id else rnd.reserve,
                deadline=rnd.deadline, required_approvals=required_approvals, now=now,
            )
            self._append(conn, "allocation.drafted", "plan", plan_id,
                         _plan_payload(plan), command_id=command_id, occurred_at=now)
            return self._finish(conn, command_id, "create_draft", "plan", plan_id,
                                self._plan_preview(result, plan))
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def decide_approval(self, *, plan_id: str, approver: str, approved: bool,
                        level: int = 1, comment: str = "",
                        command_id: str | None = None) -> CommandResult:
        """批准链逐级审批；任一级别驳回则方案作废，需要重新出版本。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "decide_approval")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            plan = self._get_plan(state, plan_id)
            if plan.status != PlanStatus.DRAFT:
                raise ApprovalConflict("只有草案可以审批")
            if level < 1 or level > plan.required_approvals:
                raise ApprovalConflict(
                    f"审批级别必须位于 1..{plan.required_approvals}"
                )
            records = [a for a in state.approvals.get(plan_id, []) if a.approved]
            levels = {a.level for a in records}
            if level in levels:
                raise ApprovalConflict(f"{level} 级已经批准")
            expected = len(levels) + 1
            if level != expected:
                raise ApprovalConflict(f"批准链必须逐级推进，当前等待第 {expected} 级")
            now = self._now()
            if not approved:
                self._append(conn, "allocation.rejected", "plan", plan_id, {
                    "plan_id": plan_id, "level": level, "approver": approver,
                    "comment": comment,
                }, command_id=command_id, occurred_at=now)
                return self._finish(conn, command_id, "decide_approval",
                                    "plan_rejected", plan_id)
            self._append(conn, "allocation.approved", "plan", plan_id, {
                "plan_id": plan_id, "level": level, "approver": approver,
                "approved": True, "comment": comment,
            }, command_id=command_id, occurred_at=now)
            return self._finish(conn, command_id, "decide_approval",
                                "plan_approved", plan_id,
                                {"approved_levels": sorted(levels | {level})})
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def publish_plan(self, *, plan_id: str, command_id: str | None = None) -> CommandResult:
        """批准链完整后发布；旧版本未发运的常规额度取消释放，已发运的继续有效。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "publish_plan")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            plan = self._get_plan(state, plan_id)
            if plan.status != PlanStatus.DRAFT:
                raise PlanNotUsable("只有草案可以发布")
            if plan.rejected:
                raise PlanNotUsable("方案已被驳回")
            levels = {a.level for a in state.approvals.get(plan_id, []) if a.approved}
            if levels != set(range(1, plan.required_approvals + 1)):
                raise ApprovalConflict(
                    f"批准链不完整：{sorted(levels)} / 需要 {plan.required_approvals} 级"
                )
            now = self._now()
            rnd = state.rounds[plan.round_id]

            # 批准链锁定的是具体数量；发布前重算若与批准草案不一致，必须重新出版本。
            approved_snapshot = {
                key: round(line.regular_net, 6) for key, line in plan.lines.items()
            }
            # 草案到发布之间承诺状态可能变化（确认/发运/挤占），发布前按最新锁定集重算，
            # 保证正式版本与真实可回收额度一致。
            supply_override = plan.supply if abs(plan.supply - rnd.supply) > EPS else None
            fresh = self._compute_plan(state, plan.round_id,
                                       supply_override=supply_override, as_of=now)
            rebuilt = self._build_plan(
                plan_id=plan.plan_id, round_id=plan.round_id, version=plan.version,
                status=PlanStatus.DRAFT, simulation_of=None,
                supersedes=rnd.active_plan_id, result=fresh,
                supply=plan.supply, reserve=plan.reserve,
                deadline=plan.deadline, required_approvals=plan.required_approvals,
                now=plan.created_at or now,
            )
            drift = sorted(
                key for key in set(approved_snapshot) | set(rebuilt.lines)
                if abs(approved_snapshot.get(key, 0.0)
                       - round(rebuilt.lines[key].regular_net, 6)) > EPS
            )
            if drift:
                raise PlanNotUsable(
                    "批准后可回收额度已变化，以下申请线分配漂移，请基于最新状态重新出版本："
                    + ", ".join(drift)
                )
            plan.lines = rebuilt.lines

            # 旧版本：仅取消尚未确认、未被挤占的常规调拨；其余继续兑现。
            cancelled = []
            if rnd.active_plan_id:
                for old in state.plan_transfers(rnd.active_plan_id):
                    if self._is_reclaimable(old, rnd.active_plan_id):
                        self._append(conn, "transfer.cancelled", "transfer",
                                     old.transfer_id, {
                                        "transfer_id": old.transfer_id,
                                        "round_id": plan.round_id,
                                        "plan_id": old.plan_id,
                                        "qty_gross": old.gross_qty,
                                        "reason": f"被 {plan_id} 替代",
                                     }, command_id=command_id, occurred_at=now)
                        cancelled.append(old.transfer_id)

            plan.status = PlanStatus.PUBLISHED
            plan.published_at = now
            self._append(conn, "allocation.published", "plan", plan_id,
                         _plan_payload(plan), command_id=command_id, occurred_at=now)

            # 按方案行生成常规调拨（毛额含损耗补偿）。
            created = []
            for key, line in plan.lines.items():
                if line.regular_net <= EPS:
                    continue
                transfer_id = f"tr-{plan_id}-{len(created) + 1:03d}"
                gross = gross_for_net(line.regular_net, line.loss_rate)
                self._append(conn, "transfer.created", "transfer", transfer_id, {
                    "transfer_id": transfer_id,
                    "round_id": plan.round_id,
                    "plan_id": plan_id,
                    "region_id": line.region_id,
                    "customer_id": line.customer_id,
                    "gross_qty": gross,
                    "net_qty": line.regular_net,
                    "loss_rate": line.loss_rate,
                    "kind": "regular",
                }, command_id=command_id, occurred_at=now)
                created.append(transfer_id)
            return self._finish(conn, command_id, "publish_plan", "plan", plan_id,
                                {"transfers": created, "cancelled": cancelled})
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 回执（顺序推进、迟到归档、幂等不重扣）--------------------------------

    def record_receipt(self, *, transfer_id: str, stage: str, qty: float,
                       happened_at: str | None = None,
                       command_id: str | None = None) -> CommandResult:
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "record_receipt")
            if existing:
                # 重试：回放首次回执结果，绝不再次推进数量或扣任何配额。
                return self._replay(conn, existing, extra={"duplicate": True})
            state = self._load(conn)
            transfer = self._get_transfer(state, transfer_id)
            if stage not in ("confirmed", "dispatched", "arrived"):
                raise ValidationFailed("回执阶段必须是 confirmed/dispatched/arrived")
            if transfer.status == TransferStatus.CANCELLED:
                raise TransferStateConflict("调拨已取消，不能再记录回执")
            if qty < 0:
                raise ValidationFailed("回执数量不能为负")

            happened = parse_time(happened_at) if happened_at else self._now()
            now = self._now()
            stage_rank = {"confirmed": 1, "dispatched": 2, "arrived": 3}[stage]

            recorded = {
                "confirmed": transfer.confirmed_qty,
                "dispatched": transfer.dispatched_qty,
                "arrived": transfer.arrived_qty,
            }
            # 容量上限：任何阶段都不能超过挤占后仍归属本客户的有效毛额。
            # （先确认后挤占的历史回执保留原值；挤占之后的新回执按有效额封顶。）
            cap = transfer.effective_gross
            if qty > cap + EPS:
                raise TransferStateConflict(
                    f"{stage} 回执 {qty} 超过有效额度 {cap}"
                )
            # 数量单调不减（允许分批累计补报）。
            if qty + EPS < recorded[stage]:
                raise TransferStateConflict(
                    f"{stage} 数量不能倒退：已记录 {recorded[stage]}，新回执 {qty}"
                )
            current_rank = self._transfer_rank(transfer)
            late = False
            if stage_rank > current_rank + 1:
                raise TransferStateConflict(
                    "回执必须按 确认->装运->到达 顺序推进，缺少前置阶段回执"
                )
            if stage_rank <= current_rank:
                # 迟到回执：允许补报（数量与真实时间），但状态不会倒退。
                late = True
            if stage == "dispatched" and transfer.confirmed_qty <= EPS and not late:
                raise TransferStateConflict("缺少确认回执，不能记录装运")
            if stage == "arrived" and transfer.dispatched_qty <= EPS and not late:
                raise TransferStateConflict("缺少装运回执，不能记录到达")
            # 真实发生时间不能晚于受理时间太多是允许的（补报）；
            # 但同一调拨的阶段真实时间不可倒挂（到达早于装运等）。
            predecessor_time = {
                "dispatched": transfer.confirmed_at,
                "arrived": transfer.dispatched_at,
            }.get(stage)
            if predecessor_time and happened < predecessor_time:
                raise TransferStateConflict(
                    f"{stage} 真实发生时间 {happened.isoformat()} 早于前置阶段 "
                    f"{predecessor_time.isoformat()}"
                )

            self._append(conn, "receipt.recorded", "transfer", transfer_id, {
                "transfer_id": transfer_id,
                "stage": stage,
                "qty": q(qty),
                "happened_at": happened.isoformat(),
                "recorded_at": now.isoformat(),
                "late": late,
            }, command_id=command_id, occurred_at=now)
            return self._finish(conn, command_id, "record_receipt",
                                f"receipt:{stage}", transfer_id, {"late": late})
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    @staticmethod
    def _transfer_rank(transfer: Transfer) -> int:
        """生命周期阶段以回执事实为准；COMPENSATING 只是挤占标记，不抬高阶段。"""
        if transfer.status == TransferStatus.CANCELLED:
            return 99
        if transfer.arrived_qty > EPS:
            return 3
        if transfer.dispatched_qty > EPS:
            return 2
        if transfer.confirmed_qty > EPS:
            return 1
        return 0

    # -- 紧急需求：保留池 -> 授权挤占 ----------------------------------------

    def request_emergency(self, *, round_id: str, customer_id: str, region_id: str,
                          qty_net: float, loss_rate: float = 0.0,
                          lead_time_h: float = 0.0, reason: str = "",
                          command_id: str | None = None) -> CommandResult:
        """常规轮次结束后的紧急需求。优先动用保留池；不足部分留待授权挤占。

        本命令不自动挤占任何人的额度；short_net 大于零时由指挥员另行授权 divert。
        """
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "request_emergency")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if rnd.status != RoundStatus.CLOSED:
                raise RoundClosed("紧急通道仅在常规轮次结束后开放")
            if region_id not in rnd.regions:
                raise ValidationFailed(f"区域不在本轮范围：{region_id}")
            if qty_net <= EPS or not 0 <= loss_rate < 1:
                raise ValidationFailed("紧急数量与损耗率非法")
            if self._now().timestamp() + lead_time_h * 3600 > rnd.deadline.timestamp() + EPS:
                raise ValidationFailed("受运输时效限制，即使立即发运也无法在截止线前到达")
            need_gross = gross_for_net(qty_net, loss_rate)
            available = rnd.reserve_balance_gross
            draw_gross = q(min(need_gross, available))
            draw_net = q(draw_gross * (1 - loss_rate))
            short_net = q(qty_net - draw_net)

            self._append(conn, "request.submitted", "round", round_id, {
                "round_id": round_id,
                "emergency": True,
                "request": {
                    "customer_id": customer_id, "region_id": region_id,
                    "contract_qty": 0.0, "urgent_qty": q(qty_net),
                    "loss_rate": loss_rate, "lead_time_h": lead_time_h,
                    "transit_before_deadline": 0.0, "reason": reason,
                },
            }, command_id=command_id)

            transfer_id = None
            if draw_gross > EPS:
                transfer_id = f"tr-em-{uuid.uuid4().hex[:10]}"
                self._append(conn, "transfer.created", "transfer", transfer_id, {
                    "transfer_id": transfer_id, "round_id": round_id,
                    "plan_id": rnd.active_plan_id or "emergency",
                    "region_id": region_id, "customer_id": customer_id,
                    "gross_qty": draw_gross, "net_qty": draw_net,
                    "loss_rate": loss_rate, "kind": "emergency",
                    "source": {"fund": "reserve", "reason": reason},
                }, command_id=command_id)
                self._append(conn, "reserve.used", "round", round_id, {
                    "round_id": round_id, "qty_gross": draw_gross,
                    "transfer_id": transfer_id,
                }, command_id=command_id)
            return self._finish(conn, command_id, "request_emergency",
                                "emergency", transfer_id or "unmet", {
                "allocated_net": draw_net,
                "allocated_gross": draw_gross,
                "short_net": short_net,
                "reserve_balance_gross": q(available - draw_gross),
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def _compensation_score(self, state: State, round_id: str, donor: Transfer,
                            diverted_gross: float) -> float:
        rnd = state.rounds[round_id]
        region = rnd.regions[donor.region_id]
        coverage = 0.0
        for t in state.round_transfers(round_id):
            if t.region_id != donor.region_id:
                continue
            coverage += t.effective_gross * (1 - t.loss_rate)
        after_net = max(0.0, coverage - diverted_gross * (1 - donor.loss_rate))
        shortfall_ratio = max(0.0, region.baseline - after_net) / max(region.baseline, EPS)
        divert_ratio = diverted_gross / max(donor.gross_qty, EPS)
        return q(1000.0 * shortfall_ratio + 100.0 * divert_ratio + (200 - region.priority))

    def divert(self, *, round_id: str, donor_transfer_id: str,
               to_region_id: str, to_customer_id: str, qty_gross: float,
               beneficiary_loss_rate: float, authorized_by: str, reason: str,
               command_id: str | None = None) -> CommandResult:
        """经授权挤占某笔尚未发运的额度给紧急需求，并为被挤占方登记补偿优先级。

        硬约束：只能挤占 ``gross - 已发运 - 已挤占`` 的部分；已发运部分受保护。
        """
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "divert")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if rnd.status == RoundStatus.FINALIZED:
                raise RoundClosed("轮次已封账")
            donor = self._get_transfer(state, donor_transfer_id)
            if donor.round_id != round_id:
                raise ValidationFailed("被挤占调拨不属于本轮次")
            if donor.kind != "regular":
                raise NothingToDivert("只有常规额度可以被挤占")
            if donor.status == TransferStatus.CANCELLED:
                raise NothingToDivert("调拨已取消")
            if not 0 <= beneficiary_loss_rate < 1:
                raise ValidationFailed("损耗率非法")
            if qty_gross <= EPS:
                raise ValidationFailed("挤占数量必须为正")
            free = donor.divertable_gross
            if qty_gross > free + EPS:
                raise NothingToDivert(
                    f"可挤占额度只有 {free}（毛额），已发运部分不可挤占"
                )
            if self._now().timestamp() > rnd.deadline.timestamp() + EPS:
                raise ValidationFailed("已过截止线，挤占发运无意义")
            if to_region_id not in rnd.regions:
                raise ValidationFailed(f"区域不在本轮范围：{to_region_id}")

            now = self._now()
            comp_id = f"comp-{uuid.uuid4().hex[:10]}"
            score = self._compensation_score(state, round_id, donor, qty_gross)
            self._append(conn, "compensation.created", "compensation", comp_id, {
                "compensation_id": comp_id,
                "round_id": round_id,
                "donor_transfer_id": donor_transfer_id,
                "donor_region_id": donor.region_id,
                "donor_customer_id": donor.customer_id,
                "qty": q(qty_gross),
                "reason": reason,
                "priority_score": score,
                "authorized_by": authorized_by,
            }, command_id=command_id, occurred_at=now)
            self._append(conn, "transfer.diverted", "transfer", donor_transfer_id, {
                "transfer_id": donor_transfer_id,
                "qty_gross": q(qty_gross),
                "compensation_id": comp_id,
                "to_region_id": to_region_id,
                "to_customer_id": to_customer_id,
                "authorized_by": authorized_by,
            }, command_id=command_id, occurred_at=now)
            em_id = f"tr-em-{uuid.uuid4().hex[:10]}"
            self._append(conn, "transfer.created", "transfer", em_id, {
                "transfer_id": em_id,
                "round_id": round_id,
                "plan_id": rnd.active_plan_id or "emergency",
                "region_id": to_region_id,
                "customer_id": to_customer_id,
                "gross_qty": q(qty_gross),
                "net_qty": q(qty_gross * (1 - beneficiary_loss_rate)),
                "loss_rate": beneficiary_loss_rate,
                "kind": "emergency",
                "source": {
                    "fund": "diversion",
                    "donor_transfer_id": donor_transfer_id,
                    "compensation_id": comp_id,
                    "authorized_by": authorized_by,
                    "reason": reason,
                },
            }, command_id=command_id, occurred_at=now)
            return self._finish(conn, command_id, "divert", "diversion", em_id, {
                "emergency_transfer_id": em_id,
                "compensation_id": comp_id,
                "priority_score": score,
                "donor_remaining_divertable_gross": q(free - qty_gross),
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 保留池补充与补偿自动清偿 --------------------------------------------

    def replenish_reserve(self, *, round_id: str, qty_gross: float,
                          source_batch: str, command_id: str | None = None) -> CommandResult:
        """保留池到货补充：入账后按补偿优先级自动清偿，部分清偿也建单追踪。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "replenish_reserve")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if qty_gross <= EPS:
                raise ValidationFailed("补充数量必须为正")
            now = self._now()
            self._append(conn, "reserve.replenished", "round", round_id, {
                "round_id": round_id, "qty_gross": q(qty_gross),
                "source_batch": source_batch,
            }, command_id=command_id, occurred_at=now)

            balance = q(qty_gross)  # 本批可用于清偿的量（历史余额保留给未来紧急需求）
            settled = []
            for comp in state.compensation_queue(round_id):
                if balance <= EPS:
                    break
                donor = state.transfers[comp.donor_transfer_id]
                remaining = q(comp.qty - comp.settled_qty)
                if remaining <= EPS:
                    continue
                take = q(min(remaining, balance))
                tr_id = f"tr-cmp-{uuid.uuid4().hex[:10]}"
                self._append(conn, "transfer.created", "transfer", tr_id, {
                    "transfer_id": tr_id, "round_id": round_id,
                    "plan_id": rnd.active_plan_id or "compensation",
                    "region_id": comp.donor_region_id,
                    "customer_id": comp.donor_customer_id,
                    "gross_qty": take,
                    "net_qty": q(take * (1 - donor.loss_rate)),
                    "loss_rate": donor.loss_rate,
                    "kind": "compensation",
                    "source": {
                        "fund": "reserve",
                        "compensation_id": comp.compensation_id,
                        "source_batch": source_batch,
                    },
                }, command_id=command_id, occurred_at=now)
                self._append(conn, "reserve.used", "round", round_id, {
                    "round_id": round_id, "qty_gross": take,
                    "transfer_id": tr_id,
                }, command_id=command_id, occurred_at=now)
                fully = q(remaining - take) <= EPS
                self._append(conn, "compensation.settled", "compensation",
                             comp.compensation_id, {
                                "compensation_id": comp.compensation_id,
                                "qty_settled": take,
                                "fully": fully,
                                "settled_from": source_batch,
                                "compensation_transfer_id": tr_id,
                             }, command_id=command_id, occurred_at=now)
                balance = q(balance - take)
                settled.append({
                    "compensation_id": comp.compensation_id,
                    "transfer_id": tr_id,
                    "qty_gross": take,
                    "fully": fully,
                })
            return self._finish(conn, command_id, "replenish_reserve",
                                "reserve_batch", source_batch, {
                "batch_gross": q(qty_gross),
                "settled_gross": q(qty_gross - balance),
                "batch_remaining_gross": balance,
                "settled": settled,
            })
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def finalize_round(self, *, round_id: str, command_id: str | None = None) -> CommandResult:
        """封账前强制对平：补偿队列必须清空。"""
        command_id = command_id or f"cmd-{uuid.uuid4()}"
        conn = self.store.connect()
        try:
            _, existing = self._dedup(conn, command_id, "finalize_round")
            if existing:
                return self._replay(conn, existing)
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            if rnd.status != RoundStatus.CLOSED:
                raise RoundClosed("只有已关闭轮次可以封账")
            pending = state.compensation_queue(round_id)
            if pending:
                raise ReconciliationMismatch(
                    f"仍有 {len(pending)} 笔未清偿补偿，不能封账："
                    + ", ".join(c.compensation_id for c in pending)
                )
            self.reconcile(round_id, strict=True)
            self._append(conn, "round.finalized", "round", round_id,
                         {"round_id": round_id}, command_id=command_id)
            return self._finish(conn, command_id, "finalize_round", "round", round_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 对账：区域底线、保留池、已发运随时对平 ------------------------------

    def reconcile(self, round_id: str, *, strict: bool = False) -> dict:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            transfers = state.round_transfers(round_id)
            cancelled = [t for t in state.round_transfers(round_id, include_cancelled=True)
                         if t.status == TransferStatus.CANCELLED]
            regular_eff = q(sum(t.effective_gross for t in transfers if t.kind == "regular"))
            diverted = q(sum(t.diverted_gross for t in transfers if t.kind == "regular"))
            emergency_reserve = q(sum(
                t.gross_qty for t in transfers
                if t.kind == "emergency" and (t.source or {}).get("fund") == "reserve"
            ))
            emergency_diverted = q(sum(
                t.gross_qty for t in transfers
                if t.kind == "emergency" and (t.source or {}).get("fund") == "diversion"
            ))
            compensation_gross = q(sum(t.gross_qty for t in transfers if t.kind == "compensation"))
            cancelled_gross = q(sum(t.gross_qty for t in cancelled))
            reserve_debits = q(emergency_reserve + compensation_gross)

            # 总池恒等式：来源 = 存活常规净占 + 已挤占 + 保留池动用 + 自由余量
            # （被取消的旧额度已回归自由池，可能已被再版重新分配，故不重复计入）。
            sources = q(rnd.supply + rnd.reserve_added_gross)
            uses = q(regular_eff + diverted + reserve_debits)
            free_pool = q(sources - uses)

            # 保留池恒等式：初始 + 补充 - 动用事件 = 余额。
            reserve_balance_from_ledger = q(
                rnd.reserve + rnd.reserve_added_gross - rnd.reserve_used_gross
            )
            reserve_balance = rnd.reserve_balance_gross
            shipped = q(sum(t.dispatched_qty for t in transfers))
            arrived = q(sum(t.arrived_qty for t in transfers))

            coverage: dict[str, dict] = {}
            intransit = self._intransit_net(state, round_id)
            for region_id, region in rnd.regions.items():
                # 承诺净覆盖：所有存活调拨挤占后的有效额度（含已发布未发运）。
                committed_net = q(sum(
                    t.effective_gross * (1 - t.loss_rate)
                    for t in transfers if t.region_id == region_id
                ))
                receipt_net = q(sum(
                    max(t.arrived_qty, t.dispatched_qty, t.confirmed_qty)
                    * (1 - t.loss_rate)
                    for t in transfers if t.region_id == region_id
                ))
                it = intransit.get(region_id, 0.0)
                gap = q(max(0.0, region.baseline - committed_net - it))
                coverage[region_id] = {
                    "baseline": region.baseline,
                    "in_transit_net": it,
                    "committed_net": committed_net,
                    "receipt_net": receipt_net,
                    "covered": q(min(region.baseline, committed_net + it)),
                    "gap_net": gap,
                    "baseline_met": gap <= EPS,
                }

            pending = [
                {
                    "compensation_id": c.compensation_id,
                    "donor_transfer_id": c.donor_transfer_id,
                    "donor_region_id": c.donor_region_id,
                    "qty_remaining_gross": q(c.qty - c.settled_qty),
                    "priority_score": c.priority_score,
                    "rank": c.rank,
                }
                for c in state.compensation_queue(round_id)
            ]
            settled_total = q(sum(
                c.settled_qty for c in state.compensations.values() if c.round_id == round_id
            ))

            report = {
                "round_id": round_id,
                "status": rnd.status.value,
                "supply_gross": rnd.supply,
                "reserve_initial_gross": rnd.reserve,
                "reserve_replenished_gross": rnd.reserve_added_gross,
                "reserve_used_gross": rnd.reserve_used_gross,
                "sources_gross": sources,
                "regular_effective_gross": regular_eff,
                "diverted_gross": diverted,
                "emergency_from_reserve_gross": emergency_reserve,
                "emergency_from_diversion_gross": emergency_diverted,
                "compensation_gross": compensation_gross,
                "cancelled_gross": cancelled_gross,
                "uses_gross": uses,
                "free_pool_gross": free_pool,
                "reserve_balance_gross": reserve_balance,
                "reserve_debits_gross": reserve_debits,
                "shipped_gross": shipped,
                "arrived_gross": arrived,
                "diverted_settled_gross": settled_total,
                "diverted_unsettled_gross": q(diverted - settled_total),
                "compensation_pending": pending,
                "regions": coverage,
                "active_plan_id": rnd.active_plan_id,
            }
            if strict:
                problems = []
                if free_pool < -EPS:
                    problems.append(f"总池超发：{-free_pool}")
                if reserve_balance < -EPS:
                    problems.append(f"保留池透支：{-reserve_balance}")
                if abs(reserve_balance - reserve_balance_from_ledger) > EPS:
                    problems.append(
                        f"保留池台账不平：投影余额 {reserve_balance} / "
                        f"事件重算 {reserve_balance_from_ledger}"
                    )
                pending_sum = q(sum(p["qty_remaining_gross"] for p in pending))
                if abs(q(diverted - settled_total) - pending_sum) > EPS:
                    problems.append(
                        f"挤占补偿台账不平：未偿 {q(diverted - settled_total)} / "
                        f"队列合计 {pending_sum}"
                    )
                if abs(diverted - emergency_diverted) > EPS:
                    problems.append(
                        f"挤占与紧急调拨不平：挤占 {diverted} / 紧急到货 {emergency_diverted}"
                    )
                if problems:
                    raise ReconciliationMismatch("；".join(problems))
            return report
        finally:
            if self.store.path != ":memory:":
                conn.close()

    # -- 查询 ----------------------------------------------------------------

    def get_round(self, round_id: str) -> dict:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            rnd = self._get_round(state, round_id)
            return {
                "round_id": rnd.round_id,
                "status": rnd.status.value,
                "opened_at": rnd.opened_at.isoformat(),
                "deadline": rnd.deadline.isoformat(),
                "closed_at": rnd.closed_at.isoformat() if rnd.closed_at else None,
                "supply_gross": rnd.supply,
                "reserve_gross": rnd.reserve,
                "reserve_balance_gross": rnd.reserve_balance_gross,
                "active_plan_id": rnd.active_plan_id,
                "requests": len(rnd.requests),
                "emergency_requests": sum(1 for r in rnd.requests if r.emergency),
                "in_transit": [
                    {"source": i.source, "region_id": i.region_id, "qty": i.qty,
                     "eta": i.eta.isoformat(), "counted": i.counted}
                    for i in rnd.in_transit
                ],
                "simulations": dict(rnd.simulations),
            }
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def get_plan(self, plan_id: str) -> dict:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            plan = self._get_plan(state, plan_id)
            approvals = [
                {"level": a.level, "approver": a.approver, "approved": a.approved,
                 "decided_at": a.decided_at.isoformat(), "comment": a.comment}
                for a in sorted(state.approvals.get(plan_id, []), key=lambda a: a.level)
            ]
            return {
                "plan_id": plan.plan_id,
                "round_id": plan.round_id,
                "version": plan.version,
                "status": plan.status.value,
                "supply_gross": plan.supply,
                "reserve_gross": plan.reserve,
                "required_approvals": plan.required_approvals,
                "approval_chain": approvals,
                "rejected": plan.rejected,
                "supersedes": plan.supersedes,
                "published_at": plan.published_at.isoformat() if plan.published_at else None,
                "lines": [
                    {"region_id": l.region_id, "customer_id": l.customer_id,
                     "loss_rate": l.loss_rate,
                     "baseline_qty": l.baseline_qty,
                     "contract_qty": l.contract_qty,
                     "share_qty": l.share_qty,
                     "emergency_qty": l.emergency_qty,
                     "net_total": l.net_total}
                    for l in sorted(plan.lines.values(), key=lambda l: (l.region_id, l.customer_id))
                ],
            }
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def get_transfer(self, transfer_id: str) -> dict:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            t = self._get_transfer(state, transfer_id)
            return {
                "transfer_id": t.transfer_id,
                "round_id": t.round_id,
                "plan_id": t.plan_id,
                "region_id": t.region_id,
                "customer_id": t.customer_id,
                "kind": t.kind,
                "status": t.status.value,
                "gross_qty": t.gross_qty,
                "net_qty": t.net_qty,
                "loss_rate": t.loss_rate,
                "effective_gross": t.effective_gross,
                "divertable_gross": t.divertable_gross,
                "diverted_gross": t.diverted_gross,
                "confirmed_qty": t.confirmed_qty,
                "dispatched_qty": t.dispatched_qty,
                "arrived_qty": t.arrived_qty,
                "confirmed_at": t.confirmed_at.isoformat() if t.confirmed_at else None,
                "dispatched_at": t.dispatched_at.isoformat() if t.dispatched_at else None,
                "arrived_at": t.arrived_at.isoformat() if t.arrived_at else None,
                "compensation_id": t.compensation_id,
                "source": t.source,
            }
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def compensation_queue(self, round_id: str) -> list[dict]:
        conn = self.store.connect()
        try:
            state = self._load(conn)
            self._get_round(state, round_id)
            return [
                {
                    "rank": c.rank,
                    "compensation_id": c.compensation_id,
                    "donor_transfer_id": c.donor_transfer_id,
                    "donor_region_id": c.donor_region_id,
                    "donor_customer_id": c.donor_customer_id,
                    "qty_gross": c.qty,
                    "settled_qty": c.settled_qty,
                    "remaining_gross": q(c.qty - c.settled_qty),
                    "priority_score": c.priority_score,
                    "status": c.status.value,
                    "reason": c.reason,
                }
                for c in state.compensation_queue(round_id)
            ]
        finally:
            if self.store.path != ":memory:":
                conn.close()

    def event_log(self, aggregate_id: str | None = None) -> list[dict]:
        conn = self.store.connect()
        try:
            if aggregate_id:
                return self.store.load_events(conn, aggregate_id=aggregate_id)
            return self.store.load_events(conn)
        finally:
            if self.store.path != ":memory:":
                conn.close()
