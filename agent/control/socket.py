from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from agent import __version__
from agent.control import handlers
from agent.control.queue import OutboundQueue
from agent.hardware import nvml
from agent.hardware.pool import PoolBusy, PoolTimeout
from agent.monitor.containers import job_containers
from protocol.messages import (
    CLOSE_BAD_HELLO,
    CLOSE_BAD_SIGNATURE,
    CLOSE_UNKNOWN_RIG,
    HELLO_TIMEOUT_SECONDS,
    PONG_TIMEOUT_SECONDS,
    RECONNECT_MAX_SECONDS,
    RECONNECT_MIN_SECONDS,
    Command,
    Event,
    Result,
    ServerMessage,
    State,
    Welcome,
    ping,
)
from protocol.session import SOCKET_PATH

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
OPEN_TIMEOUT = 15.0
STATE_TIMEOUT = 12.0


def close_code(exc: BaseException) -> int:
    received = getattr(exc, "rcvd", None)
    if received is not None and getattr(received, "code", None) is not None:
        return int(received.code)
    code = getattr(exc, "code", None)
    return int(code) if isinstance(code, int) else 0


async def _connect(url: str) -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect
    return await connect(
        url, open_timeout=OPEN_TIMEOUT, ping_interval=None, max_size=MAX_MESSAGE_BYTES
    )


class ControlSocket:
    def __init__(self, runtime: Any, queue: OutboundQueue | None = None) -> None:
        self._runtime = runtime
        self.queue = queue or OutboundQueue()
        self.connected = False
        self.connected_at = 0.0
        self.last_pong = 0.0
        self.last_close_code = 0
        self._tasks: set[asyncio.Task[None]] = set()

    def send(self, message: dict[str, Any], front: bool = False) -> None:
        self.queue.put(message, front=front)

    def send_event(self, kind: str, detail: dict[str, Any] | None = None) -> None:
        self.send(Event(kind=kind, detail=detail or {}).payload())

    def send_result(self, command_id: int, ok: bool, result: dict[str, Any]) -> None:
        self.send(Result(id=command_id, ok=ok, result=result).payload())

    async def run(self) -> None:
        backoff = RECONNECT_MIN_SECONDS
        need_register = False
        while True:
            if need_register:
                with contextlib.suppress(Exception):
                    await self._runtime.register()
            try:
                await self._session()
                backoff = RECONNECT_MIN_SECONDS
                need_register = True
                self._runtime.events.write("socket.closed", clean=True)
                await asyncio.sleep(backoff)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = close_code(exc)
                self.last_close_code = code
                self._runtime.events.write("socket.error", error=str(exc)[:300], code=code)
                need_register = True
                if code == CLOSE_UNKNOWN_RIG:
                    backoff = RECONNECT_MIN_SECONDS
                elif code in (CLOSE_BAD_HELLO, CLOSE_BAD_SIGNATURE):
                    backoff = max(backoff, 5.0)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _session(self) -> None:
        settings = self._runtime.settings
        socket = await _connect(settings.socket_url)
        try:
            hello = self._runtime.key.hello(SOCKET_PATH)
            await socket.send(json.dumps(hello.payload(), separators=(",", ":")))
            raw = await asyncio.wait_for(socket.recv(), timeout=HELLO_TIMEOUT_SECONDS)
            greeting = json.loads(raw)
            if (
                not isinstance(greeting, dict)
                or greeting.get("type") != ServerMessage.WELCOME.value
            ):
                raise RuntimeError(f"unexpected greeting type {type(greeting).__name__}")
            welcome = Welcome.from_payload(greeting)
            if welcome.rig_id:
                self._runtime.rig_id = welcome.rig_id
            self.connected = True
            self.connected_at = time.monotonic()
            self.last_pong = self.connected_at
            self._runtime.events.write(
                "socket.connected", rig_id=self._runtime.rig_id, ping=welcome.ping_seconds
            )
            tasks = [
                asyncio.create_task(self._reader(socket), name="socket-reader"),
                asyncio.create_task(self._writer(socket), name="socket-writer"),
                asyncio.create_task(self._pinger(welcome.ping_seconds), name="socket-pinger"),
                asyncio.create_task(self._stater(), name="socket-stater"),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.connected = False
            with contextlib.suppress(Exception):
                await socket.close()

    async def _reader(self, socket: Any) -> None:
        async for raw in socket:
            try:
                message = json.loads(raw)
            except ValueError:
                self._runtime.events.write("socket.bad_message", length=len(raw))
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == ServerMessage.COMMAND.value:
                command = Command.from_payload(message)
                task = asyncio.create_task(self._execute(command), name=f"command-{command.id}")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            elif kind == ServerMessage.PONG.value:
                self.last_pong = time.monotonic()
            elif kind == ServerMessage.ERROR.value:
                self._runtime.events.write(
                    "socket.server_error", detail=str(message.get("detail", ""))[:300]
                )

    async def _execute(self, command: Command) -> None:
        ok, result = await handlers.handle(self._runtime, command)
        self._runtime.events.write("command", id=command.id, command=command.kind, ok=ok)
        self.send_result(command.id, ok, result)

    async def _writer(self, socket: Any) -> None:
        while True:
            message = await self.queue.get()
            self.queue.stamp(message)
            try:
                await socket.send(json.dumps(message, separators=(",", ":"), default=str))
            except Exception:
                self.queue.requeue(message)
                raise

    async def _pinger(self, every: int) -> None:
        interval = max(1, min(int(every or 20), 20))
        while True:
            self.send(ping(), front=True)
            await asyncio.sleep(interval)
            silent = time.monotonic() - max(self.last_pong, self.connected_at)
            if silent > PONG_TIMEOUT_SECONDS:
                raise ConnectionError(f"no pong for {silent:.0f}s")

    async def state_payload(self) -> dict[str, Any]:
        try:
            utilisation = await self._runtime.pool.run(
                nvml.utilisation, timeout=STATE_TIMEOUT, stage="state"
            )
        except (PoolBusy, PoolTimeout) as exc:
            utilisation = {"error": str(exc)}
        try:
            containers = await asyncio.wait_for(asyncio.to_thread(job_containers), 8.0)
        except Exception:
            containers = []
        state = State(
            utilisation=utilisation,
            containers=containers,
            queue_depth=self.queue.depth,
            agent_version=__version__,
            draining=bool(self._runtime.draining),
            jobs=self._runtime.jobs.ids(),
        )
        return state.payload()

    async def _stater(self) -> None:
        every = int(self._runtime.settings.state_seconds)
        while True:
            self.send(await self.state_payload())
            await asyncio.sleep(every)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
