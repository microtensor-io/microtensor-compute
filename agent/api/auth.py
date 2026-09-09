import ipaddress
import json
import threading
import time
from pathlib import Path
from typing import Any

from protocol.session import (
    HEADER_HOTKEY,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SIGNATURE_WINDOW_SECONDS,
    signing_bytes,
)

SS58_FORMAT = 42
LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def fresh(timestamp: str, window: int = SIGNATURE_WINDOW_SECONDS, now: float | None = None) -> bool:
    try:
        stamped = float(timestamp)
    except (TypeError, ValueError):
        return False
    moment = time.time() if now is None else now
    return abs(moment - stamped) <= window


def verify_hotkey(hotkey: str, message: bytes, signature: str) -> bool:
    if not hotkey or not signature:
        return False
    try:
        from substrateinterface import Keypair
    except ImportError:
        return False
    raw = signature.strip()
    if raw[:2].lower() == "0x":
        raw = raw[2:]
    try:
        signature_bytes = bytes.fromhex(raw)
        keypair = Keypair(ss58_address=hotkey, ss58_format=SS58_FORMAT)
        return bool(keypair.verify(message, signature_bytes))
    except Exception:
        return False


def signer(hotkeys: list[str], message: bytes, signature: str) -> str:
    for hotkey in hotkeys:
        if verify_hotkey(hotkey, message, signature):
            return hotkey
    return ""


def is_loopback(host: str) -> bool:
    if not host:
        return False
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ValidatorSet:
    def __init__(self, path: Path | None = None) -> None:
        self._hotkeys: frozenset[str] = frozenset()
        self._lock = threading.Lock()
        self._path = Path(path) if path is not None else None
        self.updated_at = 0.0
        self.source = "empty"
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        found = saved.get("validators") if isinstance(saved, dict) else None
        if isinstance(found, list) and found:
            self._hotkeys = frozenset(str(hotkey) for hotkey in found if hotkey)
            self.updated_at = float(saved.get("updated_at", 0.0) or 0.0)
            self.source = "cache"

    def update(self, hotkeys: list[str]) -> bool:
        cleaned = frozenset(hotkey.strip() for hotkey in hotkeys if hotkey and hotkey.strip())
        with self._lock:
            changed = cleaned != self._hotkeys
            self._hotkeys = cleaned
            self.updated_at = time.time()
            self.source = "server"
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temp = self._path.with_suffix(".tmp")
                temp.write_text(
                    json.dumps({"validators": sorted(cleaned), "updated_at": self.updated_at}),
                    encoding="utf-8",
                )
                temp.replace(self._path)
            except OSError:
                pass
        return changed

    def __contains__(self, hotkey: object) -> bool:
        return hotkey in self._hotkeys

    def __len__(self) -> int:
        return len(self._hotkeys)

    def hotkeys(self) -> list[str]:
        return sorted(self._hotkeys)


class AuthFailure(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def check_request(
    validators: ValidatorSet,
    method: str,
    path: str,
    headers: Any,
    body: bytes,
    window: int = SIGNATURE_WINDOW_SECONDS,
) -> str:
    hotkey = str(headers.get(HEADER_HOTKEY, "") or "").strip()
    timestamp = str(headers.get(HEADER_TIMESTAMP, "") or "").strip()
    signature = str(headers.get(HEADER_SIGNATURE, "") or "").strip()
    if not hotkey or not timestamp or not signature:
        raise AuthFailure(401, "validator signature headers required")
    if hotkey not in validators:
        raise AuthFailure(403, "not an active compute validator")
    if not fresh(timestamp, window):
        raise AuthFailure(401, "timestamp outside the signature window")
    if not verify_hotkey(hotkey, signing_bytes(method, path, timestamp, body), signature):
        raise AuthFailure(401, "signature does not verify")
    return hotkey


def dependency(runtime: Any) -> Any:
    from fastapi import HTTPException, Request

    async def require_validator(request: Request) -> str:
        body = await request.body()
        try:
            return check_request(
                runtime.validators,
                request.method,
                request.url.path,
                request.headers,
                body,
                runtime.settings.signature_window_seconds,
            )
        except AuthFailure as exc:
            raise HTTPException(exc.status, exc.detail) from exc

    return require_validator
