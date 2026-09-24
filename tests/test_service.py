"""应用服务测试：完整业务规则与事件溯源持久化。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from allocation import AllocationService
from allocation.clock import FixedClock
from allocation.errors import (
    ApprovalConflict,
    NothingToDivert,
    PlanNotUsable,
    RoundClosed,
    TransferStateConflict,
)
from allocation.store import EventStore

TZ = timezone(timedelta(hours=8))


def make_service(path: str = ":memory:") -> tuple[AllocationService, FixedClock]:
    clock = FixedClock(datetime(2026, 9, 24, 8, 0, tzinfo=TZ))
    return AllocationService(path, clock), clock


def seed(svc: AllocationService, *, supply: float = 1000, reserve: float = 100,
         regions_=None) -> str:
    default = [
        ("HB", "湖北", 100, 10, 100),
        ("HN", "河南", 100, 20, 100),
        ("HUN", "湖南", 60, 30, 80),
        ("CSJ", "长三角", 120, 40, 120),
    ]
    for rid, name, base, prio, share in (regions_ or default):
        svc.register_region(region_id=rid, name=name, baseline=base, priority=prio,
                            historical_share=share, command_id=f"reg-{rid}")
    svc.open_round(round_id="R1", supply_gross=supply, reserve_gross=reserve,
                   deadline="2026-09-26T08:00:00+08:00", command_id="open")
    return "R1"


def add_requests(svc: AllocationService, rows=(), round_id: str = "R1") -> None:
    default = [
        ("c-hb", "HB", 120, 150, 0.05, 10),
        ("c-hn", "HN", 90, 120, 0.03, 12),
        ("c-hun", "HUN", 70, 90, 0.08, 8),
        ("c-csj", "CSJ", 130, 200, 0.02, 6),
    ]
    for cid, rid, cq, uq, lr, lt in (rows or default):
        svc.submit_request(round_id=round_id, customer_id=cid, region_id=rid,
                           contract_qty=cq, urgent_qty=uq, loss_rate=lr,
                           lead_time_h=lt, command_id=f"req-{cid}")


def publish(svc: AllocationService, *, approvals: int = 2,
            command_prefix: str = "v1", round_id: str = "R1",
            from_simulation: str | None = None) -> tuple[str, dict]:
    d = svc.create_draft(round_id=round_id, required_approvals=approvals,
                         from_simulation=from_simulation,
                         command_id=f"draft-{command_prefix}")
    for level in range(1, approvals + 1):
        svc.decide_approval(plan_id=d.result_id, approver=f"boss-{level}",
                            approved=True, level=level,
                            command_id=f"ap-{command_prefix}-{level}")
    pub = svc.publish_plan(plan_id=d.result_id, command_id=f"pub-{command_prefix}")
    return d.result_id, pub.extra


class LifecycleTest(unittest.TestCase):
    def test_full_publish_creates_transfers_and_plan(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        plan_id, extra = publish(svc)
        self.assertEqual(len(extra["transfers"]), 4)
        plan = svc.get_plan(plan_id)
        self.assertEqual(plan["status"], "published")
        self.assertEqual([a["level"] for a in plan["approval_chain"]], [1, 2])
        report = svc.reconcile("R1", strict=True)
        self.assertGreaterEqual(report["free_pool_gross"], 0)
        # 所有区域底线在充足货源下都应满足。
        self.assertTrue(all(r["baseline_met"] for r in report["regions"].values()))

    def test_approval_chain_must_be_sequential_and_complete(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        d = svc.create_draft(round_id="R1", required_approvals=2, command_id="draft")
        with self.assertRaises(ApprovalConflict):
            svc.decide_approval(plan_id=d.result_id, approver="x", approved=True,
                                level=2, command_id="ap-skip")
        svc.decide_approval(plan_id=d.result_id, approver="x", approved=True,
                            level=1, command_id="ap-1")
        with self.assertRaises(ApprovalConflict):
            svc.publish_plan(plan_id=d.result_id, command_id="pub-early")
        svc.decide_approval(plan_id=d.result_id, approver="y", approved=True,
                            level=2, command_id="ap-2")
        svc.publish_plan(plan_id=d.result_id, command_id="pub")

    def test_rejection_requires_new_version(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        d = svc.create_draft(round_id="R1", required_approvals=2, command_id="draft")
        svc.decide_approval(plan_id=d.result_id, approver="x", approved=False,
                            level=1, command_id="rej")
        with self.assertRaises(PlanNotUsable):
            svc.publish_plan(plan_id=d.result_id, command_id="pub")
        plan = svc.get_plan(d.result_id)
        self.assertTrue(plan["rejected"])

    def test_simulation_is_isolated(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        sim = svc.simulate_supply_reduction(
            round_id="R1", scenario="halt", reduced_supply_gross=400, command_id="sim")
        sim_plan = svc.get_plan(sim.result_id)
        self.assertEqual(sim_plan["status"], "simulated")
        # 模拟不产生任何调拨、不动保留池。
        self.assertEqual(svc.reconcile("R1")["regular_effective_gross"], 0)
        self.assertEqual(svc.get_round("R1")["reserve_balance_gross"], 100)
        # 可以基于确认过的模拟场景出正式草案。
        plan_id, _ = publish(svc, command_prefix="fromsim", from_simulation="halt")
        self.assertEqual(svc.get_plan(plan_id)["supply_gross"], 400)


class EmergencyTest(unittest.TestCase):
    def test_regular_request_rejected_after_close(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        with self.assertRaises(RoundClosed):
            add_requests(svc, [("late", "HB", 10, 10, 0.0, 5)])

    def test_emergency_uses_reserve_then_requires_authorization(self) -> None:
        svc, _ = make_service()
        seed(svc, supply=1000, reserve=50)
        add_requests(svc)
        _, extra = publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        em = svc.request_emergency(round_id="R1", customer_id="em1", region_id="HN",
                                   qty_net=40, loss_rate=0.0, lead_time_h=10,
                                   command_id="em1")
        self.assertAlmostEqual(em.extra["allocated_net"], 40)
        self.assertAlmostEqual(em.extra["short_net"], 0)
        big = svc.request_emergency(round_id="R1", customer_id="em2", region_id="CSJ",
                                    qty_net=100, loss_rate=0.0, lead_time_h=5,
                                    command_id="em2")
        self.assertAlmostEqual(big.extra["allocated_net"], 10)
        self.assertAlmostEqual(big.extra["short_net"], 90)
        # 部分满足：仍生成保留池调拨，剩余 90 净吨需要授权挤占。
        self.assertTrue(big.result_id.startswith("tr-em-"))
        report = svc.reconcile("R1", strict=True)
        self.assertAlmostEqual(report["reserve_balance_gross"], 0)

    def test_diversion_protects_shipped_and_creates_compensation(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        _, extra = publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        donor = extra["transfers"][0]
        info = svc.get_transfer(donor)
        # 先装运 60 毛吨。
        svc.record_receipt(transfer_id=donor, stage="confirmed", qty=info["gross_qty"],
                           happened_at="2026-09-24T09:00:00+08:00", command_id="cf")
        svc.record_receipt(transfer_id=donor, stage="dispatched", qty=60,
                           happened_at="2026-09-24T10:00:00+08:00", command_id="dp")
        # 只能挤占未发运的部分。
        self.assertAlmostEqual(svc.get_transfer(donor)["divertable_gross"],
                               info["gross_qty"] - 60, places=5)
        with self.assertRaises(NothingToDivert):
            svc.divert(round_id="R1", donor_transfer_id=donor, to_region_id="CSJ",
                       to_customer_id="em", qty_gross=info["gross_qty"],
                       beneficiary_loss_rate=0.0, authorized_by="chief",
                       reason="x", command_id="d-too-much")
        div = svc.divert(round_id="R1", donor_transfer_id=donor, to_region_id="CSJ",
                         to_customer_id="em", qty_gross=20,
                         beneficiary_loss_rate=0.02, authorized_by="chief",
                         reason="应急", command_id="d20")
        self.assertTrue(div.extra["compensation_id"])
        queue = svc.compensation_queue("R1")
        self.assertEqual(queue[0]["qty_gross"], 20)
        svc.reconcile("R1", strict=True)

    def test_compensation_priority_and_partial_settlement(self) -> None:
        svc, _ = make_service()
        seed(svc, supply=1000, reserve=100, regions_=[
            ("HB", "湖北", 100, 10, 0),
            ("HN", "河南", 100, 10, 0),
        ])
        add_requests(svc, [
            ("hb1", "HB", 100, 100, 0.0, 5),
            ("hn1", "HN", 100, 100, 0.0, 5),
        ])
        _, extra = publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        hb_tr, hn_tr = sorted(extra["transfers"])
        # HB 被挤占 90%（底线重创，优先级更高）；HN 被挤占 10%。
        svc.divert(round_id="R1", donor_transfer_id=hb_tr, to_region_id="HN",
                   to_customer_id="em-a", qty_gross=90,
                   beneficiary_loss_rate=0.0, authorized_by="chief",
                   reason="a", command_id="div-hb")
        svc.divert(round_id="R1", donor_transfer_id=hn_tr, to_region_id="HB",
                   to_customer_id="em-b", qty_gross=10,
                   beneficiary_loss_rate=0.0, authorized_by="chief",
                   reason="b", command_id="div-hn")
        queue = svc.compensation_queue("R1")
        self.assertEqual(queue[0]["donor_region_id"], "HB")
        self.assertEqual(queue[1]["donor_region_id"], "HN")

        # 部分清偿：先到 50，HB 补偿仍欠 40，队列不消失。
        r1 = svc.replenish_reserve(round_id="R1", qty_gross=50, source_batch="b1",
                                   command_id="rep1")
        self.assertEqual(r1.extra["settled"][0]["compensation_id"],
                         queue[0]["compensation_id"])
        self.assertFalse(r1.extra["settled"][0]["fully"])
        queue2 = svc.compensation_queue("R1")
        self.assertEqual(len(queue2), 2)
        self.assertAlmostEqual(queue2[0]["remaining_gross"], 40)

        # 再到 60：补 HB 40、补 HN 10，余 10 留在保留池。
        r2 = svc.replenish_reserve(round_id="R1", qty_gross=60, source_batch="b2",
                                   command_id="rep2")
        self.assertEqual({(s["compensation_id"], s["fully"]) for s in r2.extra["settled"]},
                         {(queue[0]["compensation_id"], True),
                          (queue[1]["compensation_id"], True)})
        self.assertEqual(svc.compensation_queue("R1"), [])
        self.assertAlmostEqual(svc.get_round("R1")["reserve_balance_gross"],
                               100 - 0 + 10, places=5)
        svc.finalize_round(round_id="R1", command_id="fin")

    def test_cannot_finalize_with_pending_compensation(self) -> None:
        svc, _ = make_service()
        seed(svc)
        add_requests(svc)
        _, extra = publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        svc.divert(round_id="R1", donor_transfer_id=extra["transfers"][0],
                   to_region_id="CSJ", to_customer_id="em", qty_gross=10,
                   beneficiary_loss_rate=0.0, authorized_by="chief",
                   reason="x", command_id="d")
        from allocation.errors import ReconciliationMismatch
        with self.assertRaises(ReconciliationMismatch):
            svc.finalize_round(round_id="R1", command_id="fin")


class ReceiptTest(unittest.TestCase):
    def _published_donor(self, svc) -> str:
        seed(svc)
        add_requests(svc)
        _, extra = publish(svc)
        return extra["transfers"][0]

    def test_ordering_enforced(self) -> None:
        svc, _ = make_service()
        donor = self._published_donor(svc)
        with self.assertRaises(TransferStateConflict):
            svc.record_receipt(transfer_id=donor, stage="arrived", qty=1,
                               command_id="bad")

    def test_qty_monotonic_and_cap(self) -> None:
        svc, _ = make_service()
        donor = self._published_donor(svc)
        gross = svc.get_transfer(donor)["gross_qty"]
        svc.record_receipt(transfer_id=donor, stage="confirmed", qty=gross,
                           command_id="c")
        with self.assertRaises(TransferStateConflict):
            svc.record_receipt(transfer_id=donor, stage="confirmed", qty=gross - 1,
                               command_id="c-back")
        with self.assertRaises(TransferStateConflict):
            svc.record_receipt(transfer_id=donor, stage="dispatched", qty=gross + 1,
                               command_id="d-over")

    def test_late_receipt_keeps_real_time_without_regression(self) -> None:
        svc, _ = make_service()
        donor = self._published_donor(svc)
        gross = svc.get_transfer(donor)["gross_qty"]
        svc.record_receipt(transfer_id=donor, stage="confirmed", qty=gross,
                           happened_at="2026-09-24T10:00:00+08:00", command_id="c")
        svc.record_receipt(transfer_id=donor, stage="dispatched", qty=gross,
                           happened_at="2026-09-24T12:00:00+08:00", command_id="d")
        svc.record_receipt(transfer_id=donor, stage="arrived", qty=gross,
                           happened_at="2026-09-24T18:00:00+08:00", command_id="a")
        # 迟到的装运补报（真实时间介于确认与到达之间）：状态必须保持 arrived。
        late = svc.record_receipt(
            transfer_id=donor, stage="dispatched", qty=gross,
            happened_at="2026-09-24T11:30:00+08:00", command_id="d-late")
        self.assertTrue(late.extra["late"])
        info = svc.get_transfer(donor)
        self.assertEqual(info["status"], "arrived")
        self.assertEqual(info["dispatched_at"], "2026-09-24T11:30:00+08:00")
        # 更早的确认补报归入真实时间，但状态不倒退。
        svc.record_receipt(transfer_id=donor, stage="confirmed", qty=gross,
                           happened_at="2026-09-24T09:00:00+08:00",
                           command_id="c-early")
        info = svc.get_transfer(donor)
        self.assertEqual(info["confirmed_at"], "2026-09-24T09:00:00+08:00")
        self.assertEqual(info["status"], "arrived")

    def test_retry_does_not_deduct_twice(self) -> None:
        svc, _ = make_service()
        seed(svc, supply=1000, reserve=50)
        add_requests(svc)
        publish(svc)
        svc.close_round(round_id="R1", command_id="close")
        first = svc.request_emergency(round_id="R1", customer_id="em", region_id="HN",
                                      qty_net=30, loss_rate=0.0, lead_time_h=5,
                                      command_id="em-same")
        retry = svc.request_emergency(round_id="R1", customer_id="em", region_id="HN",
                                      qty_net=30, loss_rate=0.0, lead_time_h=5,
                                      command_id="em-same")
        self.assertTrue(retry.replayed)
        self.assertEqual(first.result_id, retry.result_id)
        report = svc.reconcile("R1", strict=True)
        self.assertAlmostEqual(report["emergency_from_reserve_gross"], 30)
        self.assertAlmostEqual(report["reserve_balance_gross"], 20)
        # 回执重试同样回放，不重复推进。
        tr = first.result_id
        svc.record_receipt(transfer_id=tr, stage="confirmed", qty=30,
                           command_id="rc-same")
        again = svc.record_receipt(transfer_id=tr, stage="confirmed", qty=30,
                                   command_id="rc-same")
        self.assertTrue(again.replayed)
        self.assertEqual(svc.get_transfer(tr)["confirmed_qty"], 30)


class VersioningTest(unittest.TestCase):
    def test_new_version_cancels_only_unshipped(self) -> None:
        svc, clock = make_service()
        seed(svc)
        add_requests(svc)
        plan1, extra1 = publish(svc)
        transfers1 = extra1["transfers"]
        # 第一笔完成确认+装运（锁定，不可回收）。
        t0 = transfers1[0]
        gross0 = svc.get_transfer(t0)["gross_qty"]
        svc.record_receipt(transfer_id=t0, stage="confirmed", qty=gross0,
                           happened_at="2026-09-24T09:00:00+08:00", command_id="c1")
        svc.record_receipt(transfer_id=t0, stage="dispatched", qty=gross0,
                           happened_at="2026-09-24T10:00:00+08:00", command_id="d1")
        clock.advance(hours=1)
        plan2, extra2 = publish(svc, command_prefix="v2")
        self.assertNotIn(t0, extra2["cancelled"])
        # 旧版本未发运调拨全部被取消释放。
        self.assertEqual(set(extra2["cancelled"]), set(transfers1[1:]))
        self.assertEqual(svc.get_transfer(t0)["status"], "dispatched")
        for t in transfers1[1:]:
            self.assertEqual(svc.get_transfer(t)["status"], "cancelled")
        self.assertEqual(svc.get_plan(plan1)["status"], "superseded")
        self.assertEqual(svc.get_plan(plan2)["status"], "published")
        svc.reconcile("R1", strict=True)

    def test_new_version_does_not_reallocate_locked_line(self) -> None:
        """已确认客户在再版中不得被重复计入合同兑现（防止超发）。"""
        svc, clock = make_service()
        seed(svc, supply=200, reserve=0, regions_=[
            ("HB", "湖北", 50, 10, 0),
            ("HN", "河南", 50, 10, 0),
        ])
        add_requests(svc, [
            ("hb1", "HB", 100, 100, 0.0, 5),
            ("hn1", "HN", 100, 100, 0.0, 5),
        ])
        _, e1 = publish(svc)
        # HB 整笔确认并发运，锁定 100。
        hb, hn = sorted(e1["transfers"])
        g = svc.get_transfer(hb)["gross_qty"]
        svc.record_receipt(transfer_id=hb, stage="confirmed", qty=g, command_id="c")
        svc.record_receipt(transfer_id=hb, stage="dispatched", qty=g, command_id="d")
        clock.advance(hours=1)
        _, e2 = publish(svc, command_prefix="v2")
        # HB 的锁定单保留；旧 HN 单（PLANNED）被取消，v2 只为 HN 建新单。
        self.assertEqual(svc.get_transfer(hb)["status"], "dispatched")
        self.assertEqual(svc.get_transfer(hn)["status"], "cancelled")
        report = svc.reconcile("R1", strict=True)
        # 总承诺不得超过 200 货源；HB 100 锁定 + HN 100 新单。
        self.assertAlmostEqual(report["regular_effective_gross"], 200, places=5)
        self.assertAlmostEqual(report["free_pool_gross"], 0, places=5)
        # HB 不应在 v2 出现重复新单。
        plan2 = svc.get_plan(svc.get_round("R1")["active_plan_id"])
        hb_lines = [l for l in plan2["lines"] if l["region_id"] == "HB"]
        self.assertTrue(all(l["net_total"] == 0 for l in hb_lines))


class RestartTest(unittest.TestCase):
    def test_state_recovers_from_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "allocation.db")
            svc, clock = make_service(db)
            seed(svc)
            add_requests(svc)
            publish(svc)
            svc.close_round(round_id="R1", command_id="close")
            svc.request_emergency(round_id="R1", customer_id="em", region_id="HN",
                                  qty_net=30, loss_rate=0.0, lead_time_h=5,
                                  command_id="em")

            # 模拟进程重启：新建服务实例，从事件日志重放，未决轮次继续。
            svc2, clock2 = make_service(db)
            rnd = svc2.get_round("R1")
            self.assertEqual(rnd["status"], "closed")
            self.assertIsNotNone(rnd["active_plan_id"])
            self.assertAlmostEqual(rnd["reserve_balance_gross"], 70)
            report = svc2.reconcile("R1", strict=True)
            self.assertGreaterEqual(report["free_pool_gross"], 0)
            # 重启后用新 command_id 继续挤占与补偿，仍可封账。
            from allocation.projection import replay
            conn = svc2.store.connect()
            state = replay(svc2.store.load_events(conn))
            donor = next(
                t.transfer_id for t in state.round_transfers("R1")
                if t.kind == "regular" and t.divertable_gross > 10
            )
            conn.close()
            svc2.divert(round_id="R1", donor_transfer_id=donor, to_region_id="HUN",
                        to_customer_id="em2", qty_gross=10,
                        beneficiary_loss_rate=0.0, authorized_by="chief",
                        reason="重启后挤占", command_id="div")
            svc2.replenish_reserve(round_id="R1", qty_gross=100, source_batch="b",
                                   command_id="rep")
            svc2.finalize_round(round_id="R1", command_id="fin")
            self.assertEqual(svc2.get_round("R1")["status"], "finalized")


if __name__ == "__main__":
    unittest.main()
