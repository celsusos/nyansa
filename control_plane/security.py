from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import base64
import hashlib
import hmac
import os


PBKDF2_ITERATIONS = 210_000

TOKEN_PEPPER_ENV_VAR = "NYANSA_TOKEN_PEPPER"


def default_pepper_path(state_path: Path) -> Path:
    return state_path.parent / "token_pepper.key"


def load_or_create_pepper(path: Path) -> bytes:
    """Return the host-local token pepper, creating it on first use.

    The pepper is a server-side secret that never leaves the control-plane host.
    It keys the fast token-lookup index so an attacker who can only read the state
    file cannot brute-force low-entropy tokens from the (unsalted) lookup values.
    """
    override = os.environ.get(TOKEN_PEPPER_ENV_VAR)
    if override:
        return override.encode("utf-8")

    if path.exists():
        return path.read_bytes()

    pepper = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    tmp = path.with_suffix(".pepper.tmp")
    tmp.write_bytes(pepper)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return pepper


def token_lookup(pepper: bytes, token: str) -> str:
    """Deterministic, peppered index key for a bearer token.

    Lets authentication find the one candidate user in O(1)-ish time so it runs at
    most a single PBKDF2 verification per request instead of one per user.
    """
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        token.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_token(token: str, token_hash: str) -> bool:
    if token_hash.startswith("pbkdf2_sha256$"):
        _, iterations, encoded_salt, expected_digest = token_hash.split("$", 3)
        salt = base64.b64decode(encoded_salt.encode("ascii"))
        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            token.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(
            base64.b64encode(actual_digest).decode("ascii"),
            expected_digest,
        )

    legacy_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy_hash, token_hash)


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False

    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires
