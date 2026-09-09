from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from agent import __version__
from agent.config import Settings
from agent.hardware import host, nvml

PRIVATE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
)
IDMAPPED = re.compile(r"ID-mapped mounts supported by kernel:\s*(yes|no)", re.IGNORECASE)
SYSBOX_LOG = "/var/log/sysbox-mgr.log"
QUOTA_IMAGE = "alpine"
QUOTA_TTL_OK = 3600.0
QUOTA_TTL_FAILED = 600.0
DOCKER_INFO_TTL = 60.0

_quota_lock = threading.Lock()
_quota_cache: dict[str, Any] = {"value": None, "at": 0.0, "detail": ""}
_info_cache: dict[str, Any] = {"value": {}, "at": 0.0}


def _run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return int(done.returncode), (done.stdout or "") + (done.stderr or "")


def docker_info() -> dict[str, Any]:
    now = time.monotonic()
    if _info_cache["value"] and now - _info_cache["at"] < DOCKER_INFO_TTL:
        return dict(_info_cache["value"])
    if not shutil.which("docker"):
        return {}
    code, out = _run(["docker", "info", "--format", "{{json .}}"], 20)
    if code != 0:
        return {}
    try:
        parsed = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    if isinstance(parsed, dict):
        _info_cache["value"] = parsed
        _info_cache["at"] = now
        return dict(parsed)
    return {}


def cpu_model() -> str:
    for line in host.read_text(host.host_path("/proc/cpuinfo"), 65536).splitlines():
        if line.lower().startswith("model name") and ":" in line:
            return line.split(":", 1)[1].strip()
    binary = shutil.which("lscpu")
    if binary:
        code, out = _run([binary], 5)
        if code == 0:
            for line in out.splitlines():
                if line.lower().startswith("model name") and ":" in line:
                    return line.split(":", 1)[1].strip()
    return platform.processor() or ""


def cpu_cores() -> int:
    try:
        import psutil

        return int(psutil.cpu_count(logical=True) or 0)
    except Exception:
        return int(os.cpu_count() or 0)


def ram_mb() -> int:
    for line in host.read_text(host.host_path("/proc/meminfo"), 4096).splitlines():
        if line.startswith("MemTotal:"):
            digits = re.findall(r"\d+", line)
            if digits:
                return int(digits[0]) // 1024
    try:
        import psutil

        return int(psutil.virtual_memory().total // (1024 * 1024))
    except Exception:
        return 0


def _rotational_flag(node: Path, depth: int = 0) -> bool | None:
    if depth > 4:
        return None
    try:
        resolved = node.resolve()
    except OSError:
        return None
    slaves = resolved / "slaves"
    if slaves.is_dir():
        try:
            children = os.listdir(slaves)
        except OSError:
            children = []
        answers = [_rotational_flag(slaves / child, depth + 1) for child in children]
        known = [answer for answer in answers if answer is not None]
        if known:
            return any(known)
    for candidate in (resolved / "queue" / "rotational", resolved.parent / "queue" / "rotational"):
        text = host.read_text(candidate, 8)
        if text in ("0", "1"):
            return text == "1"
    return None


def rotational(path: str) -> bool | None:
    try:
        device = os.stat(path).st_dev
    except OSError:
        return None
    node = Path(f"/sys/dev/block/{os.major(device)}:{os.minor(device)}")
    if node.exists():
        return _rotational_flag(node)
    return None


def disk_root(info: dict[str, Any]) -> str:
    root = str(info.get("DockerRootDir", "") or "/var/lib/docker")
    candidate = host.host_path(root)
    if candidate.exists():
        return str(candidate)
    return "/"


def disk(info: dict[str, Any]) -> tuple[float, bool]:
    root = disk_root(info)
    try:
        total = shutil.disk_usage(root).total
    except OSError:
        root = "/"
        try:
            total = shutil.disk_usage(root).total
        except OSError:
            return 0.0, False
    spinning = rotational(root)
    return round(total / 1e9, 1), spinning is False


def default_address() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("1.1.1.1", 80))
            return str(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        return ""


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and not any(ip in net for net in PRIVATE)


def idmapped_mounts() -> tuple[bool | None, str]:
    binary = shutil.which("journalctl")
    if binary:
        journal = str(host.host_path("/var/log/journal"))
        for args in (
            [binary, "-u", "sysbox-mgr", "-b", "--no-pager", "-o", "cat"],
            [binary, "-D", journal, "-u", "sysbox-mgr", "-b", "--no-pager", "-o", "cat"],
        ):
            code, out = _run(args, 10)
            if code == 0:
                found = IDMAPPED.findall(out)
                if found:
                    return found[-1].lower() == "yes", "journal"
    text = host.read_text(host.host_path(SYSBOX_LOG), 1024 * 1024)
    found = IDMAPPED.findall(text)
    if found:
        return found[-1].lower() == "yes", "sysbox-mgr.log"
    return None, "unknown"


def isolation(info: dict[str, Any]) -> dict[str, Any]:
    runtimes = info.get("Runtimes") or {}
    sysbox = "sysbox-runc" in runtimes if isinstance(runtimes, dict) else False
    idmapped, source = idmapped_mounts()
    if idmapped is None:
        idmapped = sysbox and host.kernel_version_tuple() >= (5, 19)
        source = "kernel-version"
    return {"sysbox": sysbox, "idmapped": bool(idmapped), "idmapped_source": source}


def storage_quota() -> bool:
    now = time.monotonic()
    with _quota_lock:
        value = _quota_cache["value"]
        age = now - float(_quota_cache["at"])
        if value is True and age < QUOTA_TTL_OK:
            return True
        if value is False and age < QUOTA_TTL_FAILED:
            return False
        if not shutil.which("docker"):
            _quota_cache.update(value=False, at=now, detail="docker cli missing")
            return False
        code, out = _run(
            ["docker", "run", "--rm", "--storage-opt", "size=1g", QUOTA_IMAGE, "true"], 30
        )
        _quota_cache.update(value=code == 0, at=now, detail=out.strip()[-300:])
        return code == 0


def storage_quota_detail() -> str:
    return str(_quota_cache.get("detail", ""))


def hostname() -> str:
    return host.read_text(host.host_path("/etc/hostname"), 256) or socket.gethostname()


def build(settings: Settings) -> dict[str, Any]:
    info = docker_info()
    release = host.os_release()
    cards = nvml.inventory()
    disk_gb, ssd = disk(info)
    address = settings.public_address or default_address()
    return {
        "gpus": [
            {
                "uuid": card["uuid"],
                "model": card["model"],
                "memory_mb": card["memory_mb"],
                "index": card["index"],
                "serial": card["serial"],
                "pci_bus_id": card["pci_bus_id"],
                "mig_mode": card["mig_mode"],
                "virtualization": card["virtualization"],
            }
            for card in cards["gpus"]
        ],
        "cpu_model": cpu_model(),
        "cpu_cores": cpu_cores(),
        "ram_mb": ram_mb(),
        "disk_gb": disk_gb,
        "disk_ssd": ssd,
        "os": release.get("ID", "").lower() or platform.system().lower(),
        "os_version": release.get("VERSION_ID", ""),
        "kernel": host.kernel_release(),
        "arch": platform.machine(),
        "driver": cards["driver"],
        "cuda": cards["cuda"],
        "isolation": isolation(info),
        "storage_quota": storage_quota(),
        "hostname": hostname(),
        "port_range": settings.port_range,
        "address": address,
        "public_ipv4": is_public(address),
        "docker": str(info.get("ServerVersion", "") or ""),
        "storage_driver": str(info.get("Driver", "") or ""),
        "nvml_error": cards["nvml_error"],
        "agent_version": __version__,
        "host": host.summary(),
    }
