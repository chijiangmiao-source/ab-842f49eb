"""零依赖 HTTP 服务：JSON API + 演练页面 + 健康检查。

环境变量：
    HOST      监听地址（默认 0.0.0.0）
    PORT      监听端口（默认 8080）
    DATA_DIR  持久化目录（默认 ./data）
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlparse

from .engine import Reject
from .runtime import Runtime

START_TIME = time.time()


class App:
    def __init__(self, data_dir: str):
        self.runtime = Runtime(data_dir)
        self.html = self._load_html()

    @staticmethod
    def _load_html() -> bytes:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        with open(path, "rb") as f:
            return f.read()


def make_handler(app: App):
    runtime = app.runtime

    class Handler(BaseHTTPRequestHandler):
        server_version = "EdgeCollector/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 精简日志
            print("[http] " + (fmt % args), flush=True)

        # ------------------------------------------------------------ io
        def _send_json(self, code: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("请求体不是合法 JSON")

        def _drill_view(self, drill_id: str) -> Dict[str, Any]:
            engine = runtime.get_engine(drill_id)
            if engine is None:
                raise KeyError(drill_id)
            return {
                "id": drill_id,
                "state": engine.state.snapshot(),
                "safety": engine.safety_report(),
                "events": engine.replay_events(),
            }

        # ----------------------------------------------------------- GET
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "uptime_seconds": round(time.time() - START_TIME, 2),
                        "drills": runtime.list_drills(),
                    },
                )
                return
            if path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(app.html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(app.html)
                return
            if path == "/api/drills":
                drills = []
                for did in runtime.list_drills():
                    eng = runtime.get_engine(did)
                    drills.append(
                        {"id": did, "phase": eng.state.phase, "cycle": eng.state.cycle}
                    )
                self._send_json(200, {"drills": drills})
                return
            if path.startswith("/api/drills/"):
                drill_id = path[len("/api/drills/") :].strip("/")
                try:
                    self._send_json(200, self._drill_view(drill_id))
                except KeyError:
                    self._send_json(404, {"status": "error", "error": f"演练 {drill_id} 不存在"})
                return
            self._send_json(404, {"status": "error", "error": "not found"})

        # ---------------------------------------------------------- POST
        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._read_json()
            except ValueError as e:
                self._send_json(400, {"status": "error", "error": str(e)})
                return

            if path == "/api/drills":
                if not isinstance(body, dict):
                    self._send_json(400, {"status": "error", "error": "请求体须为对象"})
                    return
                drill_id = str(body.get("id", "")).strip()
                spec = body.get("spec")
                if not drill_id:
                    self._send_json(400, {"status": "error", "error": "缺少 id"})
                    return
                try:
                    runtime.create_drill(drill_id, spec if isinstance(spec, dict) else {})
                except Reject as e:
                    self._send_json(422, {"status": "rejected", "reasons": e.reasons})
                    return
                self._send_json(201, self._drill_view(drill_id))
                return

            if path.startswith("/api/drills/") and path.endswith("/events"):
                drill_id = path[len("/api/drills/") : -len("/events")]
                if not isinstance(body, dict):
                    self._send_json(400, {"status": "error", "error": "请求体须为事件对象"})
                    return
                result = runtime.dispatch(drill_id, body)
                code = 200 if result["status"] in ("accepted", "duplicate") else 422
                payload: Dict[str, Any] = dict(result)
                if result["status"] != "rejected":
                    try:
                        payload["drill"] = self._drill_view(drill_id)
                    except KeyError:
                        pass
                self._send_json(code, payload)
                return

            if path.startswith("/api/drills/") and path.endswith("/replay"):
                drill_id = path[len("/api/drills/") : -len("/replay")]
                events = body.get("events") if isinstance(body, dict) else None
                if not isinstance(events, list):
                    self._send_json(400, {"status": "error", "error": "需要 {events: [...]}"})
                    return
                results = []
                for ev in events:
                    results.append(runtime.dispatch(drill_id, ev))
                try:
                    view = self._drill_view(drill_id)
                except KeyError:
                    view = None
                self._send_json(200, {"results": results, "drill": view})
                return

            if path.startswith("/api/drills/") and path.endswith("/recover"):
                drill_id = path[len("/api/drills/") : -len("/recover")]
                engine = runtime.recover(drill_id)
                if engine is None:
                    self._send_json(404, {"status": "error", "error": f"演练 {drill_id} 不存在"})
                    return
                self._send_json(200, self._drill_view(drill_id))
                return

            self._send_json(404, {"status": "error", "error": "not found"})

    return Handler


def serve(host: str, port: int, data_dir: str) -> None:
    app = App(data_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"[server] 监听 http://{host}:{port}  数据目录={os.path.abspath(data_dir)}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(
        os.environ.get("HOST", "0.0.0.0"),
        int(os.environ.get("PORT", "8080")),
        os.environ.get("DATA_DIR", "./data"),
    )
