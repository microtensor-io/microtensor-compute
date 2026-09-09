from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from protocol.messages import CommandKind
from validator.pool import PoolClient, ServerError

DEFAULT_DEADLINE = 1800.0
POLL_SECONDS = 15.0


@dataclass
class DrainResult:
    rig_id: str
    drained: bool
    remaining: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    command_id: int = 0
    command_state: str = ""
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "rig_id": self.rig_id,
            "drained": self.drained,
            "remaining": list(self.remaining),
            "elapsed_s": round(self.elapsed_s, 1),
            "command_id": self.command_id,
            "command_state": self.command_state,
            "reason": self.reason,
        }


def running_jobs(rig: dict[str, Any]) -> list[str]:
    return [
        str(job.get("id") or job.get("job_id") or "")
        for job in rig.get("jobs") or []
        if isinstance(job, dict)
    ]


async def drain(
    pool: PoolClient,
    rig_id: str,
    reason: str = "",
    deadline: float = DEFAULT_DEADLINE,
    poll: float = POLL_SECONDS,
) -> DrainResult:
    started = time.monotonic()
    try:
        command = await pool.command(
            rig_id, CommandKind.DRAIN.value, {"drain": True, "reason": reason[:200]}
        )
    except ServerError as exc:
        return DrainResult(rig_id, False, reason=f"drain command refused: {exc.detail}")
    command_id = int(command.get("id", 0) or 0)
    state = str(command.get("state", "") or "")
    remaining: list[str] = []
    while True:
        try:
            view = await pool.rig(rig_id)
        except ServerError as exc:
            return DrainResult(
                rig_id,
                False,
                remaining,
                time.monotonic() - started,
                command_id,
                state,
                f"rig view failed: {exc.detail}",
            )
        remaining = running_jobs(view)
        if command_id and state not in ("done", "failed", "expired"):
            with contextlib.suppress(ServerError):
                state = str((await pool.command_result(command_id)).get("state", state) or state)
        if not remaining:
            return DrainResult(rig_id, True, [], time.monotonic() - started, command_id, state)
        if time.monotonic() - started >= deadline:
            return DrainResult(
                rig_id,
                False,
                remaining,
                time.monotonic() - started,
                command_id,
                state,
                f"{len(remaining)} jobs still running after {deadline:.0f} s",
            )
        await asyncio.sleep(poll)
