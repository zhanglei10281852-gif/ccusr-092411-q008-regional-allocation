"""端到端演示：四区域加急上午的分轮保供全流程。

运行：python3 examples/demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supply.errors import AuthorizationRequired
from supply.service import Service
from supply.store import Store


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    store = Store(str(Path(tmp.name) / "demo.db"))
    svc = Service(store)

    # 1) 主数据：区域底线、损耗、时效、历史份额
    for region, floor, loss, days, share in [
        ("湖北", 30, 0.10, 2, 0.40),
        ("河南", 30, 0.05, 1, 0.25),
        ("湖南", 20, 0.05, 1, 0.20),
        ("长三角", 40, 0.02, 3, 0.15),
    ]:
        svc.register_region(region, floor, loss, days, share)
    for customer, region in [
        ("鄂中粮批", "湖北"), ("豫北粮仓", "河南"),
        ("湘中商超", "湖南"), ("沪上生鲜", "长三角"),
    ]:
        svc.register_customer(customer, region)

    demand = [
        {"customer": "鄂中粮批", "quantity": 100, "contract_minimum": 40},
        {"customer": "豫北粮仓", "quantity": 100, "contract_minimum": 30},
        {"customer": "湘中商超", "quantity": 80, "contract_minimum": 20},
        {"customer": "沪上生鲜", "quantity": 120, "contract_minimum": 30},
    ]

    # 2) 开轮：总货源 200，保留池 20，湖北有 10 在途补给
    opened = svc.open_round(
        "round-20260924", 200, 20, demand,
        in_transit={"湖北": 10}, request_id="req-open-001",
    )
    show("开轮（三轮分配快照：底线→合同→历史份额）",
         {"allocations": opened["snapshot"]["allocations"],
          "floor_shortfall": opened["snapshot"]["floor_shortfall"],
          "unmet": opened["snapshot"]["unmet"]})

    # 3) 指挥员先隔离模拟供应缩减（120），确认后才发布正式版
    sim = svc.simulate_supply_reduction(
        "round-20260924", supply=120, reserve=10, request_id="req-sim-001")
    show("隔离模拟：供应缩减到 120（不影响正式方案）",
         {"version_id": sim["version_id"], "state": "simulated",
          "floor_shortfall": sim["snapshot"]["floor_shortfall"]})

    # 4) 仍按 200 货源走批准链：调度→指挥，链不完整不得发布
    ver = "round-20260924:v1"
    svc.approve(ver, "值班调度", "dispatcher", 1, comment="基线方案")
    try:
        svc.publish("round-20260924", ver, ["dispatcher", "commander"])
    except Exception as exc:
        print(f"\n[拦截] 批准链不完整：{exc}")
    svc.approve(ver, "运营指挥", "commander", 2, comment="同意发布")
    published = svc.publish("round-20260924", ver,
                            ["dispatcher", "commander"], request_id="req-pub-001")
    show("正式发布（带批准链）", {"version_id": published["version_id"]})

    # 5) 回执按序推进；网络重试不重复扣配额
    tid = "round-20260924:沪上生鲜"
    qty_d = next(a["qty"] for a in published["snapshot"]["allocations"] if a["customer"] == "沪上生鲜")
    svc.confirm(tid, occurred_at="2026-09-24T08:30:00+08:00", request_id="req-confirm-1")
    svc.confirm(tid, occurred_at="2026-09-24T08:30:00+08:00", request_id="req-confirm-1")  # 重试
    svc.dispatch(tid, occurred_at="2026-09-24T09:10:00+08:00")
    svc.arrive(tid, qty=qty_d, occurred_at="2026-09-24T13:00:00+08:00")
    show("回执推进（确认/装运/到达，重试幂等）", svc.transfer_status(tid)["stages"])

    # 6) 常规轮次后紧急需求：先保留池；不足需授权挤占未发运额度
    try:
        svc.emergency_request("round-20260924", "鄂中粮批", 45)
    except AuthorizationRequired as exc:
        print(f"\n[拦截] {exc}")
    emg = svc.emergency_request(
        "round-20260924", "鄂中粮批", 45,
        authorization="指挥长令字001号", request_id="req-emg-001")
    show("授权挤占：动用保留池并生成可追踪补偿优先级", emg)

    # 7) 迟到回执：归入真实发生时间，状态不倒退
    late = "round-20260924:鄂中粮批"
    svc.confirm(late, occurred_at="2026-09-24T10:00:00+08:00")
    svc.dispatch(late, occurred_at="2026-09-24T11:00:00+08:00")
    svc.arrive(late, qty=50, occurred_at="2026-09-24T18:00:00+08:00")

    # 8) 随时对平
    show("对平：区域底线 / 保留池 / 已发运 / 货源恒等式", svc.reconcile("round-20260924"))

    # 9) 后续补给入池，按补偿优先级兑现
    total_due = sum(c["qty"] for c in svc.reconcile("round-20260924")["compensations"])
    if total_due:
        svc.replenish_reserve("round-20260924", total_due, request_id="req-repl-001")
        for comp in sorted(svc.reconcile("round-20260924")["compensations"],
                           key=lambda c: (c["tier"], c["priority"])):
            svc.settle_compensation(comp["comp_id"])
        show("补给到达后按优先级兑现补偿", svc.reconcile("round-20260924")["compensations"])

    # 10) 重启：重放事件，未决轮次继续，账目依旧对平
    store.close()
    reopened = Store(str(Path(tmp.name) / "demo.db"))
    reopened.rebuild_projections()
    svc2 = Service(reopened)
    report = svc2.reconcile("round-20260924")
    show("重启重放后", {"state": report["state"], "balanced": report["identity"]["balanced"],
                       "shipped_qty": report["shipped_qty"]})
    reopened.close()
    tmp.cleanup()


if __name__ == "__main__":
    main()
