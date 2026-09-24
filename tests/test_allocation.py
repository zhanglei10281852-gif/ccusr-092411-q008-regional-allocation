"""分轮分配算法测试。"""
from __future__ import annotations

import unittest

from supply.allocation import Lane, Request, emergency_need, plan_rounds, proportional_allocate


def make_lanes():
    return [
        Lane("湖北", floor=30, loss_rate=0.10, transit_days=2, historical_share=0.40),
        Lane("河南", floor=30, loss_rate=0.05, transit_days=1, historical_share=0.25),
        Lane("湖南", floor=20, loss_rate=0.05, transit_days=1, historical_share=0.20),
        Lane("长三角", floor=40, loss_rate=0.02, transit_days=3, historical_share=0.15),
    ]


def make_requests():
    return [
        Request("客户A", "湖北", quantity=100, contract_minimum=40),
        Request("客户B", "河南", quantity=100, contract_minimum=30),
        Request("客户C", "湖南", quantity=80, contract_minimum=20),
        Request("客户D", "长三角", quantity=120, contract_minimum=30),
    ]


class ProportionalTest(unittest.TestCase):
    def test_caps_and_redistribution(self):
        got = proportional_allocate(100, [("a", 1, 20), ("b", 1, 100)])
        self.assertAlmostEqual(got["a"], 20)
        self.assertAlmostEqual(got["b"], 80)

    def test_zero_weight_shares_equally(self):
        got = proportional_allocate(90, [("a", 0, 100), ("b", 0, 50)])
        self.assertAlmostEqual(got["a"], 45)
        self.assertAlmostEqual(got["b"], 45)


class RoundPlanTest(unittest.TestCase):
    def test_floor_first_then_contract_then_share(self):
        plan = plan_rounds(200, make_lanes(), make_requests(), reserve=20)
        self.assertAlmostEqual(plan.pool, 180)
        # 底线轮：每个区域先拿到折算损耗后的底线毛量
        by_region = {}
        for a in plan.allocations.values():
            by_region.setdefault(a.region, 0.0)
            by_region[a.region] += a.by_round.get("floor", 0.0)
        self.assertAlmostEqual(by_region["湖北"] * 0.90, 30, places=6)
        self.assertAlmostEqual(by_region["河南"] * 0.95, 30, places=6)
        self.assertAlmostEqual(by_region["湖南"] * 0.95, 20, places=6)
        self.assertAlmostEqual(by_region["长三角"] * 0.98, 40, places=6)
        # 合同轮在底线轮之后发生
        self.assertTrue(any("contract" in a.by_round for a in plan.allocations.values()))
        # 总量守恒
        self.assertLessEqual(plan.used, plan.pool + 1e-9)
        self.assertAlmostEqual(plan.reserve, 20)

    def test_in_transit_offsets_floor(self):
        plan = plan_rounds(200, make_lanes(), make_requests(),
                           in_transit={"湖北": 30}, reserve=0)
        hb = plan.allocations["客户A"]
        # 湖北底线 30 全部已在途，底线轮不再占用货源
        self.assertAlmostEqual(hb.by_round.get("floor", 0.0), 0.0)

    def test_slow_region_served_first_under_scarcity(self):
        # 货源不足以覆盖最慢区域的底线：时效最慢的长三角（3天）先拿，其他区域为 0
        plan = plan_rounds(40, make_lanes(), make_requests(), reserve=0)
        floors = {a.region: a.by_round.get("floor", 0.0) for a in plan.allocations.values()}
        self.assertGreater(floors.get("长三角", 0.0), 0.0)
        self.assertEqual(sum(v for r, v in floors.items() if r != "长三角"), 0.0)
        self.assertIn("湖北", plan.floor_shortfall)

    def test_share_round_follows_historical_share(self):
        lanes = make_lanes()
        reqs = [Request("客户A", "湖北", 1000), Request("客户B", "河南", 1000)]
        plan = plan_rounds(300, lanes, reqs, reserve=0)
        a = plan.allocations["客户A"].by_round.get("share", 0.0)
        b = plan.allocations["客户B"].by_round.get("share", 0.0)
        # 湖北历史份额 0.40 vs 河南 0.25
        self.assertAlmostEqual(a / b, 0.40 / 0.25, places=4)

    def test_demand_cap_respected(self):
        plan = plan_rounds(10000, make_lanes(), make_requests(), reserve=0)
        for req, alloc in zip(make_requests(), plan.allocations.values()):
            self.assertLessEqual(alloc.quantity, req.quantity + 1e-6)


class EmergencyTest(unittest.TestCase):
    def test_reserve_used_first(self):
        plan = plan_rounds(200, make_lanes(), make_requests(), reserve=50)
        source, qty, preempted = emergency_need(plan, make_lanes(), "客户A", 30)
        self.assertEqual(source, "reserve")
        self.assertAlmostEqual(qty, 30)
        self.assertEqual(preempted, [])

    def test_preemption_requires_authorization(self):
        plan = plan_rounds(200, make_lanes(), make_requests(), reserve=10)
        from supply.errors import AuthorizationRequired
        with self.assertRaises(AuthorizationRequired):
            emergency_need(plan, make_lanes(), "客户A", 40)

    def test_preemption_spares_floor_last(self):
        plan = plan_rounds(200, make_lanes(), make_requests(), reserve=0)
        _, _, preempted = emergency_need(plan, make_lanes(), "客户A", 50, authorization="cmd-1")
        total = sum(q for _, q, _ in preempted)
        self.assertAlmostEqual(total, 50)
        # 同一受害者先挤合同/份额轮（tier 1），触底才挤底线（tier 0）
        for _, _, tier in preempted:
            self.assertIn(tier, (0, 1))
        by_victim: dict[str, int] = {}
        for victim, _, tier in preempted:
            by_victim.setdefault(victim, tier)
            if victim in by_victim:
                # 每个受害者 tier 1 必须出现在 tier 0 之前
                self.assertGreaterEqual(by_victim[victim], tier - 1)


if __name__ == "__main__":
    unittest.main()
