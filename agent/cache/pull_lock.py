from __future__ import annotations

import os
import time
from pathlib import Path

LOCK_PATH = Path("/tmp/mt-agent-pull.lock")
POLL_SECONDS = 0.5


class PullBusy(RuntimeError):
    pass


class PullLock:
    def __init__(self, path: Path = LOCK_PATH, timeout: float | None = 1800.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.fd: int | None = None
        self.degraded = False

    def acquire(self) -> PullLock:
        try:
            import fcntl
        except ImportError:
            self.degraded = True
            return self
        try:
            self.fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            self.degraded = True
            return self
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise PullBusy(f"another pull holds {self.path}") from None
                time.sleep(POLL_SECONDS)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> PullLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
