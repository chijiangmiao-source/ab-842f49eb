"""基于 JSONL 的持久化：演练规格 + 仅接受事件进入重放日志。

拒绝记录写入独立审计文件，绝不进入恢复重放，保证“拒绝不污染恢复状态”。
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional


class StoreError(Exception):
    pass


class JsonStore:
    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)

    # ------------------------------------------------------------ internals
    def _dir(self, drill_id: str) -> str:
        d = os.path.join(self.directory, drill_id)
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _atomic_write(path: str, data: str) -> None:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_append(path: str, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _read_jsonl(path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        out: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError as e:
                    raise StoreError(f"{path} 第 {lineno} 行损坏：{e}") from e
        return out

    # -------------------------------------------------------------- drills
    def list_drills(self) -> List[str]:
        if not os.path.isdir(self.directory):
            return []
        out = []
        for name in sorted(os.listdir(self.directory)):
            if os.path.isfile(os.path.join(self.directory, name, "spec.json")):
                out.append(name)
        return out

    def save_spec(self, drill_id: str, spec: Dict[str, Any]) -> None:
        self._atomic_write(
            os.path.join(self._dir(drill_id), "spec.json"),
            json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2),
        )

    def load_spec(self, drill_id: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.directory, drill_id, "spec.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self, drill_id: str) -> bool:
        return os.path.isfile(os.path.join(self.directory, drill_id, "spec.json"))

    # -------------------------------------------------------------- events
    def _events_path(self, drill_id: str) -> str:
        return os.path.join(self._dir(drill_id), "events.jsonl")

    def append_event(self, drill_id: str, event: Dict[str, Any]) -> None:
        self._fsync_append(
            self._events_path(drill_id),
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        )

    def load_events(self, drill_id: str) -> List[Dict[str, Any]]:
        return self._read_jsonl(self._events_path(drill_id))

    # --------------------------------------------------------- rejections
    def _rej_path(self, drill_id: str) -> str:
        return os.path.join(self._dir(drill_id), "rejected.audit.jsonl")

    def append_rejection(self, drill_id: str, rec: Dict[str, Any]) -> None:
        # 审计流水与重放日志物理分离
        self._fsync_append(
            self._rej_path(drill_id),
            json.dumps(rec, ensure_ascii=False, sort_keys=True),
        )

    def load_rejections(self, drill_id: str) -> List[Dict[str, Any]]:
        return self._read_jsonl(self._rej_path(drill_id))
