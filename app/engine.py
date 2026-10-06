"""增量三色标记-清扫引擎（纯函数、确定性）。

语义约定
========
* 颜色：白=未标记/候选垃圾，灰=已标记但出边未扫描，黑=已标记且出边已扫描。
* 标记启动：``start_mark`` 把当前所有根置灰入队，阶段进入 ``MARKING``。
* 每次 ``step`` 仅弹出灰队列中稳定排序（字典序）的**一个**灰对象，
  将其白色存活目标置灰入队，自身扫描为黑色，并记录存活证据。
* 标记期间允许 ``set_edge`` / ``set_root`` 改写。
  ``set_edge`` 载荷为 ``{src, old_dst, new_dst}``（后两者可为 null，
  分别表示纯新增 / 纯删除），在同一事件内原子地完成“删旧边、接新边”，
  屏障同时作用于两侧：

  - **旧目标（删除屏障）**：被移除出边指向的旧目标若仍为白色，立即染灰，
    使其本周期按存活处理（其出边随后随灰对象步进被扫描）；
  - **新接入白色目标（插入屏障）**：新边指向的目标若为白色，立即染灰；
  - 写入者 src 若已是黑色，退回灰色重新入队，以便观察新的出边集合。
  - ``set_root`` 同理：新增的白色根立即染灰；被移除的旧根本周期亦染灰
    保留（根身份即存活证据）。

  由此强三色不变量（不存在黑对象指向白对象的当前边）始终成立，任何周期
  都不会释放“仍可由当前根集合到达”的对象。
* ``sweep`` 前置条件（非法阶段清扫一律拒绝）：

  - 未启动周期 => ``sweep_without_mark``；
  - 灰队列未空（``MARKING``）=> ``sweep_not_ready``；
  - 仅 ``READY``（队列已清空）可清扫。

* 清扫：仅回收*不可达白色*对象（颜色为白且不在最终存活集）；屏障保留的
  灰/黑对象即便当前已不可达，本周期也不回收。存活者颜色重置为白，
  周期关闭进入 IDLE，可重开下一周期。
"""
from __future__ import annotations

from typing import Mapping

from .models import Color, Event, LedgerEntry, Phase, State

# 驱动 GC 状态机的事件；reopen 是不改变状态的恢复控制事件（在 service 层处理）
GC_EVENTS = {"start_mark", "set_edge", "set_root", "step", "sweep"}
VALID_EVENTS = GC_EVENTS | {"reopen"}


class EngineError(Exception):
    """可预期的拒绝：携带稳定 reason code 与说明 detail。"""

    def __init__(self, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# 构造与纯函数式状态变换
# ---------------------------------------------------------------------------

def initial_state(objects: list[str], roots: list[str],
                  edges: Mapping[str, list[str]]) -> State:
    for label in roots:
        if label not in objects:
            raise EngineError("unknown_object", {"object": label, "where": "roots"})
    for src, dests in edges.items():
        if src not in objects:
            raise EngineError("unknown_object", {"object": src, "where": "edges"})
        for dst in dests:
            if dst not in objects:
                raise EngineError("unknown_object", {"object": dst, "where": f"edges[{src}]"})
    edge_map = {o: frozenset(edges.get(o, ())) for o in objects}
    return State(
        universe=tuple(objects),
        alive=frozenset(objects),
        edges=edge_map,
        roots=frozenset(roots),
        colors={o: Color.WHITE for o in objects},
        queue=(),
        phase=Phase.IDLE,
    )


def _reachable(roots: frozenset[str], edges: Mapping[str, frozenset[str]],
               alive: frozenset[str]) -> frozenset[str]:
    seen: set[str] = set()
    stack = [r for r in roots if r in alive]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(d for d in edges.get(node, frozenset()) if d in alive)
    return frozenset(seen)


def apply(st: State, ev: Event) -> tuple[State, dict]:
    """把单个 GC 事件作用到状态上，返回 (新状态, 结果明细)。

    非法输入抛出 EngineError；调用方负责把拒绝登记为 accepted=False。
    ``reopen`` 不得进入此处。
    """
    if ev.type not in GC_EVENTS:
        raise EngineError("unknown_event_type", {"type": ev.type})

    if ev.type == "start_mark":
        if st.phase != Phase.IDLE:
            raise EngineError("mark_already_active", {"phase": st.phase.value})
        colors = dict(st.colors)
        marked: list[str] = []
        for r in sorted(st.roots):
            if r in st.alive and colors[r] == Color.WHITE:
                colors[r] = Color.GRAY
                marked.append(r)
        return (State(st.universe, st.alive, st.edges, st.roots, colors,
                      tuple(sorted(marked)),
                      Phase.MARKING if marked else Phase.READY),
                {"gray_roots": marked})

    if ev.type == "set_edge":
        src = _str(ev.payload, "src")
        old_dst = _str(ev.payload, "old_dst", allow_null=True)
        new_dst = _str(ev.payload, "new_dst", allow_null=True)
        if old_dst is None and new_dst is None:
            raise EngineError("malformed_payload",
                              {"field": "old_dst/new_dst",
                               "expected": "at least one endpoint"})
        if old_dst is not None and old_dst == new_dst:
            raise EngineError("malformed_payload", {"reason": "old_dst == new_dst"})
        for name, ref in (("src", src), ("old_dst", old_dst), ("new_dst", new_dst)):
            if ref is not None:
                if ref not in st.universe:
                    raise EngineError("unknown_object", {"object": ref, "where": f"set_edge.{name}"})
                if ref not in st.alive:
                    raise EngineError("reference_reclaimed",
                                      {"object": ref, "where": f"set_edge.{name}"})

        current = set(st.edges.get(src, frozenset()))
        if old_dst is not None and old_dst not in current:
            raise EngineError("edge_absent", {"src": src, "dst": old_dst})
        if new_dst is not None and new_dst in current and old_dst != new_dst:
            raise EngineError("edge_exists", {"src": src, "dst": new_dst})

        colors = dict(st.colors)
        retained: list[str] = []
        rescanned: list[str] = []
        in_cycle = st.phase != Phase.IDLE

        if in_cycle:
            # ---- 删除屏障：旧目标白色则染灰，本周期保留 ----------------
            if old_dst is not None and colors.get(old_dst) == Color.WHITE:
                colors[old_dst] = Color.GRAY
                retained.append(old_dst)
            # ---- 插入屏障：新接入的白色目标染灰 ------------------------
            if new_dst is not None and colors.get(new_dst) == Color.WHITE:
                colors[new_dst] = Color.GRAY
                retained.append(new_dst)
            # 写入者已扫描（黑）=> 退回灰色重新入队观察新出边
            if colors.get(src) == Color.BLACK:
                colors[src] = Color.GRAY
                rescanned.append(src)

        nxt = set(current)
        if old_dst is not None:
            nxt.discard(old_dst)
        if new_dst is not None:
            nxt.add(new_dst)
        edges = dict(st.edges)
        edges[src] = frozenset(nxt)
        queue = _sync_queue(st.queue, colors, st.alive)
        new = State(st.universe, st.alive, edges, st.roots, colors, queue,
                    _phase_after(queue, st.phase))
        return new, {"src": src, "old_dst": old_dst, "new_dst": new_dst,
                     "barrier_retained": sorted(set(retained)),
                     "rescanned": sorted(set(rescanned))}

    if ev.type == "set_root":
        obj = _str(ev.payload, "object")
        is_root = _bool(ev.payload, "is_root")
        if obj not in st.universe:
            raise EngineError("unknown_object", {"object": obj, "where": "set_root"})
        if obj not in st.alive:
            raise EngineError("reference_reclaimed",
                              {"object": obj, "where": "set_root"})

        roots = set(st.roots)
        colors = dict(st.colors)
        retained: list[str] = []
        if is_root:
            if obj in roots:
                raise EngineError("root_exists", {"object": obj})
            roots.add(obj)
            if st.phase != Phase.IDLE and colors.get(obj) == Color.WHITE:
                # 新接入的白色根立即标灰
                colors[obj] = Color.GRAY
                retained.append(obj)
        else:
            if obj not in roots:
                raise EngineError("root_absent", {"object": obj})
            roots.discard(obj)
            if st.phase != Phase.IDLE and colors.get(obj) == Color.WHITE:
                # 旧根在本周期仍作为存活证据保留
                colors[obj] = Color.GRAY
                retained.append(obj)
        queue = _sync_queue(st.queue, colors, st.alive)
        new = State(st.universe, st.alive, st.edges, frozenset(roots), colors,
                    queue, _phase_after(queue, st.phase))
        return new, {"action": "add" if is_root else "remove", "object": obj,
                     "barrier_retained": sorted(set(retained))}

    if ev.type == "step":
        if st.phase == Phase.IDLE:
            raise EngineError("step_without_mark", {"phase": st.phase.value})
        if not st.queue:
            raise EngineError("mark_complete", {})
        colors = dict(st.colors)
        obj = st.queue[0]                  # 队列始终字典序稳定
        queue = st.queue[1:]
        new_gray: list[str] = []
        for target in sorted(st.edges.get(obj, frozenset())):
            if target in st.alive and colors[target] == Color.WHITE:
                colors[target] = Color.GRAY
                queue = tuple(sorted(queue + (target,)))
                new_gray.append(target)
        colors[obj] = Color.BLACK
        phase = Phase.READY if not queue else Phase.MARKING
        return (State(st.universe, st.alive, st.edges, st.roots, colors,
                      queue, phase),
                {"scanned": obj, "new_gray": new_gray})

    if ev.type == "sweep":
        # ---- 非法阶段清扫守卫 ----
        if st.phase == Phase.IDLE:
            raise EngineError("sweep_without_mark", {"phase": st.phase.value})
        if st.phase == Phase.MARKING or st.queue:
            raise EngineError("sweep_not_ready",
                              {"phase": st.phase.value,
                               "gray_queue": list(st.queue)})

        roots = frozenset(r for r in st.roots if r in st.alive)
        reachable = _reachable(roots, st.edges, st.alive)
        reclaimed = sorted(o for o in st.universe
                           if o in st.alive and o not in reachable
                           and st.colors.get(o) == Color.WHITE)
        # 防御性不变量：最终可达者不得为白（屏障正确性的最后一道闸）
        reachable_white = sorted(o for o in reachable if st.colors.get(o) == Color.WHITE)
        if reachable_white:  # pragma: no cover - 不可达分支：若触发即屏障缺陷
            raise EngineError("invariant_violation",
                              {"reachable_white": reachable_white})

        alive = frozenset(o for o in st.alive if o not in set(reclaimed))
        edges = {o: frozenset(d for d in st.edges.get(o, frozenset()) if d in alive)
                 for o in alive}
        colors = {o: Color.WHITE for o in alive}
        return (State(st.universe, alive, edges, roots, colors, (), Phase.IDLE),
                {"reclaimed": reclaimed, "alive": sorted(alive)})

    raise EngineError("unknown_event_type", {"type": ev.type})  # pragma: no cover


# ---------------------------------------------------------------------------
# 审计视图
# ---------------------------------------------------------------------------

def snapshot(st: State, result: dict | None = None,
             rejected: LedgerEntry | None = None,
             evidence: list[dict] | None = None,
             history: list[dict] | None = None) -> dict:
    """生成供页面/API 回放的快照（颜色、灰队列、存活图、证据、首个拒因）。"""
    reachable = _reachable(st.roots, st.edges, st.alive)
    view = {
        "phase": st.phase.value,
        "roots": sorted(st.roots),
        "alive": sorted(st.alive),
        "reclaimed": sorted(o for o in st.universe if o not in st.alive),
        "colors": {o: st.colors[o].value for o in sorted(st.alive)},
        "gray_queue": list(st.queue),
        "survival_graph": {
            "nodes": sorted(st.alive),
            "edges": sorted([u, v] for u in sorted(st.alive)
                            for v in sorted(st.edges.get(u, frozenset()))),
            "reachable": sorted(reachable),
        },
        "evidence": evidence or [],
        "history": history or [],
        "last_result": result or {},
        "first_rejection": None,
    }
    if rejected is not None:
        view["first_rejection"] = {
            "event_id": rejected.event_id,
            "type": rejected.type,
            "payload": rejected.payload,
            "reason": rejected.reason,
            "detail": rejected.detail,
        }
    return view


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _sync_queue(queue: tuple[str, ...], colors: Mapping[str, Color],
                alive: frozenset[str]) -> tuple[str, ...]:
    """改写后依据颜色重建稳定（字典序）灰队列。"""
    grays = {o for o, c in colors.items() if c == Color.GRAY and o in alive}
    return tuple(sorted(grays))


def _phase_after(queue: tuple[str, ...], current: Phase) -> Phase:
    if current == Phase.IDLE:
        return Phase.IDLE
    return Phase.READY if not queue else Phase.MARKING


def _str(payload: Mapping[str, object], key: str,
         allow_null: bool = False) -> str | None:
    if key not in payload:
        raise EngineError("malformed_payload", {"missing": key})
    val = payload[key]
    if val is None and allow_null:
        return None
    if not isinstance(val, str) or not val:
        raise EngineError("malformed_payload",
                          {"field": key, "expected": "non-empty string"})
    return val


def _bool(payload: Mapping[str, object], key: str) -> bool:
    if key not in payload or not isinstance(payload[key], bool):
        raise EngineError("malformed_payload",
                          {"field": key, "expected": "boolean"})
    return bool(payload[key])
