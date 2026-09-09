import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from agent import __version__
from agent.access.grants import GrantError
from agent.api.auth import dependency, is_loopback
from agent.hardware import challenge, nvml
from agent.hardware.pool import PoolBusy, PoolTimeout
from agent.monitor import containers
from protocol.messages import PROTOCOL
from protocol.session import JobRecord, SessionClose, SessionOpen

OPEN_TIMEOUT = 30.0
LOGS_TIMEOUT = 30.0
STREAM_SECONDS = 300.0
LOG_STREAMS = 10
TAIL_MAX = 500
STATS_TIMEOUT = 12.0


def create_app(runtime: Any) -> Any:
    from fastapi import Depends, FastAPI, HTTPException, Query, Request
    from fastapi.responses import PlainTextResponse, StreamingResponse
    from pydantic import ValidationError

    from agent.api import models

    require_validator = dependency(runtime)
    app = FastAPI(
        title="rig-agent", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    streams = {"active": 0}

    async def parse(request: Request, model: Any) -> Any:
        body = await request.body()
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "body must be JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(422, "body must be a JSON object")
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            field = ".".join(str(part) for part in first.get("loc", ()))
            raise HTTPException(422, f"{field}: {first.get('msg', 'invalid')}".strip(": ")) from exc

    def version_view() -> dict[str, Any]:
        return models.VersionResponse(
            version=__version__,
            protocol=PROTOCOL,
            rig_id=runtime.rig_id,
            image=runtime.settings.image_digest,
            draining=bool(runtime.draining),
            challenge_library=challenge.installed(runtime.settings.challenge_library),
        ).model_dump()

    @app.get("/version")
    async def version(request: Request) -> dict[str, Any]:
        host = request.client.host if request.client else ""
        if not is_loopback(host):
            await require_validator(request)
        return version_view()

    @app.post("/ping")
    async def ping(validator: str = Depends(require_validator)) -> dict[str, Any]:
        return models.PingResponse(
            ok=True,
            rig_id=runtime.rig_id,
            version=__version__,
            draining=bool(runtime.draining),
            at=round(time.time(), 3),
        ).model_dump()

    @app.post("/session/open")
    async def session_open(
        request: Request, validator: str = Depends(require_validator)
    ) -> dict[str, Any]:
        body = await parse(request, models.SessionOpenBody)
        opening = SessionOpen.from_payload(body.model_dump())
        hotkeys = [validator] + [key for key in runtime.validators.hotkeys() if key != validator]
        try:
            grant = await asyncio.wait_for(
                asyncio.to_thread(runtime.grants.open, opening, hotkeys), OPEN_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(504, "session open timed out at attestation") from exc
        except GrantError as exc:
            runtime.events.write(
                "session.refused", validator=validator, status=exc.status, detail=exc.detail
            )
            raise HTTPException(exc.status, exc.detail) from exc
        return models.SessionGrantResponse.model_validate(grant.payload()).model_dump()

    @app.post("/session/close")
    async def session_close(
        request: Request, validator: str = Depends(require_validator)
    ) -> dict[str, Any]:
        body = await parse(request, models.SessionCloseBody)
        closing = SessionClose.from_payload(body.model_dump())
        if not closing.public_key and not closing.session_id:
            raise HTTPException(422, "public_key or session_id required")
        revoked = await asyncio.to_thread(runtime.grants.close, closing, validator)
        return models.SessionCloseResponse(revoked=revoked).model_dump()

    @app.post("/utilisation")
    async def utilisation(validator: str = Depends(require_validator)) -> dict[str, Any]:
        try:
            return await runtime.pool.run(nvml.utilisation, stage="utilisation")
        except PoolBusy as exc:
            raise HTTPException(503, str(exc)) from exc
        except PoolTimeout as exc:
            raise HTTPException(504, str(exc)) from exc

    @app.post("/containers/{name}/utilisation")
    async def container_utilisation(
        name: str, validator: str = Depends(require_validator)
    ) -> dict[str, Any]:
        if not containers.valid_name(name):
            raise HTTPException(422, "invalid container name")
        try:
            view = await runtime.pool.run(
                containers.stats, name, timeout=STATS_TIMEOUT, stage="container_stats"
            )
        except PoolBusy as exc:
            raise HTTPException(503, str(exc)) from exc
        except PoolTimeout as exc:
            raise HTTPException(504, str(exc)) from exc
        if "error" in view:
            raise HTTPException(404 if view.get("code") != 124 else 504, str(view["error"]))
        return view

    async def spawn_logs(name: str, tail: int, follow: bool) -> Any:
        args = ["docker", "logs", "--tail", str(tail)]
        if follow:
            args.append("--follow")
        args.append(name)
        return await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )

    async def finish(process: Any) -> None:
        if process.returncode is None:
            process.kill()
            await process.wait()

    @app.get("/containers/{name}/logs")
    async def container_logs(
        name: str,
        tail: int = Query(default=200, ge=1, le=TAIL_MAX),
        follow: bool = Query(default=False),
        validator: str = Depends(require_validator),
    ) -> Any:
        if not containers.valid_name(name):
            raise HTTPException(422, "invalid container name")
        if not follow:
            process = await spawn_logs(name, tail, False)
            try:
                out, _ = await asyncio.wait_for(process.communicate(), LOGS_TIMEOUT)
            except asyncio.TimeoutError as exc:
                await finish(process)
                raise HTTPException(504, "docker logs timed out") from exc
            text = out.decode("utf-8", "replace")
            if process.returncode != 0:
                raise HTTPException(404, text.strip()[-300:] or "container not found")
            return PlainTextResponse(text)
        if streams["active"] >= LOG_STREAMS:
            raise HTTPException(429, "too many log streams")
        process = await spawn_logs(name, tail, True)
        streams["active"] += 1

        async def body() -> AsyncIterator[bytes]:
            deadline = time.monotonic() + STREAM_SECONDS
            try:
                assert process.stdout is not None
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        line = await asyncio.wait_for(process.stdout.readline(), remaining)
                    except asyncio.TimeoutError:
                        break
                    if not line:
                        break
                    yield line
            finally:
                streams["active"] -= 1
                await finish(process)

        return StreamingResponse(body(), media_type="text/plain")

    @app.post("/jobs")
    async def jobs(request: Request, validator: str = Depends(require_validator)) -> dict[str, Any]:
        body = await parse(request, models.JobBody)
        job = JobRecord.from_payload(body.model_dump())
        runtime.jobs.record(job, source=validator, detail=body.detail)
        runtime.events.write(
            "job.record", job=job.job_id, job_kind=job.kind, pid=job.pid, validator=validator
        )
        return models.JobResponse(recorded=job.job_id, jobs=len(runtime.jobs)).model_dump()

    return app
