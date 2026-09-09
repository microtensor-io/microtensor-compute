from __future__ import annotations

import contextlib
import json
from typing import Any

from agent.config import Settings
from agent.control.identity import AgentKey

AGENTS = "/v1/compute/agents"
VALIDATORS = "/v1/pool/validators"
RELEASE = "/v1/pool/agent-release"


class ServerError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":"), sort_keys=True, default=str).encode()


class ServerClient:
    def __init__(self, settings: Settings, key: AgentKey, timeout: float = 30.0) -> None:
        self._settings = settings
        self._key = key
        self._timeout = timeout
        self._http: Any = None

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(base_url=self._settings.base_url, timeout=self._timeout)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None

    async def _signed(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        raw = encode(body) if body is not None else b""
        headers = {"accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        headers.update(self._key.headers(method, path, raw))
        answer = await self._client().request(
            method, path, content=raw if body is not None else None, headers=headers
        )
        if answer.status_code >= 400:
            detail = answer.text[:500]
            with contextlib.suppress(Exception):
                detail = str(answer.json().get("detail", detail))
            raise ServerError(answer.status_code, detail)
        return answer.json() if answer.content else {}

    async def _public(self, path: str) -> Any:
        answer = await self._client().get(path, headers={"accept": "application/json"})
        if answer.status_code >= 400:
            raise ServerError(answer.status_code, answer.text[:500])
        return answer.json()

    async def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._signed("POST", f"{AGENTS}/register", payload))

    async def claim(self, rig_id: str) -> dict[str, Any]:
        return dict(await self._signed("GET", f"{AGENTS}/{rig_id}/claim"))

    async def decide_claim(self, rig_id: str, hotkey: str, approved: bool) -> dict[str, Any]:
        body = {"hotkey": hotkey, "approved": approved}
        return dict(await self._signed("POST", f"{AGENTS}/{rig_id}/claim/decision", body))

    async def report_state(self, rig_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._signed("POST", f"{AGENTS}/{rig_id}/state", payload))

    async def poll_commands(self, rig_id: str) -> list[dict[str, Any]]:
        answer = await self._signed("GET", f"{AGENTS}/{rig_id}/commands")
        found = answer.get("commands", []) if isinstance(answer, dict) else []
        return [dict(item) for item in found if isinstance(item, dict)]

    async def command_result(
        self, rig_id: str, command_id: int, ok: bool, result: dict[str, Any]
    ) -> dict[str, Any]:
        body = {"ok": ok, "result": result}
        return dict(
            await self._signed("POST", f"{AGENTS}/{rig_id}/commands/{command_id}/result", body)
        )

    async def validators(self) -> list[str]:
        answer = await self._public(VALIDATORS)
        found = answer.get("validators", []) if isinstance(answer, dict) else []
        return [str(hotkey) for hotkey in found if hotkey]

    async def release(self) -> dict[str, Any]:
        answer = await self._public(RELEASE)
        return dict(answer) if isinstance(answer, dict) else {}
