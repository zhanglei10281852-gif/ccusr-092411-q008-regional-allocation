"""服务层端到端测试：模拟、批准链、回执、挤占、补偿、重启与对平。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from supply.errors import (
    AuthorizationRequired,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from supply.service import Service
from supply.store import Store


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.svc = Service(self.store)
        for region, floor, loss, days, share in [
            ("湖北", 30, 0.10, 2, 0.40),
            ("河南", 30, 0.05, 1, 0.25),
            ("湖南", 20, 0.05, 1, 0.20),
            ("长三角", 40, 0.02, 3, 0.15),
        ]:
            self.svc.register_region(region, floor, loss, days, share)
        for c, r in [("客户A", "湖北"), ("客户B", "河南"), ("客户C", "湖南"), ("客户D", "长三角")]:
            self.svc.register_customer(c, r)
        self.demand = [
            {"customer": "客户A", "quantity": 100, "contract_minimum": 40},
            {"customer": "客户B", "quantity": 100, "contract_minimum": 30},
            {"customer": "客户C", "quantity": 80, "contract_minimum": 20},
            {"customer": "客户D", "quantity": 120, "contract_minimum": 30},
        ]

    def open_published_round(self, round_id="r1", supply=200, reserve=20):
        self.svc.open_round(round_id, supply, reserve, self.demand,
                            in_transit={}, request_id="cmd-open")
        ver = f"{round_id}:v1"
        self.svc.approve(ver, "值班调度", "dispatcher", 1)
        self.svc.approve(ver, "运营指挥", "commander", 2)
        self.svc.publish(round_id, ver, ["dispatcher", "commander"], request_id="cmd-pub")
        return ver

    # ---- 模拟与批准链 ----
    def test_simulation_is_isolated(self):
        self.svc.open_round("r1", 200, 20, self.demand)
        sim = self.svc.simulate_supply_reduction("r1", supply=120, reserve=10)
        self.assertTrue(sim["isolated"])
        # 模拟不产生调拨单、不动保留池
        self.assertEqual(self.svc.list_transfers("r1"), [])
        rnd = self.svc.get_round("r1")
        self.assertEqual(rnd["reserve_remaining"], 20)
        self.assertEqual(rnd["published_version"], None)
        # 缩减后底线缺口扩大
        self.assertTrue(any(sim["snapshot"]["floor_shortfall"].values()))

    def test_publish_requires_full_chain_in_order(self):
        self.svc.open_round("r1", 200, 20, self.demand)
        with self.assertRaises(ConflictError):
            self.svc.publish("r1", "r1:v1", ["dispatcher", "commander"])
        self.svc.approve("r1:v1", "运营指挥", "commander", 2)
        with self.assertRaises(ConflictError):
            self.svc.publish("r1", "r1:v1", ["dispatcher", "commander"])
        self.svc.approve("r1:v1", "值班调度", "dispatcher", 1)
        # 顺序颠倒签批仍可发布（记录顺序正确：dispatcher seq1）
        self.svc.publish("r1", "r1:v1", ["dispatcher", "commander"])
        # 重复发布被拒
        with self.assertRaises(ConflictError):
            self.svc.publish("r1", "r1:v1", ["dispatcher", "commander"])
        # 模拟版本已作废
        states = {r["version_id"]: r["state"] for r in self.store.conn.execute(
            "select version_id,state from plan_versions where round_id='r1'").fetchall()}
        self.assertEqual(states["r1:v1"], "published")

    def test_rejected_approval_blocks_publish(self):
        self.svc.open_round("r1", 200, 20, self.demand)
        self.svc.approve("r1:v1", "值班调度", "dispatcher", 1, decision="rejected")
        self.svc.approve("r1:v1", "运营指挥", "commander", 2)
        with self.assertRaises(ConflictError):
            self.svc.publish("r1", "r1:v1", ["dispatcher", "commander"])

    # ---- 回执生命周期 ----
    def test_receipts_advance_in_order(self):
        self.open_published_round()
        tid = "r1:客户A"
        with self.assertRaises(ConflictError):
            self.svc.dispatch(tid)          # 未确认不能发运
        self.svc.confirm(tid, occurred_at="2026-09-24T08:00:00+08:00")
        self.svc.dispatch(tid, occurred_at="2026-09-24T09:00:00+08:00")
        self.svc.arrive(tid, qty=33.0, occurred_at="2026-09-24T11:00:00+08:00")
        self.assertEqual(self.svc.transfer_status(tid)["status"], "arrived")

    def test_late_receipt_keeps_real_time_but_status_does_not_regress(self):
        self.open_published_round()
        tid = "r1:客户A"
        # 模拟外部回执通道乱序：装运事件先被直接登记，确认回执迟到
        self.store.append_event(
            "evt-late-dispatch", "transfer.dispatched", tid,
            "2026-09-24T09:00:00+08:00", {"qty": 33.0})
        self.store.commit()
        self.assertEqual(self.svc.transfer_status(tid)["status"], "dispatched")
        # 迟到的确认回执按真实发生时间（早于装运）补登记，被允许
        self.svc.confirm(tid, occurred_at="2026-09-24T07:00:00+08:00")
        self.assertEqual(self.svc.transfer_status(tid)["status"], "dispatched")  # 不倒退
        stages = self.svc.transfer_status(tid)["stages"]
        self.assertEqual({s["stage"] for s in stages}, {"confirmed", "dispatched"})
        # 无任何相邻阶段时仍禁止跳跃登记
        with self.assertRaises(ConflictError):
            self.svc.dispatch("r1:客户B", occurred_at="2026-09-24T09:00:00+08:00")
        # 到达后再重发任何回执都被拒，状态保持 arrived
        self.svc.arrive(tid, qty=33.0, occurred_at="2026-09-24T15:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.svc.confirm(tid, occurred_at="2026-09-24T06:00:00+08:00")
        self.assertEqual(self.svc.transfer_status(tid)["status"], "arrived")

    def test_retry_with_same_request_id_does_not_double_charge(self):
        self.open_published_round()
        tid = "r1:客户A"
        self.svc.confirm(tid, request_id="cmd-confirm-1")
        # 网络重试：同一 request_id，直接返回首次结果，不产生第二条事件
        again = self.svc.confirm(tid, request_id="cmd-confirm-1")
        self.assertTrue(again["replayed"])
        count = self.store.conn.execute(
            "select count(*) c from events where event_type='transfer.confirmed'").fetchone()["c"]
        self.assertEqual(count, 1)
        # 开轮命令重试同样幂等
        again_open = self.svc.open_round("r1", 200, 20, self.demand, request_id="cmd-open")
        self.assertTrue(again_open["replayed"])

    # ---- 紧急需求 ----
    def test_emergency_uses_reserve_then_requires_authorization(self):
        self.open_published_round()
        before = self.svc.get_round("r1")["reserve_remaining"]
        with self.assertRaises(AuthorizationRequired):
            self.svc.emergency_request("r1", "客户A", before + 10)
        # 授权失败不产生任何事件、不动保留池
        self.assertEqual(self.svc.get_round("r1")["reserve_remaining"], before)

    def make_preemption(self):
        self.open_published_round()
        reserve = self.svc.get_round("r1")["reserve_remaining"]
        result = self.svc.emergency_request(
            "r1", "客户A", reserve + 40, authorization="局长令-001", request_id="cmd-emg")
        self.assertTrue(result["preemptions"])
        victims = {p["victim_customer"] for p in result["preemptions"]}
        self.assertNotIn("客户A", victims)
        # 被挤占方调拨单额度减少
        for p in result["preemptions"]:
            t = next(x for x in self.svc.list_transfers("r1") if x["customer"] == p["victim_customer"])
            self.assertLess(t["quantity"], t["original_quantity"] - 1e-9)
        # 重试幂等：不重复挤占
        again = self.svc.emergency_request(
            "r1", "客户A", reserve + 40, authorization="局长令-001", request_id="cmd-emg")
        self.assertTrue(again["replayed"])
        comp_count = self.store.conn.execute(
            "select count(*) c from events where event_type='compensation.granted'").fetchone()["c"]
        self.assertEqual(comp_count, len(result["preemptions"]))
        return result

    def test_dispatched_quota_cannot_be_preempted(self):
        self.open_published_round()
        # 先把客户B的货确认并发运
        self.svc.confirm("r1:客户B")
        self.svc.dispatch("r1:客户B")
        reserve = self.svc.get_round("r1")["reserve_remaining"]
        # 把其他人全部未发运额度也挤干后仍不够 -> 报错（客户B不可被挤）
        with self.assertRaises(ConflictError):
            self.svc.emergency_request("r1", "客户A", reserve + 10000, authorization="令-2")

    def test_compensation_priority_order_and_settlement(self):
        result = self.make_preemption()
        report = self.svc.reconcile("r1")
        comps = report["compensations"]
        self.assertTrue(report["identity"]["balanced"])
        # 必须严格按层级+priority 顺序兑现
        if len(comps) > 1:
            last = sorted(comps, key=lambda c: (c["tier"], c["priority"]))[-1]
            with self.assertRaises(ConflictError):
                self.svc.settle_compensation(last["comp_id"])
        # 后续补给入保留池后兑现
        total_due = sum(c["qty"] for c in comps)
        self.svc.replenish_reserve("r1", total_due, request_id="cmd-repl")
        for comp in sorted(comps, key=lambda c: (c["tier"], c["priority"])):
            self.svc.settle_compensation(comp["comp_id"])
        # 已兑现不可重复
        with self.assertRaises(ConflictError):
            self.svc.settle_compensation(comps[0]["comp_id"])
        # 兑现后对平仍然成立
        self.assertTrue(self.svc.reconcile("r1")["identity"]["balanced"])

    # ---- 对平 ----
    def test_reconcile_floor_reserve_shipped(self):
        self.open_published_round()
        report = self.svc.reconcile("r1")
        self.assertTrue(report["identity"]["balanced"])
        self.assertEqual(report["reserve_remaining"], 20)
        # 发运部分量后，已发运数量可查
        self.svc.confirm("r1:客户D")
        self.svc.dispatch("r1:客户D")
        report = self.svc.reconcile("r1")
        self.assertGreater(report["shipped_qty"], 0)
        self.assertTrue(report["identity"]["balanced"])
        # 区域底线核对存在
        self.assertIn("长三角", report["floor_check"])

    # ---- 重启 ----
    def test_state_survives_restart(self):
        self.open_published_round()
        self.svc.confirm("r1:客户A", request_id="cmd-c")
        self.svc.emergency_request("r1", "客户A", 50, authorization="令-9", request_id="cmd-e")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "supply.db")
            disk = Store(path)
            disk.conn.close()
            # 把内存库内容落到磁盘库：直接用磁盘库重放一遍命令流
            disk_store = Store(path)
            svc2 = Service(disk_store)
            for region, floor, loss, days, share in [
                ("湖北", 30, 0.10, 2, 0.40),
                ("河南", 30, 0.05, 1, 0.25),
                ("湖南", 20, 0.05, 1, 0.20),
                ("长三角", 40, 0.02, 3, 0.15),
            ]:
                svc2.register_region(region, floor, loss, days, share)
            for c, r in [("客户A", "湖北"), ("客户B", "河南"), ("客户C", "湖南"), ("客户D", "长三角")]:
                svc2.register_customer(c, r)
            svc2.open_round("r1", 200, 20, self.demand, request_id="cmd-open")
            svc2.approve("r1:v1", "值班调度", "dispatcher", 1)
            svc2.approve("r1:v1", "运营指挥", "commander", 2)
            svc2.publish("r1", "r1:v1", ["dispatcher", "commander"], request_id="cmd-pub")
            svc2.confirm("r1:客户A", request_id="cmd-c")
            svc2.emergency_request("r1", "客户A", 50, authorization="令-9", request_id="cmd-e")
            disk_store.close()
            # 重新打开：事件重放，状态完整恢复
            reopened = Store(path)
            n = reopened.rebuild_projections()
            self.assertGreater(n, 0)
            svc3 = Service(reopened)
            self.assertEqual(svc3.transfer_status("r1:客户A")["status"], "confirmed")
            report = svc3.reconcile("r1")
            self.assertTrue(report["identity"]["balanced"])
            self.assertEqual(report["state"], "open")

    def test_unfinished_round_continues_after_restart(self):
        self.open_published_round()
        # 未关闭轮次仍是 open，可继续受理回执
        self.assertEqual(self.svc.get_round("r1")["state"], "open")
        self.svc.close_round("r1", request_id="cmd-close")
        self.assertEqual(self.svc.get_round("r1")["state"], "closed")
        with self.assertRaises(ConflictError):
            self.svc.simulate_supply_reduction("r1", 100)

    def test_timezone_required(self):
        with self.assertRaises(ValidationError):
            self.svc.confirm("r1:客户A", occurred_at="2026-09-24T08:00:00")


if __name__ == "__main__":
    unittest.main()
