from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import threading
import time
import urllib.request
from typing import Any

from agent import __version__
from agent.access.grants import SessionGrants
from agent.access.nonce import NonceBook
from agent.api.auth import ValidatorSet
from agent.config import Settings
from agent.config import settings as load_settings
from agent.control.claims import ClaimPrompter, record_decision
from agent.control.client import ServerClient, ServerError
from agent.control.identity import AgentKey
from agent.control.queue import OutboundQueue
from agent.control.socket import ControlSocket
from agent.hardware import challenge, spec
from agent.hardware.pool import MetricsPool
from agent.monitor import kmsg
from agent.monitor.containers import ContainerWatcher
from agent.store.events import EventStore
from agent.store.jobs import JobBook
from agent.updater.watchtower import Updater

log = logging.getLogger("rig_agent")
SPEC_TIMEOUT = 120.0
RETRY_VALIDATORS_SECONDS = 30
API_STOP_SECONDS = 5.0


class AgentRuntime:
    def __init__(self, config: Settings) -> None:
        self.settings = config
        self.events = EventStore(config.events_path)
        self.key = AgentKey.load_or_create(config.key_path)
        self.client = ServerClient(config, self.key)
        self.validators = ValidatorSet(config.validators_path)
        self.pool = MetricsPool()
        self.nonces = NonceBook(config.nonce_ttl_seconds)
        self.grants = SessionGrants(config, self.events, self.nonces)
        self.jobs = JobBook()
        self.claims = ClaimPrompter(self)
        self.queue = OutboundQueue()
        self.socket = ControlSocket(self, self.queue)
        self.updater = Updater(self)
        self.draining = False
        self.rig_id = self._saved_rig_id()
        self.started_at = time.time()
        self.stop = asyncio.Event()
        self._register_lock = asyncio.Lock()

    def _saved_rig_id(self) -> str:
        try:
            return str(
                json.loads(self.settings.rig_path.read_text(encoding="utf-8")).get("rig_id", "")
            )
        except (OSError, ValueError, AttributeError):
            return ""

    def _save(self, view: dict[str, Any]) -> None:
        try:
            self.settings.rig_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings.rig_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(view, indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
            temp.replace(self.settings.rig_path)
        except OSError as exc:
            self.events.write("state.save_error", error=str(exc)[:200])

    async def register(self) -> dict[str, Any]:
        async with self._register_lock:
            config = self.settings
            view = await asyncio.wait_for(asyncio.to_thread(spec.build, config), SPEC_TIMEOUT)
            payload = {
                "gpus": [
                    {
                        "uuid": card["uuid"],
                        "model": card["model"],
                        "memory_mb": card["memory_mb"],
                        "index": card["index"],
                    }
                    for card in view["gpus"]
                ],
                "host": {key: value for key, value in view.items() if key != "gpus"},
                "address": view.get("address", ""),
                "api_port": config.external_port,
                "ssh_port": config.ssh_port,
                "agent_version": __version__,
                "label": config.label or str(view.get("hostname", "")),
            }
            answer = await self.client.register(payload)
            self.rig_id = str(answer.get("rig_id", "") or self.rig_id)
            self._save(answer)
            self.events.write(
                "register",
                rig_id=self.rig_id,
                state=answer.get("state"),
                tier=answer.get("tier"),
                gpus=len(view["gpus"]),
                nvml_error=view.get("nvml_error", ""),
            )
            claim = answer.get("claim") if isinstance(answer.get("claim"), dict) else {}
            code = str(claim.get("code", "") or "")
            if code and not answer.get("hotkey"):
                self.claims.announce_code(code, self.rig_id, str(answer.get("tier", "") or ""))
            return answer

    async def refresh_validators(self) -> None:
        while True:
            delay = self.settings.validators_refresh_seconds
            try:
                found = await self.client.validators()
                if found:
                    if self.validators.update(found):
                        self.events.write("validators.updated", count=len(found))
                else:
                    delay = RETRY_VALIDATORS_SECONDS
            except Exception as exc:
                self.events.write("validators.error", error=str(exc)[:200])
                if not len(self.validators):
                    delay = RETRY_VALIDATORS_SECONDS
            await asyncio.sleep(delay)

    async def sweep_grants(self) -> None:
        while True:
            await asyncio.sleep(self.settings.grant_sweep_seconds)
            try:
                await asyncio.to_thread(self.grants.sweep)
            except Exception as exc:
                self.events.write("session.sweep_error", error=str(exc)[:200])

    def _server(self) -> Any:
        import uvicorn

        from agent.api.routes import create_app

        config = uvicorn.Config(
            create_app(self),
            host="0.0.0.0",
            port=self.settings.internal_port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        return uvicorn.Server(config)

    async def watch_server_exit(self, server: Any) -> None:
        while not server.should_exit:
            await asyncio.sleep(0.5)
        self.stop.set()

    def on_signal(self, *_: Any) -> None:
        challenge.release_all()
        self.stop.set()

    def install_signals(self, loop: asyncio.AbstractEventLoop) -> None:
        for name in ("SIGTERM", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                loop.add_signal_handler(signum, self.on_signal)
            except (NotImplementedError, RuntimeError):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signum, lambda *_: loop.call_soon_threadsafe(self.on_signal))

    async def run(self) -> int:
        self.events.write(
            "agent.start", version=__version__, rig_id=self.rig_id, key=self.key.public_hex[:16]
        )
        await asyncio.to_thread(self.grants.revoke_all)
        try:
            await self.register()
        except ServerError as exc:
            print(f"registration refused: {exc.detail}", file=sys.stderr, flush=True)
            self.events.write("register.refused", status=exc.status, detail=exc.detail[:300])
            if exc.status in (409, 426):
                return 2
        except Exception as exc:
            self.events.write("register.error", error=str(exc)[:300])
            print(f"could not reach the pool yet: {exc}", file=sys.stderr, flush=True)
        server = self._server()
        tasks = {
            "api": asyncio.create_task(server.serve(), name="api"),
            "validators": asyncio.create_task(self.refresh_validators(), name="validators"),
            "socket": asyncio.create_task(self.socket.run(), name="socket"),
            "claims": asyncio.create_task(self.claims.watch(), name="claims"),
            "updater": asyncio.create_task(self.updater.run(), name="updater"),
            "grants": asyncio.create_task(self.sweep_grants(), name="grants"),
            "exit": asyncio.create_task(self.watch_server_exit(server), name="exit"),
        }
        await asyncio.sleep(0.2)
        self.install_signals(asyncio.get_running_loop())
        stop_wait = asyncio.create_task(self.stop.wait(), name="stop")
        code = 0
        try:
            done, _ = await asyncio.wait(
                [*tasks.values(), stop_wait], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is stop_wait or task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    self.events.write(
                        "agent.task_failed", task=task.get_name(), error=str(exc)[:300]
                    )
                    print(f"{task.get_name()} failed: {exc}", file=sys.stderr, flush=True)
                    code = 1
        finally:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tasks["api"], API_STOP_SECONDS)
            for task in tasks.values():
                task.cancel()
            stop_wait.cancel()
            await asyncio.gather(*tasks.values(), stop_wait, return_exceptions=True)
            await self.socket.shutdown()
            await self.client.close()
            self.pool.shutdown()
            challenge.release_all()
            self.events.write("agent.stop", code=code, queue=self.queue.snapshot())
        return code


def run_monitor(config: Settings) -> int:
    events = EventStore(config.events_path)
    stop = threading.Event()
    watcher = ContainerWatcher(events, stop)

    def on_fault(fault: kmsg.KernelFault) -> None:
        watcher.note_fault(fault)
        events.write("gpu.fault", **fault.payload())

    def on_signal(*_: Any) -> None:
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, on_signal)

    def watch_kernel() -> None:
        while not stop.is_set():
            try:
                events.write("monitor.kmsg.start")
                kmsg.watch(on_fault, stop=stop)
            except PermissionError:
                events.write(
                    "monitor.error", error="cannot read /dev/kmsg; SYSLOG capability missing"
                )
                stop.wait(60)
            except OSError as exc:
                events.write("monitor.error", error=str(exc)[:200])
                stop.wait(10)

    thread = threading.Thread(target=watch_kernel, name="kmsg", daemon=True)
    thread.start()
    events.write("monitor.start", version=__version__)
    watcher.run()
    events.write("monitor.stop")
    return 0


def status(config: Settings) -> int:
    try:
        saved = json.loads(config.rig_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    claim = saved.get("claim") if isinstance(saved.get("claim"), dict) else {}
    print(f"agent version   {__version__}")
    print(f"agent key       {AgentKey.load_or_create(config.key_path).public_hex}")
    print(f"rig id          {saved.get('rig_id', '') or '-'}")
    print(f"rig state       {saved.get('state', '') or '-'}")
    print(f"hotkey          {saved.get('hotkey', '') or '-'}")
    print(f"tier            {saved.get('tier', '') or '-'}")
    if claim:
        print(f"claim           {claim.get('state', '')} code {claim.get('code', '') or '-'}")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config.internal_port}/version", timeout=3
        ) as answer:
            live = json.loads(answer.read().decode("utf-8", "replace"))
        print(
            f"api             up, draining={live.get('draining')}, image={live.get('image') or '-'}"
        )
    except Exception as exc:
        print(f"api             down ({exc})")
    for record in EventStore(config.events_path).tail(8):
        moment = time.strftime("%H:%M:%S", time.gmtime(float(record.get("ts", 0) or 0)))
        fields = {k: v for k, v in record.items() if k not in ("ts", "kind")}
        print(f"  {moment} {record.get('kind', '')} {json.dumps(fields, default=str)[:120]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rig-agent")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("role", nargs="?", default="agent", choices=["agent", "monitor"])
    sub.add_parser("keys")
    sub.add_parser("version")
    sub.add_parser("status")
    approve = sub.add_parser("approve")
    approve.add_argument("hotkey")
    approve.add_argument("--deny", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = load_settings()

    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "keys":
        print(AgentKey.load_or_create(config.key_path).public_hex)
        return 0
    if args.command == "status":
        return status(config)
    if args.command == "approve":
        try:
            record_decision(config, args.hotkey, not args.deny)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(
            f"{'denial' if args.deny else 'approval'} recorded for {args.hotkey}; the agent will send it"
        )
        return 0
    if args.command == "run" and args.role == "monitor":
        return run_monitor(config)
    if args.command == "run":
        with contextlib.suppress(KeyboardInterrupt):
            return asyncio.run(AgentRuntime(config).run())
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
