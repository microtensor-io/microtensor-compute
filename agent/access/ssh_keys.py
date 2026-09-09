from __future__ import annotations

import os
import re
import threading
from pathlib import Path

AUTHORIZED_KEYS = Path("/root/.ssh/authorized_keys")
MARKER = "mt-session:"
KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ssh-rsa")
PUBLIC_KEY = re.compile(
    r"^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ssh-rsa) [A-Za-z0-9+/]+=*$"
)

_lock = threading.Lock()


def canonical(public_key: str) -> str | None:
    parts = (public_key or "").strip().split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None
    candidate = f"{parts[0]} {parts[1]}"
    return candidate if PUBLIC_KEY.match(candidate) else None


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(".tmp")
    temp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def grant(public_key: str, session_id: str, path: Path = AUTHORIZED_KEYS) -> str:
    key = canonical(public_key)
    if key is None:
        raise ValueError("public key is not an accepted OpenSSH key")
    line = f"{key} {MARKER}{session_id}"
    with _lock:
        lines = [
            existing for existing in _read(path) if not existing.endswith(f"{MARKER}{session_id}")
        ]
        lines.append(line)
        _write(path, lines)
    return line


def revoke(session_id: str, path: Path = AUTHORIZED_KEYS) -> bool:
    with _lock:
        lines = _read(path)
        kept = [line for line in lines if not line.endswith(f"{MARKER}{session_id}")]
        if len(kept) == len(lines):
            return False
        _write(path, kept)
    return True


def revoke_all_sessions(path: Path = AUTHORIZED_KEYS) -> int:
    with _lock:
        lines = _read(path)
        kept = [line for line in lines if MARKER not in line]
        removed = len(lines) - len(kept)
        if removed:
            _write(path, kept)
    return removed


def active_sessions(path: Path = AUTHORIZED_KEYS) -> list[str]:
    ids: list[str] = []
    for line in _read(path):
        at = line.rfind(MARKER)
        if at >= 0:
            ids.append(line[at + len(MARKER) :].strip())
    return ids
