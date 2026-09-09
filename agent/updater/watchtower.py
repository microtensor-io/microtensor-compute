from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from agent.api.auth import verify_hotkey
from agent.config import Settings
from protocol.session import RELEASE_SKEW_SECONDS, Release, well_formed_digest

PULL_TIMEOUT = 900.0
INSPECT_TIMEOUT = 20.0
HELPER_TIMEOUT = 180.0
STOP_TIMEOUT = 10
MAX_BACKOFF = 3600.0
CONTAINER_ID = re.compile(r"/docker/containers/([0-9a-f]{64})/")
HELPER_PREFIX = "rig-agent-update"


async def _docker(*args: str, timeout: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timed out"
    return int(process.returncode or 0), out.decode("utf-8", "replace")


def own_container_id() -> str:
    try:
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            match = CONTAINER_ID.search(line)
            if match:
                return match.group(1)
    except OSError:
        pass
    name = socket.gethostname()
    return name if re.match(r"^[0-9a-f]{12,64}$", name) else ""


def verify_release(
    payload: dict[str, Any], validators: Any, now: float | None = None
) -> tuple[Release | None, str]:
    raw = payload.get("release") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None, "no release published"
    release = Release.from_payload(raw)
    if not well_formed_digest(release.digest):
        return None, "digest is not sha256:<64 hex>"
    if not release.validator or release.validator not in validators:
        return None, "release signer is not an active validator"
    moment = time.time() if now is None else now
    if abs(moment - release.timestamp) > RELEASE_SKEW_SECONDS:
        return None, "release timestamp outside the skew window"
    if not verify_hotkey(release.validator, release.message, release.signature):
        return None, "release signature does not verify"
    return release, ""


def rewrite_env(path: Path, digest: str) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [line for line in lines if not line.startswith("AGENT_IMAGE_SHA256=")]
    kept.append(f"AGENT_IMAGE_SHA256={digest}")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    os.replace(temp, path)


class Updater:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self.failures = 0
        self.last_check = 0.0
        self.last_error = ""
        self.running_digest = ""

    @property
    def settings(self) -> Settings:
        return self._runtime.settings

    def _log(self, kind: str, **fields: Any) -> None:
        self._runtime.events.write(kind, **fields)

    async def running_image_digest(self) -> str:
        container = own_container_id()
        if container:
            code, out = await _docker(
                "inspect", "--format", "{{.Image}}", container, timeout=INSPECT_TIMEOUT
            )
            image = out.strip() if code == 0 else ""
            if image:
                code, out = await _docker(
                    "image",
                    "inspect",
                    "--format",
                    "{{json .RepoDigests}}",
                    image,
                    timeout=INSPECT_TIMEOUT,
                )
                if code == 0:
                    try:
                        digests = json.loads(out.strip().splitlines()[-1])
                    except (ValueError, IndexError):
                        digests = []
                    for entry in digests if isinstance(digests, list) else []:
                        if "@" in str(entry):
                            return str(entry).split("@", 1)[1]
        return self.settings.image_digest

    async def pull_and_verify(self, digest: str) -> str:
        reference = self.settings.image_reference(digest)
        code, out = await _docker("pull", reference, timeout=PULL_TIMEOUT)
        if code != 0:
            return f"pull failed ({code}): {out.strip()[-300:]}"
        code, out = await _docker(
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            reference,
            timeout=INSPECT_TIMEOUT,
        )
        if code != 0:
            return f"inspect failed ({code}): {out.strip()[-300:]}"
        try:
            digests = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return "inspect returned no digests"
        if not any(
            str(entry).endswith(f"@{digest}") for entry in digests if isinstance(digests, list)
        ):
            return "pulled image does not carry the authorised digest"
        return ""

    async def compose_project(self) -> str:
        container = own_container_id()
        if not container:
            return ""
        code, out = await _docker(
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.project"}}',
            container,
            timeout=INSPECT_TIMEOUT,
        )
        return out.strip() if code == 0 else ""

    async def restart(self, digest: str) -> str:
        compose_file = self.settings.compose_file
        env_file = self.settings.env_file
        if not compose_file or not Path(compose_file).is_file():
            return f"compose file {compose_file!r} is not mounted"
        try:
            rewrite_env(Path(env_file), digest)
        except OSError as exc:
            return f"could not write {env_file}: {exc}"
        container = own_container_id()
        project = await self.compose_project()
        helper = f"{HELPER_PREFIX}-{int(time.time())}"
        args = [
            "run",
            "-d",
            "--name",
            helper,
            "--entrypoint",
            "docker",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
        ]
        if container:
            args += ["--volumes-from", container]
        args.append(self.settings.image_reference(digest))
        args += ["compose", "-f", compose_file, "--env-file", env_file]
        if project:
            args += ["-p", project]
        args += ["up", "-d", "--no-deps", "--timeout", str(STOP_TIMEOUT), "agent"]
        code, out = await _docker(*args, timeout=60)
        if code != 0:
            return f"could not start the update helper ({code}): {out.strip()[-300:]}"
        self._log("update.helper", helper=helper, digest=digest)
        code, out = await _docker("wait", helper, timeout=HELPER_TIMEOUT)
        exit_code = out.strip().splitlines()[-1] if out.strip() else ""
        _, logs = await _docker("logs", "--tail", "40", helper, timeout=20)
        await _docker("rm", "-f", helper, timeout=20)
        if code != 0 or exit_code != "0":
            return f"update helper exited {exit_code or code}: {logs.strip()[-400:]}"
        return ""

    async def check_once(self) -> dict[str, Any]:
        self.last_check = time.time()
        payload = await self._runtime.client.release()
        release, problem = verify_release(payload, self._runtime.validators)
        if release is None:
            return {"applied": False, "reason": problem}
        running = await self.running_image_digest()
        self.running_digest = running
        if running == release.digest:
            return {
                "applied": False,
                "reason": "already running the authorised image",
                "digest": running,
            }
        self._log(
            "update.available", digest=release.digest, running=running, validator=release.validator
        )
        problem = await self.pull_and_verify(release.digest)
        if problem:
            raise RuntimeError(problem)
        problem = await self.restart(release.digest)
        if problem:
            raise RuntimeError(problem)
        self._log("update.applied", digest=release.digest)
        return {"applied": True, "digest": release.digest}

    async def run(self) -> None:
        while True:
            delay = float(self.settings.update_seconds)
            try:
                outcome = await self.check_once()
                self.failures = 0
                self.last_error = ""
                if outcome.get("applied"):
                    delay = max(delay, 120.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures += 1
                self.last_error = str(exc)[:300]
                self._log("update.error", error=self.last_error, failures=self.failures)
                delay = min(delay * (2 ** min(self.failures, 6)), MAX_BACKOFF)
            await asyncio.sleep(delay)
