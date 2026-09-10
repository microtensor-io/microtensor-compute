from __future__ import annotations

import contextlib
import errno
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEVICE = Path("/dev/kmsg")
XID = re.compile(r"NVRM: Xid \(PCI:([0-9A-Fa-f:.]+)\): (\d+)")
FALLEN = re.compile(r"GPU has fallen off the bus", re.IGNORECASE)
RESET = re.compile(r"NVRM: .*\b(reset|timeout|stuck)\b", re.IGNORECASE)

SEVERE_XIDS: dict[int, str] = {
    13: "graphics engine exception",
    31: "gpu memory page fault",
    32: "invalid or corrupted push buffer stream",
    43: "gpu stopped processing",
    45: "preemptive cleanup after a hang",
    48: "double bit ecc error",
    61: "internal micro-controller breakpoint",
    62: "internal micro-controller halt",
    63: "ecc page retirement or row remap",
    64: "ecc page retirement failure",
    68: "video processor exception",
    74: "nvlink error",
    79: "gpu has fallen off the bus",
    92: "high single bit ecc error rate",
    94: "contained ecc error",
    95: "uncontained ecc error",
    119: "gsp rpc timeout",
    120: "gsp error",
    140: "unrecoverable ecc error",
}


@dataclass(frozen=True)
class KernelFault:
    kind: str
    message: str
    pci: str = ""
    xid: int | None = None
    severe: bool = False
    seen_at: float = field(default_factory=time.time)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "pci": self.pci,
            "xid": self.xid,
            "severe": self.severe,
            "meaning": SEVERE_XIDS.get(self.xid or -1, ""),
            "seen_at": round(self.seen_at, 3),
        }


def parse_line(raw: str) -> KernelFault | None:
    message = raw.split(";", 1)[1].strip() if ";" in raw else raw.strip()
    if not message:
        return None
    match = XID.search(message)
    if match:
        xid = int(match.group(2))
        return KernelFault("xid", message, pci=match.group(1), xid=xid, severe=xid in SEVERE_XIDS)
    if FALLEN.search(message):
        return KernelFault("fallen_off_bus", message, xid=79, severe=True)
    if RESET.search(message):
        return KernelFault("driver_reset", message, severe=True)
    return None


def watch(
    callback: Callable[[KernelFault], None],
    device: Path = DEVICE,
    stop: threading.Event | None = None,
    poll_seconds: float = 0.5,
) -> None:
    fd = os.open(str(device), os.O_RDONLY | os.O_NONBLOCK)
    try:
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_END)
        while stop is None or not stop.is_set():
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                time.sleep(poll_seconds)
                continue
            except OSError as exc:
                if exc.errno in (errno.EPIPE, errno.EINVAL):
                    continue
                raise
            if not chunk:
                time.sleep(poll_seconds)
                continue
            fault = parse_line(chunk.decode("utf-8", "replace"))
            if fault is not None:
                callback(fault)
    finally:
        os.close(fd)


def scan_dmesg(text: str) -> list[KernelFault]:
    faults: list[KernelFault] = []
    for line in text.splitlines():
        fault = parse_line(line)
        if fault is not None:
            faults.append(fault)
    return faults


def watch_dmesg(
    callback: Callable[[KernelFault], None],
    stop: threading.Event | None = None,
) -> None:
    process = subprocess.Popen(
        ["dmesg", "--follow", "--kernel", "--notime"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if stop is not None and stop.is_set():
                break
            fault = parse_line(line)
            if fault is not None:
                callback(fault)
    finally:
        if process.poll() is None:
            process.terminate()
