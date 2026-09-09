from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

HOST_ROOT = Path("/proc/1/root")
DMI_FIELDS = ("sys_vendor", "product_name", "product_version", "board_vendor", "board_name", "bios_version")
NESTED_MARKERS = ("/docker/", "/lxc/", "/containerd/", "/kubepods", "/podman/", "/machine.slice/libpod")


def read_text(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except OSError:
        return ""


def host_path(relative: str) -> Path:
    candidate = HOST_ROOT / relative.lstrip("/")
    return candidate if candidate.exists() else Path("/") / relative.lstrip("/")


def init_root_is_host() -> bool:
    try:
        init_root = os.stat(HOST_ROOT)
        own_root = os.stat("/")
    except OSError:
        return False
    return (init_root.st_dev, init_root.st_ino) != (own_root.st_dev, own_root.st_ino)


def init_cgroup() -> str:
    return read_text(Path("/proc/1/cgroup"))


def cgroup_is_nested(cgroup: str | None = None) -> bool:
    text = init_cgroup() if cgroup is None else cgroup
    return any(marker in text for marker in NESTED_MARKERS)


def detect_virt_container() -> str:
    binary = shutil.which("systemd-detect-virt")
    if binary is None:
        return "unknown"
    try:
        result = subprocess.run(
            [binary, "--container"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    answer = (result.stdout or "").strip()
    return answer or "none"


def dmi() -> dict[str, str]:
    base = host_path("/sys/class/dmi/id")
    return {name: read_text(base / name, 256) for name in DMI_FIELDS}


def kernel_release() -> str:
    return read_text(host_path("/proc/sys/kernel/osrelease"), 128) or platform.release()


def kernel_version_tuple(release: str | None = None) -> tuple[int, int]:
    text = release or kernel_release()
    match = re.match(r"(\d+)\.(\d+)", text)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def os_release() -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in read_text(host_path("/etc/os-release"), 8192).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip().strip('"')
    return fields


def machine_id() -> str:
    return read_text(host_path("/etc/machine-id"), 64)


def summary() -> dict[str, Any]:
    release = kernel_release()
    return {
        "init_root_is_host": init_root_is_host(),
        "init_cgroup": init_cgroup()[:512],
        "cgroup_nested": cgroup_is_nested(),
        "virt_container": detect_virt_container(),
        "dmi": dmi(),
        "kernel": release,
        "kernel_tuple": list(kernel_version_tuple(release)),
        "arch": platform.machine(),
        "os": os_release().get("PRETTY_NAME", ""),
        "machine_id": machine_id(),
    }
