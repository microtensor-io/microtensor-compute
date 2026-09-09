from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Any

from agent.monitor.kmsg import KernelFault
from agent.store.events import EventStore

JOB_LABEL = "mt.job"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FAULT_WINDOW_SECONDS = 120.0
LIFECYCLE = {
    "start",
    "stop",
    "restart",
    "kill",
    "destroy",
    "oom",
    "die",
    "pause",
    "unpause",
    "create",
}
EXIT_REASONS = {
    0: "purposely_stopped",
    1: "application_error",
    125: "container_failed_to_run",
    126: "command_invoke_error",
    127: "file_or_directory_not_found",
    128: "invalid_argument_on_exit",
    134: "abnormal_termination",
    137: "immediate_termination",
    139: "segmentation_fault",
    143: "graceful_termination",
    255: "exit_status_out_of_range",
}


def valid_name(name: str) -> bool:
    return bool(NAME.match(name or ""))


def exit_reason(code: int | None) -> str:
    if code is None:
        return "unknown"
    return EXIT_REASONS.get(code, "exit_status_out_of_range" if code > 255 else f"exit_{code}")


def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return int(done.returncode), done.stdout or "", done.stderr or ""


def stats(name: str, timeout: float = 10.0) -> dict[str, Any]:
    if not valid_name(name):
        return {"error": "invalid container name", "code": 22}
    code, out, err = _run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", name], timeout
    )
    if code != 0:
        return {
            "error": err.strip()[-300:] or out.strip()[-300:] or "docker stats failed",
            "code": code,
        }
    try:
        parsed = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "unparseable docker stats output", "code": 1, "raw": out[-500:]}
    return dict(parsed) if isinstance(parsed, dict) else {"raw": out[-500:]}


def job_containers(timeout: float = 5.0) -> list[dict[str, Any]]:
    code, out, _ = _run(
        ["docker", "ps", "-a", "--filter", f"label={JOB_LABEL}", "--format", "{{json .}}"], timeout
    )
    if code != 0:
        return []
    found: list[dict[str, Any]] = []
    for line in out.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        labels = str(item.get("Labels", "") or "")
        job = ""
        for pair in labels.split(","):
            if pair.startswith(f"{JOB_LABEL}="):
                job = pair.split("=", 1)[1]
        found.append(
            {
                "name": str(item.get("Names", "") or ""),
                "image": str(item.get("Image", "") or ""),
                "state": str(item.get("State", "") or ""),
                "status": str(item.get("Status", "") or ""),
                "job": job,
                "id": str(item.get("ID", "") or ""),
            }
        )
    return found


class ContainerWatcher:
    def __init__(self, events: EventStore, stop: threading.Event | None = None) -> None:
        self.events = events
        self.stop = stop or threading.Event()
        self._faults: list[KernelFault] = []
        self._lock = threading.Lock()

    def note_fault(self, fault: KernelFault) -> None:
        with self._lock:
            self._faults.append(fault)
            cutoff = time.time() - FAULT_WINDOW_SECONDS
            self._faults = [item for item in self._faults if item.seen_at >= cutoff]

    def recent_fault(self) -> KernelFault | None:
        cutoff = time.time() - FAULT_WINDOW_SECONDS
        with self._lock:
            recent = [item for item in self._faults if item.seen_at >= cutoff]
            return recent[-1] if recent else None

    def classify(self, event: dict[str, Any]) -> dict[str, Any]:
        actor = event.get("Actor") if isinstance(event.get("Actor"), dict) else {}
        attributes = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
        action = str(event.get("Action", "") or "").split(":", 1)[0]
        raw_code = attributes.get("exitCode")
        try:
            code = int(raw_code) if raw_code not in (None, "") else None
        except (TypeError, ValueError):
            code = None
        record: dict[str, Any] = {
            "action": action,
            "container": str(attributes.get("name", "") or ""),
            "id": str(actor.get("ID", "") or "")[:12],
            "image": str(attributes.get("image", "") or ""),
            "job": str(attributes.get(JOB_LABEL, "") or ""),
        }
        if action in ("die", "oom", "kill"):
            record["exit_code"] = code
            record["reason"] = "oom_killed" if action == "oom" else exit_reason(code)
            fault = self.recent_fault()
            record["gpu_error"] = fault is not None
            if fault is not None:
                record["reason"] = "gpu_error"
                record["fault"] = fault.payload()
        return record

    def handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("Type", "container") != "container":
            return
        action = str(event.get("Action", "") or "").split(":", 1)[0]
        if action not in LIFECYCLE:
            return
        record = self.classify(event)
        self.events.write(f"container.{action}", **record)

    def _stream_once(self) -> None:
        process = subprocess.Popen(
            [
                "docker",
                "events",
                "--format",
                "{{json .}}",
                "--filter",
                "type=container",
                "--filter",
                f"label={JOB_LABEL}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if self.stop.is_set():
                    break
                self.handle_line(line.strip())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def run(self) -> None:
        delay = 2.0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.events.write("monitor.containers.start")
                self._stream_once()
            except Exception as exc:
                self.events.write("monitor.containers.error", error=str(exc)[:200])
            if self.stop.is_set():
                return
            delay = 2.0 if time.monotonic() - started > 60 else min(delay * 2, 60.0)
            self.stop.wait(delay)
