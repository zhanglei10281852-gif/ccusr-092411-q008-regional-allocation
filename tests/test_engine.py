"""分配引擎的单元测试：三轮水填、损耗反算、时效可行性。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from allocation.engine import gross_for_net, q, run_allocation
from allocation.models import Region, RequestLine

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 24, 8, 0, tzinfo=TZ)
DEADLINE = T0 + timedelta(hours=48)


def req(customer: str, region: str, contract: float, urgent: float,
        loss: float = 0.0, lead: float = 10.0) -> RequestLine:
    return RequestLine(
        customer_id=customer, region_id=region, contract_qty=contract,
        urgent_qty=urgent, loss_rate=loss, lead_time_h=lead,
    )


def regions() -> dict[str, Region]:
    return {
        "HB": Region("HB", "湖北", baseline=100, priority=10, historical_share=100),
        "HN": Region("HN", "河南", baseline=100, priority=10, historical_share=100),
    }


class GrossNetTest(unittest.TestCase):
    def test_gross_for_net_covers_loss(self) -> None:
        self.assertEqual(gross_for_net(95, 0.05), 100.0)
        self.assertEqual(gross_for_net(0, 0.1), 0.0)
        with self.assertRaises(ValueError):
            gross_for_net(10, 1.0)


class WaterFillTest(unittest.TestCase):
    def test_baseline_round_equalizes_coverage(self) -> None:
        """稀缺货源下底线轮必须让两个区域等覆盖率抬升，而非先来先得。"""
        result = run_allocation(
            supply_gross=100, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions=regions(),
            requests=[req("c1", "HB", 1000, 1000), req("c2", "HN", 1000, 1000)],
        )
        hb = result.line("HB", "c1").used_net
        hn = result.line("HN", "c2").used_net
        self.assertAlmostEqual(hb, 50, places=5)
        self.assertAlmostEqual(hn, 50, places=5)
        self.assertAlmostEqual(result.gross_used, 100, places=5)

    def test_intransit_counts_toward_baseline(self) -> None:
        result = run_allocation(
            supply_gross=160, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions=regions(),
            requests=[req("c1", "HB", 1000, 1000), req("c2", "HN", 1000, 1000)],
            intransit_net_by_region={"HB": 40},
        )
        hb = result.line("HB", "c1").used_net
        hn = result.line("HN", "c2").used_net
        # HB 已由在途覆盖 40：两区域共同抬到 100% 覆盖率，HB 再得 60，HN 得 100。
        self.assertAlmostEqual(hb, 60, places=4)
        self.assertAlmostEqual(hn, 100, places=4)
        self.assertEqual(len(result.baseline_gaps), 0)

    def test_low_loss_line_preferred_within_region(self) -> None:
        result = run_allocation(
            supply_gross=1000, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions={"HB": Region("HB", "湖北", baseline=100)},
            requests=[
                req("cheap", "HB", 100, 100, loss=0.0),
                req("costly", "HB", 100, 100, loss=0.5),
            ],
        )
        # 底线 100 全部走零损耗线路；高损耗线路在底线轮不被使用。
        self.assertAlmostEqual(result.line("HB", "cheap").baseline_qty, 100, places=4)
        self.assertAlmostEqual(result.line("HB", "costly").baseline_qty, 0, places=4)

    def test_loss_consumes_more_gross(self) -> None:
        result = run_allocation(
            supply_gross=200, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions={"HB": Region("HB", "湖北", baseline=100)},
            requests=[req("c1", "HB", 100, 100, loss=0.1)],
        )
        self.assertAlmostEqual(result.line("HB", "c1").baseline_qty, 100, places=4)
        self.assertAlmostEqual(result.gross_used, 100 / 0.9, places=3)

    def test_lead_time_cuts_feasibility(self) -> None:
        result = run_allocation(
            supply_gross=1000, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions={"HB": Region("HB", "湖北", baseline=100)},
            requests=[req("late", "HB", 100, 100, lead=100)],
        )
        self.assertFalse(result.line("HB", "late").feasible)
        self.assertEqual(result.line("HB", "late").used_net, 0)
        self.assertEqual(result.baseline_gaps[0].region_id, "HB")
        self.assertIn("时效", result.baseline_gaps[0].reason)

    def test_three_rounds_baseline_then_contract_then_share(self) -> None:
        # 底线 50/50；合同各 100；历史份额 HN 为 HB 的两倍，剩余货源按份额配平。
        r = {
            "HB": Region("HB", "湖北", baseline=50, historical_share=100),
            "HN": Region("HN", "河南", baseline=50, historical_share=200),
        }
        result = run_allocation(
            supply_gross=450, reserve_gross=0, deadline=DEADLINE, as_of=T0,
            regions=r,
            requests=[req("c1", "HB", 100, 1000), req("c2", "HN", 100, 1000)],
        )
        hb = result.line("HB", "c1")
        hn = result.line("HN", "c2")
        self.assertAlmostEqual(hb.baseline_qty, 50, places=4)
        self.assertAlmostEqual(hn.baseline_qty, 50, places=4)
        # 合同轮把两条线补到 100。
        self.assertAlmostEqual(hb.contract_qty_alloc, 50, places=4)
        self.assertAlmostEqual(hn.contract_qty_alloc, 50, places=4)
        # 份额轮 HN 拿到的额外量约为 HB 的两倍（货源剩余 250 毛，按 1:2 配平）。
        self.assertGreater(hn.share_qty, 0)
        self.assertGreater(hb.share_qty, 0)
        self.assertAlmostEqual(hn.share_qty / hb.share_qty, 2.0, delta=0.05)

    def test_reserve_excluded_from_regular_pool(self) -> None:
        result = run_allocation(
            supply_gross=100, reserve_gross=30, deadline=DEADLINE, as_of=T0,
            regions=regions(),
            requests=[req("c1", "HB", 1000, 1000), req("c2", "HN", 1000, 1000)],
        )
        self.assertAlmostEqual(result.pool_gross, 70, places=5)
        self.assertAlmostEqual(
            result.line("HB", "c1").used_net + result.line("HN", "c2").used_net,
            70, places=4,
        )


if __name__ == "__main__":
    unittest.main()
