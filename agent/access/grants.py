from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Any

from agent.access import ssh_keys
from agent.access.nonce import NonceBook
from agent.api.auth import signer
from agent.config import Settings
from agent.hardware import attestation
from agent.store.events import EventStore
from protocol.session import NONCE_TTL_SECONDS, SessionClose, SessionGrant, SessionOpen


class GrantError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class OpenSession:
    session_id: str
    public_key: str
    validator: str
    nonce: str
    opened_at: float
    expires_at: float

    def payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "validator": self.validator,
            "opened_at": round(self.opened_at, 3),
            "expires_at": round(self.expires_at, 3),
        }


def _iso(moment: float) -> str:
    return dt.datetime.fromtimestamp(moment, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


class SessionGrants:
    def __init__(
        self,
        settings: Settings,
        events: EventStore,
        nonces: NonceBook | None = None,
        ttl_seconds: int = NONCE_TTL_SECONDS,
    ) -> None:
        self.settings = settings
        self.events = events
        self.nonces = nonces or NonceBook(ttl_seconds)
        self.ttl = ttl_seconds
        self._open: dict[str, OpenSession] = {}
        self._lock = threading.Lock()

    @property
    def path(self) -> Any:
        return self.settings.authorized_keys

    def open(self, request: SessionOpen, hotkeys: list[str]) -> SessionGrant:
        problems = request.problems()
        if problems:
            raise GrantError(422, "; ".join(problems))
        canonical = ssh_keys.canonical(request.public_key)
        if canonical is None:
            raise GrantError(422, "public_key is not an accepted OpenSSH key")
        validator = signer(hotkeys, request.blob, request.signature)
        if not validator:
            raise GrantError(403, "signature does not verify against any active validator hotkey")
        if not self.nonces.accept(request.nonce):
            raise GrantError(403, "nonce already used or malformed")
        now = time.time()
        session_id = request.nonce[:16]
        try:
            ssh_keys.grant(canonical, session_id, self.path)
        except (OSError, ValueError) as exc:
            raise GrantError(500, f"could not install the key: {exc}") from exc
        host_key = attestation.host_key_line(self.settings.ssh_host_key)
        evidence = attestation.build(request.nonce, self.settings, host_key)
        if evidence is None:
            ssh_keys.revoke(session_id, self.path)
            raise GrantError(422, "nonce must be 32 bytes as 64 lowercase hex characters")
        expires = now + self.ttl
        with self._lock:
            self._open[session_id] = OpenSession(
                session_id=session_id,
                public_key=canonical,
                validator=validator,
                nonce=request.nonce,
                opened_at=now,
                expires_at=expires,
            )
        self.events.write(
            "session.open",
            session=session_id,
            validator=validator,
            nonce=request.nonce[:16],
            expires_at=_iso(expires),
            tdx=bool(evidence.tdx_quote),
        )
        return SessionGrant(
            ssh_username=self.settings.ssh_user,
            ssh_port=self.settings.ssh_port,
            ssh_host_key=host_key,
            python_path=self.settings.python_path,
            root_dir=self.settings.root_dir,
            port_range=self.settings.port_range,
            validator=validator,
            expires_at=_iso(expires),
            gpu_attestation=evidence.payload(),
            tdx_quote=evidence.tdx_quote,
            session_id=session_id,
        )

    def _matching(self, request: SessionClose) -> list[str]:
        canonical = ssh_keys.canonical(request.public_key) if request.public_key else None
        with self._lock:
            found = [
                sid
                for sid, session in self._open.items()
                if (request.session_id and sid == request.session_id)
                or (canonical is not None and session.public_key == canonical)
            ]
        if request.session_id and request.session_id not in found:
            found.append(request.session_id)
        return found

    def close(self, request: SessionClose, validator: str = "") -> int:
        revoked = 0
        for session_id in self._matching(request):
            if ssh_keys.revoke(session_id, self.path):
                revoked += 1
            with self._lock:
                self._open.pop(session_id, None)
            self.events.write("session.close", session=session_id, validator=validator)
        return revoked

    def sweep(self, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        with self._lock:
            expired = [sid for sid, session in self._open.items() if session.expires_at <= moment]
            for session_id in expired:
                self._open.pop(session_id, None)
            known = set(self._open)
        revoked = 0
        for session_id in expired:
            if ssh_keys.revoke(session_id, self.path):
                revoked += 1
            self.events.write("session.expire", session=session_id)
        for session_id in ssh_keys.active_sessions(self.path):
            if session_id not in known and ssh_keys.revoke(session_id, self.path):
                revoked += 1
                self.events.write("session.orphan_revoked", session=session_id)
        self.nonces.expire(moment)
        return revoked

    def revoke_all(self) -> int:
        with self._lock:
            self._open.clear()
        removed = ssh_keys.revoke_all_sessions(self.path)
        if removed:
            self.events.write("session.revoke_all", removed=removed)
        return removed

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [session.payload() for session in self._open.values()]

    def __len__(self) -> int:
        return len(self._open)
