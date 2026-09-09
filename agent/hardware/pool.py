from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

WORKERS = 4
IN_FLIGHT = 3
TIMEOUT_SECONDS = 8.0

T = TypeVar("T")


class PoolBusy(RuntimeError):
    pass


class PoolTimeout(TimeoutError):
    pass


class MetricsPool:
    def __init__(self, workers: int = WORKERS, in_flight: int = IN_FLIGHT) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="metrics")
        self._slots = threading.BoundedSemaphore(in_flight)
        self.in_flight = 0
        self.rejected = 0
        self.timed_out = 0

    async def run(
        self,
        function: Callable[..., T],
        *args: Any,
        timeout: float = TIMEOUT_SECONDS,
        stage: str = "metrics",
    ) -> T:
        if not self._slots.acquire(blocking=False):
            self.rejected += 1
            raise PoolBusy(f"{stage}: too many measurements in flight")
        self.in_flight += 1
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, lambda: function(*args))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.timed_out += 1
            raise PoolTimeout(f"{stage}: no answer within {timeout:g}s") from exc
        finally:
            self.in_flight -= 1
            self._slots.release()

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
