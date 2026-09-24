"""事件存储：SQLite 持久化的不可变事件日志 + 命令幂等表。

每个业务用例在一个事务内：查重 command_id -> 追加事件 -> 登记命令结果。
进程随时可以重启，重放事件日志即可完整恢复运营态，未决轮次与审批继续存在。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists events(
    event_id       text primary key,
    event_type     text not null,
    aggregate_type text not null,
    aggregate_id   text not null,
    occurred_at    text not null,
    seq            integer not null,
    command_id     text,
    payload        text not null
);
create index if not exists events_aggregate on events(aggregate_type, aggregate_id, seq);
create index if not exists events_time on events(occurred_at);

create table if not exists commands(
    command_id   text primary key,
    command_type text not null,
    result_type  text not null,
    result_id    text not null,
    accepted_at  text not null
);

create table if not exists metadata(
    key   text primary key,
    value text not null
);
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False 时调用方自行保证事务串行；服务层每个用例独立连接。
        self._path = str(path)
        self._owner = sqlite3.connect(self._path)
        self._owner.row_factory = sqlite3.Row
        self._owner.executescript(SCHEMA)
        self._owner.commit()

    @property
    def path(self) -> str:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """返回一个启用外键与行工厂的新连接（内存库除外，内存库复用主连接）。"""
        if self._path == ":memory:":
            return self._owner
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA)

    def next_seq(self, connection: sqlite3.Connection) -> int:
        """事务内取下一个全局序列号；SQLite 写事务串行化，单进程下无竞争。"""
        return connection.execute("select coalesce(max(seq), 0) + 1 from events").fetchone()[0]

    # -- 命令幂等 ----------------------------------------------------------

    def command_result(self, connection: sqlite3.Connection, command_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "select * from commands where command_id = ?", (command_id,)
        ).fetchone()

    def record_command(
        self,
        connection: sqlite3.Connection,
        command_id: str,
        command_type: str,
        result_type: str,
        result_id: str,
        accepted_at: str,
    ) -> None:
        connection.execute(
            "insert into commands values (?, ?, ?, ?, ?)",
            (command_id, command_type, result_type, result_id, accepted_at),
        )

    # -- 事件 --------------------------------------------------------------

    def append(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        seq: int,
        event_id: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": event_id or f"evt-{seq:08d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at.isoformat(),
            "seq": seq,
            "command_id": command_id,
            "payload": payload,
        }
        connection.execute(
            "insert into events(event_id, event_type, aggregate_type, aggregate_id, "
            "occurred_at, seq, command_id, payload) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                event_type,
                aggregate_type,
                aggregate_id,
                event["occurred_at"],
                seq,
                command_id,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        return event

    def load_events(
        self,
        connection: sqlite3.Connection,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "select * from events"
        clauses: list[str] = []
        params: list[Any] = []
        if aggregate_type:
            clauses.append("aggregate_type = ?")
            params.append(aggregate_type)
        if aggregate_id:
            clauses.append("aggregate_id = ?")
            params.append(aggregate_id)
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by seq"
        rows = connection.execute(sql, params).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        event = {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "occurred_at": row["occurred_at"],
            "seq": row["seq"],
            "command_id": row["command_id"],
            "payload": json.loads(row["payload"]),
        }
        return event

    def all_events(self) -> list[dict[str, Any]]:
        return self.load_events(self._owner)

    def meta_get(self, key: str) -> str | None:
        row = self._owner.execute("select value from metadata where key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def meta_set(self, key: str, value: str) -> None:
        self._owner.execute(
            "insert into metadata(key, value) values(?, ?) "
            "on conflict(key) do update set value = excluded.value",
            (key, value),
        )
        self._owner.commit()

    def append_many(self, connection: sqlite3.Connection, events: Iterable[dict]) -> None:
        for event in events:
            connection.execute(
                "insert into events(event_id, event_type, aggregate_type, aggregate_id, "
                "occurred_at, seq, command_id, payload) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["event_id"],
                    event["event_type"],
                    event["aggregate_type"],
                    event["aggregate_id"],
                    event["occurred_at"],
                    event["seq"],
                    event["command_id"],
                    json.dumps(event["payload"], ensure_ascii=False),
                ),
            )
