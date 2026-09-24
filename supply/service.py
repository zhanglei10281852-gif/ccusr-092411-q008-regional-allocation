"""应用服务：命令处理、幂等、批准链、紧急挤占与对平。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .allocation import Lane, Request, plan_rounds
from .errors import AuthorizationRequired, ConflictError, NotFoundError, ValidationError
from .store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValidationError("时间必须包含时区")
    return dt


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ================= 主数据 =================
    def register_region(self, region: str, floor: float, loss_rate: float,
                        transit_days: float, historical_share: float) -> None:
        if floor < 0 or not 0 <= loss_rate < 1 or transit_days < 0 or historical_share < 0:
            raise ValidationError(f"区域参数非法：{region}")
        self.store.upsert_region(region, floor, loss_rate, transit_days, historical_share)
        self.store.commit()

    def register_customer(self, customer: str, region: str) -> None:
        if not self.store.conn.execute("select 1 from regions where region=?", (region,)).fetchone():
            raise NotFoundError(f"区域未登记：{region}")
        self.store.upsert_customer(customer, region)
        self.store.commit()

    def _lanes(self) -> list[Lane]:
        return [
            Lane(r["region"], r["floor"], r["loss_rate"], r["transit_days"], r["historical_share"])
            for r in self.store.conn.execute("select * from regions").fetchall()
        ]

    # ================= 幂等外壳 =================
    def _idem(self, request_id: str | None):
        if not request_id:
            return None
        row = self.store.get_command(request_id)
        if row is not None:
            return json.loads(row["event_ids"])
        return False  # 登记过但区分"首次"

    def _finish(self, request_id: str | None, command: str, aggregate_id: str,
                event_ids: list[str], result: dict[str, Any]) -> dict[str, Any]:
        if request_id:
            self.store.record_command(request_id, command, aggregate_id, event_ids)
        self.store.commit()
        return result

    # ================= 轮次 =================
    def open_round(self, round_id: str, supply: float, reserve: float,
                   demand: list[dict[str, Any]], in_transit: dict[str, float] | None = None,
                   request_id: str | None = None, occurred_at: str | None = None) -> dict[str, Any]:
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "round_id": round_id, "event_ids": cached}
        if self.store.conn.execute("select 1 from rounds where round_id=?", (round_id,)).fetchone():
            raise ConflictError(f"轮次已存在：{round_id}")
        if supply <= 0 or not 0 <= reserve <= supply:
            raise ValidationError("货源与保留池参数非法")
        lanes = self._lanes()
        requests = [self._make_request(d) for d in demand]
        snapshot = self._snapshot(supply, reserve, lanes, requests, in_transit or {})
        eid = _new_id("evt")
        self.store.append_event(
            eid, "round.opened", round_id, occurred_at or _now(),
            {"supply": supply, "reserve": reserve, "snapshot": snapshot},
        )
        return self._finish(request_id, "open_round", round_id, [eid],
                            {"round_id": round_id, "event_ids": [eid], "snapshot": snapshot})

    def _make_request(self, d: dict[str, Any]) -> Request:
        customer = d["customer"]
        row = self.store.conn.execute("select region from customers where customer=?", (customer,)).fetchone()
        if row is None:
            raise NotFoundError(f"客户未登记：{customer}")
        region = d.get("region", row["region"])
        if region != row["region"]:
            raise ValidationError(f"客户 {customer} 不属于区域 {region}")
        return Request(customer, region, float(d["quantity"]),
                       float(d.get("contract_minimum", 0.0)), bool(d.get("urgent", False)))

    def _snapshot(self, supply, reserve, lanes, requests, in_transit) -> dict[str, Any]:
        plan = plan_rounds(supply, lanes, requests, in_transit=in_transit, reserve=reserve)
        return {
            "supply": supply,
            "reserve": reserve,
            "in_transit": in_transit,
            "requests": [
                {"customer": r.customer, "region": r.region, "quantity": r.quantity,
                 "contract_minimum": r.contract_minimum, "urgent": r.urgent}
                for r in requests
            ],
            "allocations": [
                {"customer": a.customer, "region": a.region, "qty": a.quantity,
                 "by_round": a.by_round}
                for a in sorted(plan.allocations.values(), key=lambda a: a.customer)
            ],
            "used": plan.used,
            "leftover": plan.leftover,
            "floor_shortfall": plan.floor_shortfall,
            "unmet": plan.unmet,
        }

    # ================= 隔离模拟 =================
    def simulate_supply_reduction(self, round_id: str, supply: float, reserve: float | None = None,
                                  request_id: str | None = None,
                                  occurred_at: str | None = None) -> dict[str, Any]:
        """在不影响已发布方案的前提下，模拟供应缩减后的分配。"""
        row = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        if row["state"] == "closed":
            raise ConflictError("轮次已关闭，不能再模拟")
        base = self._published_or_open_snapshot(round_id)
        reserve = base["reserve"] if reserve is None else reserve
        if not 0 <= reserve <= supply:
            raise ValidationError("保留池参数非法")
        lanes = self._lanes()
        requests = [Request(**{k: v for k, v in r.items()}) for r in base["requests"]]
        snapshot = self._snapshot(supply, reserve, lanes, requests, base.get("in_transit", {}))
        version = int(self.store.conn.execute(
            "select coalesce(max(version),0)+1 as v from plan_versions where round_id=?",
            (round_id,)).fetchone()["v"])
        version_id = f"{round_id}:v{version}"
        eid = _new_id("evt")
        self.store.append_event(
            eid, "plan.simulated", round_id, occurred_at or _now(),
            {"version_id": version_id, "version": version, "supply": supply,
             "reserve": reserve, "snapshot": snapshot},
        )
        result = {"version_id": version_id, "version": version, "isolated": True,
                  "event_ids": [eid], "snapshot": snapshot}
        return self._finish(request_id, "simulate", round_id, [eid], result)

    def _published_or_open_snapshot(self, round_id: str) -> dict[str, Any]:
        row = self.store.conn.execute(
            "select snapshot from plan_versions where round_id=? and state='published' "
            "order by version desc limit 1", (round_id,)).fetchone()
        if row:
            return json.loads(row["snapshot"])
        row = self.store.conn.execute(
            "select snapshot from plan_versions where round_id=? order by version limit 1",
            (round_id,)).fetchone()
        return json.loads(row["snapshot"])

    # ================= 批准链与发布 =================
    def approve(self, version_id: str, approver: str, role: str, sequence: int,
                decision: str = "approved", comment: str | None = None,
                decided_at: str | None = None) -> None:
        if decision not in ("approved", "rejected"):
            raise ValidationError("审批决定只能是 approved/rejected")
        if not self.store.conn.execute("select 1 from plan_versions where version_id=?",
                                       (version_id,)).fetchone():
            raise NotFoundError(f"版本不存在：{version_id}")
        try:
            self.store.add_approval(version_id, approver, role, sequence, decision,
                                    comment, decided_at or _now())
            self.store.commit()
        except Exception as exc:  # 唯一约束
            raise ConflictError(f"审批人已在该版本签批：{approver}") from exc

    def publish(self, round_id: str, version_id: str, required_chain: list[str],
                request_id: str | None = None, occurred_at: str | None = None) -> dict[str, Any]:
        """发布正式版本，required_chain 为按顺序必须全部 approved 的角色链。"""
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "round_id": round_id, "event_ids": cached}
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        if rnd["published_version"] is not None:
            raise ConflictError("该轮次已有正式发布版本")
        ver = self.store.conn.execute(
            "select * from plan_versions where version_id=?", (version_id,)).fetchone()
        if ver is None:
            raise NotFoundError(f"版本不存在：{version_id}")
        if ver["state"] == "published":
            raise ConflictError("版本已发布")
        approvals = self.store.list_approvals(version_id)
        decisions: dict[str, str] = {}
        order: list[tuple[int, str]] = []
        for a in approvals:
            decisions[a["role"]] = a["decision"]
            order.append((a["sequence"], a["role"]))
        missing = [role for role in required_chain if decisions.get(role) != "approved"]
        if missing:
            raise ConflictError("批准链不完整，缺少或未批准：" + "、".join(missing))
        chain_order = [role for _, role in sorted(order)]
        seq_required = [required_chain.index(role) for role in chain_order if role in required_chain]
        if seq_required != sorted(seq_required) or len(chain_order) != len(set(chain_order)):
            raise ConflictError("批准链顺序与重复签批校验失败")

        snapshot = json.loads(ver["snapshot"])
        events: list[str] = []
        eid = _new_id("evt")
        self.store.append_event(
            eid, "allocation.published", round_id, occurred_at or _now(),
            {"version_id": version_id, "version": ver["version"],
             "approval_chain": [a["role"] for a in approvals], "snapshot": snapshot},
        )
        events.append(eid)
        return self._finish(request_id, "publish", round_id, events,
                            {"round_id": round_id, "version_id": version_id,
                             "event_ids": events, "snapshot": snapshot})

    def close_round(self, round_id: str, request_id: str | None = None,
                    occurred_at: str | None = None) -> dict[str, Any]:
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "event_ids": cached}
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        if rnd["published_version"] is None:
            raise ConflictError("未发布的轮次不能关闭")
        if rnd["state"] == "closed":
            raise ConflictError("轮次已关闭")
        eid = _new_id("evt")
        self.store.append_event(eid, "round.closed", round_id, occurred_at or _now(), {})
        return self._finish(request_id, "close_round", round_id, [eid],
                            {"round_id": round_id, "event_ids": [eid]})

    # ================= 运输回执（顺序推进、迟到不倒退、重试幂等）=================
    def _advance(self, transfer_id: str, event_type: str, stage: str,
                 qty: float, occurred_at: str | None, request_id: str | None,
                 prerequisite: str) -> dict[str, Any]:
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "transfer_id": transfer_id, "event_ids": cached}
        when = _parse(occurred_at) if occurred_at else datetime.now(timezone.utc)
        tr = self.store.conn.execute("select * from transfers where transfer_id=?",
                                     (transfer_id,)).fetchone()
        if tr is None:
            raise NotFoundError(f"调拨单不存在：{transfer_id}")
        existing = self.store.conn.execute(
            "select stage from transfer_stages where transfer_id=? and stage=?",
            (transfer_id, stage)).fetchone()
        if existing:
            raise ConflictError(f"{stage} 回执已登记，不能重复扣配额")
        from .store import STAGE_RANK
        recorded_ranks = [STAGE_RANK[s] for s, in self.store.conn.execute(
            "select stage from transfer_stages where transfer_id=?", (transfer_id,)).fetchall()]
        if prerequisite:
            pre_rank = STAGE_RANK[prerequisite]
            # 正常情况要求前序回执；但若已有更后阶段（迟到回执补登记）也允许，
            # 状态投影只进不退。
            if not any(rank >= pre_rank for rank in recorded_ranks):
                raise ConflictError(f"必须先收到 {prerequisite} 回执")
        if qty is not None and qty < 0:
            raise ValidationError("回执数量不能为负")
        eid = _new_id("evt")
        self.store.append_event(
            eid, event_type, transfer_id, when.isoformat(),
            {"qty": qty if qty is not None else tr["quantity"]},
        )
        return self._finish(request_id, stage, transfer_id, [eid],
                            {"transfer_id": transfer_id, "stage": stage,
                             "occurred_at": when.isoformat(), "event_ids": [eid]})

    def confirm(self, transfer_id: str, occurred_at: str | None = None,
                request_id: str | None = None) -> dict[str, Any]:
        return self._advance(transfer_id, "transfer.confirmed", "confirmed", None,
                             occurred_at, request_id, prerequisite=None)

    def dispatch(self, transfer_id: str, occurred_at: str | None = None,
                 request_id: str | None = None) -> dict[str, Any]:
        return self._advance(transfer_id, "transfer.dispatched", "dispatched", None,
                             occurred_at, request_id, prerequisite="confirmed")

    def arrive(self, transfer_id: str, qty: float | None = None, occurred_at: str | None = None,
               request_id: str | None = None) -> dict[str, Any]:
        return self._advance(transfer_id, "receipt.recorded", "arrived", qty,
                             occurred_at, request_id, prerequisite="dispatched")

    # ================= 紧急需求：保留池 / 授权挤占 / 补偿 =================
    def emergency_request(self, round_id: str, customer: str, quantity: float,
                          authorization: str | None = None, request_id: str | None = None,
                          occurred_at: str | None = None) -> dict[str, Any]:
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "round_id": round_id, "event_ids": cached}
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        if rnd["published_version"] is None:
            raise ConflictError("常规轮次尚未发布，不能受理紧急需求")
        if quantity <= 0:
            raise ValidationError("紧急需求量必须为正")
        target = self.store.conn.execute(
            "select * from transfers where round_id=? and customer=?", (round_id, customer)).fetchone()
        if target is None:
            raise NotFoundError(f"客户本轮没有调拨单：{customer}")

        events: list[str] = []
        when = occurred_at or _now()
        outstanding = quantity
        from_reserve = min(outstanding, rnd["reserve_remaining"])
        if from_reserve > 1e-9:
            eid = _new_id("evt")
            self.store.append_event(
                eid, "reserve.released", round_id, when,
                {"transfer_id": target["transfer_id"], "customer": customer,
                 "region": target["region"], "qty": round(from_reserve, 9),
                 "authorization": None},
            )
            events.append(eid)
            outstanding -= from_reserve

        preemptions: list[dict[str, Any]] = []
        if outstanding > 1e-9:
            if not authorization:
                self.store.rollback()
                raise AuthorizationRequired(
                    f"保留池不足，尚缺 {outstanding:.3f}，须提供授权以挤占未发运额度")
            # 候选：未确认（未发运）的他方调拨单
            candidates = []
            for tr in self.store.conn.execute(
                    "select * from transfers where round_id=? and customer<>? order by transfer_id",
                    (round_id, customer)).fetchall():
                if tr["status"] != "published":
                    continue  # 已确认/已发运/已到达的额度不能挤占
                lane = self.store.conn.execute(
                    "select transit_days, historical_share from regions where region=?",
                    (tr["region"],)).fetchone()
                candidates.append((tr, lane["transit_days"], lane["historical_share"]))
            # 时效快者先被挤、历史份额低者先被挤
            candidates.sort(key=lambda c: (c[1], c[2], c[0]["transfer_id"]))

            priority_counter = self.store.conn.execute(
                "select coalesce(max(priority),0) as m from compensations where round_id=?",
                (round_id,)).fetchone()["m"]

            def take(victim_tr, give: float, tier: int) -> None:
                nonlocal priority_counter, outstanding
                priority_counter += 1
                comp_id = _new_id("comp")
                eid = _new_id("evt")
                self.store.append_event(
                    eid, "quota.preempted", round_id, when,
                    {"transfer_id": target["transfer_id"], "customer": customer,
                     "region": target["region"], "qty": round(give, 9),
                     "victim_transfer": victim_tr["transfer_id"],
                     "victim_customer": victim_tr["customer"],
                     "comp_id": comp_id, "tier": tier, "authorization": authorization},
                )
                events.append(eid)
                eid2 = _new_id("evt")
                self.store.append_event(
                    eid2, "compensation.granted", round_id, when,
                    {"comp_id": comp_id, "victim_customer": victim_tr["customer"],
                     "qty": round(give, 9), "tier": tier, "priority": priority_counter,
                     "victim_transfer": victim_tr["transfer_id"],
                     "authorization": authorization},
                )
                events.append(eid2)
                preemptions.append({"victim_customer": victim_tr["customer"],
                                    "qty": round(give, 9), "tier": tier,
                                    "comp_id": comp_id, "priority": priority_counter})
                outstanding -= give

            by_round_cache = {tr["transfer_id"]: json.loads(tr["by_round"]) for tr, _, _ in candidates}
            for tr, _, _ in candidates:
                if outstanding <= 1e-9:
                    break
                br = by_round_cache[tr["transfer_id"]]
                non_floor = br.get("share", 0.0) + br.get("contract", 0.0)
                # 先挤合同/份额轮额度（tier 1）
                give1 = min(tr["quantity"], non_floor, outstanding)
                if give1 > 1e-9:
                    take(tr, give1, 1)
                # 仍有缺口才授权挤占底线份额（tier 0，最高补偿优先级）
                if outstanding > 1e-9:
                    floor_left = max(0.0, tr["quantity"] - give1)
                    give0 = min(floor_left, outstanding)
                    if give0 > 1e-9:
                        take(tr, give0, 0)

            if outstanding > 1e-9:
                self.store.rollback()
                raise ConflictError(f"保留池与可挤占额度不足，尚缺 {outstanding:.3f}")

        return self._finish(request_id, "emergency", round_id, events,
                            {"round_id": round_id, "customer": customer, "qty": quantity,
                             "from_reserve": round(from_reserve, 9),
                             "preemptions": preemptions, "event_ids": events})

    def replenish_reserve(self, round_id: str, qty: float,
                          request_id: str | None = None,
                          occurred_at: str | None = None) -> dict[str, Any]:
        """后续补给入保留池，供按补偿优先级兑现被挤占方。"""
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "event_ids": cached}
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        if qty <= 0:
            raise ValidationError("补给量必须为正")
        eid = _new_id("evt")
        self.store.append_event(
            eid, "reserve.replenished", round_id, occurred_at or _now(), {"qty": qty},
        )
        return self._finish(request_id, "replenish", round_id, [eid],
                            {"round_id": round_id, "qty": qty, "event_ids": [eid]})

    def settle_compensation(self, comp_id: str, request_id: str | None = None,
                            occurred_at: str | None = None) -> dict[str, Any]:
        """用保留池补发，兑现补偿优先级（调用方按 priority 顺序调用）。"""
        cached = self._idem(request_id)
        if cached is not None and cached is not False:
            return {"replayed": True, "comp_id": comp_id, "event_ids": cached}
        comp = self.store.conn.execute("select * from compensations where comp_id=?",
                                       (comp_id,)).fetchone()
        if comp is None:
            raise NotFoundError(f"补偿单不存在：{comp_id}")
        if comp["state"] == "compensated":
            raise ConflictError("补偿单已兑现")
        rnd = self.store.conn.execute("select * from rounds where round_id=?",
                                      (comp["round_id"],)).fetchone()
        if rnd["reserve_remaining"] + 1e-9 < comp["qty"]:
            raise ConflictError("保留池余额不足以兑现该补偿")
        # 严格按"层级（0 最优先）+ 同层挤占先后"兑现
        earlier = self.store.conn.execute(
            "select count(*) as c from compensations where round_id=? and state='compensating' "
            "and (tier < ? or (tier = ? and priority < ?))",
            (comp["round_id"], comp["tier"], comp["tier"], comp["priority"])).fetchone()["c"]
        if earlier:
            raise ConflictError("存在更高优先级（更早被挤占）的补偿未兑现")
        target = self.store.conn.execute(
            "select * from transfers where round_id=? and customer=?",
            (comp["round_id"], comp["victim_customer"])).fetchone()
        when = occurred_at or _now()
        events: list[str] = []
        eid = _new_id("evt")
        self.store.append_event(
            eid, "reserve.released", comp["round_id"], when,
            {"transfer_id": target["transfer_id"], "customer": comp["victim_customer"],
             "region": target["region"], "qty": comp["qty"], "comp_id": comp_id},
        )
        events.append(eid)
        eid2 = _new_id("evt")
        self.store.append_event(
            eid2, "compensation.settled", comp["round_id"], when,
            {"comp_id": comp_id, "settlement_transfer_id": target["transfer_id"]},
        )
        events.append(eid2)
        return self._finish(request_id, "settle_comp", comp_id, events,
                            {"comp_id": comp_id, "transfer_id": target["transfer_id"],
                             "event_ids": events})

    # ================= 对平 =================
    def reconcile(self, round_id: str) -> dict[str, Any]:
        """随时对平：区域底线、保留池、已发运数量与总货源恒等式。"""
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        transfers = self.store.conn.execute(
            "select * from transfers where round_id=?", (round_id,)).fetchall()
        stages = {
            (s["transfer_id"], s["stage"]): s
            for s in self.store.conn.execute(
                "select * from transfer_stages where transfer_id in "
                "(select transfer_id from transfers where round_id=?)", (round_id,)).fetchall()
        }
        lanes = {lane.region: lane for lane in self._lanes()}

        total_live_qty = sum(t["quantity"] for t in transfers)
        shipped = 0.0
        arrived_net_by_region: dict[str, float] = {}
        confirmed_net_by_region: dict[str, float] = {}
        for t in transfers:
            rank = 0
            qty = 0.0
            for stage, r in (("arrived", 4), ("dispatched", 3), ("confirmed", 2)):
                s = stages.get((t["transfer_id"], stage))
                if s:
                    rank, qty = r, s["qty"]
                    break
            if rank >= 2:
                shipped += qty
            if rank >= 2:
                lane = lanes.get(t["region"])
                net = qty * (1 - lane.loss_rate) if lane else qty
                confirmed_net_by_region[t["region"]] = confirmed_net_by_region.get(t["region"], 0.0) + net
            if rank == 4:
                lane = lanes.get(t["region"])
                net = qty * (1 - lane.loss_rate) if lane else qty
                arrived_net_by_region[t["region"]] = arrived_net_by_region.get(t["region"], 0.0) + net

        ver = self.store.conn.execute(
            "select snapshot from plan_versions where round_id=? and state='published' "
            "order by version desc limit 1", (round_id,)).fetchone()
        snapshot = json.loads(ver["snapshot"]) if ver else json.loads(
            self.store.conn.execute(
                "select snapshot from plan_versions where round_id=? order by version limit 1",
                (round_id,)).fetchone()["snapshot"])
        in_transit = snapshot.get("in_transit", {})
        leftover = snapshot["leftover"]
        released = round(rnd["reserve_total"] - rnd["reserve_remaining"], 9)

        # 恒等式：现存调拨有效量 + 保留池余额 + 常规轮余量 = 总货源
        identity_lhs = round(total_live_qty + rnd["reserve_remaining"] + leftover, 9)
        identity_rhs = round(rnd["supply"], 9)
        balanced = abs(identity_lhs - identity_rhs) < 1e-6

        floor_check = {}
        for region, lane in lanes.items():
            floor_check[region] = {
                "floor": lane.floor,
                "in_transit": in_transit.get(region, 0.0),
                "confirmed_net": round(confirmed_net_by_region.get(region, 0.0), 9),
                "arrived_net": round(arrived_net_by_region.get(region, 0.0), 9),
                "floor_met_by_confirmation": confirmed_net_by_region.get(region, 0.0)
                + in_transit.get(region, 0.0) + 1e-9 >= lane.floor,
            }

        compensations = [
            {"comp_id": c["comp_id"], "victim_customer": c["victim_customer"],
             "qty": c["qty"], "tier": c["tier"], "priority": c["priority"], "state": c["state"]}
            for c in self.store.conn.execute(
                "select * from compensations where round_id=? order by tier,priority",
                (round_id,)).fetchall()
        ]
        return {
            "round_id": round_id,
            "state": rnd["state"],
            "supply": rnd["supply"],
            "reserve_total": rnd["reserve_total"],
            "reserve_remaining": round(rnd["reserve_remaining"], 9),
            "reserve_released": released,
            "regular_leftover": leftover,
            "live_transfer_qty": round(total_live_qty, 9),
            "shipped_qty": round(shipped, 9),
            "identity": {"lhs": identity_lhs, "rhs": identity_rhs, "balanced": balanced},
            "floor_check": floor_check,
            "compensations": compensations,
        }

    # ================= 查询 =================
    def get_round(self, round_id: str) -> dict[str, Any]:
        rnd = self.store.conn.execute("select * from rounds where round_id=?", (round_id,)).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在：{round_id}")
        return dict(rnd)

    def list_transfers(self, round_id: str) -> list[dict[str, Any]]:
        return [dict(t) for t in self.store.conn.execute(
            "select * from transfers where round_id=? order by transfer_id", (round_id,)).fetchall()]

    def transfer_status(self, transfer_id: str) -> dict[str, Any]:
        t = self.store.conn.execute("select * from transfers where transfer_id=?",
                                    (transfer_id,)).fetchone()
        if t is None:
            raise NotFoundError(f"调拨单不存在：{transfer_id}")
        d = dict(t)
        d["stages"] = [dict(s) for s in self.store.conn.execute(
            "select * from transfer_stages where transfer_id=? order by occurred_at",
            (transfer_id,)).fetchall()]
        return d
