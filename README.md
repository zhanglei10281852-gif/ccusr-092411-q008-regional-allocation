# 跨区域保供调拨系统

区域底线、客户合同、在途补给、运输时效、损耗预估与历史兑现份额共同参与的跨区域保供调拨内核。
采用**事件溯源 + SQLite**：所有状态变更都是不可变事件，进程随时可从事件日志重放恢复，
未决轮次、批准链、保留池台账与补偿队列在重启后继续存在。

## 业务规则

- **分轮决策（water-filling，杜绝按催单先后发货）**
  1. 底线轮：按区域民生底线覆盖率抬水位，覆盖率相同的区域共同抬升，谁也不能一口拿满；
  2. 合同轮：按各客户合同兑现率抬水位，底线兑现计入合同兑现；
  3. 历史份额轮：剩余货源按「额外兑现量 / 历史兑现份额」配平。
- **六因子参与**：区域底线、客户合同、在途补给（截止线前到达先冲减底线缺口）、
  运输时效（当前时间 + 时效晚于截止线的线路不可行）、损耗率（毛额 = 净额 /(1−损耗率)）、
  历史兑现份额。
- **保留池与紧急挤占**：常规轮次结束后的紧急需求只能动用保留池；不足部分须**经授权**
  挤占「尚未发运」的额度（已发运部分受保护），并为被挤占方生成带优先级分数、
  可追踪、可分批清偿的补偿单；保留池补充到货时按补偿优先级自动清偿。
- **回执状态机**：确认 → 装运 → 到达顺序推进，数量单调不减；迟到回执归入
  `happened_at` 真实发生时间，但状态不倒退；所有命令以 `command_id` 幂等，
  请求/回执重试只回放首次结果，绝不重复扣配额。
- **模拟隔离 → 批准链 → 正式版本**：供应缩减先在隔离沙盘模拟（只落 simulated 事件、
  不产生调拨、不动保留池）；指挥员确认后出正式草案，经逐级批准链审批，
  任一级驳回即作废需重新出版本；发布时仅取消旧版本「未确认、未被挤占」的额度，
  已确认/已发运继续兑现，批准后到发布前锁定集若发生漂移会拒绝发布。
- **随时对平**：区域底线（在途 + 承诺/回执净覆盖）、保留池台账（初始 + 补充 − 动用）、
  总池来源去向恒等式、已发运/已到达量、挤占—补偿台账均可核对；封账前强制严格对平，
  存在未偿补偿不允许封账。

## 模块结构

```
allocation/
  clock.py       时钟与带时区时间策略（ISO 8601 with timezone）
  errors.py      领域错误（轮次关闭、保留池耗尽、状态冲突、幂等回放、对平不等）
  models.py      领域模型（区域/轮次/保留池/方案/调拨/在途/补偿/审批/命令记录）
  engine.py      三轮水填分配引擎（损耗反算、时效可行性、行级锁定）
  store.py       SQLite 不可变事件日志 + 命令幂等表
  projection.py  事件重放投影（重启恢复的唯一依据）
  service.py     应用服务：全部用例与严格对账
domain/contract.json   实体/状态/事件/时间与数量口径合同
examples/demo.py       端到端演示（四区域同日加急全流程）
examples/events.json   按发生时间排列的示例事件
tools/validate_contract.py  领域资料离线校验
tests/                 引擎、服务规则、验收场景共 33 个测试
```

## 常用用例

```python
from allocation import AllocationService
from allocation.clock import FixedClock

svc = AllocationService("allocation.db")            # 文件持久化，重启可恢复
svc.register_region(region_id="HB", name="湖北", baseline=100,
                    priority=10, historical_share=120, command_id="...")
svc.open_round(round_id="R1", supply_gross=700, reserve_gross=100,
               deadline="2026-09-26T08:00:00+08:00", command_id="...")
svc.submit_request(round_id="R1", customer_id="c-hb", region_id="HB",
                   contract_qty=120, urgent_qty=150,
                   loss_rate=0.05, lead_time_h=10, command_id="...")

svc.simulate_supply_reduction(round_id="R1", scenario="factory-halt",
                              reduced_supply_gross=450, command_id="...")   # 隔离模拟
d = svc.create_draft(round_id="R1", required_approvals=2, command_id="...")
svc.decide_approval(plan_id=d.result_id, approver="值班长", level=1, ...)
svc.decide_approval(plan_id=d.result_id, approver="总指挥", level=2, ...)
svc.publish_plan(plan_id=d.result_id, command_id="...")

svc.close_round(round_id="R1", command_id="...")
svc.request_emergency(round_id="R1", customer_id="x", region_id="HN",
                      qty_net=60, loss_rate=0.03, lead_time_h=10, command_id="...")
svc.divert(round_id="R1", donor_transfer_id="...", to_region_id="CSJ",
           to_customer_id="y", qty_gross=40, authorized_by="总指挥", ...)
svc.record_receipt(transfer_id="...", stage="dispatched", qty=117.9,
                   happened_at="2026-09-24T14:00:00+08:00", command_id="...")
svc.replenish_reserve(round_id="R1", qty_gross=50, source_batch="batch-99", ...)
svc.reconcile("R1", strict=True)                   # 随时对平
svc.finalize_round(round_id="R1", command_id="...")
```

## 构建 / 测试 / 演示 / 校验

所有命令均在项目根目录执行，仅依赖 Python 3.11+ 标准库。

```bash
python3 -m compileall -q .
python3 -m unittest discover -s tests -v
python3 examples/demo.py
python3 tools/validate_contract.py
```
