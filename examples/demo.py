"""端到端演示：四区域同日加急 -> 分轮调拨 -> 紧急挤占 -> 回执 -> 补偿 -> 对平。

运行：python3 examples/demo.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation import AllocationService
from allocation.clock import FixedClock
from allocation.errors import AllocationError

TZ = timezone(timedelta(hours=8))


def main() -> None:
    clock = FixedClock(datetime(2026, 9, 24, 8, 0, tzinfo=TZ))
    svc = AllocationService(":memory:", clock)

    print("== 1. 登记区域：底线、优先级、历史兑现份额 ==")
    for rid, name, base, prio, share in [
        ("HB", "湖北", 100, 10, 120),
        ("HN", "河南", 80, 20, 90),
        ("HUN", "湖南", 60, 30, 70),
        ("CSJ", "长三角", 120, 40, 150),
    ]:
        svc.register_region(region_id=rid, name=name, baseline=base, priority=prio,
                            historical_share=share, command_id=f"reg-{rid}")
    svc.open_round(round_id="R1", supply_gross=700, reserve_gross=100,
                   deadline="2026-09-26T08:00:00+08:00", command_id="open")
    print("开盘：总货源 700 毛吨，其中保留池 100 毛吨，截止线 09-26 08:00")

    print("\n== 2. 四区域客户同一上午连续发来加急（含合同量、损耗、运输时效）==")
    for cid, rid, cq, uq, lr, lt in [
        ("c-hb", "HB", 120, 150, 0.05, 10),
        ("c-hn", "HN", 90, 120, 0.03, 12),
        ("c-hun", "HUN", 70, 90, 0.08, 8),
        ("c-csj", "CSJ", 130, 200, 0.02, 6),
    ]:
        svc.submit_request(round_id="R1", customer_id=cid, region_id=rid,
                           contract_qty=cq, urgent_qty=uq, loss_rate=lr,
                           lead_time_h=lt, command_id=f"req-{cid}")
    svc.register_intransit(round_id="R1", source="train-77", region_id="HB",
                           qty_net=30, eta="2026-09-25T20:00:00+08:00",
                           command_id="it-1")
    print("湖北另有在途 30 净吨，09-25 20:00 到达，计入底线冲减")

    print("\n== 3. 隔离模拟一次供应缩减（不占用运营态资源）==")
    sim = svc.simulate_supply_reduction(round_id="R1", scenario="factory-halt",
                                        reduced_supply_gross=450, command_id="sim")
    print(f"模拟方案 {sim.result_id}：动用 {sim.extra['gross_used']} 毛吨，"
          f"未满足 {sim.extra['unmet_net']} 净吨（仅模拟，无任何调拨）")

    print("\n== 4. 确认后出正式草案，经两级批准链发布 ==")
    d = svc.create_draft(round_id="R1", required_approvals=2, command_id="draft")
    svc.decide_approval(plan_id=d.result_id, approver="值班长", approved=True,
                        level=1, command_id="ap1")
    svc.decide_approval(plan_id=d.result_id, approver="总指挥", approved=True,
                        level=2, command_id="ap2")
    pub = svc.publish_plan(plan_id=d.result_id, command_id="publish")
    transfers = pub.extra["transfers"]
    print(f"正式方案 {d.result_id} 已发布，生成 {len(transfers)} 笔常规调拨")
    for line in svc.get_plan(d.result_id)["lines"]:
        print(f"  {line['region_id']:<4}{line['customer_id']}: 底线{line['baseline_qty']:>7.3f} "
              f"合同{line['contract_qty']:>7.3f} 份额{line['share_qty']:>7.3f} "
              f"= 净{line['net_total']:>7.3f}（损耗率 {line['loss_rate']}）")

    print("\n== 5. 常规轮次结束，紧急需求先吃保留池 ==")
    svc.close_round(round_id="R1", command_id="close")
    em = svc.request_emergency(round_id="R1", customer_id="c-hn-em", region_id="HN",
                               qty_net=60, loss_rate=0.03, lead_time_h=10,
                               reason="郑州民生告急", command_id="em-1")
    print(f"河南紧急 60 净吨：保留池满足 {em.extra['allocated_net']}，"
          f"保留池余额 {em.extra['reserve_balance_gross']} 毛吨")
    em2 = svc.request_emergency(round_id="R1", customer_id="c-csj-em", region_id="CSJ",
                                qty_net=120, loss_rate=0.02, lead_time_h=6,
                                reason="上海应急", command_id="em-2")
    print(f"长三角紧急 120 净吨：保留池再满足 {em2.extra['allocated_net']}，"
          f"缺口 {em2.extra['short_net']} 需授权挤占")

    print("\n== 6. 经授权挤占未发运额度，为被挤占方生成补偿优先级 ==")
    donor = transfers[0]
    info = svc.get_transfer(donor)
    div = svc.divert(round_id="R1", donor_transfer_id=donor, to_region_id="CSJ",
                     to_customer_id="c-csj-em", qty_gross=40,
                     beneficiary_loss_rate=0.02, authorized_by="总指挥",
                     reason="应急40吨", command_id="div-1")
    print(f"从 {donor}（原 {info['gross_qty']} 毛吨）挤占 40 毛吨，"
          f"补偿单 {div.extra['compensation_id']}，优先级分 {div.extra['priority_score']}")

    print("\n== 7. 确认->装运->到达 回执；已发运部分受保护 ==")
    keep = info["gross_qty"] - 40
    svc.record_receipt(transfer_id=donor, stage="confirmed", qty=keep,
                       happened_at="2026-09-24T10:00:00+08:00", command_id="rc-1")
    svc.record_receipt(transfer_id=donor, stage="dispatched", qty=keep,
                       happened_at="2026-09-24T14:00:00+08:00", command_id="rc-2")
    try:
        svc.divert(round_id="R1", donor_transfer_id=donor, to_region_id="HUN",
                   to_customer_id="x", qty_gross=1, beneficiary_loss_rate=0.0,
                   authorized_by="总指挥", reason="x", command_id="div-2")
    except AllocationError as exc:
        print(f"  已发运后再挤占被拒：{exc}")
    # 迟到回执：真实时间更早，归档到 09:30，但状态不倒退。
    svc.record_receipt(transfer_id=donor, stage="confirmed", qty=keep,
                       happened_at="2026-09-24T09:30:00+08:00", command_id="rc-3")
    late = svc.get_transfer(donor)
    print(f"  {donor} 状态={late['status']}，确认时间已归入 {late['confirmed_at']}")
    # 重试同一回执命令：回放，不重复扣配额。
    again = svc.record_receipt(transfer_id=donor, stage="dispatched", qty=keep,
                               happened_at="2026-09-24T14:00:00+08:00",
                               command_id="rc-2")
    print(f"  回执重试 replayed={again.replayed}，装运量仍为 {late['dispatched_qty']}")

    print("\n== 8. 保留池补充到货，按补偿优先级自动清偿 ==")
    rep = svc.replenish_reserve(round_id="R1", qty_gross=50, source_batch="batch-99",
                                command_id="rep-1")
    for item in rep.extra["settled"]:
        print(f"  补偿单 {item['compensation_id']} 清偿 {item['qty_gross']} 毛吨，"
              f"补偿调拨 {item['transfer_id']}，结清={item['fully']}")

    print("\n== 9. 随时对平：区域底线、保留池、已发运 ==")
    rec = svc.reconcile("R1", strict=True)
    print(f"总池：来源 {rec['sources_gross']} = 常规有效 {rec['regular_effective_gross']}"
          f" + 挤占 {rec['diverted_gross']} + 保留池动用 {rec['reserve_debits_gross']}"
          f" + 自由余量 {rec['free_pool_gross']}")
    print(f"保留池余额 {rec['reserve_balance_gross']} 毛吨；"
          f"已发运 {rec['shipped_gross']}，已到达 {rec['arrived_gross']}；"
          f"未偿补偿 {rec['diverted_unsettled_gross']}")
    for rid, cov in rec["regions"].items():
        flag = "达标" if cov["baseline_met"] else f"缺口 {cov['gap_net']}"
        print(f"  {rid:<4} 底线{cov['baseline']:>5} 在途{cov['in_transit_net']:>5} "
              f"承诺净{cov['committed_net']:>7} 回执净{cov['receipt_net']:>7} -> {flag}")

    print("\n== 10. 补偿清空、台账对平后封账 ==")
    svc.finalize_round(round_id="R1", command_id="fin")
    print(f"轮次状态：{svc.get_round('R1')['status']}")
    print("DEMO OK")


if __name__ == "__main__":
    main()
