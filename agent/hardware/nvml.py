from __future__ import annotations

import contextlib
import os
import shutil
import socket
import time
from collections.abc import Iterator
from typing import Any

MIB = 1024 * 1024
LIBRARY_MISSING = "NVML_ERROR_LIBRARY_NOT_FOUND"
VIRTUALIZATION = {0: "none", 1: "passthrough", 2: "vgpu", 3: "host_vgpu", 4: "host_vsga"}


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def error_name(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        return LIBRARY_MISSING
    value = getattr(exc, "value", None)
    if value is None:
        return f"NVML_ERROR_{type(exc).__name__.upper()}"
    try:
        import pynvml
    except ImportError:
        return LIBRARY_MISSING
    for name in dir(pynvml):
        if name.startswith("NVML_ERROR_") and getattr(pynvml, name, None) == value:
            return name
    return f"NVML_ERROR_{value}"


@contextlib.contextmanager
def session() -> Iterator[Any]:
    import pynvml

    pynvml.nvmlInit()
    try:
        yield pynvml
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def _cuda_text(raw: int) -> str:
    return f"{raw // 1000}.{(raw % 1000) // 10}"


def _driver(nvml: Any) -> tuple[str, str]:
    driver = ""
    cuda = ""
    with contextlib.suppress(Exception):
        driver = _text(nvml.nvmlSystemGetDriverVersion())
    with contextlib.suppress(Exception):
        cuda = _cuda_text(int(nvml.nvmlSystemGetCudaDriverVersion_v2()))
    if not cuda:
        with contextlib.suppress(Exception):
            cuda = _cuda_text(int(nvml.nvmlSystemGetCudaDriverVersion()))
    return driver, cuda


def _optional(call: Any, *args: Any) -> Any:
    try:
        return call(*args)
    except Exception:
        return None


def _mig_mode(nvml: Any, handle: Any) -> str:
    modes = _optional(nvml.nvmlDeviceGetMigMode, handle)
    if not modes:
        return ""
    current = modes[0] if isinstance(modes, tuple | list) else modes
    return (
        "enabled" if int(current) == int(getattr(nvml, "NVML_DEVICE_MIG_ENABLE", 1)) else "disabled"
    )


def _virtualization(nvml: Any, handle: Any) -> str:
    mode = _optional(nvml.nvmlDeviceGetVirtualizationMode, handle)
    if mode is None:
        return ""
    return VIRTUALIZATION.get(int(mode), f"mode_{int(mode)}")


def _card(nvml: Any, index: int, driver: str, cuda: str) -> dict[str, Any]:
    handle = nvml.nvmlDeviceGetHandleByIndex(index)
    memory = nvml.nvmlDeviceGetMemoryInfo(handle)
    pci = _optional(nvml.nvmlDeviceGetPciInfo, handle)
    return {
        "uuid": _text(nvml.nvmlDeviceGetUUID(handle)),
        "model": _text(nvml.nvmlDeviceGetName(handle)),
        "memory_mb": int(memory.total // MIB),
        "index": index,
        "serial": _text(_optional(nvml.nvmlDeviceGetSerial, handle) or ""),
        "pci_bus_id": _text(getattr(pci, "busId", "") or "") if pci is not None else "",
        "mig_mode": _mig_mode(nvml, handle),
        "virtualization": _virtualization(nvml, handle),
        "driver": driver,
        "cuda": cuda,
    }


def inventory() -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    driver = ""
    cuda = ""
    error = ""
    try:
        with session() as nvml:
            driver, cuda = _driver(nvml)
            for index in range(int(nvml.nvmlDeviceGetCount())):
                cards.append(_card(nvml, index, driver, cuda))
    except Exception as exc:
        error = error_name(exc)
    return {"gpus": cards, "driver": driver, "cuda": cuda, "nvml_error": error}


def gpus() -> list[dict[str, Any]]:
    return list(inventory()["gpus"])


def driver() -> dict[str, str]:
    view = inventory()
    return {"driver": view["driver"], "cuda": view["cuda"], "nvml_error": view["nvml_error"]}


def _processes(nvml: Any, handle: Any) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for name, kind in (
        ("nvmlDeviceGetComputeRunningProcesses", "compute"),
        ("nvmlDeviceGetGraphicsRunningProcesses", "graphics"),
    ):
        call = getattr(nvml, name, None)
        if call is None:
            continue
        for process in _optional(call, handle) or []:
            pid = int(getattr(process, "pid", 0) or 0)
            used = getattr(process, "usedGpuMemory", None)
            entry = seen.setdefault(pid, {"pid": pid, "memory_mb": 0, "kind": kind})
            if isinstance(used, int) and used > 0:
                entry["memory_mb"] = int(used // MIB)
    return list(seen.values())


def _card_utilisation(nvml: Any, index: int) -> dict[str, Any]:
    handle = nvml.nvmlDeviceGetHandleByIndex(index)
    rates = _optional(nvml.nvmlDeviceGetUtilizationRates, handle)
    memory = nvml.nvmlDeviceGetMemoryInfo(handle)
    power = _optional(nvml.nvmlDeviceGetPowerUsage, handle)
    temperature = _optional(nvml.nvmlDeviceGetTemperature, handle, nvml.NVML_TEMPERATURE_GPU)
    total = int(memory.total // MIB)
    used = int(memory.used // MIB)
    return {
        "uuid": _text(nvml.nvmlDeviceGetUUID(handle)),
        "index": index,
        "gpu_percent": int(getattr(rates, "gpu", 0) or 0) if rates is not None else 0,
        "memory_percent": round(100.0 * used / total, 1) if total else 0.0,
        "memory_used_mb": used,
        "memory_total_mb": total,
        "power_w": round(int(power) / 1000.0, 1) if power is not None else None,
        "temperature_c": int(temperature) if temperature is not None else None,
        "processes": _processes(nvml, handle),
    }


def host_load() -> dict[str, Any]:
    view: dict[str, Any] = {"cpu_percent": None, "ram_used_mb": None, "ram_total_mb": None}
    try:
        import psutil

        memory = psutil.virtual_memory()
        view["cpu_percent"] = psutil.cpu_percent(interval=None)
        view["ram_used_mb"] = int(memory.used // MIB)
        view["ram_total_mb"] = int(memory.total // MIB)
    except Exception:
        pass
    try:
        disk = shutil.disk_usage("/")
        view["disk_used_gb"] = round(disk.used / 1e9, 1)
        view["disk_total_gb"] = round(disk.total / 1e9, 1)
    except OSError:
        view["disk_used_gb"] = None
        view["disk_total_gb"] = None
    view["load"] = (
        [round(value, 2) for value in os.getloadavg()] if hasattr(os, "getloadavg") else []
    )
    return view


def utilisation() -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    error = ""
    try:
        with session() as nvml:
            for index in range(int(nvml.nvmlDeviceGetCount())):
                cards.append(_card_utilisation(nvml, index))
    except Exception as exc:
        error = error_name(exc)
    view = {
        "gpus": cards,
        "nvml_error": error,
        "at": round(time.time(), 3),
        "hostname": socket.gethostname(),
    }
    view.update(host_load())
    return view


def processes() -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    error = ""
    try:
        with session() as nvml:
            for index in range(int(nvml.nvmlDeviceGetCount())):
                handle = nvml.nvmlDeviceGetHandleByIndex(index)
                uuid = _text(nvml.nvmlDeviceGetUUID(handle))
                for process in _processes(nvml, handle):
                    found.append({"gpu_uuid": uuid, "index": index, **process})
    except Exception as exc:
        error = error_name(exc)
    return {"processes": found, "nvml_error": error}
