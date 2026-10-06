"""运行时：管理多个演练（drill），事件持久化 + 重启后重放恢复。"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .engine import CollectorEngine, Reject
from .store import JsonStore

VALID_TYPES = ("start", "mutate_edge", "mutate_root", "step", "sweep", "reopen")


class _DrillMeta:
    def __init__(self, drill_id: str, engine: CollectorEngine, store: JsonStore):
        self.id = drill_id
        self.engine = engine
        self.store = store
        self.lock = threading.RLock()


class Runtime:
    def __init__(self, store_dir: str):
        self.store = JsonStore(store_dir)
        self._meta: Dict[str, _DrillMeta] = {}
        self._global_lock = threading.RLock()

    # ------------------------------------------------------------- create
    def create_drill(self, drill_id: str, spec: Dict[str, Any]) -> CollectorEngine:
        """新建演练：先校验 spec（失败抛 Reject），再保存 spec 并构建引擎。"""
        with self._global_lock:
            if self.store.exists(drill_id):
                raise Reject(f"演练 {drill_id} 已存在")
            engine = CollectorEngine(spec)  # 校验失败在此抛出，不落盘
            self.store.save_spec(drill_id, spec)
            self._meta[drill_id] = _DrillMeta(drill_id, engine, self.store)
            return engine

    # ----------------------------------------------------------- dispatch
    def dispatch(self, drill_id: str, event: Any) -> Dict[str, Any]:
        with self._global_lock:
            meta = self._meta.get(drill_id)
            if meta is None:
                if not self.store.exists(drill_id):
                    return {"status": "rejected", "reasons": [f"演练 {drill_id} 不存在"]}
                self._recover_locked(drill_id)
                meta = self._meta[drill_id]

        with meta.lock:
            engine = meta.engine
            if not isinstance(event, dict):
                return {"status": "rejected", "reasons": ["事件必须是 JSON 对象"]}
            event_id = event.get("id")
            etype = event.get("type")
            payload = event.get("payload", {}) or {}

            if not isinstance(event_id, str) or not event_id:
                return {"status": "rejected", "reasons": ["缺少稳定事件标识 id"]}
            if not isinstance(etype, str) or etype not in VALID_TYPES:
                return {
                    "status": "rejected",
                    "id": event_id,
                    "reasons": [f"事件 type 缺失或非法，允许：{', '.join(VALID_TYPES)}"],
                }
            if not isinstance(payload, dict):
                return {"status": "rejected", "id": event_id, "reasons": ["payload 必须是对象"]}

            # 1) 引擎去重/校验/应用（拒绝不改变内存状态）
            result = engine.apply(event_id, etype, payload)
            # 2) 仅接受的事件 fsync 追加到 events.jsonl（恢复重放的唯一来源）
            if result["status"] == "accepted":
                meta.store.append_event(
                    drill_id, {"id": event_id, "type": etype, "payload": payload}
                )
            elif result["status"] == "rejected":
                # 拒绝进独立审计文件，物理上与恢复日志分离
                meta.store.append_rejection(
                    drill_id,
                    {
                        "id": event_id,
                        "type": etype,
                        "payload": payload,
                        "reasons": result["reasons"],
                    },
                )
            # duplicate：不写任何内容，只回放原结果
            return result

    # ------------------------------------------------------------ recovery
    def _recover_locked(self, drill_id: str) -> None:
        """从 spec + events.jsonl 重建引擎；拒绝审计永不参与恢复。"""
        spec = self.store.load_spec(drill_id)
        engine = CollectorEngine(spec)
        for ev in self.store.load_events(drill_id):
            eid, etype, payload = ev["id"], ev["type"], ev.get("payload", {})
            res = engine.apply(eid, etype, payload)
            if res["status"] != "accepted":
                raise Reject(f"恢复失败：事件 {eid} 重放异常（{res.get('reasons')}）")
        self._meta[drill_id] = _DrillMeta(drill_id, engine, self.store)

    def ensure_loaded(self, drill_id: str) -> Optional[_DrillMeta]:
        with self._global_lock:
            meta = self._meta.get(drill_id)
            if meta is not None:
                return meta
            if self.store.exists(drill_id):
                self._recover_locked(drill_id)
                return self._meta[drill_id]
            return None

    def get_engine(self, drill_id: str) -> Optional[CollectorEngine]:
        meta = self.ensure_loaded(drill_id)
        return meta.engine if meta else None

    def recover(self, drill_id: str) -> Optional[CollectorEngine]:
        """丢弃内存状态并从持久化日志重建（模拟进程重启后的恢复）。"""
        with self._global_lock:
            if not self.store.exists(drill_id):
                return None
            self._meta.pop(drill_id, None)
            self._recover_locked(drill_id)
            return self._meta[drill_id].engine

    def list_drills(self) -> List[str]:
        return self.store.list_drills()
