from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from agent.cache import prefetch
from agent.hardware import challenge, nvml, spec
from agent.hardware.pool import PoolBusy, PoolTimeout
from agent.monitor.containers import valid_name
from protocol.messages import (
    AttestationRequest,
    ChallengeRequest,
    ClaimPrompt,
    Command,
    CommandKind,
    DrainRequest,
    LogStreamRequest,
    PrefetchRequest,
)
from protocol.session import JobRecord

UNKNOWN = "unknown command"
SPEC_TIMEOUT = 90.0
CHALLENGE_TIMEOUT = 600.0
ATTESTATION_TIMEOUT = 60.0
PREFETCH_TIMEOUT = 1500.0
LOG_BYTES = 256 * 1024

Handler = Callable[[Any, Command], Awaitable[tuple[bool, dict[str, Any]]]]


async def _spec(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    view = await asyncio.wait_for(asyncio.to_thread(spec.build, runtime.settings), SPEC_TIMEOUT)
    return True, view


async def _utilisation(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    try:
        view = await runtime.pool.run(nvml.utilisation, stage="utilisation")
    except (PoolBusy, PoolTimeout) as exc:
        return False, {"error": str(exc)}
    return True, view


async def _challenge(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = ChallengeRequest.from_payload(command.payload)
    library = runtime.settings.challenge_library
    if not challenge.installed(library):
        return False, {"error": challenge.MISSING}
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(challenge.run, library, request), CHALLENGE_TIMEOUT
        )
    except asyncio.TimeoutError:
        runtime.events.write("challenge.timeout", seed=request.seed, n=request.n)
        return False, {"error": "challenge did not finish in time"}
    runtime.events.write(
        "challenge.run",
        seed=request.seed,
        n=request.n,
        rounds=request.rounds,
        ok=not answer.error,
        elapsed_ms=round(answer.elapsed_ms, 3),
        error=answer.error,
    )
    return not answer.error, answer.payload()


async def _attestation(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = AttestationRequest.from_payload(command.payload)
    from agent.hardware import attestation

    try:
        evidence = await asyncio.wait_for(
            asyncio.to_thread(attestation.build, request.nonce, runtime.settings),
            ATTESTATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return False, {"error": "attestation did not finish in time"}
    if evidence is None:
        return False, {"error": "nonce must be 32 bytes as 64 lowercase hex characters"}
    return True, evidence.payload()


async def _job_placed(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    job = JobRecord.from_payload(command.payload)
    if not job.job_id:
        return False, {"error": "job_id required"}
    detail = command.payload.get("detail")
    runtime.jobs.record(
        job, source="coordinator", detail=detail if isinstance(detail, dict) else None
    )
    runtime.events.write(
        "job.placed", job=job.job_id, job_kind=job.kind, pid=job.pid, container=job.container
    )
    return True, {"recorded": job.job_id, "jobs": len(runtime.jobs)}


async def _docker_logs(name: str, tail: int, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "logs",
        "--tail",
        str(tail),
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timed out"
    return int(process.returncode or 0), out[-LOG_BYTES:].decode("utf-8", "replace")


async def _log_stream(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = LogStreamRequest.from_payload(command.payload)
    if not valid_name(request.container):
        return False, {"error": "container name required"}
    code, out = await _docker_logs(request.container, request.tail, float(request.seconds))
    lines = out.splitlines()[-request.tail :]
    return code == 0, {
        "container": request.container,
        "lines": lines,
        "code": code,
        "tail": request.tail,
    }


async def _prefetch(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = PrefetchRequest.from_payload(command.payload)
    if not request.image:
        return False, {"error": "image required"}
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                prefetch.warm, request, runtime.settings.prefetch_path, runtime.events
            ),
            PREFETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return False, {"error": "prefetch did not finish in time", "image": request.image}
    return bool(outcome.get("ok")), outcome


async def _drain(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = DrainRequest.from_payload(command.payload)
    runtime.draining = request.drain
    runtime.events.write("drain", draining=request.drain, reason=request.reason)
    return True, {"draining": runtime.draining, "jobs_running": len(runtime.jobs)}


async def _claim_prompt(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    request = ClaimPrompt.from_payload(command.payload)
    if not request.hotkey:
        return False, {"error": "hotkey required"}
    runtime.claims.prompt(request.hotkey, request.expires_at)
    return True, {"prompted": request.hotkey, "expires_at": request.expires_at}


HANDLERS: dict[str, Handler] = {
    CommandKind.SPEC.value: _spec,
    CommandKind.UTILISATION.value: _utilisation,
    CommandKind.CHALLENGE.value: _challenge,
    CommandKind.ATTESTATION.value: _attestation,
    CommandKind.JOB_PLACED.value: _job_placed,
    CommandKind.LOG_STREAM.value: _log_stream,
    CommandKind.PREFETCH.value: _prefetch,
    CommandKind.DRAIN.value: _drain,
    CommandKind.CLAIM_PROMPT.value: _claim_prompt,
}


async def handle(runtime: Any, command: Command) -> tuple[bool, dict[str, Any]]:
    handler = HANDLERS.get(command.kind)
    if handler is None:
        return False, {"error": UNKNOWN, "kind": command.kind}
    try:
        return await handler(runtime, command)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime.events.write(
            "command.error", id=command.id, command=command.kind, error=str(exc)[:300]
        )
        return False, {"error": str(exc)[:500], "kind": command.kind}
