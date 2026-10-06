"""引擎语义测试：屏障、稳定灰队列、阶段守卫、清扫裁决。"""
from __future__ import annotations

import unittest

from app.engine import EngineError, apply, initial_state
from app.models import Color, Event, Phase


def ev(eid: str, etype: str, payload=None) -> Event:
    return Event(eid, etype, payload or {})


def run(events, objects=("R", "A", "B", "C", "G"), roots=("R",),
        edges=None):
    st = initial_state(list(objects), list(roots), edges or {"R": ["A"], "A": ["B"]})
    results = []
    for e in events:
        st, r = apply(st, e)
        results.append(r)
    return st, results


class BarrierTests(unittest.TestCase):
    def test_old_and_new_white_targets_retained(self):
        # A 仍为灰色（尚未扫描），旧目标 B、新目标 C 均为白色
        st, _ = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "set_edge",
                        {"src": "A", "old_dst": "B", "new_dst": "C"})])
        self.assertEqual(st.colors["B"], Color.GRAY)   # 删除屏障：旧目标
        self.assertEqual(st.colors["C"], Color.GRAY)   # 插入屏障：新目标
        self.assertEqual(st.queue, ("A", "B", "C"))    # 稳定排序

        st, _ = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "set_edge",
                        {"src": "A", "old_dst": "B", "new_dst": "C"}),
                     ev("e4", "step"), ev("e5", "step"), ev("e6", "step"),
                     ev("e7", "sweep")])
        # 清扫：B 虽已不可达但是屏障保留的灰对象，本周期不回收；仅 G 被回收
        self.assertIn("B", st.alive)
        self.assertIn("C", st.alive)
        self.assertNotIn("G", st.alive)

    def test_black_writer_returns_to_gray(self):
        # A 已扫描为黑后改写边：A 必须退回灰色重新扫描，新白目标染灰
        st, results = run([
            ev("e1", "start_mark"), ev("e2", "step"), ev("e3", "step"),
            ev("e4", "set_edge", {"src": "A", "old_dst": None, "new_dst": "C"})])
        self.assertEqual(st.colors["A"], Color.GRAY)
        self.assertIn("A", results[-1]["rescanned"])
        self.assertIn("C", results[-1]["barrier_retained"])

    def test_root_change_barrier(self):
        st, _ = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "set_root", {"object": "G", "is_root": True})])
        self.assertEqual(st.colors["G"], Color.GRAY)  # 新白色根染灰
        st2, _ = run([ev("e1", "start_mark"),
                      ev("e2", "set_root", {"object": "R", "is_root": False})])
        self.assertEqual(st2.colors["R"], Color.GRAY)  # 被移除的旧根本周期保留

    def test_mutations_between_cycles_have_no_barrier(self):
        st, r = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "step"), ev("e4", "step"), ev("e5", "sweep"),
                     ev("e6", "set_edge",
                        {"src": "A", "old_dst": "B", "new_dst": None})])
        self.assertEqual(st.phase, Phase.IDLE)
        self.assertEqual(st.colors["B"], Color.WHITE)
        self.assertEqual(r[-1]["barrier_retained"], [])


class MarkStepTests(unittest.TestCase):
    def test_one_stable_sorted_gray_per_step(self):
        st, r = run([ev("e1", "start_mark"),
                     ev("e2", "set_root", {"object": "G", "is_root": True}),
                     ev("e3", "step")],
                    edges={"R": ["A"]})
        self.assertEqual(r[-1]["scanned"], "G")   # 字典序 G < R
        self.assertEqual(st.queue, ("R",))

    def test_step_without_mark_rejected(self):
        st0 = initial_state(["A", "B"], ["A"], {})
        with self.assertRaises(EngineError) as cm:
            apply(st0, ev("x", "step"))
        self.assertEqual(cm.exception.reason, "step_without_mark")


class SweepGuardTests(unittest.TestCase):
    def test_sweep_without_mark(self):
        st0 = initial_state(["A", "B"], ["A"], {})
        with self.assertRaises(EngineError) as cm:
            apply(st0, ev("x", "sweep"))
        self.assertEqual(cm.exception.reason, "sweep_without_mark")

    def test_sweep_while_queue_nonempty_rejected(self):
        st, _ = run([ev("e1", "start_mark")])
        with self.assertRaises(EngineError) as cm:
            apply(st, ev("x", "sweep"))
        self.assertEqual(cm.exception.reason, "sweep_not_ready")
        self.assertEqual(cm.exception.detail["gray_queue"], ["R"])

    def test_only_unreachable_white_reclaimed(self):
        # 标记完成后只有白且不可达者被回收（此处 C 可达，仅 G 是垃圾）
        st, r = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "step"), ev("e4", "step"), ev("e5", "step"),
                     ev("e6", "sweep")],
                    edges={"R": ["A"], "A": ["B", "C"]})
        self.assertEqual(r[-1]["reclaimed"], ["G"])
        self.assertEqual(st.phase, Phase.IDLE)
        self.assertTrue(all(c == Color.WHITE for c in st.colors.values()))

    def test_reference_reclaimed_rejected(self):
        st, _ = run([ev("e1", "start_mark"), ev("e2", "step"),
                     ev("e3", "step"), ev("e4", "step"), ev("e5", "sweep")])
        with self.assertRaises(EngineError) as cm:
            apply(st, ev("x", "set_edge",
                         {"src": "A", "old_dst": None, "new_dst": "G"}))
        self.assertEqual(cm.exception.reason, "reference_reclaimed")

    def test_double_mark_rejected(self):
        st, _ = run([ev("e1", "start_mark")])
        with self.assertRaises(EngineError) as cm:
            apply(st, ev("e2", "start_mark"))
        self.assertEqual(cm.exception.reason, "mark_already_active")


if __name__ == "__main__":
    unittest.main()
