"""事件溯源的增量三色标记-清扫引擎。

核心安全性质（审查员可对任意回收周期核验）：
    清扫阶段回收的对象，必然是“标记基线快照不可达”的白色对象；
    标记期间对边/根的改写通过 SATB（snapshot-at-the-beginning）屏障保留，
    旧目标被屏障变灰并入队，新接入的白色目标同样被变灰保留，
    因此标记结束时所有在快照中存活、或标记期间仍被经保留路径持有的对象
    都不会是白色，清扫绝不会释放当前（快照）根集合可到达的对象。

阶段：
    IDLE      尚未开始（START 之后进入 MARK）
    MARK      标记进行中，灰队列未清空
    SWEEP     一次清扫完成后的稳态（可继续 SWEEP / REOPEN / START 新一轮）

事件标识稳定持久化；重复投递（同 ID 同载荷）只回放原结果，
同 ID 不同载荷拒绝且不污染恢复状态。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

WHITE, GRAY, BLACK = "white", "gray", "black"
IDLE, MARK, SWEEP = "idle", "mark", "sweep"

EVENT_TYPES = ("start", "mutate_edge", "mutate_root", "step", "sweep", "reopen")
MAX_OBJECTS = 24
MAX_EVENTS = 48
MAX_EDGES_PER_OBJECT = MAX_OBJECTS  # 至多每个对象指向其余对象各一条


class Reject(Exception):
    """事件被拒绝；携带首个（及全部）拒因，不改变引擎状态。"""

    def __init__(self, reason: str, reasons: Optional[List[str]] = None):
        super().__init__(reason)
        self.reason = reason
        self.reasons = reasons or [reason]


def _sorted_reachable(snapshot_roots: List[str], edges: Dict[str, List[str]]) -> List[str]:
    """在不可变快照上求根集合可达集合，结果稳定排序。"""
    seen: set = set()
    stack = list(snapshot_roots)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(reversed(edges.get(node, ())))
    return sorted(seen)


@dataclass
class EngineState:
    objects: List[str] = field(default_factory=list)
    edges: Dict[str, List[str]] = field(default_factory=dict)          # 稳定去重有序
    roots: List[str] = field(default_factory=list)                     # 稳定去重有序
    colors: Dict[str, str] = field(default_factory=dict)
    gray: List[str] = field(default_factory=list)
    phase: str = IDLE
    snap_roots: List[str] = field(default_factory=list)
    snap_edges: Dict[str, List[str]] = field(default_factory=dict)
    live: List[str] = field(default_factory=list)                      # 最近一次存活裁决
    reclaimed: List[str] = field(default_factory=list)                 # 最近一次回收
    cycle_reclaimed: List[str] = field(default_factory=list)           # 当前周期回收名单（重复 sweep 幂等回放）
    freed: List[str] = field(default_factory=list)                     # 累计已回收（跨周期，再引用即拒绝）
    last_processed: Optional[str] = None
    cycle: int = 0
    barrier_log: List[Dict[str, str]] = field(default_factory=list)    # 屏障保留证据
    history: List[Dict[str, Any]] = field(default_factory=list)        # 已接受事件回放结果
    rejected: List[Dict[str, Any]] = field(default_factory=list)       # 拒绝流水（含原因）

    def snapshot(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "cycle": self.cycle,
            "objects": list(self.objects),
            "roots": list(self.roots),
            "edges": {k: list(v) for k, v in self.edges.items() if v},
            "colors": {o: self.colors.get(o, WHITE) for o in self.objects},
            "gray_queue": list(self.gray),
            "gray_remaining": len(self.gray),
            "snapshot_roots": list(self.snap_roots),
            "live": list(self.live),
            "reclaimed": list(self.reclaimed),
            "freed": list(self.freed),
            "last_processed": self.last_processed,
            "barrier_retentions": list(self.barrier_log),
            "rejected": list(self.rejected),
            "applied_count": len(self.history),
        }


@dataclass
class _Spec:
    objects: List[str]
    edges: Dict[str, List[str]]
    roots: List[str]


class CollectorEngine:
    """纯内存、线程安全的事件回放引擎（持久化由上层 store 负责）。"""

    def __init__(self, spec: Dict[str, Any]):
        parsed = self._validate_spec(spec)
        self._spec = parsed
        self._lock = threading.RLock()
        self.state = EngineState(objects=list(parsed.objects))
        self.state.edges = {o: list(parsed.edges.get(o, [])) for o in parsed.objects}
        self.state.roots = list(parsed.roots)
        self.state.colors = {o: WHITE for o in parsed.objects}
        self.state.live = _sorted_reachable(self.state.roots, self.state.edges)
        # event_id -> 规范化载荷（重复投递判定）
        self._seen: Dict[str, str] = {}

    # ------------------------------------------------------------------ spec
    @staticmethod
    def _dedup_ordered(items: List[str]) -> List[str]:
        out: List[str] = []
        for x in items:
            if x not in out:
                out.append(x)
        return out

    @classmethod
    def _validate_spec(cls, spec: Dict[str, Any]) -> _Spec:
        if not isinstance(spec, dict):
            raise Reject("spec 必须是对象")
        objs = spec.get("objects", [])
        if not isinstance(objs, list) or not all(isinstance(o, str) and o for o in objs):
            raise Reject("objects 必须是非空字符串数组")
        if not (2 <= len(objs) <= MAX_OBJECTS):
            raise Reject(f"objects 数量须在 2..{MAX_OBJECTS} 之间")
        if len(set(objs)) != len(objs):
            raise Reject("objects 中存在重复标识")
        objset = set(objs)

        raw_edges = spec.get("edges", {})
        if not isinstance(raw_edges, dict):
            raise Reject("edges 必须是 {源: [目标]} 对象")
        edges: Dict[str, List[str]] = {}
        edge_count = 0
        for src, targets in raw_edges.items():
            if src not in objset:
                raise Reject(f"边的源 {src!r} 不在对象集合中")
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise Reject(f"edges[{src!r}] 必须是字符串数组")
            for t in targets:
                if t not in objset:
                    raise Reject(f"边 {src}->{t} 的目标不在对象集合中")
            deduped = cls._dedup_ordered(targets)
            edges[src] = deduped
            edge_count += len(deduped)
            if edge_count > MAX_OBJECTS * MAX_OBJECTS:
                raise Reject("边数量过多")

        roots = spec.get("roots", [])
        if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
            raise Reject("roots 必须是字符串数组")
        for r in roots:
            if r not in objset:
                raise Reject(f"根 {r!r} 不在对象集合中")
        roots = cls._dedup_ordered(roots)
        return _Spec(objects=list(objs), edges=edges, roots=roots)

    # ------------------------------------------------------------- utilities
    @staticmethod
    def _canonical(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _reject_record(self, event_id: str, etype: str, payload: Dict[str, Any], reasons: List[str]):
        rec = {"id": event_id, "type": etype, "reasons": reasons, "payload": payload}
        self.state.rejected.append(rec)
        return rec

    def _turn_gray(self, obj: str, why: str, event_id: str) -> bool:
        """屏障/着色：把对象置灰并入队（已灰/已黑则不动）。"""
        if self.state.colors.get(obj) == WHITE:
            self.state.colors[obj] = GRAY
            self.state.gray.append(obj)
            self.state.barrier_log.append(
                {"event_id": event_id, "object": obj, "reason": why}
            )
            return True
        return False

    def _retain_old(self, obj: str, why: str, event_id: str) -> None:
        """SATB 保留旧目标/旧根：白色则变灰入队；已是灰/黑同样记录保留证据。"""
        color = self.state.colors.get(obj, WHITE)
        if color == WHITE:
            self.state.colors[obj] = GRAY
            self.state.gray.append(obj)
        self.state.barrier_log.append(
            {"event_id": event_id, "object": obj, "reason": why, "from_color": color}
        )

    def _check_not_freed(self, objs: List[str], what: str) -> None:
        bad = sorted(set(objs) & set(self.state.freed))
        if bad:
            raise Reject(f"拒绝引用已回收对象 {bad}（{what}）：已回收对象不可再被根/边引用")

    @staticmethod
    def _parse_targets(targets: Any) -> Tuple[List[str], List[str]]:
        errors: List[str] = []
        if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
            return [], ["targets 必须是字符串数组"]
        return targets, errors

    # -------------------------------------------------------------- apply()
    def apply(
        self, event_id: str, etype: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """应用一个事件。

        成功返回 {"status":"accepted", ...}；拒绝返回
        {"status":"rejected","reasons":[...]}，且不改变任何恢复状态。
        重复（同 ID 同载荷）返回 {"status":"duplicate", ...}，只回放原结果。
        """
        payload = payload or {}
        with self._lock:
            if not isinstance(event_id, str) or not event_id:
                # 无稳定标识无法去重/持久化，直接拒绝
                return {"status": "rejected", "reasons": ["event_id 必须是非空字符串"]}
            canonical = self._canonical({"type": etype, "payload": payload})
            if event_id in self._seen:
                if self._seen[event_id] == canonical:
                    prev = next(
                        (h for h in reversed(self.state.history) if h["id"] == event_id), None
                    )
                    return {
                        "status": "duplicate",
                        "id": event_id,
                        "original": prev["result"] if prev else None,
                    }
                return {
                    "status": "rejected",
                    "id": event_id,
                    "reasons": [f"事件标识 {event_id} 已使用但载荷不同（同 ID 不同载荷拒绝）"],
                }

            if len(self.state.history) >= MAX_EVENTS:
                return {
                    "status": "rejected",
                    "id": event_id,
                    "reasons": [f"演练事件数上限 {MAX_EVENTS}"],
                }

            try:
                if etype not in EVENT_TYPES:
                    raise Reject(f"未知事件类型 {etype!r}，允许：{', '.join(EVENT_TYPES)}")
                handler = getattr(self, f"_ev_{etype}")
                effect = handler(event_id, payload)
            except Reject as rej:
                rec = self._reject_record(event_id, etype, payload, rej.reasons)
                return {"status": "rejected", "id": event_id, "reasons": rec["reasons"]}

            # 仅在完全成功后登记稳定标识与流水（拒绝路径不登记 => 可重试）
            self._seen[event_id] = canonical
            result = {"status": "accepted", "id": event_id, "type": etype, **(effect or {})}
            self.state.history.append({"id": event_id, "type": etype, "payload": payload, "result": result})
            return result

    # ---------------------------------------------------------------- events
    def _ev_start(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        if s.phase == MARK:
            raise Reject("标记进行中，不能再次 START（请先 step 至灰队列清空或 reopen）")
        if p:
            raise Reject("start 事件不接受载荷字段")
        s.phase = MARK
        s.cycle += 1
        s.reclaimed = []
        s.cycle_reclaimed = []
        s.last_processed = None
        s.barrier_log = []
        s.rejected = []
        # —— 标记基线快照（SATB）——
        s.snap_roots = list(s.roots)
        s.snap_edges = {k: list(v) for k, v in s.edges.items()}
        s.colors = {o: WHITE for o in s.objects}
        s.gray = []
        for r in s.snap_roots:  # 稳定顺序
            self._turn_gray(r, "root", event_id)
        s.live = _sorted_reachable(s.snap_roots, s.snap_edges)
        return {"cycle": s.cycle, "grayed_roots": list(s.snap_roots)}

    def _ev_mutate_edge(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        src = p.get("src")
        if not isinstance(src, str) or src not in s.colors:
            raise Reject("mutate_edge 需要有效字段 src（对象集合内）")
        if src in s.freed:
            raise Reject(f"拒绝引用已回收对象 {[src]}（边的源）")
        action = p.get("action", "set")
        targets, errs = self._parse_targets(p.get("targets", []))
        if errs:
            raise Reject("; ".join(errs))
        for t in targets:
            if t not in s.colors:
                raise Reject(f"目标 {t!r} 不在对象集合中")
        self._check_not_freed(targets, "边目标")
        new_targets = self._dedup_ordered(targets)
        cur = s.edges.setdefault(src, [])

        old_targets: List[str]   # 因本次改写而被断开的目标（SATB 旧目标）
        added_targets: List[str] # 因本次改写而新接入的目标（新白色目标）
        if action == "set":
            old_targets = list(cur)
            added_targets = [t for t in new_targets if t not in cur]
            final = list(new_targets)
        elif action == "add":
            old_targets = []
            added_targets = [t for t in new_targets if t not in cur]
            final = self._dedup_ordered(cur + new_targets)
        elif action == "remove":
            old_targets = [t for t in cur if t in new_targets]
            added_targets = []
            final = [t for t in cur if t not in new_targets]
        else:
            raise Reject("mutate_edge.action 必须是 set/add/remove")
        if len(final) > MAX_EDGES_PER_OBJECT:
            raise Reject(f"单个对象出边数量上限 {MAX_EDGES_PER_OBJECT}")

        # 仅在 MARK 期间需要屏障；IDLE/SWEEP 下改写是普通修改
        barrier: List[str] = []
        if s.phase == MARK:
            # SATB 屏障：保留被断开的旧目标（改写瞬间仍可能存活；无论其当时颜色）
            for t in old_targets:
                self._retain_old(t, f"satb-old-target:{src}", event_id)
                barrier.append(t)
            # 新接入的白色目标：增量不变式要求其保留（灰），其后续出边由标记负责
            for t in added_targets:
                if self._turn_gray(t, f"new-white-target:{src}", event_id):
                    barrier.append(t)
        s.edges[src] = final
        return {
            "src": src,
            "action": action,
            "old_targets": old_targets,
            "added_targets": added_targets,
            "new_targets": final,
            "barrier_retained": barrier,
        }

    def _ev_mutate_root(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        action = p.get("action", "set")
        roots, errs = self._parse_targets(p.get("roots", []))
        if errs:
            raise Reject("; ".join(errs))
        for r in roots:
            if r not in s.colors:
                raise Reject(f"根 {r!r} 不在对象集合中")
        self._check_not_freed(roots, "根")
        new_roots = self._dedup_ordered(roots)

        old_roots: List[str]    # 被断开的旧根
        added_roots: List[str]  # 新接入的根
        if action == "set":
            old_roots = list(s.roots)
            added_roots = [r for r in new_roots if r not in s.roots]
            final = list(new_roots)
        elif action == "add":
            old_roots = []
            added_roots = [r for r in new_roots if r not in s.roots]
            final = self._dedup_ordered(s.roots + new_roots)
        elif action == "remove":
            old_roots = [r for r in s.roots if r in new_roots]
            added_roots = []
            final = [r for r in s.roots if r not in new_roots]
        else:
            raise Reject("mutate_root.action 必须是 set/add/remove")

        barrier: List[str] = []
        if s.phase == MARK:
            for r in old_roots:  # 旧根：SATB 保留（无论当时颜色，均留证）
                self._retain_old(r, "satb-old-root", event_id)
                barrier.append(r)
            for r in added_roots:  # 新接入白色根：保留
                if self._turn_gray(r, "new-white-root", event_id):
                    barrier.append(r)
        s.roots = final
        return {
            "action": action,
            "old_roots": old_roots,
            "added_roots": added_roots,
            "new_roots": final,
            "barrier_retained": barrier,
        }

    def _ev_step(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        if s.phase != MARK:
            raise Reject(f"step 仅允许在标记阶段（当前 {s.phase}）")
        if p:
            raise Reject("step 事件不接受载荷字段")
        if not s.gray:
            raise Reject("灰队列已清空，标记已完成，应执行 sweep（或 reopen 后重开）")
        # 每次步进仅处理一个稳定排序的灰对象（按对象标识排序）
        s.gray.sort()
        obj = s.gray.pop(0)
        s.colors[obj] = BLACK
        s.last_processed = obj
        newly_grayed: List[str] = []
        for child in s.edges.get(obj, []):  # 当前出边；白色新目标此前已被屏障变灰
            if s.colors.get(child) == WHITE:
                s.colors[child] = GRAY
                s.gray.append(child)
                newly_grayed.append(child)
        completed = not s.gray
        if completed:
            # 标记终止：快照存活集必须全部非白（安全不变式）
            survivors = _sorted_reachable(s.snap_roots, s.snap_edges)
            leaked = [o for o in survivors if s.colors.get(o) == WHITE]
            if leaked:
                raise Reject(f"内部不变式破坏：快照存活对象仍为白色 {leaked}")
        return {
            "processed": obj,
            "newly_grayed": newly_grayed,
            "gray_remaining": len(s.gray),
            "marking_complete": completed,
        }

    def _ev_sweep(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        if p:
            raise Reject("sweep 事件不接受载荷字段")
        if s.phase == IDLE:
            raise Reject("非法阶段清扫：尚未 start，不能 sweep")
        if s.phase == MARK and s.gray:
            raise Reject(
                f"非法清扫：灰队列未清空（剩余 {len(s.gray)} 个：{sorted(s.gray)}），不得清扫"
            )
        # 同周期稳态下重复 sweep：幂等回放当前周期的回收裁决
        if s.phase == SWEEP:
            survivors = _sorted_reachable(s.snap_roots, s.snap_edges)
            return {
                "reclaimed": list(s.cycle_reclaimed),
                "live": list(s.live),
                "snapshot_survivors": survivors,
                "count_reclaimed": len(s.cycle_reclaimed),
                "idempotent": True,
            }
        # 安全裁决：仅“快照不可达”的白色对象可回收
        survivors = _sorted_reachable(s.snap_roots, s.snap_edges)
        survivor_set = set(survivors)
        unreachable_white = sorted(
            o
            for o in s.objects
            if s.colors.get(o) == WHITE and o not in survivor_set and o not in s.freed
        )
        live_now = sorted(
            o for o in s.objects if s.colors.get(o) != WHITE or o in survivor_set
        )
        # 防御性校验：任何快照可达对象都不得出现在回收名单
        assert not (set(unreachable_white) & survivor_set), "安全性断言失败"
        # 物理摘除已回收对象的全部入边/出边/根引用：其内存已回收，不可复活
        freed_set = set(unreachable_white)
        new_edges: Dict[str, List[str]] = {}
        for src, targets in s.edges.items():
            if src in freed_set:
                continue
            new_edges[src] = [t for t in targets if t not in freed_set]
        s.edges = new_edges
        s.roots = [r for r in s.roots if r not in freed_set]
        for o in unreachable_white:
            if o not in s.freed:
                s.freed.append(o)
        s.live = live_now
        s.reclaimed = unreachable_white
        s.cycle_reclaimed = unreachable_white
        s.phase = SWEEP
        return {
            "reclaimed": unreachable_white,
            "live": live_now,
            "snapshot_survivors": survivors,
            "count_reclaimed": len(unreachable_white),
        }

    def _ev_reopen(self, event_id: str, p: Dict[str, Any]) -> Dict[str, Any]:
        s = self.state
        if p:
            raise Reject("reopen 事件不接受载荷字段")
        if s.phase != MARK:
            raise Reject(f"reopen 仅允许在标记中途（当前阶段 {s.phase}）")
        if not s.gray:
            raise Reject("标记已完成（灰队列已空），无需 reopen；可直接 sweep")
        # 中断恢复：颜色、灰队列、快照、屏障证据原样保留，随后继续同一轮标记。
        # 屏障证据中追加一条恢复记录，使“中断后继续”可在存活图中被观察到。
        s.barrier_log.append(
            {
                "event_id": event_id,
                "object": "*",
                "reason": "reopen-resume",
            }
        )
        return {
            "resumed_cycle": s.cycle,
            "gray_queue": list(s.gray),
            "colors": dict(s.colors),
        }

    # -------------------------------------------------------------- queries
    def safety_report(self) -> Dict[str, Any]:
        """审查员视图：对当前/最近回收周期的安全核验。"""
        s = self.state
        snapshot_survivors = _sorted_reachable(s.snap_roots, s.snap_edges)
        report = {
            "phase": s.phase,
            "cycle": s.cycle,
            "snapshot_roots": list(s.snap_roots),
            "snapshot_reachable": snapshot_survivors,
            "white_objects": sorted(o for o, c in s.colors.items() if c == WHITE),
            "gray_queue": list(s.gray),
            "barrier_retentions": list(s.barrier_log),
            "last_reclaimed": list(s.reclaimed),
            "live": list(s.live),
        }
        if s.phase == SWEEP:
            bad = sorted(set(s.reclaimed) & set(snapshot_survivors))
            report["cycle_safe"] = not bad
            report["violations"] = bad
        else:
            report["cycle_safe"] = None  # 周期尚未结束
        return report

    def replay_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"id": h["id"], "type": h["type"], "payload": h["payload"], "result": h["result"]}
                for h in self.state.history
            ]
