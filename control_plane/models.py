from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field


RoleName = Literal[
    "root",
    "platform_owner",
    "org_owner",
    "security_admin",
    "developer",
    "intern",
    "auditor",
]

ProviderName = Literal["openai_compatible", "anthropic", "claude_code"]


class UserRecord(BaseModel):
    id: str
    display_name: str = ""
    token_hash: str
    token_lookup: str | None = None
    token_expires_at: str | None = None
    password_hash: str | None = None
    session_token_hash: str | None = None
    session_token_lookup: str | None = None
    session_token_expires_at: str | None = None
    microsoft_subject: str | None = None
    email: str | None = None
    platform_role: RoleName | None = None
    disabled: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cli_tokens: list["CliTokenRecord"] = Field(default_factory=list)


class CliTokenRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "CLI token"
    token_hash: str
    token_lookup: str
    token_expires_at: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_used_at: str | None = None
    revoked_at: str | None = None


class OrganizationRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    domains: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MembershipRecord(BaseModel):
    user_id: str
    organization_id: str
    role: RoleName
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AuditEventRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: str
    actor: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str | None = None
    event_hash: str | None = None


class AttestationRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    repository: str
    commit_sha: str | None = None
    branch: str | None = None
    actor: dict[str, Any] = Field(default_factory=dict)
    model_id: str
    prompt_sha256: str | None = None
    file_hashes: dict[str, str] = Field(default_factory=dict)
    policy_version: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    previous_hash: str | None = None
    entry_hash: str | None = None
    signature: str | None = None
    signing_key_id: str | None = None


class UsagePolicyRecord(BaseModel):
    enabled: bool = True
    requests_per_minute: int | None = None
    requests_per_hour: int | None = None
    requests_per_day: int | None = None
    org_requests_per_day: int | None = None
    max_turns_per_session: int | None = None
    max_session_minutes: int | None = None
    max_context_tokens: int | None = None


class UsageEventRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    user_id: str
    organization_id: str | None = None
    role: RoleName
    model: str
    session_id: str | None = None
    event_type: str = "model.request"


class ModelCatalogEntry(BaseModel):
    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    provider: ProviderName = "openai_compatible"
    base_url: str | None = None
    requires_api_key: bool = True
    enabled: bool = True
    free: bool = False
    notes: str = ""


class NetworkPolicyRecord(BaseModel):
    allow_network: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    require_proxy: bool = False
    proxy_url: str | None = None


class IntegrationRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: Literal[
        "github",
        "gitlab",
        "jira",
        "slack",
        "siem",
        "scim",
        "saml",
        "mcp",
        "custom",
    ]
    name: str = Field(..., min_length=1)
    organization_id: str | None = None
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ProjectProfileRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    organization_id: str
    repository: str = Field(..., min_length=1)
    default_branch: str = "main"
    verification_commands: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    issue_tracker_url: str | None = None
    secret_env_vars: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ApprovalRequestRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    organization_id: str | None = None
    requester_user_id: str
    requester_role: RoleName
    request_type: Literal[
        "tool",
        "network",
        "model",
        "workspace",
        "integration",
        "other",
    ] = "other"
    target: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=8, max_length=1000)
    status: Literal["pending", "approved", "rejected"] = "pending"
    decided_by: str | None = None
    decided_at: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentSessionRecord(BaseModel):
    id: str
    user_id: str
    organization_id: str | None = None
    role: RoleName
    cwd: str
    model: str
    status: Literal["active", "ended", "revoked"] = "active"
    started_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_heartbeat_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: str | None = None
    turn_count: int = 0
    last_message_preview: str = ""


class ControlPlaneState(BaseModel):
    users: dict[str, UserRecord] = Field(default_factory=dict)
    organizations: dict[str, OrganizationRecord] = Field(default_factory=dict)
    memberships: list[MembershipRecord] = Field(default_factory=list)
    audit_events: list[AuditEventRecord] = Field(default_factory=list)
    attestations: list[AttestationRecord] = Field(default_factory=list)
    role_policies: dict[str, list[str] | None] = Field(default_factory=dict)
    org_role_policies: dict[str, dict[str, list[str] | None]] = Field(default_factory=dict)
    usage_policies: dict[str, UsagePolicyRecord] = Field(default_factory=dict)
    usage_events: list[UsageEventRecord] = Field(default_factory=list)
    agent_sessions: dict[str, AgentSessionRecord] = Field(default_factory=dict)
    # Approved model catalog and per-organization model choice. The CLI syncs the
    # selected entry's provider metadata on startup.
    model_catalog: dict[str, ModelCatalogEntry] = Field(default_factory=dict)
    model_settings: dict[str, str] = Field(default_factory=dict)
    network_policies: dict[str, NetworkPolicyRecord] = Field(default_factory=dict)
    integrations: dict[str, IntegrationRecord] = Field(default_factory=dict)
    project_profiles: dict[str, ProjectProfileRecord] = Field(default_factory=dict)
    approval_requests: dict[str, ApprovalRequestRecord] = Field(default_factory=dict)


class BootstrapRequest(BaseModel):
    organization_name: str = "Default Organization"
    organization_domains: list[str] = Field(default_factory=list)
    root_user_id: str = "root"
    root_display_name: str = "Root User"
    root_password: str | None = Field(None, min_length=12)
    root_token: str | None = Field(None, min_length=16)
    root_token_expires_at: str | None = None


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    token: str
    token_expires_at: str
    actor: dict[str, Any]


class CliTokenRequest(BaseModel):
    name: str = Field(default="CLI token", min_length=1, max_length=80)
    token_expires_at: str | None = None


class CliTokenResponse(BaseModel):
    id: str
    token: str
    token_expires_at: str | None = None
    name: str = "CLI token"


class CliTokenSummary(BaseModel):
    id: str
    name: str
    token_expires_at: str | None = None
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None


class CreateOrganizationRequest(BaseModel):
    name: str = Field(..., min_length=1)
    domains: list[str] = Field(default_factory=list)


class UpsertMemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: RoleName
    display_name: str = ""
    token: str | None = Field(None, min_length=16)
    password: str | None = Field(None, min_length=12)
    token_expires_at: str | None = None


class RotateTokenRequest(BaseModel):
    token: str = Field(..., min_length=16)
    token_expires_at: str | None = None


class UpdateRolePolicyRequest(BaseModel):
    allowed_tools: list[str] | None = None


class UpdateUsagePolicyRequest(BaseModel):
    enabled: bool = True
    requests_per_minute: int | None = Field(default=None, ge=1)
    requests_per_hour: int | None = Field(default=None, ge=1)
    requests_per_day: int | None = Field(default=None, ge=1)
    org_requests_per_day: int | None = Field(default=None, ge=1)
    max_turns_per_session: int | None = Field(default=None, ge=1)
    max_session_minutes: int | None = Field(default=None, ge=1)
    max_context_tokens: int | None = Field(default=None, ge=1)


class OrgRolePolicyRequest(BaseModel):
    role: Literal["developer", "intern", "auditor"]
    allowed_tools: list[str] | None = None


class InternEscalationRequest(BaseModel):
    organization_id: str | None = None
    reason: str = Field(..., min_length=8, max_length=500)
    requested_tool: str | None = Field(None, min_length=1, max_length=80)
    session_id: str | None = None


class UpdateNetworkPolicyRequest(BaseModel):
    organization_id: str | None = None
    allow_network: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    require_proxy: bool = False
    proxy_url: str | None = None


class UpsertIntegrationRequest(BaseModel):
    id: str | None = None
    provider: Literal[
        "github",
        "gitlab",
        "jira",
        "slack",
        "siem",
        "scim",
        "saml",
        "mcp",
        "custom",
    ]
    name: str = Field(..., min_length=1)
    organization_id: str | None = None
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class UpsertProjectProfileRequest(BaseModel):
    id: str | None = None
    organization_id: str | None = None
    repository: str = Field(..., min_length=1, max_length=300)
    default_branch: str = Field(default="main", min_length=1, max_length=120)
    verification_commands: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    issue_tracker_url: str | None = Field(default=None, max_length=500)
    secret_env_vars: list[str] = Field(default_factory=list)


class CreateApprovalRequest(BaseModel):
    organization_id: str | None = None
    request_type: Literal[
        "tool",
        "network",
        "model",
        "workspace",
        "integration",
        "other",
    ] = "other"
    target: str = Field(..., min_length=1, max_length=200)
    reason: str = Field(..., min_length=8, max_length=1000)


class DecideApprovalRequest(BaseModel):
    status: Literal["approved", "rejected"]


class ProductionReadinessResponse(BaseModel):
    score: int
    ready: bool
    gaps: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class AuditExportResponse(BaseModel):
    organization_id: str | None = None
    generated_at: str
    audit: AuditVerificationResponse
    events: list[dict[str, Any]]
    attestations: list[dict[str, Any]]


class RoleDashboardResponse(BaseModel):
    role: RoleName
    organization_id: str | None = None
    capabilities: list[str]
    next_actions: list[str]
    usage_policy: UsagePolicyRecord | None = None
    allowed_tools: list[str] | None = None
    active_sessions: int = 0
    visible_sessions: int = 0
    audit_events_visible: int = 0
    audit_chain_valid: bool = True
    pending_escalations: int = 0


class UsageCheckRequest(BaseModel):
    model: str = Field(..., min_length=1)
    organization_id: str | None = None
    session_id: str | None = None
    turn_number: int | None = Field(default=None, ge=1)
    context_tokens: int | None = Field(default=None, ge=1)


class UsageCheckResponse(BaseModel):
    ok: bool
    role: RoleName
    policy: UsagePolicyRecord
    remaining: dict[str, int | None] = Field(default_factory=dict)


class RegisterSessionRequest(BaseModel):
    session_id: str
    cwd: str
    model: str
    organization_id: str | None = None


class SessionHeartbeatRequest(BaseModel):
    turn_count: int = Field(default=0, ge=0)
    last_message_preview: str = ""
    model: str | None = None


class SessionTurnRequest(BaseModel):
    turn_number: int = Field(..., ge=1)
    message_preview: str = ""
    model: str | None = None


class EndSessionRequest(BaseModel):
    status: Literal["ended", "revoked"] = "ended"


class IngestAuditEventRequest(BaseModel):
    event_type: str = Field(..., min_length=1)
    organization_id: str | None = None
    # Retained for backwards compatibility; the server derives the authoritative
    # actor identity from the authenticated token and ignores identity fields here.
    actor: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class RecordAttestationRequest(BaseModel):
    repository: str = Field(..., min_length=1)
    commit_sha: str | None = None
    branch: str | None = None
    model_id: str = Field(..., min_length=1)
    prompt_sha256: str | None = None
    file_hashes: dict[str, str] = Field(default_factory=dict)
    policy_version: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    organization_id: str | None = None
    # Retained for backwards compatibility; identity is derived server-side.
    actor: dict[str, Any] = Field(default_factory=dict)


class ModelConnectivityRequest(BaseModel):
    provider: ProviderName
    base_url: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    api_key: str | None = None
    timeout_seconds: float = Field(default=10, ge=1, le=30)


class ModelConnectivityResponse(BaseModel):
    ok: bool
    provider: ProviderName
    message: str
    status_code: int | None = None
    latency_ms: int | None = None


class ActorResponse(BaseModel):
    user_id: str
    organization_id: str | None = None
    role: RoleName
    platform_role: RoleName | None = None
    allowed_tools: list[str] | None = None
    usage_policy: UsagePolicyRecord | None = None
    # Organization's governed model choice, applied by the CLI on startup.
    model_name: str | None = None
    model_provider: ProviderName | None = None
    model_base_url: str | None = None
    model_requires_api_key: bool | None = None
    network_policy: NetworkPolicyRecord | None = None


class SetModelRequest(BaseModel):
    model: str = Field(..., min_length=1)
    organization_id: str | None = None


class UpsertModelCatalogRequest(BaseModel):
    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    provider: ProviderName = "openai_compatible"
    base_url: str | None = None
    requires_api_key: bool = True
    enabled: bool = True
    free: bool = False
    notes: str = ""


class AuditVerificationResponse(BaseModel):
    valid: bool
    event_count: int
    broken_index: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None


class AttestationVerificationResponse(BaseModel):
    valid: bool
    attestation_count: int
    broken_index: int | None = None
    reason: str | None = None
    signing_key_id: str | None = None


class GateCheckRequest(BaseModel):
    repository: str = Field(..., min_length=1)
    commit_sha: str | None = None
    file_hashes: dict[str, str] = Field(default_factory=dict)
    require_policy_version: str | None = None


class GateCheckResponse(BaseModel):
    passed: bool
    repository: str
    commit_sha: str | None = None
    chain_valid: bool
    matched_attestations: int
    uncovered_files: list[str] = Field(default_factory=list)
    mismatched_files: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
