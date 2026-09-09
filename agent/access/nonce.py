from __future__ import annotations

import re
import secrets
import threading
import time

NONCE_BYTES = 32
NONCE_TTL_SECONDS = 600
HEX_NONCE = re.compile(r"^[0-9a-f]{64}$")


def issue() -> str:
    return secrets.token_hex(NONCE_BYTES)


def well_formed(nonce: str) -> bool:
    return bool(HEX_NONCE.match(nonce or ""))


class NonceBook:
    def __init__(self, ttl_seconds: int = NONCE_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, nonce: str, now: float | None = None) -> bool:
        if not well_formed(nonce):
            return False
        moment = time.time() if now is None else now
        with self._lock:
            self.expire(moment)
            if nonce in self._seen:
                return False
            self._seen[nonce] = moment + self.ttl
            return True

    def expire(self, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        stale = [nonce for nonce, until in self._seen.items() if until <= moment]
        for nonce in stale:
            self._seen.pop(nonce, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._seen)
