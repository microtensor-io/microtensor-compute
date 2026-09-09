from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SessionOpenBody(BaseModel):
    public_key: str = Field(min_length=1, max_length=4096)
    nonce: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=1, max_length=512)


class SessionCloseBody(BaseModel):
    public_key: str = Field(default="", max_length=4096)
    session_id: str = Field(default="", max_length=64)


class JobBody(BaseModel):
    job_id: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="", max_length=40)
    subnet: str = Field(default="", max_length=40)
    pid: int = Field(default=0, ge=0)
    gpu_uuid: str = Field(default="", max_length=80)
    container: str = Field(default="", max_length=128)
    started_at: str = Field(default="", max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class VersionResponse(BaseModel):
    version: str
    protocol: int
    rig_id: str = ""
    image: str = ""
    draining: bool = False
    challenge_library: bool = False


class PingResponse(BaseModel):
    ok: bool = True
    rig_id: str = ""
    version: str = ""
    draining: bool = False
    at: float = 0.0


class SessionGrantResponse(BaseModel):
    ssh_username: str
    ssh_port: int
    ssh_host_key: str
    python_path: str
    root_dir: str
    port_range: str
    gpu_attestation: dict[str, Any] = Field(default_factory=dict)
    tdx_quote: str = ""
    validator: str = ""
    expires_at: str = ""
    session_id: str = ""


class SessionCloseResponse(BaseModel):
    revoked: int = 0


class JobResponse(BaseModel):
    recorded: str
    jobs: int


class ErrorResponse(BaseModel):
    detail: str
    stage: str = ""
