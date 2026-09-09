from __future__ import annotations

import json
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from validator.session.runner import Runner

NVIDIA_SMI_TIMEOUT = 30.0
MIN_RATIO = 0.9
STALE_GRACE_SECONDS = 1800.0
QUERY = "nvidia-smi -i {uuid} --query-gpu=power.limit,power.default_limit,power.min_limit,power.max_limit,persistence_mode --format=csv,noheader,nounits"


@dataclass(frozen=True)
class PowerState:
    limit_w: float
    default_w: float
    min_w: float
    max_w: float
    persistence: bool


class PowerRecords:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.gpus: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.gpus = dict(data.get("gpus") or {})
        self.jobs = {k: list(v) for k, v in (data.get("jobs") or {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"gpus": self.gpus, "jobs": self.jobs}, indent=1), encoding="utf-8")
        os.replace(temp, self.path)

    def remember_original(self, uuid: str, original_w: float, job_id: str) -> None:
        if uuid not in self.gpus:
            self.gpus[uuid] = {"original_w": original_w, "recorded_at": time.time(), "job": job_id}
        self.jobs.setdefault(job_id, [])
        if uuid not in self.jobs[job_id]:
            self.jobs[job_id].append(uuid)
        self.save()

    def original(self, uuid: str) -> float | None:
        record = self.gpus.get(uuid)
        return float(record["original_w"]) if record else None

    def forget(self, uuid: str, job_id: str | None = None) -> None:
        self.gpus.pop(uuid, None)
        if job_id is not None:
            remaining = [u for u in self.jobs.get(job_id, []) if u != uuid]
            if remaining:
                self.jobs[job_id] = remaining
            else:
                self.jobs.pop(job_id, None)
        self.save()

    def stale(self, now: float | None = None) -> list[str]:
        moment = time.time() if now is None else now
        return [
            uuid
            for uuid, record in self.gpus.items()
            if moment - float(record.get("recorded_at", moment)) > STALE_GRACE_SECONDS
        ]


async def read_state(runner: Runner, uuid: str) -> PowerState | None:
    result = await runner.run(QUERY.format(uuid=shlex.quote(uuid)), timeout=NVIDIA_SMI_TIMEOUT)
    if not result.ok:
        return None
    parts = [part.strip() for part in result.stdout.strip().splitlines()[-1].split(",")] if result.stdout.strip() else []
    if len(parts) != 5:
        return None
    try:
        return PowerState(
            limit_w=float(parts[0]),
            default_w=float(parts[1]),
            min_w=float(parts[2]),
            max_w=float(parts[3]),
            persistence=parts[4].lower().startswith("enabled"),
        )
    except ValueError:
        return None


async def set_limit(runner: Runner, uuid: str, watts: float) -> PowerState | None:
    quoted = shlex.quote(uuid)
    await runner.run(f"nvidia-smi -i {quoted} -pm 1", timeout=NVIDIA_SMI_TIMEOUT)
    result = await runner.run(f"nvidia-smi -i {quoted} -pl {int(round(watts))}", timeout=NVIDIA_SMI_TIMEOUT)
    if not result.ok:
        return None
    state = await read_state(runner, uuid)
    if state is None or abs(state.limit_w - watts) > 1.0:
        return None
    return state


async def apply_caps(runner: Runner, records: PowerRecords, job_id: str, caps: dict[str, float]) -> tuple[bool, str]:
    applied: list[str] = []
    for uuid, requested in caps.items():
        state = await read_state(runner, uuid)
        if state is None:
            await restore_job(runner, records, job_id)
            return False, f"{uuid}: could not read power state"
        target = min(max(requested, state.min_w), state.max_w)
        records.remember_original(uuid, records.original(uuid) or state.limit_w, job_id)
        if await set_limit(runner, uuid, target) is None:
            await restore_job(runner, records, job_id)
            return False, f"{uuid}: limit {target:.0f} W did not read back"
        applied.append(uuid)
    return True, ""


async def restore_job(runner: Runner, records: PowerRecords, job_id: str) -> list[str]:
    restored: list[str] = []
    for uuid in list(records.jobs.get(job_id, [])):
        original = records.original(uuid)
        if original is None:
            records.forget(uuid, job_id)
            continue
        if await set_limit(runner, uuid, original) is not None:
            records.forget(uuid, job_id)
            restored.append(uuid)
    return restored


async def raise_low_limits(runner: Runner, uuids: list[str]) -> list[str]:
    raised: list[str] = []
    for uuid in uuids:
        state = await read_state(runner, uuid)
        if state is None or state.default_w <= 0:
            continue
        if state.limit_w < MIN_RATIO * state.default_w and await set_limit(runner, uuid, state.default_w) is not None:
            raised.append(uuid)
    return raised
