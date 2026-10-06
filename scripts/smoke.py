#!/usr/bin/env python3
"""API/HTTP 冒烟验收：实际观察三类业务结果并断言裁决。

用法：
    BASE_URL=http://web:8080 python3 scripts/smoke.py
    python3 scripts/smoke.py            # 自举本地服务（临时数据库）
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL  {name}  {detail}")


def request(method: str, url: str, body=None, timeout: int = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_healthy(base: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        try:
            status, body = request("GET", f"{base}/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> int:
    base = os.environ.get("BASE_URL", "").rstrip("/")
    proc = None
    if not base:
        tmp = tempfile.mkdtemp(prefix="gc-smoke-")
        env = dict(os.environ, DB_PATH=f"{tmp}/smoke.db", PORT="8099", HOST="127.0.0.1")
        proc = subprocess.Popen([sys.executable, "-m", "app.server"], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=os.setsid)
        base = "http://127.0.0.1:8099"
    try:
        print("== 0. 健康检查与页面 ==")
        ok = wait_healthy(base)
        check("/health 返回 ok", ok)
        status, body = request("GET", f"{base}/api/drills")
        ids = {d["id"] for d in body.get("drills", [])}
        check("三个内置演练已就绪",
              {"demo-barrier", "demo-illegal-sweep", "demo-reopen"} <= ids, str(ids))
        page = urllib.request.urlopen(f"{base}/").read().decode()
        check("页面可访问且含演练台标题", "缓存回收演练台" in page)
        for asset in ("/app.js", "/styles.css"):
            check(f"静态资源 {asset} 200",
                  urllib.request.urlopen(f"{base}{asset}").status == 200)

        print("== 1. 屏障保留：旧目标与新接入白色目标均不释放 ==")
        _, tl = request("GET", f"{base}/api/drills/demo-barrier/timeline")
        by_id = {f.get("event_id"): f for f in tl["frames"]}
        s3 = by_id["e3"]["snapshot"]
        check("改写时旧目标 B 染灰（删除屏障）", s3["colors"].get("B") == "gray")
        check("改写时新目标 C 染灰（插入屏障）", s3["colors"].get("C") == "gray")
        check("屏障保留明细 [B,C]",
              by_id["e3"]["result"]["barrier_retained"] == ["B", "C"],
              str(by_id["e3"]["result"]))
        s7 = by_id["e7"]["snapshot"]
        check("本周期清扫仅回收真垃圾 G", by_id["e7"]["result"]["reclaimed"] == ["G"])
        check("屏障保留的 B 本周期仍存活", "B" in s7["alive"])
        s12 = by_id["e12"]["snapshot"]
        check("下一周期 B 作为不可达白对象回收", "B" not in s12["alive"])

        print("== 2. 非法阶段清扫 / 引用回收对象拒绝且不污染 ==")
        _, d = request("GET", f"{base}/api/drills/demo-illegal-sweep")
        reasons = {r["event_id"]: (r["accepted"], r["reason"]) for r in d["ledger"]}
        check("未启动清扫 => sweep_without_mark",
              reasons["s0"] == (False, "sweep_without_mark"))
        check("灰队列非空清扫 => sweep_not_ready",
              reasons["s2"] == (False, "sweep_not_ready")
              and reasons["s4"] == (False, "sweep_not_ready"))
        check("合法清扫（队列空）通过", reasons["s6"][0] is True)
        check("引用已回收对象 G => reference_reclaimed",
              reasons["s7"] == (False, "reference_reclaimed"))
        fr = d["state"]["first_rejection"]
        check("首个拒因可追溯到 s0", fr and fr["event_id"] == "s0"
              and fr["reason"] == "sweep_without_mark")
        check("拒绝未污染最终状态", d["state"]["reclaimed"] == ["G"]
              and d["state"]["phase"] == "idle")

        print("== 3. 稳定标识：重复回放 / 同 ID 不同载荷拒绝 ==")
        # 使用专用演练（幂等创建），节末把周期跑完回到 idle，保证可对持久库重复执行
        replay_spec = {"id": "smoke-replay", "objects": ["A", "B", "G"],
                       "roots": ["A"], "edges": {"A": ["B"]}, "events": []}
        status, r = request("POST", f"{base}/api/drills", replay_spec)
        if status == 400 and r.get("reason") == "drill_exists":
            status = 200
        check("幂等演练就绪（新建或已存在）", status in (200, 201))
        target = f"{base}/api/drills/smoke-replay/events"
        # 每次运行使用唯一事件 ID 开启全新周期（周期自身结束于 idle），
        # 因此可对同一持久库重复执行；“重复投递”在本次运行内用同一 ID 验证。
        run = f"smk-{int(time.time()*1000)}"
        id_start, id_bad = f"{run}-start", f"{run}-bad"
        id_s1, id_s2, id_ok = f"{run}-s1", f"{run}-s2", f"{run}-ok"

        status, r = request("POST", target,
                            {"id": id_start, "type": "start_mark", "payload": {}})
        check("启动新周期被接受（或幂等回放原结果）",
              r.get("accepted") is True and status in (200, 201))
        _, before = request("GET", f"{base}/api/drills/smoke-replay")
        q_before = before["state"]["gray_queue"]
        status, r = request("POST", target,
                            {"id": id_bad, "type": "sweep", "payload": {}})
        check("标记中途清扫被拒 sweep_not_ready（首次 409 / 重复回放 200）",
              r.get("accepted") is False and r.get("reason") == "sweep_not_ready"
              and (status == 409 and not r.get("replayed")
                   or status == 200 and r.get("replayed") is True),
              f"status={status} {r}")
        status, r = request("POST", target,
                            {"id": id_bad, "type": "sweep", "payload": {}})
        check("同 ID 重复投递回放原拒绝（replayed=true）",
              status == 200 and r["replayed"] is True and r["accepted"] is False
              and r["reason"] == "sweep_not_ready")
        status, r = request("POST", target,
                            {"id": id_bad, "type": "sweep", "payload": {"x": 1}})
        check("同 ID 不同载荷 => 409 conflicting_event_payload",
              status == 409 and r["reason"] == "conflicting_event_payload")
        _, after = request("GET", f"{base}/api/drills/smoke-replay")
        check("拒单未污染灰队列与阶段",
              after["state"]["gray_queue"] == q_before
              and after["state"]["phase"] == "marking")
        # 完成该周期：step x2 后合法清扫，回收 G 后回到 idle
        for sid in (id_s1, id_s2):
            request("POST", target, {"id": sid, "type": "step", "payload": {}})
        status, r = request("POST", target,
                            {"id": id_ok, "type": "sweep", "payload": {}})
        # 首次运行回收唯一垃圾 G；持久库上重复运行时 G 已不存在 => reclaimed 为 []
        check("灰队列清空后合法清扫（或回放原结果），回收集 ⊆ {G}",
              r.get("accepted") is True
              and (r.get("result") or {}).get("reclaimed") in (["G"], []),
              str(r))
        _, final = request("GET", f"{base}/api/drills/smoke-replay")
        check("周期结束回到 idle 且本次拒单仍留痕为 rejected",
              final["state"]["phase"] == "idle"
              and any(row["event_id"] == id_bad and row["accepted"] is False
                      for row in final["ledger"]))

        print("== 4. 标记中途重开：恢复裁决与连续运行一致 ==")
        _, rep = request("GET", f"{base}/api/drills/demo-reopen/recovery")
        check("存在 1 次重开", rep["reopen_count"] == 1)
        check("检查点保留相同颜色与灰队列",
              rep["checkpoints"][0]["gray_queue"] == ["B"]
              and rep["checkpoints"][0]["phase"] == "marking")
        check("恢复与连续运行完全一致（颜色/队列/存活/证据）",
              rep["consistent"] is True
              and rep["continuous"]["alive"] == rep["recovered"]["alive"]
              and rep["continuous"]["colors"] == rep["recovered"]["colors"]
              and rep["continuous"]["evidence"] == rep["recovered"]["evidence"],
              json.dumps(rep, ensure_ascii=False))
        check("存活裁决 R/A/B 存活、G 回收",
              rep["recovered"]["alive"] == ["A", "B", "R"]
              and rep["recovered"]["reclaimed"] == ["G"])

        print("== 5. 自定义演练导入（2 对象）与非法导入拒绝 ==")
        spec = {"id": "smoke-custom", "objects": ["A", "B"], "roots": ["A"],
                "edges": {"A": ["B"]}, "events": [
                    {"id": "c1", "type": "start_mark", "payload": {}},
                    {"id": "c2", "type": "step", "payload": {}},
                    {"id": "c3", "type": "step", "payload": {}},
                    {"id": "c4", "type": "sweep", "payload": {}}]}
        status, r = request("POST", f"{base}/api/drills", spec)
        if status == 400 and r.get("reason") == "drill_exists":
            # 幂等重跑：已存在则拉取既有结果
            status, r = 200, request("GET", f"{base}/api/drills/smoke-custom")[1]
        check("合法演练导入成功（或幂等命中既有）",
              status in (200, 201) and r["id"] == "smoke-custom", f"status={status}")
        check("全部可达，无回收", r["state"]["reclaimed"] == [])
        bad = {"id": "smoke-bad", "objects": ["A"], "roots": [], "events": []}
        status, r = request("POST", f"{base}/api/drills", bad)
        check("1 对象演练拒绝 400 bad_objects",
              status == 400 and r["reason"] == "bad_objects", str(r))
        too_many = {"id": "smoke-25", "objects": [f"o{i}" for i in range(25)],
                    "roots": [], "events": []}
        status, r = request("POST", f"{base}/api/drills", too_many)
        check("25 对象演练拒绝", status == 400 and r["reason"] == "bad_objects")

        print(f"\n结果：{PASS_COUNT} 通过，{FAIL_COUNT} 失败")
        return 1 if FAIL_COUNT else 0
    finally:
        if proc is not None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main())
