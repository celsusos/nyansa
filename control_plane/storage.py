from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config.loader import get_data_dir
from control_plane.models import (
    ActorResponse,
    AgentSessionRecord,
    ApprovalRequestRecord,
    AttestationRecord,
    AttestationVerificationResponse,
    AuditEventRecord,
    AuditVerificationResponse,
    BootstrapRequest,
    CliTokenRecord,
    ControlPlaneState,
    CreateApprovalRequest,
    CreateOrganizationRequest,
    DecideApprovalRequest,
    GateCheckResponse,
    IngestAuditEventRequest,
    IntegrationRecord,
    InternEscalationRequest,
    MembershipRecord,
    ModelCatalogEntry,
    NetworkPolicyRecord,
    OrgRolePolicyRequest,
    OrganizationRecord,
    ProductionReadinessResponse,
    ProjectProfileRecord,
    RecordAttestationRequest,
    RoleName,
    RoleDashboardResponse,
    RotateTokenRequest,
    SetModelRequest,
    EndSessionRequest,
    RegisterSessionRequest,
    SessionHeartbeatRequest,
    SessionTurnRequest,
    UpdateUsagePolicyRequest,
    UsageCheckRequest,
    UsageCheckResponse,
    UsageEventRecord,
    UsagePolicyRecord,
    UpdateRolePolicyRequest,
    UpdateNetworkPolicyRequest,
    UpsertModelCatalogRequest,
    UpsertIntegrationRequest,
    UpsertMemberRequest,
    UpsertProjectProfileRequest,
    UserRecord,
)
from control_plane.security import (
    default_pepper_path,
    hash_token,
    is_expired,
    load_or_create_pepper,
    token_lookup as compute_token_lookup,
    verify_token,
)
from control_plane.signing import (
    AttestationSigner,
    default_key_path,
    verify_signature,
)
from safety.policy import ROLE_TOOL_ALLOWLISTS


DEFAULT_USAGE_POLICIES: dict[str, UsagePolicyRecord] = {
    "root": UsagePolicyRecord(
        requests_per_minute=60,
        requests_per_hour=600,
        requests_per_day=5000,
        org_requests_per_day=10000,
        max_turns_per_session=200,
        max_session_minutes=120,
        max_context_tokens=256000,
    ),
    "platform_owner": UsagePolicyRecord(
        requests_per_minute=40,
        requests_per_hour=400,
        requests_per_day=3000,
        org_requests_per_day=8000,
        max_turns_per_session=200,
        max_session_minutes=120,
        max_context_tokens=256000,
    ),
    "org_owner": UsagePolicyRecord(
        requests_per_minute=20,
        requests_per_hour=200,
        requests_per_day=1000,
        org_requests_per_day=3000,
        max_turns_per_session=120,
        max_session_minutes=60,
        max_context_tokens=128000,
    ),
    "security_admin": UsagePolicyRecord(
        requests_per_minute=10,
        requests_per_hour=100,
        requests_per_day=500,
        org_requests_per_day=3000,
        max_turns_per_session=80,
        max_session_minutes=45,
        max_context_tokens=128000,
    ),
    "developer": UsagePolicyRecord(
        requests_per_minute=5,
        requests_per_hour=40,
        requests_per_day=200,
        org_requests_per_day=1000,
        max_turns_per_session=100,
        max_session_minutes=20,
        max_context_tokens=64000,
    ),
    "intern": UsagePolicyRecord(
        requests_per_minute=2,
        requests_per_hour=10,
        requests_per_day=50,
        org_requests_per_day=1000,
        max_turns_per_session=30,
        max_session_minutes=10,
        max_context_tokens=32000,
    ),
    "auditor": UsagePolicyRecord(
        enabled=False,
        requests_per_minute=1,
        requests_per_hour=5,
        requests_per_day=10,
        org_requests_per_day=1000,
        max_turns_per_session=10,
        max_session_minutes=10,
        max_context_tokens=32000,
    ),
}

DEFAULT_MODEL_CATALOG: dict[str, ModelCatalogEntry] = {
    "poolside/laguna-s-2.1:free": ModelCatalogEntry(
        id="poolside/laguna-s-2.1:free",
        label="Laguna S 2.1 Free",
        provider="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
        free=True,
        notes="Default free OpenRouter coding model. Requires an OpenRouter key.",
    ),
    "openai/gpt-4o-mini": ModelCatalogEntry(
        id="openai/gpt-4o-mini",
        label="OpenAI GPT-4o mini",
        provider="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
        notes="Paid OpenRouter-hosted fallback with higher reliability than free models.",
    ),
    "qwen2.5-coder:7b": ModelCatalogEntry(
        id="qwen2.5-coder:7b",
        label="Qwen2.5 Coder 7B Local",
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        requires_api_key=False,
        free=True,
        notes="Local Ollama-compatible model for private/offline development.",
    ),
    "sonnet": ModelCatalogEntry(
        id="sonnet",
        label="Claude Code Subscription",
        provider="claude_code",
        base_url=None,
        requires_api_key=False,
        notes="Uses the locally authenticated Claude Code CLI subscription.",
    ),
}


class ControlPlaneError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def audit_event_hash(event: AuditEventRecord) -> str:
    payload = event.model_dump(
        exclude={"event_hash"},
        mode="json",
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def attestation_entry_hash(attestation: AttestationRecord) -> str:
    payload = attestation.model_dump(
        exclude={"entry_hash", "signature", "signing_key_id"},
        mode="json",
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_state_path() -> Path:
    return get_data_dir() / "control-plane" / "state.json"


class ControlPlaneStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()
        self._signer: AttestationSigner | None = None
        self._pepper: bytes | None = None
        # Serializes read-modify-write cycles. FastAPI runs sync path operations in
        # a threadpool, so without this two concurrent writers could each load the
        # same state, mutate, and save — losing events and breaking the audit chain.
        self._lock = threading.RLock()

    @property
    def signer(self) -> AttestationSigner:
        if self._signer is None:
            self._signer = AttestationSigner(default_key_path(self.path))
        return self._signer

    @property
    def _token_pepper(self) -> bytes:
        if self._pepper is None:
            self._pepper = load_or_create_pepper(default_pepper_path(self.path))
        return self._pepper

    def _apply_token(self, user: UserRecord, token: str, expires_at: str | None) -> None:
        """Set a user's verification hash and fast-lookup index, and re-enable them."""
        user.token_hash = hash_token(token)
        user.token_lookup = compute_token_lookup(self._token_pepper, token)
        user.token_expires_at = expires_at
        user.disabled = False

    def _apply_password(self, user: UserRecord, password: str) -> None:
        user.password_hash = hash_token(password)

    def _apply_session_token(self, user: UserRecord, token: str, expires_at: str) -> None:
        user.session_token_hash = hash_token(token)
        user.session_token_lookup = compute_token_lookup(self._token_pepper, token)
        user.session_token_expires_at = expires_at

    def _default_member_expiry(self, expires_at: str | None) -> str | None:
        """Apply a default lifetime to issued member tokens so none are eternal.

        An explicit expiry from the caller wins. Otherwise `NYANSA_TOKEN_TTL_HOURS`
        (default 720h / 30 days) applies; set it to 0 to keep tokens non-expiring.
        The root bootstrap token is intentionally exempt to avoid a lockout.
        """
        if expires_at:
            return expires_at
        raw = os.environ.get("NYANSA_TOKEN_TTL_HOURS", "720").strip()
        try:
            hours = max(int(raw), 0)
        except ValueError:
            hours = 720
        if hours <= 0:
            return None
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    def _normalize_domains(self, domains: list[str]) -> list[str]:
        return sorted(
            {
                domain.strip().lower().lstrip("@")
                for domain in domains
                if domain.strip()
            }
        )

    def _resolve_microsoft_organization(
        self,
        state: ControlPlaneState,
        *,
        user_id: str | None,
        email_domain: str,
        domain_org_map: dict[str, str],
    ) -> OrganizationRecord:
        mapped = domain_org_map.get(email_domain)
        if mapped:
            for org in state.organizations.values():
                if org.id == mapped or org.name.lower() == mapped.lower():
                    return org
            raise ControlPlaneError(403, "Microsoft domain is mapped to an unknown organization")

        for org in state.organizations.values():
            if email_domain in self._normalize_domains(org.domains):
                return org

        if user_id:
            memberships = [
                membership
                for membership in state.memberships
                if membership.user_id == user_id
            ]
            if len(memberships) == 1:
                org = state.organizations.get(memberships[0].organization_id)
                if org:
                    return org
            if len(memberships) > 1:
                raise ControlPlaneError(
                    409,
                    "Microsoft user belongs to multiple organizations; configure domain mapping",
                )

        if len(state.organizations) == 1:
            return next(iter(state.organizations.values()))

        raise ControlPlaneError(
            409,
            "Microsoft domain is not mapped to an organization",
        )

    def _load(self) -> ControlPlaneState:
        if not self.path.exists():
            return self._with_default_policies(ControlPlaneState())

        with open(self.path, "r", encoding="utf-8") as fp:
            return self._with_default_policies(ControlPlaneState(**json.load(fp)))

    def _with_default_policies(self, state: ControlPlaneState) -> ControlPlaneState:
        if not state.role_policies:
            state.role_policies = {
                role: sorted(allowlist) if allowlist is not None else None
                for role, allowlist in ROLE_TOOL_ALLOWLISTS.items()
            }
        if not state.usage_policies:
            state.usage_policies = {
                role: policy.model_copy(deep=True)
                for role, policy in DEFAULT_USAGE_POLICIES.items()
            }
        for model_id, entry in DEFAULT_MODEL_CATALOG.items():
            state.model_catalog.setdefault(model_id, entry.model_copy(deep=True))
        return state

    def _save(self, state: ControlPlaneState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)

        tmp_path = self.path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(state.model_dump(mode="json"), fp, indent=2, sort_keys=True)
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(self.path)

    def is_bootstrapped(self) -> bool:
        state = self._load()
        return bool(state.users and state.organizations)

    def bootstrap(self, request: BootstrapRequest) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            if state.users or state.organizations:
                raise ControlPlaneError(409, "Control plane is already bootstrapped")

            root_token = request.root_token or secrets.token_urlsafe(32)
            organization = OrganizationRecord(
                name=request.organization_name,
                domains=self._normalize_domains(request.organization_domains),
            )
            user = UserRecord(
                id=request.root_user_id,
                display_name=request.root_display_name,
                token_hash="",
                platform_role="root",
            )
            self._apply_token(user, root_token, request.root_token_expires_at)
            if request.root_password:
                self._apply_password(user, request.root_password)
            membership = MembershipRecord(
                user_id=user.id,
                organization_id=organization.id,
                role="root",
            )

            state.organizations[organization.id] = organization
            state.users[user.id] = user
            state.memberships.append(membership)
            self._save(state)

            return {
                "organization": organization.model_dump(mode="json"),
                "root_user": user.model_dump(
                    exclude={
                        "token_hash",
                        "token_lookup",
                        "password_hash",
                        "session_token_hash",
                        "session_token_lookup",
                    },
                    mode="json",
                ),
                "root_token": root_token if request.root_token is None else None,
            }

    def authenticate(self, token: str | None) -> UserRecord:
        if not token:
            raise ControlPlaneError(401, "Missing bearer token")

        lookup = compute_token_lookup(self._token_pepper, token)
        with self._lock:
            state = self._load()

            # Fast path: at most one PBKDF2 verification for primary/session tokens,
            # keyed by the peppered index.
            indexed = self._find_by_lookup(state, lookup)
            if indexed is not None:
                if indexed.token_lookup and hmac.compare_digest(indexed.token_lookup, lookup):
                    token_hash = indexed.token_hash
                    expires_at = indexed.token_expires_at
                else:
                    token_hash = indexed.session_token_hash or ""
                    expires_at = indexed.session_token_expires_at
                if not verify_token(token, token_hash):
                    raise ControlPlaneError(401, "Invalid bearer token")
                return self._finalize_auth(indexed, expires_at=expires_at)

            cli_match = self._find_cli_token_by_lookup(state, lookup)
            if cli_match is not None:
                cli_user, cli_token = cli_match
                if cli_token.revoked_at:
                    raise ControlPlaneError(401, "Token revoked")
                if not verify_token(token, cli_token.token_hash):
                    raise ControlPlaneError(401, "Invalid bearer token")
                authed = self._finalize_auth(cli_user, expires_at=cli_token.token_expires_at)
                cli_token.last_used_at = datetime.now(timezone.utc).isoformat()
                self._save(state)
                return authed

        # Legacy fallback: users created before the lookup index existed. Bounded to
        # un-indexed users and each is migrated on first successful authentication.
        for user in state.users.values():
            if user.token_lookup:
                continue
            if verify_token(token, user.token_hash):
                self._migrate_token_index(user.id, token)
                return self._finalize_auth(user, expires_at=user.token_expires_at)

        raise ControlPlaneError(401, "Invalid bearer token")

    def _find_by_lookup(self, state: ControlPlaneState, lookup: str) -> UserRecord | None:
        for user in state.users.values():
            if user.token_lookup and hmac.compare_digest(user.token_lookup, lookup):
                return user
            if user.session_token_lookup and hmac.compare_digest(user.session_token_lookup, lookup):
                return user
        return None

    def _find_cli_token_by_lookup(
        self, state: ControlPlaneState, lookup: str
    ) -> tuple[UserRecord, CliTokenRecord] | None:
        for user in state.users.values():
            for cli_token in user.cli_tokens:
                if hmac.compare_digest(cli_token.token_lookup, lookup):
                    return user, cli_token
        return None

    def _finalize_auth(self, user: UserRecord, *, expires_at: str | None) -> UserRecord:
        if user.disabled:
            raise ControlPlaneError(401, "User is disabled")
        if is_expired(expires_at):
            raise ControlPlaneError(401, "Token expired")
        return user

    def login(self, user_id: str, password: str) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            user = state.users.get(user_id)
            if not user or user.disabled or not user.password_hash:
                raise ControlPlaneError(401, "Invalid user id or password")
            if not verify_token(password, user.password_hash):
                raise ControlPlaneError(401, "Invalid user id or password")
            session_token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            self._apply_session_token(user, session_token, expires_at)
            self._save(state)
        actor = self.actor_for_user(user)
        return {
            "token": session_token,
            "token_expires_at": expires_at,
            "actor": actor.model_dump(mode="json"),
        }

    def login_microsoft(
        self,
        *,
        subject: str,
        email: str,
        display_name: str,
        default_role: RoleName = "developer",
        auto_provision: bool = True,
        domain_org_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if default_role in {"root", "platform_owner"}:
            raise ControlPlaneError(400, "Microsoft default role cannot be a platform role")

        with self._lock:
            state = self._load()
            if not state.organizations:
                raise ControlPlaneError(503, "Control plane is not bootstrapped")

            user = next(
                (
                    candidate
                    for candidate in state.users.values()
                    if candidate.microsoft_subject == subject
                ),
                None,
            )
            if user is None and email:
                user = state.users.get(email)

            email_domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
            organization = self._resolve_microsoft_organization(
                state,
                user_id=user.id if user else email,
                email_domain=email_domain,
                domain_org_map=domain_org_map or {},
            )

            if user is None:
                if not auto_provision:
                    raise ControlPlaneError(403, "Microsoft user is not provisioned")
                user = UserRecord(
                    id=email or f"microsoft-{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:16]}",
                    display_name=display_name or email,
                    token_hash="",
                    microsoft_subject=subject,
                    email=email,
                )
                state.users[user.id] = user
                state.memberships.append(
                    MembershipRecord(
                        user_id=user.id,
                        organization_id=organization.id,
                        role=default_role,
                    )
                )
            else:
                user.microsoft_subject = user.microsoft_subject or subject
                user.email = user.email or email
                if display_name:
                    user.display_name = user.display_name or display_name
                if not self._membership(state, user.id, organization.id):
                    if not auto_provision:
                        raise ControlPlaneError(403, "Microsoft user is not a member of this organization")
                    state.memberships.append(
                        MembershipRecord(
                            user_id=user.id,
                            organization_id=organization.id,
                            role=default_role,
                        )
                    )

            if user.disabled:
                raise ControlPlaneError(401, "User is disabled")

            session_token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            self._apply_session_token(user, session_token, expires_at)
            self._save(state)

        actor = self.actor_for_user(user)
        return {
            "token": session_token,
            "token_expires_at": expires_at,
            "actor": actor.model_dump(mode="json"),
        }

    def issue_cli_token(
        self,
        user: UserRecord,
        name: str = "CLI token",
        token_expires_at: str | None = None,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        with self._lock:
            state = self._load()
            target = state.users.get(user.id)
            if not target:
                raise ControlPlaneError(404, "User not found")
            record = CliTokenRecord(
                name=name.strip() or "CLI token",
                token_hash=hash_token(token),
                token_lookup=compute_token_lookup(self._token_pepper, token),
                token_expires_at=self._default_member_expiry(token_expires_at),
            )
            target.cli_tokens.append(record)
            self._save(state)
        return {
            "id": record.id,
            "name": record.name,
            "token": token,
            "token_expires_at": record.token_expires_at,
        }

    def list_cli_tokens(self, user: UserRecord) -> list[dict[str, Any]]:
        state = self._load()
        target = state.users.get(user.id)
        if not target:
            raise ControlPlaneError(404, "User not found")
        return [
            token.model_dump(
                exclude={"token_hash", "token_lookup"},
                mode="json",
            )
            for token in sorted(
                target.cli_tokens,
                key=lambda item: item.created_at,
                reverse=True,
            )
        ]

    def revoke_cli_token(self, user: UserRecord, token_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            target = state.users.get(user.id)
            if not target:
                raise ControlPlaneError(404, "User not found")
            for token in target.cli_tokens:
                if token.id == token_id:
                    token.revoked_at = token.revoked_at or datetime.now(timezone.utc).isoformat()
                    self._save(state)
                    return token.model_dump(
                        exclude={"token_hash", "token_lookup"},
                        mode="json",
                    )
        raise ControlPlaneError(404, "CLI token not found")

    def _migrate_token_index(self, user_id: str, token: str) -> None:
        with self._lock:
            state = self._load()
            user = state.users.get(user_id)
            if not user:
                return
            user.token_lookup = compute_token_lookup(self._token_pepper, token)
            if not user.token_hash.startswith("pbkdf2_sha256$"):
                user.token_hash = hash_token(token)
            self._save(state)

    def _require_platform_role(self, user: UserRecord) -> None:
        if user.platform_role not in {"root", "platform_owner"}:
            raise ControlPlaneError(403, "Requires root or platform_owner")

    def _membership(self, state: ControlPlaneState, user_id: str, org_id: str) -> MembershipRecord | None:
        for membership in state.memberships:
            if membership.user_id == user_id and membership.organization_id == org_id:
                return membership
        return None

    def _require_org_admin(self, state: ControlPlaneState, user: UserRecord, org_id: str) -> None:
        if user.platform_role in {"root", "platform_owner"}:
            return

        membership = self._membership(state, user.id, org_id)
        if membership and membership.role in {"org_owner", "security_admin"}:
            return

        raise ControlPlaneError(403, "Requires organization admin")

    def actor_for_user(self, user: UserRecord, organization_id: str | None = None) -> ActorResponse:
        state = self._load()
        membership = None

        if organization_id:
            membership = self._membership(state, user.id, organization_id)
            if not membership and user.platform_role not in {"root", "platform_owner"}:
                raise ControlPlaneError(403, "User is not a member of this organization")
        else:
            membership = next((m for m in state.memberships if m.user_id == user.id), None)

        # Fail closed: a user with neither a membership nor a platform role has no
        # role. Previously this defaulted to "developer", so a user deprovisioned by
        # membership removal kept an implicit global developer role.
        org_id = organization_id
        if membership:
            role: RoleName = membership.role
            org_id = membership.organization_id
        elif user.platform_role:
            role = user.platform_role
        else:
            raise ControlPlaneError(403, "User has no active role assignment")

        model_name = state.model_settings.get(org_id) if org_id else None
        model_entry = state.model_catalog.get(model_name) if model_name else None
        network_policy = state.network_policies.get(org_id) if org_id else None
        return ActorResponse(
            user_id=user.id,
            organization_id=org_id,
            role=role,
            platform_role=user.platform_role,
            allowed_tools=(
                state.org_role_policies.get(org_id, {}).get(role)
                if org_id
                and role in {"developer", "intern", "auditor"}
                and role in state.org_role_policies.get(org_id, {})
                else state.role_policies.get(role)
            ),
            usage_policy=state.usage_policies.get(role),
            model_name=model_name,
            model_provider=model_entry.provider if model_entry else None,
            model_base_url=model_entry.base_url if model_entry else None,
            model_requires_api_key=(
                model_entry.requires_api_key if model_entry else None
            ),
            network_policy=network_policy,
        )

    def list_model_catalog(self, user: UserRecord) -> list[dict[str, Any]]:
        self.actor_for_user(user)
        state = self._load()
        entries = state.model_catalog.values()
        if user.platform_role not in {"root", "platform_owner"}:
            entries = [entry for entry in entries if entry.enabled]
        return [
            entry.model_dump(mode="json")
            for entry in sorted(entries, key=lambda item: item.label.lower())
        ]

    def upsert_model_catalog_entry(
        self, user: UserRecord, request: UpsertModelCatalogRequest
    ) -> dict[str, Any]:
        self._require_platform_role(user)
        model_id = request.id.strip()
        if not model_id:
            raise ControlPlaneError(400, "model id is required")
        with self._lock:
            state = self._load()
            entry = ModelCatalogEntry(
                id=model_id,
                label=request.label.strip(),
                provider=request.provider,
                base_url=request.base_url.strip() if request.base_url else None,
                requires_api_key=request.requires_api_key,
                enabled=request.enabled,
                free=request.free,
                notes=request.notes.strip(),
            )
            state.model_catalog[model_id] = entry
            if not entry.enabled:
                disabled_orgs = [
                    org_id
                    for org_id, selected_model in state.model_settings.items()
                    if selected_model == model_id
                ]
                for org_id in disabled_orgs:
                    state.model_settings.pop(org_id, None)
            self._save(state)
            return entry.model_dump(mode="json")

    def _normalize_domains_for_policy(self, domains: list[str]) -> list[str]:
        return sorted(
            {
                domain.strip().lower().lstrip("@")
                for domain in domains
                if domain.strip()
            }
        )

    def get_network_policy(
        self, user: UserRecord, organization_id: str | None = None
    ) -> dict[str, Any]:
        actor = self.actor_for_user(user, organization_id)
        if not actor.organization_id:
            raise ControlPlaneError(400, "organization_id is required")
        state = self._load()
        policy = state.network_policies.get(actor.organization_id) or NetworkPolicyRecord()
        return {
            "organization_id": actor.organization_id,
            "policy": policy.model_dump(mode="json"),
        }

    def update_network_policy(
        self, user: UserRecord, request: UpdateNetworkPolicyRequest
    ) -> dict[str, Any]:
        org_id = request.organization_id or self.actor_for_user(user).organization_id
        if not org_id:
            raise ControlPlaneError(400, "organization_id is required")
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, org_id)
            policy = NetworkPolicyRecord(
                allow_network=request.allow_network,
                allowed_domains=self._normalize_domains_for_policy(request.allowed_domains),
                blocked_domains=self._normalize_domains_for_policy(request.blocked_domains),
                require_proxy=request.require_proxy,
                proxy_url=request.proxy_url.strip() if request.proxy_url else None,
            )
            state.network_policies[org_id] = policy
            self._save(state)
        self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type="policy.network.updated",
                organization_id=org_id,
                details=policy.model_dump(mode="json"),
            ),
        )
        return {"organization_id": org_id, "policy": policy.model_dump(mode="json")}

    def list_integrations(
        self, user: UserRecord, organization_id: str | None = None
    ) -> list[dict[str, Any]]:
        state = self._load()
        actor = self.actor_for_user(user, organization_id)
        if user.platform_role in {"root", "platform_owner"}:
            integrations = state.integrations.values()
        elif actor.organization_id and actor.role in {"org_owner", "security_admin", "auditor"}:
            integrations = [
                integration
                for integration in state.integrations.values()
                if integration.organization_id == actor.organization_id
            ]
        else:
            raise ControlPlaneError(403, "Requires organization admin or auditor")
        return [
            integration.model_dump(mode="json")
            for integration in sorted(integrations, key=lambda item: item.name.lower())
        ]

    def upsert_integration(
        self, user: UserRecord, request: UpsertIntegrationRequest
    ) -> dict[str, Any]:
        org_id = request.organization_id
        with self._lock:
            state = self._load()
            if org_id:
                self._require_org_admin(state, user, org_id)
            else:
                self._require_platform_role(user)
            integration_id = request.id or str(uuid.uuid4())
            existing = state.integrations.get(integration_id)
            if existing and existing.organization_id:
                self._require_org_admin(state, user, existing.organization_id)
            integration = IntegrationRecord(
                id=integration_id,
                provider=request.provider,
                name=request.name.strip(),
                organization_id=org_id,
                enabled=request.enabled,
                config=request.config,
                created_at=existing.created_at if existing else datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            state.integrations[integration_id] = integration
            self._save(state)
        self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type="integration.updated",
                organization_id=org_id,
                details={
                    "integration_id": integration.id,
                    "provider": integration.provider,
                    "enabled": integration.enabled,
                },
            ),
        )
        return integration.model_dump(mode="json")

    def list_project_profiles(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        repository: str | None = None,
    ) -> list[dict[str, Any]]:
        state = self._load()
        if user.platform_role in {"root", "platform_owner"}:
            profiles = list(state.project_profiles.values())
        else:
            memberships = [
                membership
                for membership in state.memberships
                if membership.user_id == user.id
            ]
            allowed_org_ids = {membership.organization_id for membership in memberships}
            profiles = [
                profile
                for profile in state.project_profiles.values()
                if profile.organization_id in allowed_org_ids
            ]
        if organization_id:
            if user.platform_role not in {"root", "platform_owner"}:
                if not self._membership(state, user.id, organization_id):
                    raise ControlPlaneError(403, "User is not a member of this organization")
            profiles = [
                profile
                for profile in profiles
                if profile.organization_id == organization_id
            ]
        if repository:
            normalized_repository = repository.strip().lower()
            profiles = [
                profile
                for profile in profiles
                if profile.repository.lower() == normalized_repository
            ]
        return [
            profile.model_dump(mode="json")
            for profile in sorted(profiles, key=lambda item: item.repository.lower())
        ]

    def upsert_project_profile(
        self, user: UserRecord, request: UpsertProjectProfileRequest
    ) -> dict[str, Any]:
        org_id = request.organization_id or self.actor_for_user(user).organization_id
        if not org_id:
            raise ControlPlaneError(400, "organization_id is required")
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, org_id)
            if org_id not in state.organizations:
                raise ControlPlaneError(404, "Organization not found")

            profile_id = (
                request.id.strip()
                if request.id and request.id.strip()
                else str(uuid.uuid4())
            )
            existing = state.project_profiles.get(profile_id)
            if existing and existing.organization_id != org_id:
                raise ControlPlaneError(409, "Project profile belongs to another organization")
            profile = ProjectProfileRecord(
                id=profile_id,
                organization_id=org_id,
                repository=request.repository.strip(),
                default_branch=request.default_branch.strip(),
                verification_commands=self._normalize_profile_list(
                    request.verification_commands
                ),
                allowed_domains=self._normalize_domains_for_policy(
                    request.allowed_domains
                ),
                issue_tracker_url=(
                    request.issue_tracker_url.strip()
                    if request.issue_tracker_url
                    else None
                ),
                secret_env_vars=self._normalize_profile_list(
                    request.secret_env_vars,
                    lowercase=False,
                ),
                created_at=existing.created_at if existing else datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            state.project_profiles[profile.id] = profile
            self._save(state)
        self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type="project_profile.updated",
                organization_id=org_id,
                details={
                    "project_profile_id": profile.id,
                    "repository": profile.repository,
                    "verification_commands": profile.verification_commands,
                    "allowed_domains": profile.allowed_domains,
                },
            ),
        )
        return profile.model_dump(mode="json")

    def _normalize_profile_list(
        self,
        values: list[str],
        *,
        lowercase: bool = False,
    ) -> list[str]:
        normalized = []
        seen = set()
        for value in values:
            item = value.strip()
            if not item:
                continue
            key = item.lower() if lowercase else item
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key if lowercase else item)
        return normalized

    def create_approval_request(
        self, user: UserRecord, request: CreateApprovalRequest
    ) -> dict[str, Any]:
        actor = self.actor_for_user(user, request.organization_id)
        with self._lock:
            state = self._load()
            approval_id = str(uuid.uuid4())
            record = ApprovalRequestRecord(
                id=approval_id,
                organization_id=actor.organization_id,
                requester_user_id=actor.user_id,
                requester_role=actor.role,
                request_type=request.request_type,
                target=request.target.strip(),
                reason=request.reason.strip(),
            )
            state.approval_requests[approval_id] = record
            self._save(state)
        self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type="approval.requested",
                organization_id=record.organization_id,
                details=record.model_dump(mode="json"),
            ),
        )
        return record.model_dump(mode="json")

    def list_approval_requests(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        state = self._load()
        actor = self.actor_for_user(user, organization_id)
        if user.platform_role in {"root", "platform_owner"}:
            approvals = state.approval_requests.values()
        elif actor.organization_id and actor.role in {"org_owner", "security_admin", "auditor"}:
            approvals = [
                approval
                for approval in state.approval_requests.values()
                if approval.organization_id == actor.organization_id
            ]
        else:
            approvals = [
                approval
                for approval in state.approval_requests.values()
                if approval.requester_user_id == user.id
            ]
        if status:
            approvals = [approval for approval in approvals if approval.status == status]
        return [
            approval.model_dump(mode="json")
            for approval in sorted(
                approvals,
                key=lambda item: item.created_at,
                reverse=True,
            )
        ]

    def decide_approval_request(
        self, user: UserRecord, approval_id: str, request: DecideApprovalRequest
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            approval = state.approval_requests.get(approval_id)
            if not approval:
                raise ControlPlaneError(404, "Approval request not found")
            if approval.organization_id:
                self._require_org_admin(state, user, approval.organization_id)
            else:
                self._require_platform_role(user)
            approval.status = request.status
            approval.decided_by = user.id
            approval.decided_at = datetime.now(timezone.utc).isoformat()
            self._save(state)
        self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type=f"approval.{request.status}",
                organization_id=approval.organization_id,
                details=approval.model_dump(mode="json"),
            ),
        )
        return approval.model_dump(mode="json")

    def production_readiness(self, user: UserRecord) -> ProductionReadinessResponse:
        self._require_platform_role(user)
        state = self._load()
        has_enabled_model = any(entry.enabled for entry in state.model_catalog.values())
        org_ids = set(state.organizations)
        checks = {
            "bootstrapped": bool(state.users and state.organizations),
            "approved_model_catalog": has_enabled_model,
            "org_model_selected": bool(org_ids)
            and all(org_id in state.model_settings for org_id in org_ids),
            "network_policy_per_org": bool(org_ids)
            and all(org_id in state.network_policies for org_id in org_ids),
            "role_policies_configured": bool(state.role_policies),
            "usage_policies_configured": bool(state.usage_policies),
            "audit_chain_valid": self.verify_audit_events_for_system().valid,
            "microsoft_domains_mapped": all(
                bool(org.domains) for org in state.organizations.values()
            ),
            "enabled_integrations_registered": any(
                integration.enabled for integration in state.integrations.values()
            ),
            "project_profiles_registered": any(state.project_profiles.values()),
            "no_pending_approvals": not any(
                approval.status == "pending"
                for approval in state.approval_requests.values()
            ),
        }
        gaps = [
            label.replace("_", " ")
            for label, ok in checks.items()
            if not ok
        ]
        score = round(sum(1 for ok in checks.values() if ok) / len(checks) * 10)
        return ProductionReadinessResponse(
            score=score,
            ready=score == 10,
            gaps=gaps,
            checks=checks,
        )

    def get_model_setting(
        self, user: UserRecord, organization_id: str | None = None
    ) -> dict[str, Any]:
        # actor_for_user validates the caller belongs to (or governs) the org.
        actor = self.actor_for_user(user, organization_id)
        state = self._load()
        model = (
            state.model_settings.get(actor.organization_id)
            if actor.organization_id
            else None
        )
        entry = state.model_catalog.get(model) if model else None
        return {
            "organization_id": actor.organization_id,
            "model": model,
            "catalog_entry": entry.model_dump(mode="json") if entry else None,
        }

    def set_model_setting(
        self, user: UserRecord, request: SetModelRequest
    ) -> dict[str, Any]:
        org_id = request.organization_id or self.actor_for_user(user).organization_id
        if not org_id:
            raise ControlPlaneError(400, "organization_id is required")
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, org_id)
            model_id = request.model.strip()
            entry = state.model_catalog.get(model_id)
            if not entry or not entry.enabled:
                raise ControlPlaneError(400, "Model is not approved for use")
            state.model_settings[org_id] = model_id
            self._save(state)
            return {
                "organization_id": org_id,
                "model": state.model_settings[org_id],
                "catalog_entry": entry.model_dump(mode="json"),
            }

    def _authoritative_actor(
        self, user: UserRecord, organization_id: str | None = None
    ) -> dict[str, Any]:
        """Server-derived identity for audit/attestation records.

        Derived from the authenticated token, never from client-supplied fields, so
        a caller cannot attribute an audit event or a signed attestation to another
        user or organization.
        """
        actor = self.actor_for_user(user, organization_id)
        return {
            "user_id": actor.user_id,
            "organization_id": actor.organization_id,
            "role": actor.role,
            "platform_role": actor.platform_role,
        }

    def list_role_policies(self, user: UserRecord) -> dict[str, list[str] | None]:
        self._require_platform_role(user)
        state = self._load()
        return state.role_policies

    def update_role_policy(
        self,
        user: UserRecord,
        role: RoleName,
        request: UpdateRolePolicyRequest,
    ) -> dict[str, Any]:
        self._require_platform_role(user)
        with self._lock:
            state = self._load()
            state.role_policies[role] = (
                sorted(set(request.allowed_tools)) if request.allowed_tools is not None else None
            )
            self._save(state)
            return {"role": role, "allowed_tools": state.role_policies[role]}

    def list_org_role_policies(
        self,
        user: UserRecord,
        organization_id: str,
    ) -> dict[str, list[str] | None]:
        state = self._load()
        self._require_org_admin(state, user, organization_id)
        defaults = {
            role: state.role_policies.get(role)
            for role in ("developer", "intern", "auditor")
        }
        return {**defaults, **state.org_role_policies.get(organization_id, {})}

    def update_org_role_policy(
        self,
        user: UserRecord,
        organization_id: str,
        request: OrgRolePolicyRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, organization_id)
            if organization_id not in state.organizations:
                raise ControlPlaneError(404, "Organization not found")
            org_policies = state.org_role_policies.setdefault(organization_id, {})
            org_policies[request.role] = (
                sorted(set(request.allowed_tools)) if request.allowed_tools is not None else None
            )
            self._save(state)
            return {
                "organization_id": organization_id,
                "role": request.role,
                "allowed_tools": org_policies[request.role],
            }

    def list_usage_policies(self, user: UserRecord) -> dict[str, dict[str, Any]]:
        self._require_platform_role(user)
        state = self._load()
        return {
            role: policy.model_dump(mode="json")
            for role, policy in state.usage_policies.items()
        }

    def update_usage_policy(
        self,
        user: UserRecord,
        role: RoleName,
        request: UpdateUsagePolicyRequest,
    ) -> dict[str, Any]:
        self._require_platform_role(user)
        with self._lock:
            state = self._load()
            state.usage_policies[role] = UsagePolicyRecord(**request.model_dump())
            self._save(state)
            return {"role": role, "policy": state.usage_policies[role].model_dump(mode="json")}

    def check_usage(self, user: UserRecord, request: UsageCheckRequest) -> UsageCheckResponse:
        # Locked across the count → enforce → append cycle so concurrent requests
        # cannot both read the same window count and slip past the limit.
        with self._lock:
            state = self._load()
            actor = self.actor_for_user(user, request.organization_id)
            policy = state.usage_policies.get(actor.role) or DEFAULT_USAGE_POLICIES[actor.role]

            if not policy.enabled:
                raise ControlPlaneError(429, f"Model usage is disabled for role {actor.role}")

            if (
                policy.max_context_tokens is not None
                and request.context_tokens is not None
                and request.context_tokens > policy.max_context_tokens
            ):
                raise ControlPlaneError(429, f"Context token limit exceeded for role {actor.role}")

            if (
                policy.max_turns_per_session is not None
                and request.turn_number is not None
                and request.turn_number > policy.max_turns_per_session
            ):
                raise ControlPlaneError(429, f"Session turn limit exceeded for role {actor.role}")

            now = datetime.now(timezone.utc)
            user_events = [
                event
                for event in state.usage_events
                if event.user_id == user.id and event.event_type == "model.request"
            ]
            org_events = [
                event
                for event in state.usage_events
                if event.organization_id == actor.organization_id and event.event_type == "model.request"
            ]

            self._enforce_window(user_events, now, timedelta(minutes=1), policy.requests_per_minute, "minute")
            self._enforce_window(user_events, now, timedelta(hours=1), policy.requests_per_hour, "hour")
            self._enforce_window(user_events, now, timedelta(days=1), policy.requests_per_day, "day")
            self._enforce_window(org_events, now, timedelta(days=1), policy.org_requests_per_day, "organization day")

            if policy.max_session_minutes is not None and request.session_id:
                session_events = [
                    event for event in user_events if event.session_id == request.session_id
                ]
                if session_events:
                    first_seen = min(_parse_timestamp(event.timestamp) for event in session_events)
                    if now - first_seen > timedelta(minutes=policy.max_session_minutes):
                        raise ControlPlaneError(429, f"Session time limit exceeded for role {actor.role}")

            state.usage_events.append(
                UsageEventRecord(
                    user_id=user.id,
                    organization_id=actor.organization_id,
                    role=actor.role,
                    model=request.model,
                    session_id=request.session_id,
                )
            )
            self._prune_usage_events(state)
            self._save(state)

            return UsageCheckResponse(
                ok=True,
                role=actor.role,
                policy=policy,
                remaining={
                    "minute": self._remaining(user_events, now, timedelta(minutes=1), policy.requests_per_minute),
                    "hour": self._remaining(user_events, now, timedelta(hours=1), policy.requests_per_hour),
                    "day": self._remaining(user_events, now, timedelta(days=1), policy.requests_per_day),
                    "organization_day": self._remaining(org_events, now, timedelta(days=1), policy.org_requests_per_day),
                },
            )

    def usage_summary(
        self,
        user: UserRecord,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        state = self._load()
        if user.platform_role not in {"root", "platform_owner"}:
            if not organization_id:
                raise ControlPlaneError(400, "organization_id is required")
            self._require_org_admin(state, user, organization_id)

        now = datetime.now(timezone.utc)
        events = state.usage_events
        if organization_id:
            events = [event for event in events if event.organization_id == organization_id]

        return {
            "last_day": self._usage_counts(events, now, timedelta(days=1)),
            "last_hour": self._usage_counts(events, now, timedelta(hours=1)),
            "event_count": len(events),
        }

    def register_session(
        self,
        user: UserRecord,
        request: RegisterSessionRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            actor = self.actor_for_user(user, request.organization_id)
            now = datetime.now(timezone.utc).isoformat()
            session = AgentSessionRecord(
                id=request.session_id,
                user_id=user.id,
                organization_id=actor.organization_id,
                role=actor.role,
                cwd=request.cwd,
                model=request.model,
                status="active",
                started_at=now,
                last_heartbeat_at=now,
            )
            state.agent_sessions[session.id] = session
            self._save(state)
            return session.model_dump(mode="json")

    def heartbeat_session(
        self,
        user: UserRecord,
        session_id: str,
        request: SessionHeartbeatRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            session = self._require_session_access(state, user, session_id, write=True)
            if session.status == "revoked":
                raise ControlPlaneError(423, "Session has been revoked")

            session.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            session.turn_count = request.turn_count
            session.last_message_preview = request.last_message_preview[:200]
            if request.model:
                session.model = request.model
            self._save(state)
            return session.model_dump(mode="json")

    def record_session_turn(
        self,
        user: UserRecord,
        session_id: str,
        request: SessionTurnRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            session = self._require_session_access(state, user, session_id, write=True)
            if session.status == "revoked":
                raise ControlPlaneError(423, "Session has been revoked")

            session.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            session.turn_count = max(session.turn_count, request.turn_number)
            session.last_message_preview = request.message_preview[:200]
            if request.model:
                session.model = request.model
            self._save(state)
            return session.model_dump(mode="json")

    def end_session(
        self,
        user: UserRecord,
        session_id: str,
        request: EndSessionRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            session = self._require_session_access(state, user, session_id, write=True)
            session.status = request.status
            session.ended_at = datetime.now(timezone.utc).isoformat()
            session.last_heartbeat_at = session.ended_at
            self._save(state)
            return session.model_dump(mode="json")

    def revoke_session(self, user: UserRecord, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            session = self._require_session_access(state, user, session_id, admin=True)
            session.status = "revoked"
            session.ended_at = datetime.now(timezone.utc).isoformat()
            session.last_heartbeat_at = session.ended_at
            self._save(state)
            return session.model_dump(mode="json")

    def list_agent_sessions(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        state = self._load()
        sessions = list(state.agent_sessions.values())
        if user.platform_role not in {"root", "platform_owner"}:
            allowed_org_ids = {
                membership.organization_id
                for membership in state.memberships
                if membership.user_id == user.id
                and membership.role in {"org_owner", "security_admin", "auditor"}
            }
            if allowed_org_ids:
                sessions = [
                    session
                    for session in sessions
                    if session.organization_id in allowed_org_ids
                ]
            else:
                sessions = [session for session in sessions if session.user_id == user.id]

        if organization_id:
            sessions = [
                session for session in sessions if session.organization_id == organization_id
            ]
        if status:
            sessions = [session for session in sessions if session.status == status]

        sessions.sort(key=lambda session: session.last_heartbeat_at, reverse=True)
        return [session.model_dump(mode="json") for session in sessions[:limit]]

    def list_organizations(self, user: UserRecord) -> list[dict[str, Any]]:
        state = self._load()
        if user.platform_role in {"root", "platform_owner"}:
            return [org.model_dump(mode="json") for org in state.organizations.values()]

        org_ids = {
            membership.organization_id
            for membership in state.memberships
            if membership.user_id == user.id
        }
        return [
            org.model_dump(mode="json")
            for org_id, org in state.organizations.items()
            if org_id in org_ids
        ]

    def create_organization(self, user: UserRecord, request: CreateOrganizationRequest) -> dict[str, Any]:
        self._require_platform_role(user)
        with self._lock:
            state = self._load()
            organization = OrganizationRecord(
                name=request.name,
                domains=self._normalize_domains(request.domains),
            )
            state.organizations[organization.id] = organization
            self._save(state)
            return organization.model_dump(mode="json")

    def list_members(self, user: UserRecord, organization_id: str) -> list[dict[str, Any]]:
        state = self._load()
        self._require_org_admin(state, user, organization_id)
        return [
            membership.model_dump(mode="json")
            for membership in state.memberships
            if membership.organization_id == organization_id
        ]

    def upsert_member(
        self,
        user: UserRecord,
        organization_id: str,
        request: UpsertMemberRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, organization_id)

            if organization_id not in state.organizations:
                raise ControlPlaneError(404, "Organization not found")

            existing_user = state.users.get(request.user_id)
            generated_token: str | None = None
            if existing_user is None:
                if not request.token and not request.password:
                    generated_token = secrets.token_urlsafe(32)
                existing_user = UserRecord(id=request.user_id, token_hash="")
                existing_user.display_name = request.display_name
                self._apply_token(
                    existing_user,
                    request.token or generated_token or secrets.token_urlsafe(32),
                    self._default_member_expiry(request.token_expires_at),
                )
                if request.password:
                    self._apply_password(existing_user, request.password)
                state.users[existing_user.id] = existing_user
            elif request.token:
                self._apply_token(
                    existing_user,
                    request.token,
                    self._default_member_expiry(request.token_expires_at),
                )
                existing_user.display_name = request.display_name or existing_user.display_name
                if request.password:
                    self._apply_password(existing_user, request.password)
            elif request.password:
                self._apply_password(existing_user, request.password)
                existing_user.display_name = request.display_name or existing_user.display_name
            # Re-adding a member re-enables a previously deprovisioned account.
            existing_user.disabled = False

            membership = self._membership(state, request.user_id, organization_id)
            if membership:
                membership.role = request.role
            else:
                membership = MembershipRecord(
                    user_id=request.user_id,
                    organization_id=organization_id,
                    role=request.role,
                )
                state.memberships.append(membership)

            self._save(state)
            response = membership.model_dump(mode="json")
            if generated_token:
                response["token"] = generated_token
            return response

    def create_superuser(
        self,
        user: UserRecord,
        *,
        user_id: str,
        token: str,
        platform_role: RoleName = "platform_owner",
        display_name: str = "",
        organization_id: str | None = None,
        token_expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create (or promote) a platform-level superuser.

        `platform_role` (root / platform_owner) is what grants global admin power;
        it's otherwise only set at bootstrap. This is an operator action authorized
        by an existing superuser — the equivalent of Django's createsuperuser.
        """
        if platform_role not in {"root", "platform_owner"}:
            raise ControlPlaneError(400, "platform_role must be root or platform_owner")
        self._require_platform_role(user)
        with self._lock:
            state = self._load()
            target = state.users.get(user_id) or UserRecord(id=user_id, token_hash="")
            if display_name:
                target.display_name = display_name
            target.platform_role = platform_role
            self._apply_token(target, token, token_expires_at)
            state.users[target.id] = target

            if organization_id:
                if organization_id not in state.organizations:
                    raise ControlPlaneError(404, "Organization not found")
                membership = self._membership(state, user_id, organization_id)
                if membership:
                    membership.role = platform_role
                else:
                    state.memberships.append(
                        MembershipRecord(
                            user_id=user_id,
                            organization_id=organization_id,
                            role=platform_role,
                        )
                    )

            self._save(state)
            return {
                "user_id": user_id,
                "platform_role": platform_role,
                "organization_id": organization_id,
            }

    def remove_member(self, user: UserRecord, organization_id: str, member_user_id: str) -> None:
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, organization_id)
            state.memberships = [
                membership
                for membership in state.memberships
                if not (
                    membership.organization_id == organization_id
                    and membership.user_id == member_user_id
                )
            ]

            # Deprovision: once a user has no memberships left and no platform role,
            # disable the account so its bearer token stops authenticating. Without
            # this the token stayed valid and the user fell back to a default role.
            target = state.users.get(member_user_id)
            if target and not target.platform_role:
                still_a_member = any(
                    membership.user_id == member_user_id for membership in state.memberships
                )
                if not still_a_member:
                    target.disabled = True

            self._save(state)

    def rotate_member_token(
        self,
        user: UserRecord,
        organization_id: str,
        member_user_id: str,
        request: RotateTokenRequest,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            self._require_org_admin(state, user, organization_id)

            if not self._membership(state, member_user_id, organization_id):
                raise ControlPlaneError(404, "Member not found")

            member = state.users.get(member_user_id)
            if not member:
                raise ControlPlaneError(404, "User not found")

            self._apply_token(
                member, request.token, self._default_member_expiry(request.token_expires_at)
            )
            self._save(state)
            return {
                "user_id": member_user_id,
                "organization_id": organization_id,
                "rotated": True,
            }

    def role_dashboard(
        self,
        user: UserRecord,
        organization_id: str | None = None,
    ) -> RoleDashboardResponse:
        state = self._load()
        actor = self.actor_for_user(user, organization_id)
        org_id = actor.organization_id

        if user.platform_role in {"root", "platform_owner"}:
            visible_sessions = list(state.agent_sessions.values())
        elif org_id:
            visible_sessions = [
                session
                for session in state.agent_sessions.values()
                if session.organization_id == org_id
                and (
                    session.user_id == user.id
                    or actor.role in {"org_owner", "security_admin", "auditor"}
                )
            ]
        else:
            visible_sessions = [
                session for session in state.agent_sessions.values() if session.user_id == user.id
            ]

        if user.platform_role in {"root", "platform_owner"}:
            visible_audit_events = state.audit_events
        elif org_id and actor.role in {"org_owner", "security_admin", "auditor"}:
            visible_audit_events = [
                event
                for event in state.audit_events
                if event.actor.get("organization_id") == org_id
            ]
        else:
            visible_audit_events = [
                event for event in state.audit_events if event.actor.get("user_id") == user.id
            ]

        pending_escalations = sum(
            1
            for event in visible_audit_events
            if event.event_type == "intern.escalation.requested"
            and event.details.get("status") == "open"
        )

        capabilities_by_role: dict[str, list[str]] = {
            "root": [
                "Platform administration",
                "Organization creation",
                "Role and usage policy control",
                "Audit and attestation verification",
                "Session revocation",
            ],
            "platform_owner": [
                "Platform administration",
                "Organization creation",
                "Role and usage policy control",
                "Audit and attestation verification",
                "Session revocation",
            ],
            "org_owner": [
                "Member management",
                "Organization model selection",
                "Org-scoped developer/intern/auditor tool policies",
                "Org session oversight",
                "Audit review",
            ],
            "security_admin": [
                "Member and token review",
                "Org-scoped restricted-role policies",
                "Session revocation",
                "Audit review",
                "Intern escalation triage",
            ],
            "developer": [
                "Governed agent execution",
                "Self-service CLI token issuance",
                "Own session visibility",
                "Model and usage limit visibility",
            ],
            "intern": [
                "Read-only guided investigation tools",
                "Self-service CLI token issuance",
                "Escalation requests for additional access",
                "Own session visibility",
            ],
            "auditor": [
                "Read-only audit review",
                "Attestation review",
                "Exportable audit evidence",
                "Session visibility without mutation",
            ],
        }
        actions_by_role: dict[str, list[str]] = {
            "root": ["Invite platform owners", "Review production gaps", "Verify audit chain"],
            "platform_owner": ["Create organizations", "Tune platform policies", "Review usage"],
            "org_owner": ["Add teammates", "Set org model", "Tune restricted-role tools"],
            "security_admin": ["Review active sessions", "Triage escalations", "Rotate risky tokens"],
            "developer": ["Issue CLI token", "Start governed CLI", "Review personal usage limits"],
            "intern": ["Issue CLI token", "Start a guided investigation", "Request escalation when blocked"],
            "auditor": ["Export audit package", "Review attestations", "Verify audit chain status"],
        }

        return RoleDashboardResponse(
            role=actor.role,
            organization_id=org_id,
            capabilities=capabilities_by_role[actor.role],
            next_actions=actions_by_role[actor.role],
            usage_policy=actor.usage_policy,
            allowed_tools=actor.allowed_tools,
            active_sessions=sum(1 for session in visible_sessions if session.status == "active"),
            visible_sessions=len(visible_sessions),
            audit_events_visible=len(visible_audit_events),
            audit_chain_valid=self.verify_audit_events_for_system().valid,
            pending_escalations=pending_escalations,
        )

    def request_intern_escalation(
        self,
        user: UserRecord,
        request: InternEscalationRequest,
    ) -> dict[str, Any]:
        actor = self.actor_for_user(user, request.organization_id)
        if actor.role != "intern":
            raise ControlPlaneError(403, "Only interns can request escalation")
        return self.ingest_audit_event(
            user,
            IngestAuditEventRequest(
                event_type="intern.escalation.requested",
                organization_id=actor.organization_id,
                details={
                    "status": "open",
                    "reason": request.reason,
                    "requested_tool": request.requested_tool,
                    "session_id": request.session_id,
                },
            ),
        )

    def export_audit_package(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        state = self._load()
        self._require_attestation_reader(state, user, organization_id)
        return {
            "organization_id": organization_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "audit": self.verify_audit_events_for_system().model_dump(mode="json"),
            "events": self.list_audit_events(user, organization_id, limit),
            "attestations": self.list_attestations(user, organization_id, limit=limit),
        }

    def ingest_audit_event(self, user: UserRecord, request: IngestAuditEventRequest) -> dict[str, Any]:
        # Actor is derived from the authenticated token, not from request.actor, so
        # a caller cannot forge an audit entry attributed to another user or org.
        actor = self._authoritative_actor(user, request.organization_id)
        with self._lock:
            state = self._load()
            previous_hash = state.audit_events[-1].event_hash if state.audit_events else None
            event = AuditEventRecord(
                event_type=request.event_type,
                actor=actor,
                details=request.details,
                previous_hash=previous_hash,
            )
            event.event_hash = audit_event_hash(event)
            state.audit_events.append(event)
            self._save(state)
            return event.model_dump(mode="json")

    def list_audit_events(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        state = self._load()

        if user.platform_role not in {"root", "platform_owner"}:
            if not organization_id:
                raise ControlPlaneError(400, "organization_id is required")
            membership = self._membership(state, user.id, organization_id)
            if not membership or membership.role not in {
                "org_owner",
                "security_admin",
                "auditor",
            }:
                raise ControlPlaneError(403, "Requires audit review role")

        events = state.audit_events
        if organization_id:
            events = [
                event
                for event in events
                if event.actor.get("organization_id") == organization_id
            ]

        return [event.model_dump(mode="json") for event in events[-limit:]]

    def record_attestation(
        self, user: UserRecord, request: RecordAttestationRequest
    ) -> dict[str, Any]:
        # Signed provenance must bind the true author: derive the actor from the
        # authenticated token rather than trusting request.actor.
        actor = self._authoritative_actor(user, request.organization_id)
        with self._lock:
            state = self._load()
            previous_hash = (
                state.attestations[-1].entry_hash if state.attestations else None
            )
            attestation = AttestationRecord(
                repository=request.repository,
                commit_sha=request.commit_sha,
                branch=request.branch,
                actor=actor,
                model_id=request.model_id,
                prompt_sha256=request.prompt_sha256,
                file_hashes=request.file_hashes,
                policy_version=request.policy_version,
                skill_id=request.skill_id,
                skill_version=request.skill_version,
                previous_hash=previous_hash,
            )
            attestation.entry_hash = attestation_entry_hash(attestation)
            attestation.signature = self.signer.sign(attestation.entry_hash)
            attestation.signing_key_id = self.signer.key_id()

            state.attestations.append(attestation)
            self._save(state)
            return attestation.model_dump(mode="json")

    def _require_attestation_reader(
        self, state: ControlPlaneState, user: UserRecord, organization_id: str | None
    ) -> None:
        if user.platform_role in {"root", "platform_owner"}:
            return
        if not organization_id:
            raise ControlPlaneError(400, "organization_id is required")
        membership = self._membership(state, user.id, organization_id)
        if not membership or membership.role not in {
            "org_owner",
            "security_admin",
            "auditor",
        }:
            raise ControlPlaneError(403, "Requires audit review role")

    def list_attestations(
        self,
        user: UserRecord,
        organization_id: str | None = None,
        repository: str | None = None,
        commit_sha: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        state = self._load()
        self._require_attestation_reader(state, user, organization_id)

        results = state.attestations
        if organization_id:
            results = [
                a for a in results if a.actor.get("organization_id") == organization_id
            ]
        if repository:
            results = [a for a in results if a.repository == repository]
        if commit_sha:
            results = [a for a in results if a.commit_sha == commit_sha]

        return [a.model_dump(mode="json") for a in results[-limit:]]

    def verify_attestations(self, user: UserRecord) -> AttestationVerificationResponse:
        self._require_platform_role(user)
        return self.verify_attestations_for_system()

    def verify_attestations_for_system(self) -> AttestationVerificationResponse:
        state = self._load()
        previous_hash = None
        public_key_pem = self.signer.public_key_pem()

        for index, attestation in enumerate(state.attestations):
            if attestation.previous_hash != previous_hash:
                return AttestationVerificationResponse(
                    valid=False,
                    attestation_count=len(state.attestations),
                    broken_index=index,
                    reason="broken hash chain",
                    signing_key_id=self.signer.key_id(),
                )

            expected_hash = attestation_entry_hash(attestation)
            if attestation.entry_hash != expected_hash:
                return AttestationVerificationResponse(
                    valid=False,
                    attestation_count=len(state.attestations),
                    broken_index=index,
                    reason="entry hash does not match content",
                    signing_key_id=self.signer.key_id(),
                )

            if not attestation.signature or not verify_signature(
                public_key_pem, attestation.entry_hash, attestation.signature
            ):
                return AttestationVerificationResponse(
                    valid=False,
                    attestation_count=len(state.attestations),
                    broken_index=index,
                    reason="invalid or missing signature",
                    signing_key_id=self.signer.key_id(),
                )

            previous_hash = attestation.entry_hash

        return AttestationVerificationResponse(
            valid=True,
            attestation_count=len(state.attestations),
            signing_key_id=self.signer.key_id(),
        )

    def evaluate_gate(self, user: UserRecord, request) -> GateCheckResponse:
        state = self._load()
        reasons: list[str] = []

        verification = self.verify_attestations_for_system()
        chain_valid = verification.valid
        if not chain_valid:
            reasons.append(
                f"attestation chain is invalid at index {verification.broken_index} "
                f"({verification.reason})"
            )

        matching = [
            a
            for a in state.attestations
            if a.repository == request.repository
            and (request.commit_sha is None or a.commit_sha == request.commit_sha)
        ]

        if not matching:
            target = request.commit_sha or "any commit"
            reasons.append(
                f"no attestation found for {request.repository} @ {target}"
            )

        if request.require_policy_version and matching:
            versions = {a.policy_version for a in matching}
            if request.require_policy_version not in versions:
                reasons.append(
                    f"no attestation under required policy version "
                    f"{request.require_policy_version}"
                )

        covered: dict[str, set[str]] = {}
        for attestation in matching:
            for path, digest in attestation.file_hashes.items():
                covered.setdefault(path, set()).add(digest)

        uncovered: list[str] = []
        mismatched: list[str] = []
        for path, digest in request.file_hashes.items():
            if path not in covered:
                uncovered.append(path)
            elif digest not in covered[path]:
                mismatched.append(path)

        if uncovered:
            reasons.append(f"{len(uncovered)} changed file(s) have no attestation")
        if mismatched:
            reasons.append(
                f"{len(mismatched)} changed file(s) do not match any attested hash"
            )

        passed = (
            chain_valid
            and bool(matching)
            and not uncovered
            and not mismatched
            and (
                not request.require_policy_version
                or request.require_policy_version
                in {a.policy_version for a in matching}
            )
        )

        return GateCheckResponse(
            passed=passed,
            repository=request.repository,
            commit_sha=request.commit_sha,
            chain_valid=chain_valid,
            matched_attestations=len(matching),
            uncovered_files=sorted(uncovered),
            mismatched_files=sorted(mismatched),
            reasons=reasons,
        )

    def attestation_public_key(self) -> dict[str, str]:
        return {
            "algorithm": "ed25519",
            "key_id": self.signer.key_id(),
            "public_key_pem": self.signer.public_key_pem(),
        }

    def verify_audit_events(self, user: UserRecord) -> AuditVerificationResponse:
        self._require_platform_role(user)
        return self.verify_audit_events_for_system()

    def verify_audit_events_for_system(self) -> AuditVerificationResponse:
        state = self._load()
        previous_hash = None

        for index, event in enumerate(state.audit_events):
            if event.previous_hash != previous_hash:
                return AuditVerificationResponse(
                    valid=False,
                    event_count=len(state.audit_events),
                    broken_index=index,
                    expected_hash=previous_hash,
                    actual_hash=event.previous_hash,
                )

            expected_hash = audit_event_hash(event)
            if event.event_hash != expected_hash:
                return AuditVerificationResponse(
                    valid=False,
                    event_count=len(state.audit_events),
                    broken_index=index,
                    expected_hash=expected_hash,
                    actual_hash=event.event_hash,
                )

            previous_hash = event.event_hash

        return AuditVerificationResponse(
            valid=True,
            event_count=len(state.audit_events),
        )

    def _enforce_window(
        self,
        events: list[UsageEventRecord],
        now: datetime,
        window: timedelta,
        limit: int | None,
        label: str,
    ) -> None:
        if limit is None:
            return
        if self._count_since(events, now, window) >= limit:
            raise ControlPlaneError(429, f"Usage limit exceeded for {label}")

    def _remaining(
        self,
        events: list[UsageEventRecord],
        now: datetime,
        window: timedelta,
        limit: int | None,
    ) -> int | None:
        if limit is None:
            return None
        return max(limit - self._count_since(events, now, window) - 1, 0)

    def _count_since(
        self,
        events: list[UsageEventRecord],
        now: datetime,
        window: timedelta,
    ) -> int:
        cutoff = now - window
        return sum(1 for event in events if _parse_timestamp(event.timestamp) >= cutoff)

    def _usage_counts(
        self,
        events: list[UsageEventRecord],
        now: datetime,
        window: timedelta,
    ) -> dict[str, int]:
        cutoff = now - window
        counts: dict[str, int] = {}
        for event in events:
            if _parse_timestamp(event.timestamp) >= cutoff:
                counts[event.role] = counts.get(event.role, 0) + 1
        return counts

    def _prune_usage_events(self, state: ControlPlaneState) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=35)
        state.usage_events = [
            event
            for event in state.usage_events
            if _parse_timestamp(event.timestamp) >= cutoff
        ]

    def _require_session_access(
        self,
        state: ControlPlaneState,
        user: UserRecord,
        session_id: str,
        *,
        write: bool = False,
        admin: bool = False,
    ) -> AgentSessionRecord:
        session = state.agent_sessions.get(session_id)
        if not session:
            raise ControlPlaneError(404, "Session not found")

        if user.platform_role in {"root", "platform_owner"}:
            return session

        membership = (
            self._membership(state, user.id, session.organization_id)
            if session.organization_id
            else None
        )

        if admin:
            if membership and membership.role in {"org_owner", "security_admin"}:
                return session
            raise ControlPlaneError(403, "Requires session admin role")

        if write:
            if session.user_id == user.id:
                return session
            if membership and membership.role in {"org_owner", "security_admin"}:
                return session
            raise ControlPlaneError(403, "Requires session owner or admin role")

        if session.user_id == user.id:
            return session
        if membership and membership.role in {"org_owner", "security_admin", "auditor"}:
            return session
        raise ControlPlaneError(403, "Requires session visibility role")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
