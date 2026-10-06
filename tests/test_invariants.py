"""随机不变量测试：任何合法/非法事件序列下回收安全性都成立。"""
from __future__ import annotations

import random
import unittest

from app.engine import EngineError, apply, initial_state
from app.models import Color, Event, Phase


def black_to_white_violation(st) -> list[tuple[str, str]]:
    """强三色不变量：当前边不得出现 黑 → 白。"""
    bad = []
    for u, dests in st.edges.items():
        if st.colors.get(u) == Color.BLACK:
            for v in dests:
                if v in st.alive and st.colors.get(v) == Color.WHITE:
                    bad.append((u, v))
    return bad


def reachable(st) -> set[str]:
    seen, stack = set(), [r for r in st.roots if r in st.alive]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(st.edges.get(n, frozenset()))
    return seen


class RandomInvariantTests(unittest.TestCase):
    def test_random_sequences_never_free_reachable(self):
        rng = random.Random(20261006)
        for trial in range(300):
            n = rng.randint(2, 10)
            objs = [f"o{i}" for i in range(n)]
            roots = rng.sample(objs, rng.randint(1, min(2, n)))
            edges = {o: rng.sample([x for x in objs if x != o],
                                   rng.randint(0, min(2, n - 1)))
                     for o in objs}
            st = initial_state(objs, roots, edges)
            seq_id = 0

            def fresh():
                nonlocal seq_id
                seq_id += 1
                return f"t{trial}-e{seq_id}"

            for _ in range(60):
                if st.phase == Phase.IDLE:
                    choices = ["start", "mut_edge", "mut_root"]
                else:
                    choices = ["step", "step", "mut_edge", "mut_root", "sweep"]
                op = rng.choice(choices)
                if op == "start":
                    st, _ = apply(st, Event(fresh(), "start_mark", {}))
                elif op == "step":
                    try:
                        st, _ = apply(st, Event(fresh(), "step", {}))
                    except EngineError:
                        pass
                elif op == "sweep":
                    before = st
                    try:
                        st, result = apply(st, Event(fresh(), "sweep", {}))
                    except EngineError as e:
                        # 拒绝时状态必须原样不动
                        self.assertEqual(before, st)
                        self.assertIn(e.reason,
                                      {"sweep_without_mark", "sweep_not_ready"})
                    else:
                        # 核心安全裁决：所有当前可达对象都存活
                        self.assertTrue(reachable(st) <= set(st.alive))
                        # 回收的恰好是“白且不可达”
                        self.assertFalse(
                            any(o in reachable(before) for o in result["reclaimed"]))
                elif op == "mut_edge":
                    src = rng.choice(objs)
                    if src not in st.alive:
                        continue
                    cur = list(st.edges.get(src, frozenset()))
                    new_choice = rng.choice([None] + [o for o in objs
                                                      if o in st.alive and o not in cur])
                    old_choice = rng.choice(cur) if cur and rng.random() < 0.6 else None
                    if old_choice is None and new_choice is None:
                        continue
                    payload = {"src": src, "old_dst": old_choice, "new_dst": new_choice}
                    before = st
                    try:
                        st, _ = apply(st, Event(fresh(), "set_edge", payload))
                    except EngineError as e:
                        self.assertEqual(before, st)
                        self.assertIn(e.reason, {"reference_reclaimed", "edge_absent",
                                                 "edge_exists", "malformed_payload"})
                elif op == "mut_root":
                    obj = rng.choice(objs)
                    if obj not in st.alive:
                        continue
                    is_root = obj not in st.roots
                    before = st
                    try:
                        st, _ = apply(st, Event(
                            fresh(), "set_root",
                            {"object": obj, "is_root": is_root}))
                    except EngineError:
                        self.assertEqual(before, st)

                if st.phase != Phase.IDLE:
                    self.assertEqual(black_to_white_violation(st), [],
                                     f"trial {trial}: strong tri-color invariant broken")
                    # 灰队列恒为字典序，且与颜色一致
                    grays = sorted(o for o, c in st.colors.items() if c == Color.GRAY)
                    self.assertEqual(list(st.queue), grays)
                    if st.phase == Phase.READY:
                        # READY 时可达对象不得为白（否则会被错误清扫）
                        self.assertFalse(
                            any(st.colors[o] == Color.WHITE for o in reachable(st)))


if __name__ == "__main__":
    unittest.main()
