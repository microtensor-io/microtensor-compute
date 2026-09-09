from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agent.cache.pull_lock import PullBusy, PullLock
from agent.store.events import EventStore
from protocol.messages import PrefetchRequest
from protocol.session import well_formed_digest

REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?$")
PULL_TIMEOUT = 900.0
MANIFEST_TIMEOUT = 20.0
INSPECT_TIMEOUT = 15.0
LOCK_WAIT = 600.0
STATE_LIMIT = 4096
FREE_MULTIPLIER = 3
DOCKER_ROOT = "/var/lib/docker"

_state_lock = threading.Lock()
_started_at = round(time.time(), 3)


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 127, str(exc)
    return int(done.returncode), (done.stdout or "") + (done.stderr or "")


class PrefetchState:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("started_at", _started_at)
        parsed.setdefault("sweep_count", 0)
        parsed.setdefault("images", {})
        if not isinstance(parsed["images"], dict):
            parsed["images"] = {}
        return parsed

    def _encode(self, state: dict[str, Any]) -> bytes:
        return json.dumps(state, separators=(",", ":"), sort_keys=True).encode()

    def save(self, state: dict[str, Any]) -> None:
        state["started_at"] = state.get("started_at") or _started_at
        state["sweep_count"] = int(state.get("sweep_count", 0) or 0)
        state["updated_at"] = round(time.time(), 3)
        images = state.get("images") or {}
        encoded = self._encode(state)
        while len(encoded) > STATE_LIMIT and images:
            oldest = min(images, key=lambda name: float(images[name].get("pulled_at", 0) or 0))
            images.pop(oldest)
            state["images"] = images
            encoded = self._encode(state)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_bytes(encoded)
            os.replace(temp, self.path)
        except OSError:
            pass

    def record(self, reference: str, **fields: Any) -> None:
        with _state_lock:
            state = self.load()
            state["images"][reference] = {"pulled_at": round(time.time(), 3), **fields}
            state["sweep_count"] = int(state.get("sweep_count", 0) or 0) + 1
            self.save(state)

    def note_error(self, reference: str, error: str) -> None:
        with _state_lock:
            state = self.load()
            state["last_error"] = {
                "reference": reference,
                "error": error[:300],
                "at": round(time.time(), 3),
            }
            state["sweep_count"] = int(state.get("sweep_count", 0) or 0) + 1
            self.save(state)


def repo_digests(reference: str) -> list[str]:
    code, out = _run(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference],
        INSPECT_TIMEOUT,
    )
    if code != 0:
        return []
    try:
        parsed = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def image_size(reference: str) -> int:
    code, out = _run(["docker", "manifest", "inspect", "--verbose", reference], MANIFEST_TIMEOUT)
    if code != 0:
        return 0
    try:
        parsed = json.loads(out.strip())
    except ValueError:
        return 0
    entries = parsed if isinstance(parsed, list) else [parsed]
    sizes: list[int] = []
    for entry in entries:
        manifest = (
            entry.get("SchemaV2Manifest") or entry.get("OCIManifest") or {}
            if isinstance(entry, dict)
            else {}
        )
        layers = manifest.get("layers") if isinstance(manifest, dict) else None
        if isinstance(layers, list):
            sizes.append(
                sum(int(layer.get("size", 0) or 0) for layer in layers if isinstance(layer, dict))
            )
    return max(sizes) if sizes else 0


def free_bytes(root: str = DOCKER_ROOT) -> int:
    for candidate in (root, "/"):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return 0


def warm(
    request: PrefetchRequest, state_path: Path, events: EventStore | None = None
) -> dict[str, Any]:
    reference = request.reference
    state = PrefetchState(state_path)
    result: dict[str, Any] = {
        "image": request.image,
        "digest": request.digest,
        "reference": reference,
    }
    if not reference or not REFERENCE.match(reference):
        result["error"] = "invalid image reference"
        return result
    if request.digest and not well_formed_digest(request.digest):
        result["error"] = "invalid digest"
        return result
    present = repo_digests(reference)
    if request.digest and any(item.endswith(f"@{request.digest}") for item in present):
        result.update(ok=True, warm=True, already_present=True)
        state.record(reference, digest=request.digest, size_bytes=0, already_present=True)
        return result
    size = image_size(reference)
    if size:
        free = free_bytes()
        result["size_bytes"] = size
        result["free_bytes"] = free
        if free and free < size * FREE_MULTIPLIER:
            result["error"] = "not enough free disk for the image"
            state.note_error(reference, result["error"])
            return result
    lock = PullLock(timeout=LOCK_WAIT)
    try:
        lock.acquire()
    except PullBusy:
        result["error"] = "another pull holds the lock"
        state.note_error(reference, result["error"])
        return result
    started = time.monotonic()
    try:
        code, out = _run(["docker", "pull", reference], PULL_TIMEOUT)
    finally:
        lock.release()
    result["pull_seconds"] = round(time.monotonic() - started, 1)
    result["lock_degraded"] = lock.degraded
    if code != 0:
        result["error"] = f"docker pull failed ({code}): {out.strip()[-300:]}"
        state.note_error(reference, result["error"])
        if events is not None:
            events.write("image.prefetch", image=reference, ok=False, error=result["error"])
        return result
    digests = repo_digests(reference)
    result["repo_digests"] = digests
    if request.digest and not any(item.endswith(f"@{request.digest}") for item in digests):
        result["error"] = "pulled image does not carry the requested digest"
        state.note_error(reference, result["error"])
        if events is not None:
            events.write("image.prefetch", image=reference, ok=False, error=result["error"])
        return result
    pulled = request.digest or (
        digests[0].split("@", 1)[1] if digests and "@" in digests[0] else ""
    )
    result.update(ok=True, warm=True, pulled_digest=pulled)
    state.record(reference, digest=pulled, size_bytes=size, seconds=result["pull_seconds"])
    if events is not None:
        events.write(
            "image.prefetch",
            image=reference,
            ok=True,
            digest=pulled,
            seconds=result["pull_seconds"],
        )
    return result
