"""验收场景：四区域同日加急、分轮决策、紧急挤占、回执、模拟发布、重启续跑、随时对平。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from allocation import AllocationService
from allocation.clock import FixedClock
from allocation.engine import run_allocation
from allocation.models import Region, RequestLine

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 8, 0, tzinfo=TZ)
DEADLINE = "2026-09-26T08:00:00+08:00"

# 湖北、河南、湖南、长三角
REGION_DEFS = [
    ("HB", "湖北", 100, 10, 100),
    ("HN", "河南", 80, 20, 80),
    ("HUN", "湖南", 60, 30, 60),
    ("CSJ", "长三角", 120, 40, 120),
]


class FourRegionMorningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(T0)
        self.svc = AllocationService(":memory:", self.clock)
        for rid, name, base, prio, share in REGION_DEFS:
            self.svc.register_region(region_id=rid, name=name, baseline=base,
                                     priority=prio, historical_share=share,
                                     command_id=f"reg-{rid}")

    def _plan_lines(self, supply: float, reserve: float):
        self.svc.open_round(round_id="R1", supply_gross=supply, reserve_gross=reserve,
                            deadline=DEADLINE, command_id="open")
        rows = [
            ("c-hb", "HB", 200, 500, 0.05, 10),
            ("c-hn", "HN", 160, 400, 0.03, 12),
            ("c-hun", "HUN", 120, 300, 0.08, 8),
            ("c-csj", "CSJ", 240, 800, 0.02, 6),
        ]
        for cid, rid, cq, uq, lr, lt in rows:
            self.svc.submit_request(round_id="R1", customer_id=cid, region_id=rid,
                                    contract_qty=cq, urgent_qty=uq, loss_rate=lr,
                                    lead_time_h=lt, command_id=f"req-{cid}")
        d = self.svc.create_draft(round_id="R1", required_approvals=1, command_id="draft")
        return d, self.svc.get_plan(d.result_id)

    def test_scarce_supply_waterfills_baseline_not_first_come(self) -> None:
        """货源只够部分底线时，四区域等覆盖率抬升，长三角不能凭催单最急拿走大部分。"""
        _, plan = self._plan_lines(supply=200, reserve=0)
        baseline = {rid: base for rid, _, base, _, _ in REGION_DEFS}
        got = {rid: 0.0 for rid in baseline}
        for line in plan["lines"]:
            got[line["region_id"]] += line["baseline_qty"]
        ratios = {rid: got[rid] / baseline[rid] for rid in baseline}
        # 所有区域覆盖率几乎相同；毛额含损耗，净覆盖率约 0.533（低于无损耗的 0.556）。
        values = list(ratios.values())
        self.assertLess(max(values) - min(values), 1e-3)
        self.assertAlmostEqual(values[0], 0.53276, places=3)
        # 即使长三角申请量最大，也不能独占货源。
        self.assertLess(got["CSJ"], baseline["CSJ"])

    def test_order_of_requests_does_not_change_outcome(self) -> None:
        """先到先得被排除：申请到达顺序不影响分轮结果。"""
        def build(order):
            clock = FixedClock(T0)
            svc = AllocationService(":memory:", clock)
            for rid, name, base, prio, share in REGION_DEFS:
                svc.register_region(region_id=rid, name=name, baseline=base,
                                    priority=prio, historical_share=share,
                                    command_id=f"reg-{rid}")
            svc.open_round(round_id="R", supply_gross=300, reserve_gross=40,
                           deadline=DEADLINE, command_id="open")
            rows = {
                "HB": ("c-hb", 200, 500, 0.05, 10),
                "HN": ("c-hn", 160, 400, 0.03, 12),
                "HUN": ("c-hun", 120, 300, 0.08, 8),
                "CSJ": ("c-csj", 240, 800, 0.02, 6),
            }
            for i, rid in enumerate(order):
                cid, cq, uq, lr, lt = rows[rid]
                svc.submit_request(round_id="R", customer_id=cid, region_id=rid,
                                   contract_qty=cq, urgent_qty=uq, loss_rate=lr,
                                   lead_time_h=lt, command_id=f"req-{i}")
            d = svc.create_draft(round_id="R", required_approvals=1, command_id="draft")
            p = svc.get_plan(d.result_id)
            return sorted((l["region_id"], l["customer_id"], l["net_total"])
                          for l in p["lines"])

        a = build(["CSJ", "HB", "HN", "HUN"])
        b = build(["HUN", "HN", "HB", "CSJ"])
        self.assertEqual(a, b)

    def test_all_six_factors_participate(self) -> None:
        """区域底线、合同、在途、时效、损耗、历史份额共同参与：构造直接引擎对照。"""
        regions = {
            rid: Region(rid, name, baseline=base, priority=prio, historical_share=share)
            for rid, name, base, prio, share in REGION_DEFS
        }
        requests = [
            RequestLine("c-hb", "HB", 200, 500, loss_rate=0.05, lead_time_h=10),
            RequestLine("c-hn", "HN", 160, 400, loss_rate=0.03, lead_time_h=12),
            # 湖南线路时效不达标：本轮不可行。
            RequestLine("c-hun", "HUN", 120, 300, loss_rate=0.08, lead_time_h=200),
            RequestLine("c-csj", "CSJ", 240, 800, loss_rate=0.02, lead_time_h=6),
        ]
        result = run_allocation(
            supply_gross=2000, reserve_gross=200, deadline=T0 + timedelta(hours=48),
            as_of=T0, regions=regions, requests=requests,
            intransit_net_by_region={"HB": 50},
        )
        # 在途冲减：湖北底线先由在途覆盖 50。
        self.assertEqual(result.intransit_by_region["HB"], 50)
        # 时效不达标线路完全不分配。
        self.assertFalse(result.line("HUN", "c-hun").feasible)
        self.assertEqual(result.line("HUN", "c-hun").used_net, 0)
        # 湖南无可行线路 -> 底线缺口并说明时效原因。
        hun_gap = next(g for g in result.baseline_gaps if g.region_id == "HUN")
        self.assertIn("时效", hun_gap.reason)
        # 损耗影响毛额：至少一条有损耗线路的毛额严格大于净额。
        self.assertTrue(any(line.gross > line.used_net + 1e-6 for line in result.lines))
        # 保留池不参与常规三轮。
        self.assertLessEqual(result.gross_used, 1800 + 1e-6)

    def test_region_without_requests_still_reports_baseline_gap(self) -> None:
        regions = {"HB": Region("HB", "湖北", 100), "XJ": Region("XJ", "新疆", 50)}
        result = run_allocation(
            supply_gross=10, reserve_gross=0, deadline=T0 + timedelta(hours=48),
            as_of=T0, regions=regions,
            requests=[RequestLine("c1", "HB", 100, 100)],
        )
        xj = next(g for g in result.baseline_gaps if g.region_id == "XJ")
        self.assertIn("无在案申请线", xj.reason)


class ApprovalAndSimulationTest(unittest.TestCase):
    def test_simulate_then_publish_formal_version_with_chain(self) -> None:
        clock = FixedClock(T0)
        svc = AllocationService(":memory:", clock)
        for rid, name, base, prio, share in REGION_DEFS:
            svc.register_region(region_id=rid, name=name, baseline=base,
                                priority=prio, historical_share=share,
                                command_id=f"reg-{rid}")
        svc.open_round(round_id="R1", supply_gross=1200, reserve_gross=150,
                       deadline=DEADLINE, command_id="open")
        for cid, rid, cq, uq, lr, lt in [
            ("c-hb", "HB", 200, 500, 0.05, 10),
            ("c-hn", "HN", 160, 400, 0.03, 12),
            ("c-hun", "HUN", 120, 300, 0.08, 8),
            ("c-csj", "CSJ", 240, 800, 0.02, 6),
        ]:
            svc.submit_request(round_id="R1", customer_id=cid, region_id=rid,
                               contract_qty=cq, urgent_qty=uq, loss_rate=lr,
                               lead_time_h=lt, command_id=f"req-{cid}")

        # 1) 隔离模拟一次供应缩减：确认前运营态不被占用。
        sim = svc.simulate_supply_reduction(
            round_id="R1", scenario="factory-halt", reduced_supply_gross=700,
            command_id="sim")
        self.assertEqual(svc.get_plan(sim.result_id)["status"], "simulated")
        self.assertEqual(svc.reconcile("R1")["regular_effective_gross"], 0)

        # 2) 指挥员确认后，以缩减货源出正式草案，走完整批准链。
        d = svc.create_draft(round_id="R1", from_simulation="factory-halt",
                             required_approvals=2, command_id="draft")
        self.assertEqual(svc.get_plan(d.result_id)["supply_gross"], 700)
        svc.decide_approval(plan_id=d.result_id, approver="值班长", approved=True,
                            level=1, command_id="ap1")
        svc.decide_approval(plan_id=d.result_id, approver="总指挥", approved=True,
                            level=2, command_id="ap2")
        pub = svc.publish_plan(plan_id=d.result_id, command_id="publish")
        self.assertEqual(len(pub.extra["transfers"]), 4)
        self.assertEqual(svc.get_plan(d.result_id)["status"], "published")
        svc.reconcile("R1", strict=True)


class ReceiptLifecycleTest(unittest.TestCase):
    def _setup_published(self):
        clock = FixedClock(T0)
        svc = AllocationService(":memory:", clock)
        for rid, name, base, prio, share in REGION_DEFS:
            svc.register_region(region_id=rid, name=name, baseline=base,
                                priority=prio, historical_share=share,
                                command_id=f"reg-{rid}")
        svc.open_round(round_id="R1", supply_gross=1000, reserve_gross=100,
                       deadline=DEADLINE, command_id="open")
        for cid, rid in [("c-hb", "HB"), ("c-hn", "HN"),
                         ("c-hun", "HUN"), ("c-csj", "CSJ")]:
            svc.submit_request(round_id="R1", customer_id=cid, region_id=rid,
                               contract_qty=100, urgent_qty=100, loss_rate=0.0,
                               lead_time_h=5, command_id=f"req-{cid}")
        d = svc.create_draft(round_id="R1", required_approvals=1, command_id="draft")
        svc.decide_approval(plan_id=d.result_id, approver="boss", approved=True,
                            level=1, command_id="ap")
        pub = svc.publish_plan(plan_id=d.result_id, command_id="publish")
        return svc, pub.extra["transfers"]

    def test_confirm_ship_arrive_in_order(self) -> None:
        svc, transfers = self._setup_published()
        t = transfers[0]
        g = svc.get_transfer(t)["gross_qty"]
        svc.record_receipt(transfer_id=t, stage="confirmed", qty=g,
                           happened_at="2026-09-24T09:00:00+08:00", command_id="c")
        self.assertEqual(svc.get_transfer(t)["status"], "confirmed")
        svc.record_receipt(transfer_id=t, stage="dispatched", qty=g,
                           happened_at="2026-09-24T12:00:00+08:00", command_id="d")
        self.assertEqual(svc.get_transfer(t)["status"], "dispatched")
        svc.record_receipt(transfer_id=t, stage="arrived", qty=g,
                           happened_at="2026-09-25T08:00:00+08:00", command_id="a")
        self.assertEqual(svc.get_transfer(t)["status"], "arrived")
        report = svc.reconcile("R1")
        self.assertAlmostEqual(report["shipped_gross"], g)
        self.assertAlmostEqual(report["arrived_gross"], g)

    def test_late_receipt_filed_at_real_time_without_regression(self) -> None:
        svc, transfers = self._setup_published()
        t = transfers[0]
        g = svc.get_transfer(t)["gross_qty"]
        svc.record_receipt(transfer_id=t, stage="confirmed", qty=g,
                           happened_at="2026-09-24T10:00:00+08:00", command_id="c")
        svc.record_receipt(transfer_id=t, stage="dispatched", qty=g,
                           happened_at="2026-09-24T14:00:00+08:00", command_id="d")
        svc.record_receipt(transfer_id=t, stage="arrived", qty=g,
                           happened_at="2026-09-25T06:00:00+08:00", command_id="a")
        # 迟到的确认补报（真实时间更早）归入真实时间，但到达状态不倒退。
        svc.record_receipt(transfer_id=t, stage="confirmed", qty=g,
                           happened_at="2026-09-24T09:30:00+08:00",
                           command_id="c-late")
        info = svc.get_transfer(t)
        self.assertEqual(info["status"], "arrived")
        self.assertEqual(info["confirmed_at"], "2026-09-24T09:30:00+08:00")


class RestartContinuityTest(unittest.TestCase):
    def test_pending_round_and_approval_continue_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.db")
            clock = FixedClock(T0)
            svc = AllocationService(db, clock)
            for rid, name, base, prio, share in REGION_DEFS:
                svc.register_region(region_id=rid, name=name, baseline=base,
                                    priority=prio, historical_share=share,
                                    command_id=f"reg-{rid}")
            svc.open_round(round_id="R1", supply_gross=1000, reserve_gross=100,
                           deadline=DEADLINE, command_id="open")
            for cid, rid in [("c-hb", "HB"), ("c-hn", "HN")]:
                svc.submit_request(round_id="R1", customer_id=cid, region_id=rid,
                                   contract_qty=100, urgent_qty=100, lead_time_h=5,
                                   command_id=f"req-{cid}")
            d = svc.create_draft(round_id="R1", required_approvals=2,
                                 command_id="draft")
            svc.decide_approval(plan_id=d.result_id, approver="boss-1",
                                approved=True, level=1, command_id="ap1")

            # 重启：草案与第一级批准必须仍然存在，可继续完成第二级并发布。
            svc2 = AllocationService(db, FixedClock(T0))
            plan = svc2.get_plan(d.result_id)
            self.assertEqual(plan["status"], "draft")
            self.assertEqual([a["level"] for a in plan["approval_chain"]], [1])
            self.assertEqual(svc2.get_round("R1")["status"], "open")
            svc2.decide_approval(plan_id=d.result_id, approver="boss-2",
                                 approved=True, level=2, command_id="ap2")
            pub = svc2.publish_plan(plan_id=d.result_id, command_id="publish")
            self.assertEqual(len(pub.extra["transfers"]), 2)
            svc2.reconcile("R1", strict=True)


if __name__ == "__main__":
    unittest.main()
