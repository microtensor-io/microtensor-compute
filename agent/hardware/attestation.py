from __future__ import annotations

import http.client
import json
import socket
import time
from pathlib import Path
from typing import Any

from agent import __version__
from agent.access.ssh_keys import canonical
from agent.config import Settings
from agent.hardware import host, nvml
from protocol.attestation import Attestation, GpuEvidence, report_data
from protocol.session import well_formed_nonce

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
QUOTE_PATHS = ("/GetQuote", "/prpc/Tappd.TdxQuote?json")
QUOTE_TIMEOUT = 10.0


def host_key_line(path: Path) -> str:
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""
    return canonical(first) or first.strip()


def boot_id() -> str:
    return host.read_text(BOOT_ID, 64)


def hostname() -> str:
    return host.read_text(host.host_path("/etc/hostname"), 256) or socket.gethostname()


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _quote_request(socket_path: Path, path: str, body: dict[str, Any]) -> str:
    connection = _UnixConnection(str(socket_path), QUOTE_TIMEOUT)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
        answer = connection.getresponse()
        raw = answer.read(1024 * 1024)
        if answer.status != 200:
            return ""
    finally:
        connection.close()
    parsed = json.loads(raw.decode("utf-8", "replace"))
    quote = parsed.get("quote", "") if isinstance(parsed, dict) else ""
    return str(quote) if isinstance(quote, str) else ""


def tdx_quote(socket_path: Path, data: bytes) -> str:
    if not socket_path.exists():
        return ""
    body = {"report_data": data.hex(), "hash_algorithm": "raw"}
    for path in QUOTE_PATHS:
        try:
            quote = _quote_request(socket_path, path, body)
        except Exception:
            quote = ""
        if quote:
            return quote
    return ""


def gpu_evidence(nonce: str) -> dict[str, Any]:
    try:
        from nv_attestation_sdk import attestation as sdk
    except Exception:
        return {}
    try:
        client = sdk.Attestation()
        client.set_name(hostname())
        client.set_nonce(nonce)
        client.add_verifier(sdk.Devices.GPU, sdk.Environment.LOCAL, "", "")
        evidence = client.get_evidence()
        return {"nonce": nonce, "evidence": json.loads(json.dumps(evidence, default=str))}
    except Exception:
        return {}


def build(nonce: str, settings: Settings, host_key: str = "") -> Attestation | None:
    nonce = (nonce or "").strip().lower()
    if not well_formed_nonce(nonce):
        return None
    key_line = host_key or host_key_line(settings.ssh_host_key)
    data = report_data(key_line, nonce)
    view = nvml.inventory()
    cards = [GpuEvidence.from_payload(card) for card in view["gpus"]]
    return Attestation(
        nonce=nonce,
        agent_version=__version__,
        hostname=hostname(),
        gpus=cards,
        driver=view["driver"],
        cuda=view["cuda"],
        machine_id=host.machine_id(),
        boot_id=boot_id(),
        host_key=key_line,
        report_data_hex=data.hex(),
        tdx_quote=tdx_quote(settings.dstack_socket, data),
        gpu_evidence=gpu_evidence(nonce),
        generated_at=round(time.time(), 3),
    )
