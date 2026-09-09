from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("/var/lib/rig-agent-logs/events.jsonl")
MAX_FILE_SIZE = 50 * 1024 * 1024
KEEP_TAIL = 25 * 1024 * 1024


class EventStore:
    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        max_bytes: int = MAX_FILE_SIZE,
        keep_bytes: int = KEEP_TAIL,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep_bytes = keep_bytes
        self._lock = threading.Lock()

    def write(self, kind: str, **fields: Any) -> None:
        record: dict[str, Any] = {"ts": round(time.time(), 3), "kind": kind}
        record.update(fields)
        try:
            line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        except Exception:
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                if self.path.stat().st_size > self.max_bytes:
                    self._truncate()
        except Exception:
            return

    def _truncate(self) -> None:
        data = self.path.read_bytes()
        tail = data[-self.keep_bytes :]
        cut = tail.find(b"\n")
        if 0 <= cut < len(tail) - 1:
            tail = tail[cut + 1 :]
        temp = self.path.with_suffix(".jsonl.tmp")
        temp.write_bytes(tail)
        os.replace(temp, self.path)

    def tail(self, count: int = 200) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                window = min(size, max(count, 1) * 512)
                handle.seek(size - window)
                lines = handle.read().splitlines()[-count:]
        except Exception:
            return []
        records: list[dict[str, Any]] = []
        for raw in lines:
            try:
                records.append(json.loads(raw.decode("utf-8")))
            except Exception:
                continue
        return records
