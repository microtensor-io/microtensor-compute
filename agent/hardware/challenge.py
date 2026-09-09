from __future__ import annotations

import contextlib
import ctypes
import threading
import time
from pathlib import Path
from typing import Any

from protocol.messages import ChallengeAnswer, ChallengeRequest

MISSING = "challenge library not installed"
BUSY = "challenge already running"
CODES = {
    0: "ok",
    -1: "bad arguments",
    -2: "allocation failed",
    -3: "kernel launch failed",
    -4: "unavailable",
    -5: "device error",
}

_lock = threading.Lock()
_loaded: dict[str, ctypes.CDLL] = {}
_loaded_lock = threading.Lock()


def _bind(library: ctypes.CDLL) -> ctypes.CDLL:
    library.mt_challenge_version.restype = ctypes.c_int
    library.mt_challenge_version.argtypes = []
    library.mt_challenge_build.restype = ctypes.c_char_p
    library.mt_challenge_build.argtypes = []
    library.mt_challenge_last_error.restype = ctypes.c_char_p
    library.mt_challenge_last_error.argtypes = []
    library.mt_challenge_solve.restype = ctypes.c_int
    library.mt_challenge_solve.argtypes = [
        ctypes.c_uint64,
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    library.mt_challenge_benchmark.restype = ctypes.c_int
    library.mt_challenge_benchmark.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
    ]
    library.mt_challenge_release.restype = ctypes.c_int
    library.mt_challenge_release.argtypes = []
    return library


def load(path: Path) -> ctypes.CDLL | None:
    key = str(path)
    with _loaded_lock:
        if key in _loaded:
            return _loaded[key]
        if not Path(path).is_file():
            return None
        try:
            library = _bind(ctypes.CDLL(key))
        except (OSError, AttributeError):
            return None
        _loaded[key] = library
        return library


def installed(path: Path) -> bool:
    return load(path) is not None


def _last_error(library: ctypes.CDLL) -> str:
    try:
        raw = library.mt_challenge_last_error()
    except Exception:
        return ""
    return raw.decode("utf-8", "replace") if raw else ""


def _describe(library: ctypes.CDLL, stage: str, code: int) -> str:
    detail = _last_error(library) or CODES.get(code, f"code {code}")
    return f"{stage} failed ({code}): {detail}"


def release(library: ctypes.CDLL | None) -> int:
    if library is None:
        return 0
    try:
        return int(library.mt_challenge_release())
    except Exception:
        return -5


def release_all() -> None:
    with _loaded_lock:
        libraries = list(_loaded.values())
    for library in libraries:
        with contextlib.suppress(Exception):
            release(library)


def describe(path: Path) -> dict[str, Any]:
    library = load(path)
    if library is None:
        return {"installed": False, "path": str(path)}
    build = ""
    version = 0
    with contextlib.suppress(Exception):
        build = (library.mt_challenge_build() or b"").decode("utf-8", "replace")
    with contextlib.suppress(Exception):
        version = int(library.mt_challenge_version())
    return {"installed": True, "path": str(path), "build": build, "version": version}


def run(path: Path, request: ChallengeRequest) -> ChallengeAnswer:
    library = load(path)
    if library is None:
        return ChallengeAnswer(digest="", elapsed_ms=0.0, error=MISSING)
    if not _lock.acquire(blocking=False):
        return ChallengeAnswer(digest="", elapsed_ms=0.0, error=BUSY)
    build = ""
    version = 0
    with contextlib.suppress(Exception):
        build = (library.mt_challenge_build() or b"").decode("utf-8", "replace")
    with contextlib.suppress(Exception):
        version = int(library.mt_challenge_version())
    try:
        digest = ctypes.c_uint64(0)
        started = time.perf_counter()
        code = int(
            library.mt_challenge_solve(
                ctypes.c_uint64(request.seed & ((1 << 64) - 1)),
                ctypes.c_uint64(request.cipher & ((1 << 64) - 1)),
                ctypes.c_uint32(request.n),
                ctypes.c_uint32(request.rounds),
                ctypes.byref(digest),
            )
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if code != 0:
            return ChallengeAnswer(
                digest="",
                elapsed_ms=elapsed_ms,
                build=build,
                version=version,
                error=_describe(library, "solve", code),
            )
        gops = ctypes.c_double(0.0)
        gbps = ctypes.c_double(0.0)
        bench_ms = ctypes.c_double(0.0)
        bench_code = int(
            library.mt_challenge_benchmark(
                ctypes.c_uint32(request.benchmark_n),
                ctypes.c_uint32(request.iterations),
                ctypes.byref(gops),
                ctypes.byref(gbps),
                ctypes.byref(bench_ms),
            )
        )
        error = "" if bench_code == 0 else _describe(library, "benchmark", bench_code)
        return ChallengeAnswer(
            digest=f"{digest.value:016x}",
            elapsed_ms=elapsed_ms,
            gops=float(gops.value) if bench_code == 0 else 0.0,
            gbps=float(gbps.value) if bench_code == 0 else 0.0,
            benchmark_ms=float(bench_ms.value) if bench_code == 0 else 0.0,
            build=build,
            version=version,
            error=error,
        )
    finally:
        with contextlib.suppress(Exception):
            release(library)
        _lock.release()
