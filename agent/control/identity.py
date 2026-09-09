from __future__ import annotations

import os
import time
from pathlib import Path

from protocol.messages import Hello
from protocol.session import HEADER_AGENT, HEADER_SIGNATURE, HEADER_TIMESTAMP, signing_bytes


def stamp() -> str:
    return f"{time.time():.3f}"


class AgentKey:
    def __init__(self, private_bytes: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if len(private_bytes) != 32:
            raise ValueError("agent key must be 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        self._public = self._raw_public()

    def _raw_public(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def generate(cls) -> AgentKey:
        return cls(os.urandom(32))

    @classmethod
    def load_or_create(cls, path: Path) -> AgentKey:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        if text:
            return cls(bytes.fromhex(text))
        key = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(key.private_hex, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        return key

    @property
    def private_hex(self) -> str:
        from cryptography.hazmat.primitives import serialization

        raw = self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return raw.hex()

    @property
    def public_hex(self) -> str:
        return self._public.hex()

    def sign(self, message: bytes) -> str:
        return self._key.sign(message).hex()

    def headers(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        timestamp = stamp()
        return {
            HEADER_AGENT: self.public_hex,
            HEADER_TIMESTAMP: timestamp,
            HEADER_SIGNATURE: self.sign(signing_bytes(method, path, timestamp, body)),
        }

    def hello(self, path: str) -> Hello:
        timestamp = stamp()
        return Hello(
            agent=self.public_hex,
            timestamp=timestamp,
            signature=self.sign(signing_bytes("WS", path, timestamp, b"")),
        )
