"""HTTP 服务：演练 API + 回放页面 + 健康检查（仅依赖标准库）。"""
from __future__ import annotations

import json
import os
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .engine import EngineError
from .service import DrillService, ValidationError
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"

DEMO_DRILLS = [
    {
        "id": "demo-barrier",
        "name": "屏障保留：标记中途改写边",
        "objects": ["R", "A", "B", "C", "G"],
        "roots": ["R"],
        "edges": {"R": ["A"], "A": ["B"]},
        "events": [
            # 周期一：A 尚未扫描（旧目标 B 仍白）时改写 A: B→C。
            # 删除屏障保留旧白目标 B、插入屏障保留新白目标 C；
            # 清扫时真正的垃圾只有 G，B 本周期存活。
            {"id": "e1", "type": "start_mark", "payload": {}},
            {"id": "e2", "type": "step", "payload": {}},
            {"id": "e3", "type": "set_edge",
             "payload": {"src": "A", "old_dst": "B", "new_dst": "C"}},
            {"id": "e4", "type": "step", "payload": {}},
            {"id": "e5", "type": "step", "payload": {}},
            {"id": "e6", "type": "step", "payload": {}},
            {"id": "e7", "type": "sweep", "payload": {}},
            # 周期二：屏障只承诺“本周期不释放”，下一周期 B 已确属
            # 不可达白色对象，被正常回收。
            {"id": "e8", "type": "start_mark", "payload": {}},
            {"id": "e9", "type": "step", "payload": {}},
            {"id": "e10", "type": "step", "payload": {}},
            {"id": "e11", "type": "step", "payload": {}},
            {"id": "e12", "type": "sweep", "payload": {}},
        ],
    },
    {
        "id": "demo-illegal-sweep",
        "name": "非法阶段清扫拒绝 & 引用已回收对象",
        "objects": ["A", "B", "G"],
        "roots": ["A"],
        "edges": {"A": ["B"]},
        "events": [
            {"id": "s0", "type": "sweep", "payload": {}},
            {"id": "s1", "type": "start_mark", "payload": {}},
            {"id": "s2", "type": "sweep", "payload": {}},
            {"id": "s3", "type": "step", "payload": {}},
            {"id": "s4", "type": "sweep", "payload": {}},
            {"id": "s5", "type": "step", "payload": {}},
            {"id": "s6", "type": "sweep", "payload": {}},
            {"id": "s7", "type": "set_edge",
             "payload": {"src": "A", "old_dst": None, "new_dst": "G"}},
        ],
    },
    {
        "id": "demo-reopen",
        "name": "标记中途重开：恢复裁决与连续运行一致",
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
    },
]


class Handler(BaseHTTPRequestHandler):
    server_version = "EdgeCollectorGC/1.0"

    # ---- 公共基础设施 ---------------------------------------------------

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("malformed_json", {})
        if not isinstance(data, dict):
            raise ValidationError("malformed_json", {"expected": "object"})
        return data

    def log_message(self, fmt, *args):  # 精简访问日志
        pass

    # ---- 路由 -----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/health":
                svc: DrillService = self.server.service  # type: ignore[attr-defined]
                self._json({"status": "ok",
                            "drills": [d["id"] for d in svc.store.list_drills()]})
            elif path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            elif path == "/api/drills":
                drills = self.server.service.store.list_drills()
                self._json({"drills": [{"id": d["id"], "name": d.get("name")}
                                       for d in drills]})
            elif path.startswith("/api/drills/"):
                rest = path[len("/api/drills/"):]
                parts = [p for p in rest.split("/") if p]
                if len(parts) == 1:
                    self._json(self.server.service.get_drill(parts[0]))
                elif len(parts) == 2 and parts[1] == "timeline":
                    self._json(self.server.service.get_timeline(parts[0]))
                elif len(parts) == 2 and parts[1] == "recovery":
                    self._json(self.server.service.recovery_report(parts[0]))
                else:
                    self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            else:
                self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValidationError as exc:
            self._json({"accepted": False, "reason": exc.reason,
                        "detail": exc.detail}, HTTPStatus.NOT_FOUND
                       if exc.reason == "drill_not_found" else HTTPStatus.BAD_REQUEST)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": "internal"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/drills":
                body = self._read_json()
                drill = self.server.service.create_drill(body)
                self._json(drill, HTTPStatus.CREATED)
            elif path.startswith("/api/drills/") and path.endswith("/events"):
                drill_id = path[len("/api/drills/"):-len("/events")]
                body = self._read_json()
                event_id = body.get("id")
                ev_type = body.get("type")
                payload = body.get("payload", {})
                if not isinstance(event_id, str) or not event_id:
                    raise ValidationError("bad_event_id", {})
                try:
                    result = self.server.service.deliver(
                        drill_id, event_id, ev_type, payload)
                except EngineError as exc:
                    # 拒单已持久化：返回 409 与稳定 reason，状态未被污染
                    self._json({"event_id": event_id, "replayed": False,
                                "accepted": False, "reason": exc.reason,
                                "detail": exc.detail}, HTTPStatus.CONFLICT)
                else:
                    self._json(result, HTTPStatus.OK if result["replayed"]
                               else HTTPStatus.CREATED)
            else:
                self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except ValidationError as exc:
            self._json({"accepted": False, "reason": exc.reason,
                        "detail": exc.detail}, HTTPStatus.BAD_REQUEST)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": "internal"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _static(self, name: str, content_type: str) -> None:
        fpath = STATIC_DIR / name
        if not fpath.is_file():
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        body = fpath.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def seed_demos(service: DrillService) -> None:
    for spec in DEMO_DRILLS:
        if service.store.get_drill_spec(spec["id"]) is None:
            try:
                service.create_drill(spec, spec["id"])
            except Exception:  # noqa: BLE001
                traceback.print_exc()


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = DrillService(store)
    seed_demos(service)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "/data/gc.db")
    httpd = build_server(host, port, db_path)
    print(f"edge-collector-gc listening on http://{host}:{port} (db={db_path})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
