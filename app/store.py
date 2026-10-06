"""SQLite 事件溯源存储：演练与稳定标识事件账本。

* 事件以 (drill_id, event_id) 为主键持久化，accepted=0 的拒单同样落库，
  但不参与状态重放——重复投递只回放原始结果，且恢复状态不被污染。
* 所有载荷/结果以 JSON 原文保存，重放结果与首次处理逐字节一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drills (
    id         TEXT PRIMARY KEY,
    spec_json  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    drill_id     TEXT NOT NULL REFERENCES drills(id),
    seq          INTEGER NOT NULL,
    event_id     TEXT NOT NULL,
    type         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    accepted     INTEGER NOT NULL,
    reason       TEXT,
    detail_json  TEXT,
    result_json  TEXT,
    PRIMARY KEY (drill_id, event_id),
    UNIQUE (drill_id, seq)
);
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def lock(self) -> threading.RLock:
        return self._lock

    # -- drills -----------------------------------------------------------

    def insert_drill(self, drill_id: str, spec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO drills(id, spec_json) VALUES (?, ?)",
                (drill_id, json.dumps(spec, ensure_ascii=False, sort_keys=True)))
            self._conn.commit()

    def get_drill_spec(self, drill_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT spec_json FROM drills WHERE id=?", (drill_id,)).fetchone()
        return json.loads(row["spec_json"]) if row else None

    def list_drills(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, spec_json, created_at FROM drills ORDER BY id").fetchall()
        return [{"id": r["id"], "created_at": r["created_at"],
                 **json.loads(r["spec_json"])} for r in rows]

    # -- events -----------------------------------------------------------

    def next_seq(self, drill_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM events WHERE drill_id=?",
                (drill_id,)).fetchone()
        return int(row["n"])

    def get_event(self, drill_id: str, event_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE drill_id=? AND event_id=?",
                (drill_id, event_id)).fetchone()
        return self._row_to_entry(row) if row else None

    def append_event(self, drill_id: str, seq: int, event_id: str, ev_type: str,
                     payload: dict, accepted: bool, reason: str | None,
                     detail: dict | None, result: dict | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(drill_id, seq, event_id, type, payload_json,"
                " accepted, reason, detail_json, result_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (drill_id, seq, event_id, ev_type,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 1 if accepted else 0, reason,
                 json.dumps(detail or {}, ensure_ascii=False, sort_keys=True) if detail else None,
                 json.dumps(result, ensure_ascii=False, sort_keys=True) if result else None))
            self._conn.commit()

    def ledger(self, drill_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE drill_id=? ORDER BY seq",
                (drill_id,)).fetchall()
        return [self._row_to_entry(r) for r in rows]  # type: ignore[list-item]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict:
        return {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "type": row["type"],
            "payload": json.loads(row["payload_json"]),
            "accepted": bool(row["accepted"]),
            "reason": row["reason"],
            "detail": json.loads(row["detail_json"]) if row["detail_json"] else {},
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }
