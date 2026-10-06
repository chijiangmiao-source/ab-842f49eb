"""演练服务：导入校验、幂等事件投递、账本重放与重开恢复比对。"""
from __future__ import annotations

from .engine import EngineError, VALID_EVENTS, apply, initial_state, snapshot
from .models import Color, Event, LedgerEntry, Phase, State
from .store import Store

MIN_OBJECTS = 2
MAX_OBJECTS = 24
MAX_EVENTS = 48


class ValidationError(Exception):
    def __init__(self, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# 演练导入与校验
# ---------------------------------------------------------------------------

def validate_spec(spec: dict) -> tuple[list[str], list[str], dict, list[dict]]:
    if not isinstance(spec, dict):
        raise ValidationError("malformed_spec", {"expected": "object"})
    objects = spec.get("objects")
    if not isinstance(objects, list) or not (MIN_OBJECTS <= len(objects) <= MAX_OBJECTS):
        raise ValidationError("bad_objects",
                              {"expected": f"list of {MIN_OBJECTS}..{MAX_OBJECTS} objects",
                               "got": type(objects).__name__})
    objs: list[str] = []
    for o in objects:
        if not isinstance(o, str) or not o:
            raise ValidationError("bad_objects", {"bad": o})
        if o in objs:
            raise ValidationError("duplicate_object", {"object": o})
        objs.append(o)

    roots = spec.get("roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise ValidationError("bad_roots", {"expected": "list of strings"})
    for r in roots:
        if r not in objs:
            raise ValidationError("unknown_object", {"object": r, "where": "roots"})
    if len(set(roots)) != len(roots):
        raise ValidationError("duplicate_root", {})

    edges_in = spec.get("edges", {})
    if not isinstance(edges_in, dict):
        raise ValidationError("bad_edges", {"expected": "object"})
    edges: dict[str, list[str]] = {}
    for src, dests in edges_in.items():
        if src not in objs:
            raise ValidationError("unknown_object", {"object": src, "where": "edges"})
        if not isinstance(dests, list) or not all(isinstance(d, str) for d in dests):
            raise ValidationError("bad_edges", {"src": src})
        for d in dests:
            if d not in objs:
                raise ValidationError("unknown_object",
                                      {"object": d, "where": f"edges[{src}]"})
        if len(set(dests)) != len(dests):
            raise ValidationError("duplicate_edge", {"src": src})
        edges[src] = list(dests)

    events = spec.get("events", [])
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValidationError("bad_events",
                              {"expected": f"at most {MAX_EVENTS} events"})
    seen_ids: set[str] = set()
    clean: list[dict] = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValidationError("bad_event", {"index": i})
        ev_id = ev.get("id")
        ev_type = ev.get("type")
        payload = ev.get("payload", {})
        if not isinstance(ev_id, str) or not ev_id:
            raise ValidationError("bad_event_id", {"index": i})
        if ev_id in seen_ids:
            raise ValidationError("duplicate_event_id", {"event_id": ev_id})
        seen_ids.add(ev_id)
        if ev_type not in VALID_EVENTS:
            raise ValidationError("unknown_event_type",
                                  {"event_id": ev_id, "type": ev_type})
        if not isinstance(payload, dict):
            raise ValidationError("malformed_payload", {"event_id": ev_id})
        clean.append({"id": ev_id, "type": ev_type, "payload": payload})
    return objs, list(dict.fromkeys(roots)), edges, clean


# ---------------------------------------------------------------------------
# 状态检查点序列化（模拟进程重启后从颜色/队列/证据恢复）
# ---------------------------------------------------------------------------

def state_to_dict(st: State) -> dict:
    return {
        "universe": list(st.universe),
        "alive": sorted(st.alive),
        "edges": {u: sorted(st.edges.get(u, frozenset())) for u in sorted(st.alive)},
        "roots": sorted(st.roots),
        "colors": {o: st.colors[o].value for o in sorted(st.alive)},
        "queue": list(st.queue),
        "phase": st.phase.value,
    }


def state_from_dict(data: dict) -> State:
    colors = {o: Color(c) for o, c in data["colors"].items()}
    for o in data["universe"]:
        colors.setdefault(o, Color.WHITE)
    return State(
        universe=tuple(data["universe"]),
        alive=frozenset(data["alive"]),
        edges={u: frozenset(v) for u, v in data["edges"].items()},
        roots=frozenset(data["roots"]),
        colors=colors,
        queue=tuple(data["queue"]),
        phase=Phase(data["phase"]),
    )


# ---------------------------------------------------------------------------
# 折叠（fold）：从账本重建状态、证据与时间线
# ---------------------------------------------------------------------------

def _record_evidence(evidence: list[dict], row: dict, ev: Event,
                     result: dict, st: State) -> None:
    seq = row["seq"]
    if ev.type == "start_mark":
        evidence.append({"kind": "roots_gray", "event_id": ev.id, "seq": seq,
                         "objects": result["gray_roots"]})
    elif ev.type == "step":
        evidence.append({"kind": "scan", "event_id": ev.id, "seq": seq,
                         "scanned": result["scanned"],
                         "new_gray": result["new_gray"],
                         "queue_after": list(st.queue)})
    elif ev.type in ("set_edge", "set_root") and result.get("barrier_retained"):
        evidence.append({"kind": "barrier_retain", "event_id": ev.id, "seq": seq,
                         "type": ev.type, "objects": result["barrier_retained"],
                         "rescanned": result.get("rescanned", [])})
    elif ev.type == "sweep":
        evidence.append({"kind": "sweep", "event_id": ev.id, "seq": seq,
                         "reclaimed": result["reclaimed"], "alive": result["alive"]})
    elif ev.type == "reopen":
        evidence.append({"kind": "reopen", "event_id": ev.id, "seq": seq,
                         "phase": st.phase.value, "gray_queue": list(st.queue)})


def fold(objects, roots, edges, rows: list[dict], st: State | None = None):
    """从账本（或其片段）折叠。返回 (state, evidence, history, first_rejection)。"""
    if st is None:
        st = initial_state(objects, roots, edges)
    evidence: list[dict] = []
    history: list[dict] = []
    first_rejection: LedgerEntry | None = None
    for row in rows:
        ev = Event(row["event_id"], row["type"], row["payload"])
        if not row["accepted"]:
            if first_rejection is None:
                first_rejection = LedgerEntry(ev.id, ev.type, ev.payload,
                                              False, row["reason"], row["detail"])
            continue
        if ev.type == "reopen":
            result = {"continued": True, "phase": st.phase.value,
                      "gray_queue": list(st.queue),
                      "colors": {o: st.colors[o].value for o in sorted(st.alive)
                                 if st.colors[o] != Color.WHITE}}
        else:
            st, result = apply(st, ev)
        _record_evidence(evidence, row, ev, result, st)
        history.append({"seq": row["seq"], "event_id": ev.id, "type": ev.type,
                        "payload": ev.payload, "result": result})
    return st, evidence, history, first_rejection


class DrillService:
    def __init__(self, store: Store):
        self.store = store

    # -- 导入 -------------------------------------------------------------

    def create_drill(self, spec: dict, drill_id: str | None = None) -> dict:
        objects, roots, edges, events = validate_spec(spec)
        drill_id = drill_id or spec.get("id")
        if not isinstance(drill_id, str) or not drill_id:
            raise ValidationError("missing_drill_id", {})
        if self.store.get_drill_spec(drill_id) is not None:
            raise ValidationError("drill_exists", {"id": drill_id})
        clean_spec = {"objects": objects, "roots": roots, "edges": edges,
                      "events": events}
        if "name" in spec and isinstance(spec["name"], str):
            clean_spec["name"] = spec["name"]
        with self.store.lock():
            self.store.insert_drill(drill_id, clean_spec)
            for ev in events:
                # 导入期事件若非法，同样以拒单落库（不污染状态）
                try:
                    self._deliver_locked(drill_id, ev["id"], ev["type"], ev["payload"])
                except EngineError:
                    pass
        return self.get_drill(drill_id)

    # -- 投递 -------------------------------------------------------------

    def deliver(self, drill_id: str, event_id: str, ev_type: str,
                payload: dict) -> dict:
        if self.store.get_drill_spec(drill_id) is None:
            raise ValidationError("drill_not_found", {"id": drill_id})
        if ev_type not in VALID_EVENTS:
            raise ValidationError("unknown_event_type", {"type": ev_type})
        if not isinstance(payload, dict):
            raise ValidationError("malformed_payload", {"event_id": event_id})
        with self.store.lock():
            return self._deliver_locked(drill_id, event_id, ev_type, payload)

    def _deliver_locked(self, drill_id: str, event_id: str, ev_type: str,
                        payload: dict) -> dict:
        existing = self.store.get_event(drill_id, event_id)
        if existing is not None:
            # 稳定标识：重复投递只回放原结果；同标识不同载荷拒绝
            if existing["type"] != ev_type or existing["payload"] != payload:
                raise EngineError("conflicting_event_payload", {
                    "event_id": event_id,
                    "stored": {"type": existing["type"], "payload": existing["payload"]},
                    "received": {"type": ev_type, "payload": payload},
                })
            return {"event_id": event_id, "replayed": True,
                    "accepted": existing["accepted"], "reason": existing["reason"],
                    "detail": existing["detail"], "result": existing["result"]}

        spec = self.store.get_drill_spec(drill_id)
        assert spec is not None
        seq = self.store.next_seq(drill_id)

        ledger = self.store.ledger(drill_id)
        st, _, _, _ = fold(spec["objects"], spec["roots"], spec["edges"], ledger)
        if ev_type == "reopen":
            if st.phase == Phase.IDLE:
                # 空闲态（无进行中的标记）重开没有可恢复的颜色/队列
                self.store.append_event(drill_id, seq, event_id, ev_type, payload,
                                        False, "reopen_without_mark",
                                        {"phase": st.phase.value}, None)
                raise EngineError("reopen_without_mark", {"phase": st.phase.value})
            result = {"continued": True}
            self.store.append_event(drill_id, seq, event_id, ev_type, payload,
                                    True, None, None, result)
            return {"event_id": event_id, "replayed": False, "accepted": True,
                    "reason": None, "detail": {}, "result": result}

        try:
            _, result = apply(st, Event(event_id, ev_type, payload))
        except EngineError as exc:
            # 拒单落库但不驱动状态 => 恢复状态不被污染
            self.store.append_event(drill_id, seq, event_id, ev_type, payload,
                                    False, exc.reason, exc.detail, None)
            raise
        self.store.append_event(drill_id, seq, event_id, ev_type, payload,
                                True, None, None, result)
        return {"event_id": event_id, "replayed": False, "accepted": True,
                "reason": None, "detail": {}, "result": result}

    # -- 查询 / 重放 ------------------------------------------------------

    def _rebuild_drill(self, drill_id: str):
        spec = self.store.get_drill_spec(drill_id)
        if spec is None:
            raise ValidationError("drill_not_found", {"id": drill_id})
        st, evidence, history, first_rej = fold(
            spec["objects"], spec["roots"], spec["edges"],
            self.store.ledger(drill_id))
        return spec, st, evidence, history, first_rej

    def get_drill(self, drill_id: str) -> dict:
        spec, st, evidence, history, first_rej = self._rebuild_drill(drill_id)
        last_result = history[-1]["result"] if history else None
        return {
            "id": drill_id,
            "name": spec.get("name"),
            "spec": {"objects": spec["objects"], "roots": spec["roots"],
                     "edges": spec["edges"]},
            "ledger": self.store.ledger(drill_id),
            "state": snapshot(st, last_result, first_rej, evidence, history),
        }

    def get_timeline(self, drill_id: str) -> dict:
        spec, final_st, evidence, history, first_rej = self._rebuild_drill(drill_id)
        cur = initial_state(spec["objects"], spec["roots"], spec["edges"])
        cur_evidence: list[dict] = []
        cur_history: list[dict] = []
        cur_first: LedgerEntry | None = None
        frames = [{"index": -1, "title": "初始（全白）",
                   "snapshot": snapshot(cur)}]
        for row in self.store.ledger(drill_id):
            ev = Event(row["event_id"], row["type"], row["payload"])
            if not row["accepted"]:
                cur_first = cur_first or LedgerEntry(ev.id, ev.type, ev.payload,
                                                     False, row["reason"], row["detail"])
                frames.append({"index": row["seq"], "event_id": ev.id,
                               "type": ev.type, "payload": ev.payload,
                               "accepted": False, "reason": row["reason"],
                               "detail": row["detail"],
                               "snapshot": snapshot(cur, None, cur_first,
                                                    cur_evidence, cur_history)})
                continue
            if ev.type == "reopen":
                result = {"continued": True, "phase": cur.phase.value,
                          "gray_queue": list(cur.queue)}
            else:
                cur, result = apply(cur, ev)
            _record_evidence(cur_evidence, row, ev, result, cur)
            cur_history.append({"seq": row["seq"], "event_id": ev.id,
                                "type": ev.type, "payload": ev.payload,
                                "result": result})
            frames.append({"index": row["seq"], "event_id": ev.id, "type": ev.type,
                           "payload": ev.payload, "accepted": True,
                           "reason": None, "detail": {}, "result": result,
                           "snapshot": snapshot(cur, result, cur_first,
                                                cur_evidence, cur_history)})
        return {"id": drill_id, "frames": frames,
                "first_rejection": snapshot(
                    final_st, history[-1]["result"] if history else None,
                    first_rej, evidence, history)["first_rejection"]}

    # -- 重开恢复一致性 ---------------------------------------------------

    def recovery_report(self, drill_id: str) -> dict:
        """比较“连续运行”与“标记中途重开（检查点序列化）后续跑”的最终裁决。"""
        spec, _, _, _, _ = self._rebuild_drill(drill_id)
        ledger = self.store.ledger(drill_id)

        continuous_st, continuous_ev, _, _ = fold(
            spec["objects"], spec["roots"], spec["edges"], ledger)
        continuous = self._signature(continuous_st, continuous_ev)

        reopens = [i for i, r in enumerate(ledger) if r["accepted"] and r["type"] == "reopen"]
        checkpoints: list[dict] = []
        recovered = continuous
        if reopens:
            last = reopens[-1]
            st1, ev1, _, _ = fold(spec["objects"], spec["roots"], spec["edges"],
                                  ledger[:last + 1])
            # 模拟重启：颜色/队列/证据经检查点序列化后原样恢复
            checkpoint = state_to_dict(st1)
            st2 = state_from_dict(checkpoint)
            st2, ev2, _, _ = fold(spec["objects"], spec["roots"], spec["edges"],
                                  ledger[last + 1:], st=st2)
            recovered = self._signature(st2, ev1 + ev2)
            checkpoints.append({"at_seq": ledger[last]["seq"],
                                "event_id": ledger[last]["event_id"],
                                "phase": checkpoint["phase"],
                                "gray_queue": checkpoint["queue"],
                                "colors": checkpoint["colors"]})
        return {
            "drill_id": drill_id,
            "reopen_count": len(reopens),
            "checkpoints": checkpoints,
            "continuous": continuous,
            "recovered": recovered,
            "consistent": continuous == recovered,
        }

    @staticmethod
    def _signature(st: State, evidence: list[dict]) -> dict:
        return {
            "phase": st.phase.value,
            "roots": sorted(st.roots),
            "alive": sorted(st.alive),
            "reclaimed": sorted(o for o in st.universe if o not in st.alive),
            "colors": {o: st.colors[o].value for o in sorted(st.alive)},
            "gray_queue": list(st.queue),
            "evidence": evidence,
        }
