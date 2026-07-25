from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNING_KEY_ENV_VAR = "NYANSA_ATTESTATION_KEY"
SIGNING_KEY_SEED_ENV_VAR = "NYANSA_ATTESTATION_KEY_SEED"


def default_key_path(state_path: Path) -> Path:
    return state_path.parent / "attestation_signing_key.pem"


class AttestationSigner:
    """Ed25519 signer whose private key lives only on the control-plane host.

    Developers authenticate with bearer tokens and never hold this key, so a user
    who can rewrite the state file still cannot forge a valid signature. Anyone can
    verify with the exported public key.
    """

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self._private_key = self._load_or_create()

    def _load_or_create(self) -> Ed25519PrivateKey:
        override = os.environ.get(SIGNING_KEY_ENV_VAR)
        if override:
            return serialization.load_pem_private_key(
                override.encode("utf-8"), password=None
            )

        seed = os.environ.get(SIGNING_KEY_SEED_ENV_VAR)
        if seed:
            return Ed25519PrivateKey.from_private_bytes(
                hashlib.sha256(seed.encode("utf-8")).digest()
            )

        if self.key_path.exists():
            return serialization.load_pem_private_key(
                self.key_path.read_bytes(), password=None
            )

        key = Ed25519PrivateKey.generate()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.key_path.parent, 0o700)

        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        tmp = self.key_path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o600)
        tmp.replace(self.key_path)

        return key

    def public_key_pem(self) -> str:
        return (
            self._private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )

    def key_id(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return hashlib.sha256(raw).hexdigest()[:16]

    def sign(self, message: str) -> str:
        signature = self._private_key.sign(message.encode("utf-8"))
        return base64.b64encode(signature).decode("ascii")

    def verify(self, message: str, signature: str) -> bool:
        return verify_signature(self.public_key_pem(), message, signature)


def verify_signature(public_key_pem: str, message: str, signature: str) -> bool:
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(base64.b64decode(signature), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError):
        return False
