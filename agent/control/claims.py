from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

from agent.config import Settings

HOTKEY = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{46,50}$")
POLL_SECONDS = 2.0

BANNER = """
==============================================================
  CLAIM REQUEST
  Hotkey {hotkey}
  wants to claim this rig{until}.

  Approve from this machine with:
    docker compose exec agent rig-agent approve {hotkey}
  Deny with:
    docker compose exec agent rig-agent approve {hotkey} --deny
==============================================================
"""

CODE_BANNER = """
==============================================================
  REGISTRATION CODE   {code}
  Enter it on the portal under Compute Pool > Add rig.
  Rig id {rig_id}   tier {tier}
==============================================================
"""


def well_formed_hotkey(hotkey: str) -> bool:
    return bool(HOTKEY.match(hotkey or ""))


def record_decision(settings: Settings, hotkey: str, approved: bool) -> Path:
    if not well_formed_hotkey(hotkey):
        raise ValueError("that does not look like an ss58 hotkey")
    folder = settings.approvals_dir
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / hotkey
    target.write_text("approve" if approved else "deny", encoding="utf-8")
    return target


class ClaimPrompter:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.pending: dict[str, str] = {}

    def prompt(self, hotkey: str, expires_at: str) -> None:
        self.pending[hotkey] = expires_at
        until = f" until {expires_at}" if expires_at else ""
        print(BANNER.format(hotkey=hotkey, until=until), flush=True)
        self._runtime.events.write("claim.prompt", hotkey=hotkey, expires_at=expires_at)

    def announce_code(self, code: str, rig_id: str, tier: str) -> None:
        print(CODE_BANNER.format(code=code, rig_id=rig_id, tier=tier or "-"), flush=True)

    async def _send(self, hotkey: str, approved: bool) -> bool:
        rig_id = self._runtime.rig_id
        if not rig_id:
            return False
        try:
            await self._runtime.client.decide_claim(rig_id, hotkey, approved)
        except Exception as exc:
            self._runtime.events.write("claim.decide.error", hotkey=hotkey, error=str(exc)[:200])
            print(
                f"could not send the claim decision for {hotkey}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        self._runtime.events.write("claim.decide", hotkey=hotkey, approved=approved)
        print(f"claim by {hotkey} {'approved' if approved else 'denied'}", flush=True)
        self.pending.pop(hotkey, None)
        return True

    async def _drain(self) -> None:
        folder = self._runtime.settings.approvals_dir
        if not folder.is_dir():
            return
        for entry in sorted(folder.iterdir()):
            if not entry.is_file() or not well_formed_hotkey(entry.name):
                entry.unlink(missing_ok=True)
                continue
            try:
                decision = entry.read_text(encoding="utf-8").strip().lower()
            except OSError:
                continue
            sent = await self._send(entry.name, decision == "approve")
            if sent or decision not in ("approve", "deny"):
                entry.unlink(missing_ok=True)

    async def watch(self) -> None:
        while True:
            try:
                await self._drain()
            except OSError as exc:
                self._runtime.events.write("claim.watch.error", error=str(exc)[:200])
            await asyncio.sleep(POLL_SECONDS)
