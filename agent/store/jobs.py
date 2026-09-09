from __future__ import annotations

import threading
import time
from typing import Any

from protocol.session import JobRecord


class JobBook:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(
        self, job: JobRecord, source: str = "", detail: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        entry = job.payload()
        entry["recorded_at"] = round(time.time(), 3)
        entry["source"] = source
        if detail:
            entry["detail"] = dict(detail)
        with self._lock:
            self._jobs[job.job_id] = entry
        return dict(entry)

    def forget(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def view(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._jobs.values()]

    def __len__(self) -> int:
        return len(self._jobs)

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._jobs
