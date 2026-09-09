from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from protocol.session import well_formed_digest
from validator.data.tiers import WorkKind
from validator.recovery import gpu_wedge
from validator.session.runner import Runner
from validator.session.ssh import SshError
from validator.storage import keys, volumes
from validator.work import power

OWNER_LABEL = "mt.owner=validator"
NAME_PREFIX = "mt-"
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$")
PULL_TIMEOUT = 3 * 3600.0
PULL_LOCK = "/tmp/mt-agent-pull.lock"
COMMAND_TIMEOUT = 60.0
RUN_TIMEOUT = 300.0
PORT_RETRY_SECONDS = 90.0
PORT_RETRY_INTERVAL = 5.0
RUNNING_POLL_SECONDS = 10.0
RUNNING_POLL_INTERVAL = 1.0
STOP_GRACE_CUSTOMER = 30
STOP_GRACE_FILLER = 15
CANCEL_WAIT_SECONDS = 30.0
LOG_TAIL_LINES = 100
LOG_TAIL_CHARS = 4000
MAX_GPUS = 14
PORT_BUSY_MARKERS = (
    "port is already allocated",
    "address already in use",
    "failed to bind host port",
    "bind for",
)
DEVICE_NODES = ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset")
HOST_WIDE_DIRS = ("/dev/nvidia-caps", "/dev/infiniband")
DEVICE_PROBE = (
    "nvidia-smi --query-gpu=uuid,pci.bus_id --format=csv,noheader; echo ---; "
    "grep -H 'Device Minor' /proc/driver/nvidia/gpus/*/information 2>/dev/null; echo ---; "
    "ls -1 /dev/nvidia* /dev/nvidia-caps/* /dev/infiniband/* 2>/dev/null"
)


@dataclass(frozen=True)
class PortMapping:
    host: int
    container: int
    protocol: str = "tcp"

    def payload(self) -> dict[str, Any]:
        return {"host": self.host, "container": self.container, "protocol": self.protocol}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PortMapping:
        return cls(
            int(payload.get("host", 0) or 0),
            int(payload.get("container", 0) or 0),
            str(payload.get("protocol", "tcp") or "tcp"),
        )

    @property
    def token(self) -> str:
        suffix = "" if self.protocol == "tcp" else f"/{self.protocol}"
        return f"{self.host}:{self.container}{suffix}"


@dataclass
class JobSpec:
    job_id: str
    kind: str
    image: str
    gpu_uuids: list[str] = field(default_factory=list)
    owner: str = ""
    memory_gb: float = 0.0
    cpus: float = 0.0
    storage_gb: float = 0.0
    shm_size: str = "1g"
    ports: list[PortMapping] = field(default_factory=list)
    container_ports: list[int] = field(default_factory=list)
    port_range: str = ""
    env: dict[str, str] = field(default_factory=dict)
    command: str = ""
    entrypoint: str = ""
    volumes: list[volumes.VolumeSpec] = field(default_factory=list)
    whole_node: bool = False
    devices: list[str] = field(default_factory=list)
    isolation: bool = False
    power_caps: dict[str, float] = field(default_factory=dict)
    quotas_supported: bool = False
    workspace_user: str = ""

    @property
    def container_name(self) -> str:
        return f"{NAME_PREFIX}{self.job_id}"

    @property
    def sensitive(self) -> bool:
        return self.kind == WorkKind.RENTAL.value

    @property
    def stop_grace(self) -> int:
        return (
            STOP_GRACE_CUSTOMER
            if self.kind in (WorkKind.RENTAL.value, WorkKind.INFERENCE.value)
            else STOP_GRACE_FILLER
        )

    def payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "image": self.image,
            "gpu_uuids": list(self.gpu_uuids),
            "owner": self.owner,
            "memory_gb": self.memory_gb,
            "cpus": self.cpus,
            "storage_gb": self.storage_gb,
            "shm_size": self.shm_size,
            "ports": [p.payload() for p in self.ports],
            "container_ports": list(self.container_ports),
            "port_range": self.port_range,
            "env": dict(self.env),
            "command": self.command,
            "entrypoint": self.entrypoint,
            "volumes": [v.payload() for v in self.volumes],
            "whole_node": self.whole_node,
            "devices": list(self.devices),
            "isolation": self.isolation,
            "power_caps": dict(self.power_caps),
            "quotas_supported": self.quotas_supported,
            "workspace_user": self.workspace_user,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> JobSpec:
        return cls(
            job_id=str(payload.get("job_id", "")),
            kind=str(payload.get("kind", "")),
            image=str(payload.get("image", "")),
            gpu_uuids=[str(u) for u in payload.get("gpu_uuids") or []],
            owner=str(payload.get("owner", "") or ""),
            memory_gb=float(payload.get("memory_gb", 0.0) or 0.0),
            cpus=float(payload.get("cpus", 0.0) or 0.0),
            storage_gb=float(payload.get("storage_gb", 0.0) or 0.0),
            shm_size=str(payload.get("shm_size", "1g") or "1g"),
            ports=[PortMapping.from_payload(p) for p in payload.get("ports") or []],
            container_ports=[int(p) for p in payload.get("container_ports") or []],
            port_range=str(payload.get("port_range", "") or ""),
            env={str(k): str(v) for k, v in (payload.get("env") or {}).items()},
            command=str(payload.get("command", "") or ""),
            entrypoint=str(payload.get("entrypoint", "") or ""),
            volumes=[volumes.VolumeSpec.from_payload(v) for v in payload.get("volumes") or []],
            whole_node=bool(payload.get("whole_node", False)),
            devices=[str(d) for d in payload.get("devices") or []],
            isolation=bool(payload.get("isolation", False)),
            power_caps={str(k): float(v) for k, v in (payload.get("power_caps") or {}).items()},
            quotas_supported=bool(payload.get("quotas_supported", False)),
            workspace_user=str(payload.get("workspace_user", "") or ""),
        )

    def problems(self, allowlist: tuple[str, ...]) -> list[str]:
        found: list[str] = []
        if not JOB_ID.match(self.job_id or ""):
            found.append(f"job id {self.job_id!r} is not accepted")
        if self.kind not in {kind.value for kind in WorkKind}:
            found.append(f"unknown work kind {self.kind!r}")
        found.extend(image_problems(self.image, allowlist))
        if not self.gpu_uuids:
            found.append("no GPU assigned")
        if len(self.gpu_uuids) > MAX_GPUS:
            found.append(f"more than {MAX_GPUS} GPUs assigned")
        if any(not re.match(r"^GPU-[0-9a-f-]{36}$", uuid) for uuid in self.gpu_uuids):
            found.append("a GPU uuid is malformed")
        if self.sensitive and not self.isolation:
            found.append("rental requires the isolation runtime")
        for key in self.env:
            if not ENV_KEY.match(key):
                found.append(f"environment key {key!r} is not accepted")
        if not re.match(r"^[0-9]+[kmgKMG]?$", self.shm_size or ""):
            found.append(f"shm size {self.shm_size!r} is not accepted")
        for volume in self.volumes:
            found.extend(volume.problems())
        if self.storage_gb > 0 and not self.quotas_supported:
            found.append("storage quota requested on a host whose driver cannot enforce it")
        return found


def image_problems(image: str, allowlist: tuple[str, ...]) -> list[str]:
    if not IMAGE_REF.match(image or "") or not well_formed_digest(image.rsplit("@", 1)[-1]):
        return [f"image {image!r} must be pinned by sha256 digest"]
    if image not in allowlist:
        return [f"image {image} is not on the allowlist"]
    return []


def user_tokens(text: str) -> list[str] | None:
    if not text.strip():
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return None


def run_command(spec: JobSpec) -> str:
    parts: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        spec.container_name,
        "--restart",
        "unless-stopped",
    ]
    if spec.isolation:
        parts += ["--runtime", "sysbox-runc"]
    parts += ["--gpus", '"device=' + ",".join(spec.gpu_uuids) + '"']
    for device in spec.devices:
        parts += ["--device", f"{device}:{device}:rwm"]
    parts += ["--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.conf.all.src_valid_mark=1"]
    if spec.memory_gb > 0:
        parts += ["--memory", f"{int(spec.memory_gb * 1024)}m"]
    if spec.cpus > 0:
        parts += ["--cpus", f"{spec.cpus:g}"]
    if spec.storage_gb > 0 and spec.quotas_supported:
        parts += ["--storage-opt", f"size={int(spec.storage_gb)}G"]
    parts += ["--shm-size", spec.shm_size]
    for mapping in spec.ports:
        parts += ["-p", mapping.token]
    parts += [
        "--label",
        f"mt.job={spec.job_id}",
        "--label",
        f"mt.kind={spec.kind}",
        "--label",
        OWNER_LABEL,
    ]
    if spec.owner:
        parts += ["--label", f"mt.tenant={spec.owner}"]
    parts += ["-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
    for key, value in sorted(spec.env.items()):
        parts += ["-e", f"{key}={value}"]
    encrypted = False
    for volume in spec.volumes:
        parts += volumes.mount_args(volume)
        encrypted = encrypted or volume.encrypted
    if encrypted and not spec.isolation:
        parts += ["--cap-add", "SYS_ADMIN", "--security-opt", "apparmor=unconfined"]
    entrypoint = user_tokens(spec.entrypoint)
    if entrypoint:
        parts += ["--entrypoint", entrypoint[0]]
    parts.append(spec.image)
    if entrypoint and len(entrypoint) > 1:
        parts += entrypoint[1:]
    command = user_tokens(spec.command)
    if command:
        parts += command
    return " ".join(shlex.quote(part) for part in parts)


@dataclass
class CreateResult:
    ok: bool
    step: str = ""
    reason: str = ""
    container: str = ""
    ports: list[PortMapping] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "step": self.step,
            "reason": self.reason,
            "container": self.container,
            "ports": [p.payload() for p in self.ports],
            "diagnostics": dict(self.diagnostics),
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


@dataclass
class DestroyResult:
    ok: bool
    reason: str = ""
    retry_later: bool = False
    steps: dict[str, Any] = field(default_factory=dict)


class Cancelled(RuntimeError):
    pass


class Inflight:
    def __init__(self) -> None:
        self._cancelled: set[str] = set()
        self._done: dict[str, asyncio.Event] = {}

    def start(self, job_id: str) -> None:
        self._cancelled.discard(job_id)
        self._done[job_id] = asyncio.Event()

    def finish(self, job_id: str) -> None:
        event = self._done.pop(job_id, None)
        if event is not None:
            event.set()
        self._cancelled.discard(job_id)

    def active(self, job_id: str) -> bool:
        return job_id in self._done

    def cancel(self, job_id: str) -> bool:
        if job_id not in self._done:
            return False
        self._cancelled.add(job_id)
        return True

    def cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    async def wait(self, job_id: str, timeout: float = CANCEL_WAIT_SECONDS) -> bool:
        event = self._done.get(job_id)
        if event is None:
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _file(self, job_id: str) -> Path:
        if not JOB_ID.match(job_id or ""):
            raise ValueError(f"job id {job_id!r} is not accepted")
        return self.path / f"{job_id}.json"

    def save(self, spec: JobSpec, rig_id: str = "") -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        target = self._file(spec.job_id)
        temp = target.with_suffix(".tmp")
        body = {"rig_id": rig_id, "saved_at": time.time(), "spec": spec.payload()}
        temp.write_text(json.dumps(body, indent=1), encoding="utf-8")
        os.replace(temp, target)

    def load(self, job_id: str) -> tuple[JobSpec, str] | None:
        target = self._file(job_id)
        if not target.exists():
            return None
        try:
            body = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return JobSpec.from_payload(body.get("spec") or {}), str(body.get("rig_id", "") or "")

    def forget(self, job_id: str) -> None:
        try:
            self._file(job_id).unlink()
        except (OSError, ValueError):
            return

    def all(self) -> list[tuple[JobSpec, str]]:
        if not self.path.exists():
            return []
        found: list[tuple[JobSpec, str]] = []
        for entry in sorted(self.path.glob("*.json")):
            loaded = self.load(entry.stem)
            if loaded is not None:
                found.append(loaded)
        return found


def parse_range(text: str) -> tuple[int, int] | None:
    match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", text or "")
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if low < 1 or high > 65535 or low > high:
        return None
    return low, high


async def used_ports(runner: Runner) -> set[int]:
    result = await runner.run(
        "ss -Hltn 2>/dev/null || netstat -ltn 2>/dev/null", timeout=COMMAND_TIMEOUT
    )
    found: set[int] = set()
    for line in result.stdout.splitlines():
        for token in line.split():
            match = re.match(r"^.*:(\d+)$", token)
            if match:
                found.add(int(match.group(1)))
    return found


async def reserve_ports(runner: Runner, spec: JobSpec) -> tuple[list[PortMapping], str]:
    if spec.ports or not spec.container_ports:
        return list(spec.ports), ""
    span = parse_range(spec.port_range)
    if span is None:
        return [], f"port range {spec.port_range!r} is not usable"
    busy = await used_ports(runner)
    mappings: list[PortMapping] = []
    candidate = span[0]
    for container_port in spec.container_ports:
        while candidate <= span[1] and candidate in busy:
            candidate += 1
        if candidate > span[1]:
            return [], "no free port left in the reserved range"
        mappings.append(PortMapping(candidate, int(container_port)))
        candidate += 1
    return mappings, ""


def normalise_bus(text: str) -> str:
    return text.strip().lower()[-12:]


def parse_device_probe(stdout: str) -> tuple[dict[str, str], dict[str, int], list[str]]:
    sections = stdout.split("---")
    bus_by_uuid: dict[str, str] = {}
    minor_by_bus: dict[str, int] = {}
    nodes: list[str] = []
    for line in sections[0].splitlines() if sections else []:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[0].startswith("GPU-"):
            bus_by_uuid[parts[0]] = normalise_bus(parts[1])
    for line in sections[1].splitlines() if len(sections) > 1 else []:
        match = re.match(
            r"^/proc/driver/nvidia/gpus/([^/]+)/information:\s*Device Minor:\s*(\d+)", line.strip()
        )
        if match:
            minor_by_bus[normalise_bus(match.group(1))] = int(match.group(2))
    for line in sections[2].splitlines() if len(sections) > 2 else []:
        clean = line.strip()
        if clean.startswith("/dev/"):
            nodes.append(clean)
    return bus_by_uuid, minor_by_bus, nodes


async def device_nodes(runner: Runner, uuids: list[str], whole_node: bool) -> tuple[list[str], str]:
    result = await runner.run(DEVICE_PROBE, timeout=COMMAND_TIMEOUT)
    if result.transport_failed:
        return [], result.error
    bus_by_uuid, minor_by_bus, nodes = parse_device_probe(result.stdout)
    present = set(nodes)
    chosen: list[str] = []
    for uuid in uuids:
        bus = bus_by_uuid.get(uuid)
        minor = minor_by_bus.get(bus or "")
        if bus is None or minor is None:
            return [], f"no device node resolved for {uuid}"
        node = f"/dev/nvidia{minor}"
        if node not in present:
            return [], f"{node} for {uuid} is absent"
        chosen.append(node)
    chosen.extend(node for node in DEVICE_NODES if node in present)
    if whole_node:
        chosen.extend(
            node
            for node in nodes
            if any(node.startswith(prefix + "/") for prefix in HOST_WIDE_DIRS)
        )
    return chosen, ""


async def state_of(runner: Runner, name: str) -> dict[str, Any] | None:
    result = await runner.run(
        f"docker inspect --format '{{{{json .State}}}}' {shlex.quote(name)}",
        timeout=COMMAND_TIMEOUT,
    )
    if not result.ok:
        return None
    try:
        parsed = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def running(runner: Runner, name: str) -> bool | None:
    state = await state_of(runner, name)
    if state is None:
        return None
    return bool(state.get("Running"))


async def diagnostics(runner: Runner, name: str) -> dict[str, Any]:
    state = await state_of(runner, name) or {}
    logs = await runner.run(
        f"docker logs --tail {LOG_TAIL_LINES} {shlex.quote(name)} 2>&1", timeout=COMMAND_TIMEOUT
    )
    return {
        "status": state.get("Status"),
        "exit_code": state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "error": state.get("Error"),
        "logs": (logs.stdout or "")[-LOG_TAIL_CHARS:],
    }


async def wait_running(runner: Runner, name: str) -> tuple[bool, str]:
    deadline = time.monotonic() + RUNNING_POLL_SECONDS
    last = ""
    while time.monotonic() < deadline:
        state = await state_of(runner, name)
        if state is not None:
            if state.get("Running"):
                return True, ""
            status = str(state.get("Status", "") or "")
            last = status
            if status in ("exited", "dead"):
                return False, f"container {status} with code {state.get('ExitCode')}"
        await asyncio.sleep(RUNNING_POLL_INTERVAL)
    return False, f"container not running after {RUNNING_POLL_SECONDS:.0f} s ({last or 'no state'})"


async def docker_run(runner: Runner, spec: JobSpec) -> tuple[bool, str]:
    command = run_command(spec)
    deadline = time.monotonic() + PORT_RETRY_SECONDS
    while True:
        result = await runner.run(command, timeout=RUN_TIMEOUT)
        if result.ok:
            return True, ""
        if result.transport_failed:
            return False, result.error
        stderr = result.stderr.lower()
        if any(marker in stderr for marker in PORT_BUSY_MARKERS) and time.monotonic() < deadline:
            await runner.run(
                f"docker rm -f {shlex.quote(spec.container_name)}", timeout=COMMAND_TIMEOUT
            )
            await asyncio.sleep(PORT_RETRY_INTERVAL)
            continue
        return False, result.stderr_tail(600).strip() or result.error


async def remove_probe_containers(runner: Runner) -> None:
    await runner.run(
        "docker ps -aq --filter label=mt.kind=probe | xargs -r docker rm -f",
        timeout=COMMAND_TIMEOUT,
    )


def _checkpoint(inflight: Inflight | None, job_id: str) -> None:
    if inflight is not None and inflight.cancelled(job_id):
        raise Cancelled(f"job {job_id} was cancelled during creation")


async def create(
    runner: Runner,
    spec: JobSpec,
    allowlist: tuple[str, ...],
    records: power.PowerRecords | None = None,
    store: JobStore | None = None,
    inflight: Inflight | None = None,
    rig_id: str = "",
) -> CreateResult:
    started = time.monotonic()
    name = spec.container_name
    step = "validate"
    created_volumes: list[str] = []
    capped = False
    launched = False
    succeeded = False
    if inflight is not None:
        inflight.start(spec.job_id)
    try:
        problems = spec.problems(allowlist)
        if problems:
            return CreateResult(False, step, problems[0])
        secret = b""
        if any(v.encrypted for v in spec.volumes):
            try:
                secret = keys.master_secret()
            except ValueError as exc:
                return CreateResult(False, step, str(exc))
        _checkpoint(inflight, spec.job_id)
        step = "reserve ports"
        ports, reason = await reserve_ports(runner, spec)
        if reason:
            return CreateResult(False, step, reason)
        spec.ports = ports
        step = "pull"
        pull = await runner.run(
            f"flock -w 1800 {shlex.quote(PULL_LOCK)} docker pull {shlex.quote(spec.image)} 2>&1 || docker pull {shlex.quote(spec.image)} 2>&1",
            timeout=PULL_TIMEOUT,
        )
        if not pull.ok:
            return CreateResult(False, step, (pull.error or pull.stdout[-600:]).strip())
        _checkpoint(inflight, spec.job_id)
        step = "cleanup"
        await runner.run(f"docker rm -f {shlex.quote(name)} 2>/dev/null", timeout=COMMAND_TIMEOUT)
        await remove_stale_volumes(runner)
        step = "volumes"
        for volume in spec.volumes:
            if not volume.create:
                continue
            ok, reason = await volumes.create(runner, volume)
            if not ok:
                return CreateResult(False, step, f"{volume.name}: {reason}")
            created_volumes.append(volume.name)
        step = "devices"
        devices, reason = await device_nodes(runner, spec.gpu_uuids, spec.whole_node)
        if reason:
            return CreateResult(False, step, reason)
        spec.devices = devices
        step = "power"
        if spec.power_caps:
            if records is None:
                return CreateResult(False, step, "power caps requested without a records file")
            ok, reason = await power.apply_caps(runner, records, spec.job_id, spec.power_caps)
            if not ok:
                return CreateResult(False, step, reason)
            capped = True
        await remove_probe_containers(runner)
        _checkpoint(inflight, spec.job_id)
        step = "run"
        ok, reason = await docker_run(runner, spec)
        if not ok:
            return CreateResult(False, step, reason)
        launched = True
        step = "running"
        ok, reason = await wait_running(runner, name)
        if not ok:
            return CreateResult(
                False, step, reason, container=name, diagnostics=await diagnostics(runner, name)
            )
        step = "encrypted volumes"
        for volume in spec.volumes:
            if not volume.encrypted:
                continue
            ok, reason = await volumes.setup_encrypted(
                runner, name, volume, secret, fresh=volume.create
            )
            if not ok:
                return CreateResult(
                    False,
                    step,
                    f"{volume.name}: {reason}",
                    container=name,
                    diagnostics=await diagnostics(runner, name),
                )
            if spec.workspace_user:
                await volumes.grant_workspace(runner, name, volume.mount_path, spec.workspace_user)
        _checkpoint(inflight, spec.job_id)
        step = "record"
        if store is not None:
            store.save(spec, rig_id)
        succeeded = True
        return CreateResult(
            True,
            "done",
            container=name,
            ports=list(spec.ports),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
    except Cancelled as exc:
        return CreateResult(False, "cancelled", str(exc), container=name if launched else "")
    except SshError as exc:
        return CreateResult(False, step, exc.detail, container=name if launched else "")
    except OSError as exc:
        return CreateResult(
            False, step, f"{type(exc).__name__}: {exc}", container=name if launched else ""
        )
    finally:
        cancelled = inflight is not None and inflight.cancelled(spec.job_id)
        if (not succeeded or cancelled) and (launched or capped or created_volumes):
            await _rollback(runner, spec, name, launched, capped, created_volumes, records)
            if store is not None and cancelled:
                store.forget(spec.job_id)
        if inflight is not None:
            inflight.finish(spec.job_id)


async def _rollback(
    runner: Runner,
    spec: JobSpec,
    name: str,
    launched: bool,
    capped: bool,
    created_volumes: list[str],
    records: power.PowerRecords | None,
) -> None:
    if launched:
        await runner.run(f"docker rm -f -v {shlex.quote(name)}", timeout=COMMAND_TIMEOUT)
    if capped and records is not None:
        await power.restore_job(runner, records, spec.job_id)
    for volume in created_volumes:
        await volumes.destroy(runner, volume)
    await gpu_wedge.sweep(runner)


async def remove_stale_volumes(runner: Runner) -> list[str]:
    ours = await volumes.list_ours(runner)
    mounted = await volumes.mounted_volumes(runner)
    if mounted is None:
        return []
    removed: list[str] = []
    for name in ours:
        if name in mounted:
            continue
        ok, _reason = await volumes.destroy(runner, name)
        if ok:
            removed.append(name)
    return removed


async def destroy(
    runner: Runner,
    name: str,
    *,
    grace: int = STOP_GRACE_CUSTOMER,
    job_id: str = "",
    records: power.PowerRecords | None = None,
    volume_names: tuple[str, ...] = (),
    store: JobStore | None = None,
    prune_images: bool = True,
) -> DestroyResult:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$", name or ""):
        return DestroyResult(False, f"container name {name!r} is not accepted")
    quoted = shlex.quote(name)
    steps: dict[str, Any] = {}
    stop = await runner.run(f"docker stop -t {int(grace)} {quoted}", timeout=grace + 60.0)
    steps["stop"] = stop.ok or stop.stderr_tail(200).strip()
    if stop.transport_failed:
        return DestroyResult(False, stop.error, retry_later=True, steps=steps)
    removed = await runner.run(f"docker rm -f -v {quoted}", timeout=120.0)
    stderr = removed.stderr.lower()
    if removed.ok or "no such container" in stderr:
        steps["remove"] = True
    else:
        steps["remove"] = removed.stderr_tail(300).strip() or removed.error
        await gpu_wedge.sweep(runner)
        retry = "removal" in stderr and "in progress" in stderr
        return DestroyResult(
            False, steps["remove"], retry_later=retry or removed.transport_failed, steps=steps
        )
    if records is not None and job_id:
        steps["power"] = await power.restore_job(runner, records, job_id)
    steps["gpu_wedge"] = (await gpu_wedge.sweep(runner)).payload()
    if prune_images:
        pruned = await runner.run("docker image prune -f", timeout=COMMAND_TIMEOUT)
        steps["image_prune"] = pruned.ok
    volume_steps: dict[str, Any] = {}
    for volume in volume_names:
        ok, reason = await volumes.destroy(runner, volume)
        volume_steps[volume] = ok or reason
    steps["volumes"] = volume_steps
    if store is not None and job_id:
        store.forget(job_id)
    return DestroyResult(True, steps=steps)


async def recreate(
    runner: Runner,
    spec: JobSpec,
    allowlist: tuple[str, ...],
    records: power.PowerRecords | None = None,
    store: JobStore | None = None,
    rig_id: str = "",
) -> CreateResult:
    for volume in spec.volumes:
        volume.create = False
    spec.devices = []
    torn = await destroy(
        runner,
        spec.container_name,
        grace=spec.stop_grace,
        job_id="",
        records=None,
        prune_images=False,
    )
    if not torn.ok:
        return CreateResult(False, "destroy", torn.reason, container=spec.container_name)
    return await create(runner, spec, allowlist, records, store, rig_id=rig_id)
