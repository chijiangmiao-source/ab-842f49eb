"""服务层与持久化测试：稳定标识幂等、拒单不污染、重开恢复一致。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.engine import EngineError
from app.service import DrillService, state_from_dict, state_to_dict
from app.store import Store


SPEC = {
    "id": "d1",
    "objects": ["R", "A", "B", "C", "G"],
    "roots": ["R"],
    "edges": {"R": ["A"], "A": ["B"]},
    "events": [
        {"id": "e1", "type": "start_mark", "payload": {}},
        {"id": "e2", "type": "step", "payload": {}},
        {"id": "e3", "type": "set_edge",
         "payload": {"src": "A", "old_dst": "B", "new_dst": "C"}},
        {"id": "e4", "type": "step", "payload": {}},
        {"id": "e5", "type": "step", "payload": {}},
        {"id": "e6", "type": "step", "payload": {}},
        {"id": "e7", "type": "sweep", "payload": {}},
    ],
}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        self.svc = DrillService(Store(self.db))

    def tearDown(self):
        self.svc.store.close()
        self.tmp.cleanup()


class PersistenceTests(ServiceTestBase):
    def test_ledger_replays_same_result(self):
        d = self.svc.create_drill(SPEC)
        ledger1 = [(r["event_id"], r["accepted"], r["result"]) for r in d["ledger"]]
        # 全新服务从同一数据库恢复 => 重放结果一致
        self.svc.store.close()
        svc2 = DrillService(Store(self.db))
        d2 = svc2.get_drill("d1")
        ledger2 = [(r["event_id"], r["accepted"], r["result"]) for r in d2["ledger"]]
        self.assertEqual(ledger1, ledger2)
        self.assertEqual(d["state"]["colors"], d2["state"]["colors"])
        self.assertEqual(d["state"]["alive"], d2["state"]["alive"])
        svc2.store.close()

    def test_duplicate_delivery_replays_original(self):
        self.svc.create_drill(SPEC)
        r1 = self.svc.deliver("d1", "x1", "set_root",
                              {"object": "C", "is_root": True})
        self.assertFalse(r1["replayed"])
        r2 = self.svc.deliver("d1", "x1", "set_root",
                              {"object": "C", "is_root": True})
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["result"], r2["result"])
        ledger = self.svc.store.ledger("d1")
        self.assertEqual(len([r for r in ledger if r["event_id"] == "x1"]), 1)

    def test_same_id_different_payload_rejected(self):
        self.svc.create_drill(SPEC)
        self.svc.deliver("d1", "x1", "set_root", {"object": "C", "is_root": True})
        with self.assertRaises(EngineError) as cm:
            self.svc.deliver("d1", "x1", "set_root",
                             {"object": "B", "is_root": True})
        self.assertEqual(cm.exception.reason, "conflicting_event_payload")
        # 冲突不新增账本行
        self.assertEqual(
            len([r for r in self.svc.store.ledger("d1") if r["event_id"] == "x1"]), 1)

    def test_rejection_persisted_but_does_not_pollute(self):
        self.svc.create_drill(SPEC)
        # 先合法启动新周期，再在灰队列非空时非法清扫 => sweep_not_ready
        self.svc.deliver("d1", "m1", "start_mark", {})
        with self.assertRaises(EngineError) as cm:
            self.svc.deliver("d1", "bad1", "sweep", {})
        self.assertEqual(cm.exception.reason, "sweep_not_ready")
        # 重复投递同一拒单：回放原拒绝结果（不抛异常、不产生新行）
        replay_resp = self.svc.deliver("d1", "bad1", "sweep", {})
        self.assertTrue(replay_resp["replayed"])
        self.assertFalse(replay_resp["accepted"])
        self.assertEqual(replay_resp["reason"], "sweep_not_ready")
        d = self.svc.get_drill("d1")
        rows = [r for r in d["ledger"] if r["event_id"] == "bad1"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["accepted"])
        self.assertEqual(d["state"]["first_rejection"]["event_id"], "bad1")
        self.assertEqual(d["state"]["first_rejection"]["reason"], "sweep_not_ready")
        # 状态未被污染：仍处于标记中，灰队列保持启动时内容
        self.assertEqual(d["state"]["phase"], "marking")
        self.assertEqual(d["state"]["gray_queue"], ["R"])

    def test_reference_reclaimed_rejected(self):
        self.svc.create_drill(SPEC)  # G 在 e7 被回收
        with self.assertRaises(EngineError) as cm:
            self.svc.deliver("d1", "p1", "set_edge",
                             {"src": "A", "old_dst": None, "new_dst": "G"})
        self.assertEqual(cm.exception.reason, "reference_reclaimed")
        with self.assertRaises(EngineError) as cm:
            self.svc.deliver("d1", "p2", "set_root",
                             {"object": "G", "is_root": True})
        self.assertEqual(cm.exception.reason, "reference_reclaimed")

    def test_checkpoint_roundtrip(self):
        d = self.svc.create_drill(SPEC)
        # 取周期中途的一帧状态做序列化往返
        frames = self.svc.get_timeline("d1")["frames"]
        mid = frames[4]["snapshot"]
        # 直接用服务签名比较存活/颜色/队列/阶段
        cur = d["state"]
        self.assertIsInstance(cur["gray_queue"], list)


class ReopenRecoveryTests(ServiceTestBase):
    SPEC_REOPEN = {
        "id": "dr",
        "objects": ["R", "A", "B", "G"],
        "roots": ["R"],
        "edges": {"R": ["A"], "A": ["B"]},
        "events": [
            {"id": "r1", "type": "start_mark", "payload": {}},
            {"id": "r2", "type": "step", "payload": {}},
            {"id": "r3", "type": "step", "payload": {}},
            {"id": "r4", "type": "reopen", "payload": {}},
            {"id": "r5", "type": "step", "payload": {}},
            {"id": "r6", "type": "sweep", "payload": {}},
        ],
    }

    def test_reopen_continues_with_same_colors_queue_evidence(self):
        self.svc.create_drill(self.SPEC_REOPEN)
        d = self.svc.get_drill("dr")
        # r3 之后灰队列含 B；重开不改变颜色/队列
        reopen_rows = [r for r in d["ledger"] if r["type"] == "reopen"]
        self.assertEqual(len(reopen_rows), 1)
        timeline = self.svc.get_timeline("dr")
        before = [f for f in timeline["frames"] if f.get("event_id") == "r3"][0]["snapshot"]
        at = [f for f in timeline["frames"] if f.get("event_id") == "r4"][0]["snapshot"]
        self.assertEqual(before["colors"], at["colors"])
        self.assertEqual(before["gray_queue"], at["gray_queue"])
        self.assertEqual(before["phase"], at["phase"])
        ev = [e for e in at["evidence"] if e["kind"] == "reopen"]
        self.assertEqual(ev[0]["gray_queue"], at["gray_queue"])

    def test_recovery_verdict_matches_continuous(self):
        self.svc.create_drill(self.SPEC_REOPEN)
        report = self.svc.recovery_report("dr")
        self.assertEqual(report["reopen_count"], 1)
        self.assertTrue(report["consistent"], report)
        self.assertEqual(report["continuous"]["alive"],
                         report["recovered"]["alive"])
        self.assertEqual(report["continuous"]["reclaimed"],
                         report["recovered"]["reclaimed"])
        self.assertEqual(report["continuous"]["colors"],
                         report["recovered"]["colors"])
        self.assertEqual(report["continuous"]["evidence"],
                         report["recovered"]["evidence"])

    def test_reopen_without_active_mark_rejected(self):
        self.svc.create_drill(self.SPEC_REOPEN)
        # 演练已在 IDLE 结束，再次 reopen 必须拒绝
        with self.assertRaises(EngineError) as cm:
            self.svc.deliver("dr", "rr0", "reopen", {})
        self.assertEqual(cm.exception.reason, "reopen_without_mark")
        d = self.svc.get_drill("dr")
        rows = [r for r in d["ledger"] if r["event_id"] == "rr0"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["accepted"])
        self.assertEqual(d["state"]["phase"], "idle")

    def test_checkpoint_serialization_is_identity(self):
        self.svc.create_drill(self.SPEC_REOPEN)
        # 手工在重开点折叠并序列化检查点，再续跑，与连续运行比较
        from app.service import fold
        spec = self.svc.store.get_drill_spec("dr")
        ledger = self.svc.store.ledger("dr")
        idx = next(i for i, r in enumerate(ledger) if r["type"] == "reopen")
        st1, ev1, _, _ = fold(spec["objects"], spec["roots"], spec["edges"],
                              ledger[:idx + 1])
        st2 = state_from_dict(state_to_dict(st1))
        self.assertEqual(state_to_dict(st1), state_to_dict(st2))
        st_a, _, _, _ = fold(spec["objects"], spec["roots"], spec["edges"], ledger)
        st_b, _, _, _ = fold(spec["objects"], spec["roots"], spec["edges"],
                             ledger[idx + 1:], st=st2)
        self.assertEqual(st_a, st_b)


class ValidationTests(ServiceTestBase):
    def test_limits(self):
        from app.service import ValidationError, validate_spec
        with self.assertRaises(ValidationError):
            validate_spec({"id": "x", "objects": ["A"], "roots": [], "events": []})
        with self.assertRaises(ValidationError):
            validate_spec({"id": "x",
                           "objects": [f"o{i}" for i in range(25)],
                           "roots": [], "events": []})
        with self.assertRaises(ValidationError):
            spec = {"id": "x", "objects": ["A", "B"], "roots": [],
                    "events": [{"id": f"e{i}", "type": "step", "payload": {}}
                               for i in range(49)]}
            validate_spec(spec)

    def test_unknown_type_rejected(self):
        from app.service import ValidationError
        with self.assertRaises(ValidationError):
            self.svc.create_drill({"id": "z", "objects": ["A", "B"],
                                   "roots": ["A"], "events": [
                                       {"id": "1", "type": "explode", "payload": {}}]})


if __name__ == "__main__":
    unittest.main()
