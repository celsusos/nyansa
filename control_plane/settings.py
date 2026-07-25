from __future__ import annotations

import os
from pydantic import BaseModel

from control_plane.models import RoleName


class ControlPlaneSettings(BaseModel):
    production: bool = False
    bootstrap_secret: str | None = None
    database_url: str | None = None
    token_pepper_present: bool = False
    attestation_key_present: bool = False
    # Requests allowed per client IP per minute (0 disables the limiter).
    rate_limit_per_minute: int = 120
    # Default lifetime for issued member tokens; 0 = never expire.
    token_ttl_hours: int = 720
    microsoft_enabled: bool = False
    microsoft_tenant: str = "organizations"
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_redirect_uri: str | None = None
    microsoft_allowed_domains: list[str] = []
    microsoft_domain_org_map: dict[str, str] = {}
    microsoft_auto_provision: bool = True
    microsoft_default_role: RoleName = "developer"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _mapping_env(name: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in os.environ.get(name, "").split(","):
        raw = item.strip()
        if not raw or ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def load_settings() -> ControlPlaneSettings:
    production = os.environ.get("NYANSA_PRODUCTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    microsoft_domains = [
        domain.strip().lower()
        for domain in os.environ.get("NYANSA_MICROSOFT_ALLOWED_DOMAINS", "").split(",")
        if domain.strip()
    ]
    microsoft_role = os.environ.get("NYANSA_MICROSOFT_DEFAULT_ROLE", "developer").strip()
    if microsoft_role not in {
        "root",
        "platform_owner",
        "org_owner",
        "security_admin",
        "developer",
        "intern",
        "auditor",
    }:
        microsoft_role = "developer"
    return ControlPlaneSettings(
        production=production,
        bootstrap_secret=os.environ.get("NYANSA_BOOTSTRAP_SECRET"),
        database_url=os.environ.get("NYANSA_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        token_pepper_present=bool(os.environ.get("NYANSA_TOKEN_PEPPER")),
        attestation_key_present=bool(
            os.environ.get("NYANSA_ATTESTATION_KEY")
            or os.environ.get("NYANSA_ATTESTATION_KEY_SEED")
        ),
        rate_limit_per_minute=_int_env("NYANSA_RATE_LIMIT_PER_MINUTE", 120),
        token_ttl_hours=_int_env("NYANSA_TOKEN_TTL_HOURS", 720),
        microsoft_enabled=os.environ.get("NYANSA_MICROSOFT_ENABLED", "").lower()
        in {"1", "true", "yes"},
        microsoft_tenant=os.environ.get("NYANSA_MICROSOFT_TENANT", "organizations"),
        microsoft_client_id=os.environ.get("NYANSA_MICROSOFT_CLIENT_ID"),
        microsoft_client_secret=os.environ.get("NYANSA_MICROSOFT_CLIENT_SECRET"),
        microsoft_redirect_uri=os.environ.get("NYANSA_MICROSOFT_REDIRECT_URI"),
        microsoft_allowed_domains=microsoft_domains,
        microsoft_domain_org_map=_mapping_env("NYANSA_MICROSOFT_DOMAIN_ORG_MAP"),
        microsoft_auto_provision=os.environ.get(
            "NYANSA_MICROSOFT_AUTO_PROVISION", "true"
        ).lower()
        in {"1", "true", "yes"},
        microsoft_default_role=microsoft_role,  # type: ignore[arg-type]
    )
