"""引擎与运行时的验收单元测试（纯标准库）：运行 python -m unittest -v"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import unittest

from app.engine import CollectorEngine, GRAY, BLACK, WHITE, MARK, SWEEP, IDLE, Reject
from app.runtime import Runtime


def run(engine, events):
    out = []
    for i, (eid, etype, *rest) in enumerate(events):
        payload = rest[0] if rest else {}
        out.append(engine.apply(eid, etype, payload))
    return out


def drain(engine, prefix="st"):
    results = []
    for i in range(100):
        r = engine.apply(f"{prefix}-{i}", "step", {})
        if r["status"] != "accepted":
            break
        results.append(r)
    return results


class BasicMarkSweepTests(unittest.TestCase):
    def setUp(self):
        # A->B->C 存活；D->E 中 D 为根存活；F 不可达
        spec = {
            "objects": ["A", "B", "C", "D", "E", "F"],
            "edges": {"A": ["B"], "B": ["C"], "D": ["E"]},
            "roots": ["A", "D"],
        }
        self.e = CollectorEngine(spec)

    def test_normal_cycle_reclaims_only_unreachable_white(self):
        self.e.apply("1", "start")
        drain(self.e)
        r = self.e.apply("2", "sweep")
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["reclaimed"], ["F"])
        self.assertEqual(r["live"], ["A", "B", "C", "D", "E"])
        self.assertTrue(self.e.safety_report()["cycle_safe"])
        self.assertEqual(self.e.state.freed, ["F"])

    def test_step_is_stable_sorted_and_single(self):
        self.e.apply("1", "start")  # 根 A,D 入灰，稳定排序后队首 A
        r = self.e.apply("2", "step")
        self.assertEqual(r["processed"], "A")
        r = self.e.apply("3", "step")  # 灰队列 [B,D] 排序 -> B
        self.assertEqual(r["processed"], "B")
        r = self.e.apply("4", "step")  # [C,D] -> C
        self.assertEqual(r["processed"], "C")
        r = self.e.apply("5", "step")  # [D] -> D
        self.assertEqual(r["processed"], "D")
        r = self.e.apply("6", "step")  # [E] -> E
        self.assertEqual(r["processed"], "E")
        self.assertTrue(r["marking_complete"])
        # 灰队列空后再 step 必须拒绝
        r = self.e.apply("7", "step")
        self.assertEqual(r["status"], "rejected")

    def test_each_step_processes_exactly_one_gray(self):
        self.e.apply("1", "start")
        before = list(self.e.state.gray)
        self.assertEqual(len(before), 2)
        self.e.apply("2", "step")
        # 一次只少一个（A 出队，其子 B 此前已灰；这里只验证净处理一个对象）
        processed_black = [o for o, c in self.e.state.colors.items() if c == BLACK]
        self.assertEqual(processed_black, ["A"])


class BarrierTests(unittest.TestCase):
    def test_old_target_retained_when_edge_removed_during_marking(self):
        # A->B->C；A 扫描后（B 仍灰、C 仍白）删掉 B->C：旧目标 C 必须保留
        e = CollectorEngine(
            {"objects": ["A", "B", "C", "G"], "edges": {"A": ["B"], "B": ["C"]}, "roots": ["A"]}
        )
        e.apply("1", "start")
        e.apply("2", "step")  # A 黑，B 灰，C 仍白
        r = e.apply("3", "mutate_edge", {"src": "B", "action": "set", "targets": []})
        self.assertIn("C", r["barrier_retained"])  # 旧目标 C 被屏障变灰
        self.assertEqual(e.state.colors["C"], GRAY)
        drain(e)
        sw = e.apply("9", "sweep")
        self.assertNotIn("C", sw["reclaimed"])       # C 存活
        self.assertEqual(sw["reclaimed"], ["G"])

    def test_new_white_target_retained(self):
        # A 扫描完成后新增 A->F（白色新目标），F 必须保留
        e = CollectorEngine(
            {"objects": ["A", "F", "G"], "edges": {"A": []}, "roots": ["A"]}
        )
        e.apply("1", "start")
        e.apply("2", "step")  # A 黑，队列空，标记完成
        r = e.apply("3", "mutate_edge", {"src": "A", "action": "add", "targets": ["F"]})
        self.assertIn("F", r["barrier_retained"])
        self.assertEqual(e.state.colors["F"], GRAY)
        e.apply("4", "step")  # F
        sw = e.apply("5", "sweep")
        self.assertIn("F", sw["live"])
        self.assertEqual(sw["reclaimed"], ["G"])

    def test_new_white_root_retained_and_old_root_set_semantics(self):
        e = CollectorEngine(
            {"objects": ["A", "B"], "edges": {}, "roots": ["A"]}
        )
        e.apply("1", "start")
        e.apply("2", "step")  # A 黑
        r = e.apply("3", "mutate_root", {"action": "set", "roots": ["B"]})
        # A 是被断开的旧根但已黑；B 为新接入白色根 -> 保留
        self.assertEqual(r["old_roots"], ["A"])
        self.assertEqual(r["added_roots"], ["B"])
        self.assertIn("B", r["barrier_retained"])
        drain(e)
        sw = e.apply("4", "sweep")
        self.assertEqual(sorted(sw["live"]), ["A", "B"])  # A 快照存活，B 新根存活

    def test_add_action_has_no_old_targets(self):
        e = CollectorEngine({"objects": ["A", "B"], "edges": {"A": ["B"]}, "roots": ["A"]})
        e.apply("1", "start")
        r = e.apply("2", "mutate_edge", {"src": "A", "action": "add", "targets": ["B"]})
        self.assertEqual(r["old_targets"], [])
        self.assertEqual(r["added_targets"], [])


class IllegalSweepTests(unittest.TestCase):
    def test_sweep_before_start_rejected_without_state_change(self):
        e = CollectorEngine({"objects": ["A", "B"], "edges": {}, "roots": ["A"]})
        r = e.apply("1", "sweep")
        self.assertEqual(r["status"], "rejected")
        self.assertIn("非法阶段清扫", r["reasons"][0])
        self.assertEqual(e.state.phase, IDLE)
        self.assertEqual(len(e.state.history), 0)  # 不进流水

    def test_sweep_with_nonempty_gray_queue_rejected(self):
        e = CollectorEngine({"objects": ["A", "B"], "edges": {"A": ["B"]}, "roots": ["A"]})
        e.apply("1", "start")
        r = e.apply("2", "sweep")
        self.assertEqual(r["status"], "rejected")
        self.assertIn("灰队列未清空", r["reasons"][0])
        self.assertEqual(e.state.phase, MARK)
        self.assertEqual(e.state.gray, ["A"])
        # 拒绝后可正常继续
        self.assertEqual(e.apply("3", "step")["status"], "accepted")

    def test_sweep_again_after_sweep_is_allowed_idempotent_verdict(self):
        e = CollectorEngine({"objects": ["A", "G"], "edges": {}, "roots": ["A"]})
        e.apply("1", "start")
        e.apply("2", "step")
        r1 = e.apply("3", "sweep")
        r2 = e.apply("4", "sweep")
        self.assertEqual(r1["reclaimed"], r2["reclaimed"])

    def test_second_cycle_does_not_re_reclaim_freed_objects(self):
        # 第一轮回收 G；第二轮把 B 也变为不可达并回收 B；G 不得重复出现在名单
        e = CollectorEngine(
            {"objects": ["A", "B", "G"], "edges": {"A": ["B"]}, "roots": ["A"]}
        )
        e.apply("1", "start")
        drain(e, "d1")
        sw1 = e.apply("2", "sweep")
        self.assertEqual(sw1["reclaimed"], ["G"])
        # 第二轮开始前断开 A->B
        e.apply("3", "mutate_edge", {"src": "A", "action": "set", "targets": []})
        e.apply("4", "start")
        drain(e, "d2")
        sw2 = e.apply("5", "sweep")
        self.assertEqual(sw2["reclaimed"], ["B"])  # G 不重复回收
        self.assertEqual(e.state.freed, ["G", "B"])
        # 再引用任一已回收对象都拒绝
        r = e.apply("6", "mutate_edge", {"src": "A", "action": "add", "targets": ["G"]})
        self.assertEqual(r["status"], "rejected")


class IdempotencyAndRejectTests(unittest.TestCase):
    def test_duplicate_delivery_replays_original(self):
        e = CollectorEngine({"objects": ["A", "G"], "edges": {}, "roots": ["A"]})
        r1 = e.apply("x1", "start")
        r2 = e.apply("x1", "start")
        self.assertEqual(r1["status"], "accepted")
        self.assertEqual(r2["status"], "duplicate")
        self.assertEqual(r2["original"]["cycle"], 1)
        # 重复不产生新的已接受流水
        self.assertEqual(len([h for h in e.state.history]), 1)

    def test_same_id_different_payload_rejected(self):
        e = CollectorEngine({"objects": ["A", "B"], "edges": {}, "roots": ["A"]})
        self.assertEqual(e.apply("x1", "start")["status"], "accepted")
        r = e.apply("x1", "step", {"extra": 1})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("载荷不同", r["reasons"][0])
        # 原结果仍可回放
        self.assertEqual(e.apply("x1", "start")["status"], "duplicate")

    def test_reference_to_reclaimed_object_rejected(self):
        e = CollectorEngine(
            {"objects": ["A", "B", "C"], "edges": {"A": ["B"]}, "roots": ["A"]}
        )
        e.apply("1", "start")
        drain(e)
        sw = e.apply("2", "sweep")
        self.assertEqual(sw["reclaimed"], ["C"])
        r = e.apply("3", "mutate_edge", {"src": "A", "action": "add", "targets": ["C"]})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("已回收", r["reasons"][0])
        # 拒绝未污染：A 的出边仍是 [B]，且事件可换 ID 重试合法操作
        self.assertEqual(e.state.edges["A"], ["B"])
        ok = e.apply("4", "mutate_edge", {"src": "A", "action": "remove", "targets": ["B"]})
        self.assertEqual(ok["status"], "accepted")

    def test_rejected_event_id_can_be_retried_then_accepted(self):
        # 拒绝路径不登记稳定标识 => 同一 ID 修正载荷后可接受
        e = CollectorEngine({"objects": ["A", "G"], "edges": {}, "roots": ["A"]})
        bad = e.apply("1", "sweep")  # 非法阶段
        self.assertEqual(bad["status"], "rejected")
        good = e.apply("1", "start")
        self.assertEqual(good["status"], "accepted")


class ReopenRecoveryTests(unittest.TestCase):
    SPEC = {
        "objects": ["A", "B", "C", "D", "E"],
        "edges": {"A": ["B"], "B": ["C"]},
        "roots": ["A"],
    }

    def _continuous(self):
        e = CollectorEngine(self.SPEC)
        seq = [
            ("1", "start"), ("2", "step"),
            ("3", "mutate_edge", {"src": "A", "action": "set", "targets": ["D"]}),
            ("4", "step"), ("5", "step"), ("6", "step"),
            ("7", "sweep"),
        ]
        run(e, seq)
        return e

    def _interrupted(self):
        e = CollectorEngine(self.SPEC)
        seq = [
            ("1", "start"), ("2", "step"),
            ("3", "mutate_edge", {"src": "A", "action": "set", "targets": ["D"]}),
            ("4", "step"),
            ("5", "reopen"),  # 标记中途重开：颜色/队列/证据不变
            ("6", "step"), ("7", "step"),
            ("8", "sweep"),
        ]
        run(e, seq)
        return e

    def test_reopen_keeps_color_queue_and_evidence(self):
        e = CollectorEngine(self.SPEC)
        run(e, [("1", "start"), ("2", "step"),
                ("3", "mutate_edge", {"src": "A", "action": "set", "targets": ["D"]}),
                ("4", "step")])
        colors_before = dict(e.state.colors)
        queue_before = list(e.state.gray)
        barriers_before = list(e.state.barrier_log)
        r = e.apply("5", "reopen")
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(e.state.colors, colors_before)
        self.assertEqual(e.state.gray, queue_before)
        # 屏障证据在原证据基础上仅追加恢复记录
        self.assertEqual(e.state.barrier_log[:len(barriers_before)], barriers_before)
        self.assertEqual(e.state.barrier_log[-1]["reason"], "reopen-resume")

    def test_reopen_when_queue_empty_rejected(self):
        e = CollectorEngine(self.SPEC)
        e.apply("1", "start")
        drain(e)
        r = e.apply("2", "reopen")
        self.assertEqual(r["status"], "rejected")

    def test_interrupted_verdict_equals_continuous(self):
        c = self._continuous()
        i = self._interrupted()
        self.assertEqual(c.state.live, i.state.live)
        self.assertEqual(c.state.reclaimed, i.state.reclaimed)
        self.assertEqual(c.state.reclaimed, ["E"])
        self.assertEqual(c.state.live, ["A", "B", "C", "D"])

    def test_reopen_after_sweep_phase_rejected(self):
        e = CollectorEngine(self.SPEC)
        e.apply("1", "start")
        drain(e)
        e.apply("2", "sweep")
        r = e.apply("3", "reopen")
        self.assertEqual(r["status"], "rejected")


class SpecValidationTests(unittest.TestCase):
    def test_bounds(self):
        with self.assertRaises(Reject):
            CollectorEngine({"objects": ["A"], "roots": []})  # 少于 2
        with self.assertRaises(Reject):
            CollectorEngine({"objects": [f"o{i}" for i in range(25)], "roots": []})
        with self.assertRaises(Reject):
            CollectorEngine({"objects": ["A", "A"], "roots": []})
        with self.assertRaises(Reject):
            CollectorEngine({"objects": ["A", "B"], "roots": ["X"]})
        with self.assertRaises(Reject):
            CollectorEngine({"objects": ["A", "B"], "edges": {"A": ["X"]}, "roots": []})

    def test_event_cap_48(self):
        e = CollectorEngine({"objects": ["A", "G"], "edges": {}, "roots": ["A"]})
        e.apply("c0", "start")
        e.apply("c1", "step")
        e.apply("c2", "sweep")
        # 多轮 start/step/sweep 直到第 48 个事件后拒绝
        n = 3
        while len(e.state.history) < 48:
            e.apply(f"c{n}", "start"); n += 1
            if len(e.state.history) >= 48:
                break
            e.apply(f"c{n}", "step"); n += 1
            if len(e.state.history) >= 48:
                break
            e.apply(f"c{n}", "sweep"); n += 1
        r = e.apply("overflow", "step")
        self.assertEqual(r["status"], "rejected")
        self.assertIn("48", r["reasons"][0])


class RuntimePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _spec(self):
        return {"objects": ["A", "B", "C", "G"], "edges": {"A": ["B"], "B": ["C"]}, "roots": ["A"]}

    def test_replay_after_recovery_matches(self):
        rt = Runtime(self.dir)
        rt.create_drill("d1", self._spec())
        rt.dispatch("d1", {"id": "1", "type": "start"})
        rt.dispatch("d1", {"id": "2", "type": "step"})
        rt.dispatch("d1", {"id": "3", "type": "mutate_edge",
                           "payload": {"src": "B", "action": "set", "targets": []}})
        rt.dispatch("d1", {"id": "4", "type": "reopen"})
        rt.dispatch("d1", {"id": "5", "type": "step"})
        rt.dispatch("d1", {"id": "6", "type": "step"})
        rt.dispatch("d1", {"id": "7", "type": "sweep"})
        eng = rt.get_engine("d1")
        live, reclaimed = list(eng.state.live), list(eng.state.reclaimed)

        # 模拟重启：新 Runtime 从持久化日志恢复
        rt2 = Runtime(self.dir)
        eng2 = rt2.get_engine("d1")
        self.assertEqual(eng2.state.live, live)
        self.assertEqual(eng2.state.reclaimed, reclaimed)
        self.assertEqual(eng2.state.colors, eng.state.colors)
        self.assertEqual(eng2.state.barrier_log, eng.state.barrier_log)
        # 旧目标 C 在 B->C 删除后仍被屏障保留；G 被回收
        self.assertEqual(reclaimed, ["G"])
        self.assertIn("C", live)

    def test_rejections_never_persisted_into_event_log(self):
        rt = Runtime(self.dir)
        rt.create_drill("d2", self._spec())
        rt.dispatch("d2", {"id": "bad1", "type": "sweep"})  # 非法阶段
        rt.dispatch("d2", {"id": "bad2", "type": "mutate_edge",
                           "payload": {"src": "A", "action": "add", "targets": ["ZZ"]}})
        rt.dispatch("d2", {"id": "ok1", "type": "start"})
        rt.dispatch("d2", {"id": "ok1", "type": "start"})  # 重复
        with open(os.path.join(self.dir, "d2", "events.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([x["id"] for x in lines], ["ok1"])
        # 拒绝审计独立存在
        audit = rt.store.load_rejections("d2")
        self.assertEqual({x["id"] for x in audit}, {"bad1", "bad2"})
        # 恢复后内存拒绝流水为空（审计不污染恢复状态）
        rt2 = Runtime(self.dir)
        eng = rt2.get_engine("d2")
        self.assertEqual(eng.state.rejected, [])
        self.assertEqual(eng.state.phase, MARK)

    def test_duplicate_over_http_runtime_replays(self):
        rt = Runtime(self.dir)
        rt.create_drill("d3", self._spec())
        r1 = rt.dispatch("d3", {"id": "z", "type": "start"})
        r2 = rt.dispatch("d3", {"id": "z", "type": "start"})
        self.assertEqual(r1["status"], "accepted")
        self.assertEqual(r2["status"], "duplicate")

    def test_recover_endpoint_rebuilds_mid_cycle(self):
        rt = Runtime(self.dir)
        rt.create_drill("d4", self._spec())
        rt.dispatch("d4", {"id": "1", "type": "start"})
        rt.dispatch("d4", {"id": "2", "type": "step"})
        colors = dict(rt.get_engine("d4").state.colors)
        rebuilt = rt.recover("d4")
        self.assertEqual(rebuilt.state.colors, colors)
        self.assertEqual(rebuilt.state.gray, ["B"])


class RandomizedSafetyTests(unittest.TestCase):
    """对随机图与标记期改写做性质测试：回收集 ∩ 快照可达集 恒为空。"""

    def test_random_cycles_never_free_snapshot_reachable(self):
        rng = random.Random(20261006)
        for trial in range(60):
            n = rng.randint(2, 12)
            objs = [f"o{i}" for i in range(n)]
            edges = {}
            for s in objs:
                edges[s] = [t for t in objs if rng.random() < 0.18 and t != s]
            roots = rng.sample(objs, k=rng.randint(1, max(1, n // 3)))
            e = CollectorEngine({"objects": objs, "edges": edges, "roots": roots})

            for cycle in range(2):
                r = e.apply(f"t{trial}-c{cycle}-start", "start")
                self.assertEqual(r["status"], "accepted")
                # 标记中途随机改写边/根；每个循环无论改写是否被拒都尝试一次步进
                guard = 0
                while e.state.gray and guard < 80:
                    guard += 1
                    if rng.random() < 0.5:
                        src = rng.choice(objs)
                        action = rng.choice(["set", "add", "remove"])
                        targets = rng.sample(objs, k=rng.randint(0, 2))
                        e.apply(
                            f"t{trial}-c{cycle}-m{guard}",
                            "mutate_edge",
                            {"src": src, "action": action, "targets": targets},
                        )  # 引用已回收等拒绝不影响状态，忽略
                    if e.state.gray:
                        sr = e.apply(f"t{trial}-c{cycle}-s{guard}", "step")
                        self.assertEqual(sr["status"], "accepted")
                        self.assertIsNotNone(sr.get("processed"))
                self.assertEqual(e.state.gray, [])
                sw = e.apply(f"t{trial}-c{cycle}-sweep", "sweep")
                self.assertEqual(sw["status"], "accepted")
                report = e.safety_report()
                self.assertTrue(report["cycle_safe"],
                                f"trial {trial} cycle {cycle}: {report['violations']}")
                self.assertEqual(report["violations"], [])
                # 下一轮开始前允许任意普通改写
                if cycle == 0:
                    e.apply(f"t{trial}-mut", "mutate_root",
                            {"action": "set", "roots": rng.sample(objs, k=1)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
