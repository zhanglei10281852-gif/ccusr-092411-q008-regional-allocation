"""事件日志与投影存储（SQLite，标准库）。

事件表只追加，是唯一事实来源；其余表都是投影。每次启动时清空投影、
按事件序号重放重建——进程中途被杀死也能恢复全部未决轮次。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
create table if not exists events (
    seq          integer primary key autoincrement,
    event_id     text not null unique,
    event_type   text not null,
    aggregate_id text not null,
    occurred_at  text not null,
    recorded_at  text not null,
    payload      text not null
);
create table if not exists commands (
    request_id   text primary key,
    command      text not null,
    aggregate_id text not null,
    event_ids    text not null,
    created_at   text not null
);
create table if not exists regions (
    region text primary key,
    floor real not null,
    loss_rate real not null,
    transit_days real not null,
    historical_share real not null
);
create table if not exists customers (
    customer text primary key,
    region text not null
);

-- 以下均为投影
create table if not exists rounds (
    round_id text primary key,
    supply real not null,
    reserve_total real not null,
    reserve_remaining real not null,
    state text not null,                 -- open / closed
    opened_at text,
    closed_at text,
    published_version integer
);
create table if not exists plan_versions (
    version_id text primary key,
    round_id text not null,
    version integer not null,
    state text not null,                 -- draft / simulated / published / superseded
    supply real not null,
    reserve real not null,
    snapshot text not null,
    created_at text not null,
    published_at text,
    unique(round_id, version)
);
create table if not exists transfers (
    transfer_id text primary key,
    round_id text not null,
    customer text not null,
    region text not null,
    quantity real not null,             -- 当前有效额度（被挤占会减少）
    original_quantity real not null,
    source text not null,               -- allocation / reserve / preempted / mixed
    status text not null,              -- published / confirmed / dispatched / arrived
    by_round text not null,
    created_event_seq integer not null
);
create table if not exists transfer_stages (
    transfer_id text not null,
    stage text not null,                -- confirmed / dispatched / arrived
    occurred_at text not null,
    qty real not null,
    primary key (transfer_id, stage)
);
create table if not exists approvals (
    approval_id integer primary key autoincrement,
    version_id text not null,
    approver text not null,
    role text not null,
    sequence integer not null,
    decision text not null,             -- approved / rejected
    comment text,
    decided_at text not null,
    unique(version_id, approver)
);
create table if not exists compensations (
    comp_id text primary key,
    round_id text not null,
    victim_customer text not null,
    qty real not null,
    tier integer not null,              -- 0 = 含底线份额，最优先
    priority integer not null,          -- 同层内按挤占发生先后
    state text not null,                -- compensating / compensated
    created_at text not null,
    settled_at text,
    settlement_transfer_id text
);
create table if not exists reconciliation_runs (
    run_at text primary key,
    round_id text,
    report text not null
);
"""

# rounds/plan_versions/transfers 等全部为事件投影；
# approvals 是带独立生命周期的持久化记录（发布时链快照同时进事件），不参与重放清空。
PROJECTION_TABLES = [
    "rounds", "plan_versions", "transfers", "transfer_stages",
    "compensations", "reconciliation_runs",
]

STAGE_RANK = {"published": 1, "confirmed": 2, "dispatched": 3, "arrived": 4}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma foreign_keys=on")
        if path != ":memory:":
            self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(SCHEMA)
        self.rebuild_projections()

    # ---- 基础 ----
    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    # ---- 主数据 ----
    def upsert_region(self, region: str, floor: float, loss_rate: float,
                      transit_days: float, historical_share: float) -> None:
        self.conn.execute(
            "insert into regions values(?,?,?,?,?) "
            "on conflict(region) do update set floor=excluded.floor, "
            "loss_rate=excluded.loss_rate, transit_days=excluded.transit_days, "
            "historical_share=excluded.historical_share",
            (region, floor, loss_rate, transit_days, historical_share),
        )

    def upsert_customer(self, customer: str, region: str) -> None:
        self.conn.execute(
            "insert into customers values(?,?) on conflict(customer) do update set region=excluded.region",
            (customer, region),
        )

    # ---- 事件追加 ----
    def append_event(self, event_id: str, event_type: str, aggregate_id: str,
                     occurred_at: str, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "insert into events(event_id,event_type,aggregate_id,occurred_at,recorded_at,payload) "
            "values(?,?,?,?,?,?)",
            (event_id, event_type, aggregate_id, occurred_at, utc_now_iso(), json.dumps(payload, ensure_ascii=False)),
        )
        seq = int(cur.lastrowid)
        self._apply(seq, event_type, aggregate_id, occurred_at, payload)
        return seq

    def record_command(self, request_id: str, command: str, aggregate_id: str,
                       event_ids: Iterable[str]) -> None:
        self.conn.execute(
            "insert into commands values(?,?,?,?,?)",
            (request_id, command, aggregate_id, json.dumps(list(event_ids)), utc_now_iso()),
        )

    def get_command(self, request_id: str):
        return self.conn.execute("select * from commands where request_id=?", (request_id,)).fetchone()

    # ---- 重启重放 ----
    def rebuild_projections(self) -> int:
        """清空投影并按事件序号重放。返回重放事件数。"""
        with self.conn:
            for table in PROJECTION_TABLES:
                self.conn.execute(f"delete from {table}")
            rows = self.conn.execute(
                "select seq,event_type,aggregate_id,occurred_at,payload from events order by seq"
            ).fetchall()
            for row in rows:
                self._apply(row["seq"], row["event_type"], row["aggregate_id"],
                            row["occurred_at"], json.loads(row["payload"]))
        return len(rows)

    # ---- 投影函数（事件的唯一状态演变入口）----
    def _apply(self, seq: int, event_type: str, aggregate_id: str,
               occurred_at: str, p: dict[str, Any]) -> None:
        c = self.conn
        if event_type == "round.opened":
            c.execute("insert into rounds values(?,?,?,?,?,?,?,?)",
                      (aggregate_id, p["supply"], p["reserve"], p["reserve"],
                       "open", occurred_at, None, None))
            c.execute("insert into plan_versions values(?,?,?,?,?,?,?,?,?)",
                      (f"{aggregate_id}:v1", aggregate_id, 1, "draft", p["supply"],
                       p["reserve"], json.dumps(p["snapshot"], ensure_ascii=False),
                       occurred_at, None))
        elif event_type == "plan.simulated":
            c.execute("insert into plan_versions values(?,?,?,?,?,?,?,?,?)",
                      (p["version_id"], aggregate_id, p["version"], "simulated",
                       p["supply"], p["reserve"],
                       json.dumps(p["snapshot"], ensure_ascii=False), occurred_at, None))
        elif event_type == "allocation.published":
            c.execute("update plan_versions set state='published', published_at=? where version_id=?",
                      (occurred_at, p["version_id"]))
            # 同轮其他草稿/模拟版本作废（发布事件的投影副作用，保证重放一致）
            c.execute(
                "update plan_versions set state='superseded' where round_id=? and version_id<>?",
                (aggregate_id, p["version_id"]))
            c.execute("update rounds set published_version=? where round_id=?",
                      (p["version"], aggregate_id))
            for a in p["snapshot"]["allocations"]:
                if a["qty"] <= 0:
                    continue
                tid = a.get("transfer_id", f"{aggregate_id}:{a['customer']}")
                c.execute("insert into transfers values(?,?,?,?,?,?,?,?,?,?)",
                          (tid, aggregate_id, a["customer"], a["region"], a["qty"],
                           a["qty"], "allocation", "published",
                           json.dumps(a.get("by_round", {}), ensure_ascii=False), seq))
        elif event_type == "round.closed":
            c.execute("update rounds set state='closed', closed_at=? where round_id=?",
                      (occurred_at, aggregate_id))
        elif event_type in ("reserve.released", "quota.preempted"):
            tid = p["transfer_id"]
            row = c.execute("select quantity from transfers where transfer_id=?", (tid,)).fetchone()
            if row is None:
                c.execute("insert into transfers values(?,?,?,?,?,?,?,?,?,?)",
                          (tid, aggregate_id, p["customer"], p["region"], p["qty"],
                           p["qty"], "mixed", "published",
                           json.dumps({"emergency": p["qty"]}, ensure_ascii=False), seq))
            if event_type == "reserve.released":
                c.execute("update rounds set reserve_remaining=reserve_remaining-? where round_id=?",
                          (p["qty"], aggregate_id))
                if row is not None:
                    c.execute("update transfers set quantity=quantity+?, original_quantity=original_quantity+? "
                              "where transfer_id=?", (p["qty"], p["qty"], tid))
            else:
                # 受害者额度被挤走，紧急方额度增加
                c.execute("update transfers set quantity=quantity-? where transfer_id=?",
                          (p["qty"], p["victim_transfer"]))
                if row is not None:
                    c.execute("update transfers set quantity=quantity+?, original_quantity=original_quantity+? "
                              "where transfer_id=?", (p["qty"], p["qty"], tid))
        elif event_type == "compensation.granted":
            c.execute("insert or ignore into compensations values(?,?,?,?,?,?,?,?,?,?)",
                      (p["comp_id"], aggregate_id, p["victim_customer"], p["qty"],
                       p["tier"], p["priority"], "compensating", occurred_at, None, None))
        elif event_type == "reserve.replenished":
            c.execute("update rounds set supply=supply+?, reserve_total=reserve_total+?, "
                      "reserve_remaining=reserve_remaining+? where round_id=?",
                      (p["qty"], p["qty"], p["qty"], aggregate_id))
        elif event_type == "compensation.settled":
            c.execute(
                "update compensations set state='compensated', settled_at=?, "
                "settlement_transfer_id=? where comp_id=?",
                (occurred_at, p["settlement_transfer_id"], p["comp_id"]))
        elif event_type in ("transfer.confirmed", "transfer.dispatched", "receipt.recorded"):
            stage = "arrived" if event_type == "receipt.recorded" else event_type.split(".")[1]
            tid = aggregate_id
            c.execute("insert or ignore into transfer_stages values(?,?,?,?)",
                      (tid, stage, occurred_at, p["qty"]))
            # 状态只进不退：取已登记阶段的最高序
            ranks = [STAGE_RANK[s] for s, in c.execute(
                "select stage from transfer_stages where transfer_id=?", (tid,)).fetchall()]
            ranks.append(STAGE_RANK["published"])
            top = max(ranks)
            new_status = {v: k for k, v in STAGE_RANK.items()}[top]
            c.execute("update transfers set status=? where transfer_id=?", (new_status, tid))
        else:
            raise ValueError(f"未知事件类型：{event_type}")

    # ---- 审批（作为正式配置持久化，并在发布时快照进事件）----
    def add_approval(self, version_id: str, approver: str, role: str, sequence: int,
                     decision: str, comment: str | None, decided_at: str) -> None:
        self.conn.execute(
            "insert into approvals(version_id,approver,role,sequence,decision,comment,decided_at) "
            "values(?,?,?,?,?,?,?)",
            (version_id, approver, role, sequence, decision, comment, decided_at),
        )

    def list_approvals(self, version_id: str):
        return self.conn.execute(
            "select * from approvals where version_id=? order by sequence", (version_id,)).fetchall()
