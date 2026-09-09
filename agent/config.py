from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from protocol.messages import PING_SECONDS, STATE_SECONDS
from protocol.session import (
    NONCE_TTL_SECONDS,
    RELEASE_SKEW_SECONDS,
    SIGNATURE_WINDOW_SECONDS,
    SOCKET_PATH,
)

DEFAULT_SERVER = "https://api.microtensor.cloud"
DEFAULT_IMAGE = "ghcr.io/microtensor-io/rig-agent"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _text(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _path(name: str, default: str) -> Path:
    return Path(_text(name, default))


@dataclass(frozen=True)
class Settings:
    server_url: str = field(default_factory=lambda: _text("RIG_SERVER_URL", DEFAULT_SERVER))
    state_dir: Path = field(default_factory=lambda: _path("RIG_STATE_DIR", "/var/lib/rig-agent"))
    log_dir: Path = field(default_factory=lambda: _path("RIG_LOG_DIR", "/var/lib/rig-agent-logs"))
    internal_port: int = field(default_factory=lambda: _int("RIG_INTERNAL_PORT", 8800))
    external_port: int = field(default_factory=lambda: _int("RIG_EXTERNAL_PORT", 8800))
    ssh_port: int = field(default_factory=lambda: _int("RIG_SSH_PORT", 2200))
    ssh_user: str = field(default_factory=lambda: _text("RIG_SSH_USER", "rig"))
    ssh_home: Path = field(default_factory=lambda: _path("RIG_SSH_HOME", "/home/rig"))
    ssh_host_key: Path = field(
        default_factory=lambda: _path("RIG_SSH_HOST_KEY", "/etc/ssh/ssh_host_ed25519_key.pub")
    )
    public_address: str = field(default_factory=lambda: _text("RIG_PUBLIC_ADDRESS", ""))
    label: str = field(default_factory=lambda: _text("RIG_LABEL", ""))
    port_range: str = field(default_factory=lambda: _text("RIG_PORT_RANGE", "40000-40100"))
    python_path: str = field(default_factory=lambda: _text("RIG_PYTHON", "/usr/bin/python3"))
    root_dir: str = field(default_factory=lambda: _text("RIG_ROOT_DIR", "/opt/mt-agent"))
    image_repository: str = field(default_factory=lambda: _text("AGENT_IMAGE", DEFAULT_IMAGE))
    image_digest: str = field(default_factory=lambda: _text("AGENT_IMAGE_SHA256", ""))
    compose_file: str = field(
        default_factory=lambda: _text("RIG_COMPOSE_FILE", "/opt/rig-agent/docker-compose.yml")
    )
    env_file: str = field(default_factory=lambda: _text("RIG_ENV_FILE", "/opt/rig-agent/.env"))
    update_seconds: int = field(default_factory=lambda: _int("RIG_UPDATE_SECONDS", 300))
    validators_refresh_seconds: int = field(
        default_factory=lambda: _int("RIG_VALIDATORS_REFRESH_SECONDS", 300)
    )
    challenge_library: Path = field(
        default_factory=lambda: _path("RIG_CHALLENGE_LIBRARY", "/usr/lib/libmtchallenge.so")
    )
    dstack_socket: Path = field(
        default_factory=lambda: _path("RIG_DSTACK_SOCKET", "/var/run/dstack.sock")
    )
    nonce_ttl_seconds: int = NONCE_TTL_SECONDS
    ping_seconds: int = PING_SECONDS
    state_seconds: int = STATE_SECONDS
    signature_window_seconds: int = SIGNATURE_WINDOW_SECONDS
    release_skew_seconds: int = RELEASE_SKEW_SECONDS
    grant_sweep_seconds: int = 60

    @property
    def key_path(self) -> Path:
        return self.state_dir / "agent.key"

    @property
    def rig_path(self) -> Path:
        return self.state_dir / "rig.json"

    @property
    def validators_path(self) -> Path:
        return self.state_dir / "validators.json"

    @property
    def prefetch_path(self) -> Path:
        return self.state_dir / "prefetch.json"

    @property
    def approvals_dir(self) -> Path:
        return self.state_dir / "approvals"

    @property
    def events_path(self) -> Path:
        return self.log_dir / "events.jsonl"

    @property
    def authorized_keys(self) -> Path:
        return self.ssh_home / ".ssh" / "authorized_keys"

    @property
    def base_url(self) -> str:
        return self.server_url.rstrip("/")

    @property
    def socket_url(self) -> str:
        base = self.base_url
        scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[1] if "://" in base else base
        return f"{scheme}://{host}{SOCKET_PATH}"

    def image_reference(self, digest: str) -> str:
        return f"{self.image_repository}@{digest}"


def settings() -> Settings:
    return Settings()
