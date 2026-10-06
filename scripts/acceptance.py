#!/usr/bin/env python3
"""验收服务 verify：代码测试 + 构建检查 + API/HTTP 冒烟 + 三类业务结果实测。

用法：
    python3 scripts/acceptance.py                # 自动拉起临时服务
    python3 scripts/acceptance.py --base URL     # 对已运行服务做 HTTP 验收

退出码：0 全部通过；非 0 存在失败。
环境：HOST(默认0.0.0.0) PORT(默认8080) DATA_DIR(默认./data_acceptance)
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""), flush=True)


def check(cond: bool, name: str, detail: str = "") -> bool:
    record(name, bool(cond), detail)
    return bool(cond)


# --------------------------------------------------------------------- HTTP
def request(base: str, method: str, path: str, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_healthy(base: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, body = request(base, "GET", "/health")
            if code == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ----------------------------------------------------------------- checks
def build_check() -> None:
    files = list((ROOT / "app").glob("*.py")) + list((ROOT / "scripts").glob("*.py"))
    bad = []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{f.name}: {e}")
    check(not bad, "构建检查 py_compile", "; ".join(bad) or f"{len(files)} 个文件全部编译通过")


def unit_tests() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT, capture_output=True, text=True,
    )
    ok = proc.returncode == 0
    tail = (proc.stderr.strip().splitlines() or [""])[-1]
    check(ok, "代码测试 unittest", tail)


def http_smoke(base: str) -> None:
    code, body = request(base, "GET", "/health")
    check(code == 200 and body.get("status") == "ok", "健康响应 GET /health", json.dumps(body, ensure_ascii=False))

    with urllib.request.urlopen(base + "/?t=" + str(time.time_ns()), timeout=10) as resp:
        page = resp.read().decode("utf-8")
        code = resp.status
    check(code == 200, "页面 GET / 返回 200")
    check("三色标记" in page and "屏障保留" in page and "非法清扫" in page,
          "页面含颜色/屏障/非法清扫等验收要素")


def scenario_barrier(base: str, did: str) -> None:
    spec = {"objects": ["A", "B", "C", "D", "E", "F", "G", "H"],
            "edges": {"A": ["B"], "B": ["C"], "D": ["E"]}, "roots": ["A", "D"]}
    code, _ = request(base, "POST", "/api/drills", {"id": did, "spec": spec})
    check(code == 201, f"[{did}] 创建演练")

    def ev(eid, etype, payload=None):
        return request(base, "POST", f"/api/drills/{did}/events",
                       {"id": eid, "type": etype, "payload": payload or {}})

    ev("m1", "start")
    ev("m2", "step")
    _, r3 = ev("m3", "mutate_edge", {"src": "A", "action": "set", "targets": ["F"]})
    _, r4 = ev("m4", "mutate_root", {"action": "add", "roots": ["G"]})
    ok_old = "B" in r3.get("barrier_retained", [])
    ok_new = "F" in r3.get("barrier_retained", [])
    ok_root = "G" in r4.get("barrier_retained", [])
    check(ok_old and ok_new, f"[{did}] 业务结果① 改边屏障同时保留旧目标 B 与新白色目标 F",
          json.dumps(r3.get("barrier_retained"), ensure_ascii=False))
    check(ok_root, f"[{did}] 业务结果① 新接入白色根 G 屏障保留",
          json.dumps(r4.get("barrier_retained"), ensure_ascii=False))
    for i in range(20):
        code, r = ev(f"ms{i}", "step")
        if r.get("status") != "accepted":
            break
    _, sw = ev("m9", "sweep")
    check(sw.get("reclaimed") == ["H"], f"[{did}] 清扫仅回收不可达白色对象 H",
          f"reclaimed={sw.get('reclaimed')} live={sw.get('live')}")
    check(sorted(sw.get("live", [])) == ["A", "B", "C", "D", "E", "F", "G"],
          f"[{did}] 屏障保留对象 B/C/F/G 全部存活", json.dumps(sw.get("live"), ensure_ascii=False))
    code, view = request(base, "GET", f"/api/drills/{did}")
    check(view["safety"]["cycle_safe"] is True and view["safety"]["violations"] == [],
          f"[{did}] 审查员安全报告：周期安全、零违规")


def scenario_illegal(base: str, did: str) -> None:
    spec = {"objects": ["A", "B", "C"], "edges": {"A": ["B"]}, "roots": ["A"]}
    code, _ = request(base, "POST", "/api/drills", {"id": did, "spec": spec})
    check(code == 201, f"[{did}] 创建演练")

    def ev(eid, etype, payload=None):
        return request(base, "POST", f"/api/drills/{did}/events",
                       {"id": eid, "type": etype, "payload": payload or {}})

    _, r0 = ev("x0", "sweep")
    _, r1 = ev("x1", "start")
    _, r2 = ev("x2", "sweep")
    ev("x3", "step")
    _, r3dup = ev("x3", "step", {"extra": 1})
    ev("x4", "step")
    _, sw = ev("x5", "sweep")
    _, r6 = ev("x6", "mutate_edge", {"src": "A", "action": "add", "targets": ["C"]})
    check(r0["status"] == "rejected" and "非法阶段清扫" in r0["reasons"][0],
          f"[{did}] 业务结果② 未启动清扫被拒绝", r0["reasons"][0])
    check(r2["status"] == "rejected" and "灰队列未清空" in r2["reasons"][0],
          f"[{did}] 业务结果② 灰队列未清空清扫被拒绝", r2["reasons"][0])
    check(r3dup["status"] == "rejected" and "载荷不同" in r3dup["reasons"][0],
          f"[{did}] 同标识不同载荷被拒绝", r3dup["reasons"][0])
    check(sw["reclaimed"] == ["C"], f"[{did}] C 被合法回收", json.dumps(sw["reclaimed"]))
    check(r6["status"] == "rejected" and "已回收" in r6["reasons"][0],
          f"[{did}] 引用已回收对象被拒绝", r6["reasons"][0])

    # 拒绝不污染恢复状态：重建后历史中只有被接受事件，阶段仍正确
    code, rec = request(base, "POST", f"/api/drills/{did}/recover", {})
    ids = [e["id"] for e in rec["events"]]
    check(code == 200 and "x0" not in ids and "x2" not in ids and "x6" not in ids
          and set(ids) == {"x1", "x3", "x4", "x5"},
          f"[{did}] 恢复日志不含任何被拒事件", json.dumps(ids))
    check(rec["state"]["phase"] == "sweep" and rec["state"]["edges"]["A"] == ["B"],
          f"[{did}] 恢复后状态未污染（phase=sweep，A->B 保持）")


def scenario_recovery(base: str, tag: str) -> None:
    spec = {"objects": ["A", "B", "C", "D", "E"], "edges": {"A": ["B"], "B": ["C"]}, "roots": ["A"]}

    def run_drill(did, seq, recover_after=None):
        request(base, "POST", "/api/drills", {"id": did, "spec": spec})
        done = 0
        for k, (i, t, *p) in enumerate(seq):
            request(base, "POST", f"/api/drills/{did}/events",
                    {"id": i, "type": t, "payload": p[0] if p else {}})
            done = k + 1
            if recover_after is not None and done == recover_after:
                code, view = request(base, "POST", f"/api/drills/{did}/recover", {})
                mid = view["state"]
        code, view = request(base, "GET", f"/api/drills/{did}")
        return view, mid if recover_after is not None else None

    seq = [("1", "start"), ("2", "step"),
           ("3", "mutate_edge", {"src": "A", "action": "set", "targets": ["D"]}),
           ("4", "step"), ("5", "step"), ("6", "step"), ("7", "sweep")]
    cont, _ = run_drill(f"{tag}-continuous", seq)

    seq_i = [("1", "start"), ("2", "step"),
             ("3", "mutate_edge", {"src": "A", "action": "set", "targets": ["D"]}),
             ("4", "step"), ("5", "reopen")]
    inter, mid = run_drill(f"{tag}-interrupted", seq_i, recover_after=5)
    # 恢复后继续投递剩余事件（新进程语义下事件 ID 仍唯一）
    for i, t, *p in [("6", "step"), ("7", "step"), ("8", "sweep")]:
        request(base, "POST", f"/api/drills/{tag}-interrupted/events",
                {"id": i, "type": t, "payload": p[0] if p else {}})
    code, inter = request(base, "GET", f"/api/drills/{tag}-interrupted")

    same = (cont["state"]["live"] == inter["state"]["live"]
            and cont["state"]["reclaimed"] == inter["state"]["reclaimed"])
    check(mid is not None and mid["phase"] == "mark" and set(mid["gray_queue"]) == {"C", "D"},
          f"[{tag}] 业务结果③ 标记中途重开/重建后颜色与灰队列延续",
          f"gray={mid['gray_queue'] if mid else None}")
    check(same and inter["state"]["reclaimed"] == ["E"],
          f"[{tag}] 业务结果③ 中断恢复与连续运行存活裁决一致",
          f"continuous live={cont['state']['live']} reclaimed={cont['state']['reclaimed']}; "
          f"interrupted live={inter['state']['live']} reclaimed={inter['state']['reclaimed']}")
    check(inter["safety"]["cycle_safe"] is True,
          f"[{tag}] 中断恢复后审查员安全报告为安全")


def scenario_idempotency(base: str, did: str) -> None:
    spec = {"objects": ["A", "G"], "edges": {}, "roots": ["A"]}
    request(base, "POST", "/api/drills", {"id": did, "spec": spec})

    def ev(eid, etype, payload=None):
        return request(base, "POST", f"/api/drills/{did}/events",
                       {"id": eid, "type": etype, "payload": payload or {}})

    _, r1 = ev("d1", "start")
    _, r2 = ev("d1", "start")  # 重复投递
    check(r1["status"] == "accepted" and r2["status"] == "duplicate"
          and r2["original"]["cycle"] == 1,
          f"[{did}] 重复投递只回放原结果（duplicate）")
    code, view = request(base, "GET", f"/api/drills/{did}")
    check(len(view["events"]) == 1, f"[{did}] 重复投递不产生第二条持久化事件")


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="对已运行的服务做验收（不自行启动）")
    args = ap.parse_args()

    build_check()
    unit_tests()

    spawned = None
    if args.base:
        base = args.base.rstrip("/")
    else:
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8080"))
        data_dir = tempfile.mkdtemp(prefix="edge-accept-")
        env = dict(os.environ, HOST=host, PORT=str(port), DATA_DIR=data_dir)
        spawned = subprocess.Popen(
            [sys.executable, "-m", "app.server"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        if not wait_healthy(base):
            record("等待服务启动", False, f"{base} 未在超时内就绪")
            spawned.kill()
            return finish(1)
        record("等待服务启动", True, base)

    try:
        http_smoke(base)
        ts = str(int(time.time() * 1000))
        scenario_barrier(base, f"acc-barrier-{ts}")
        scenario_illegal(base, f"acc-illegal-{ts}")
        scenario_recovery(base, f"acc-rec-{ts}")
        scenario_idempotency(base, f"acc-idem-{ts}")
    finally:
        if spawned is not None:
            spawned.kill()
            spawned.wait(timeout=5)

    return finish(0)


def finish(code: int) -> int:
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 68)
    print(f"验收结论：{total - len(failed)}/{total} 通过")
    if failed:
        print("失败项：")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
        rc = 1
    else:
        print("ALL ACCEPTANCE CHECKS PASSED")
        rc = 0
    print("=" * 68)
    return rc if code == 0 else code


if __name__ == "__main__":
    sys.exit(main())
