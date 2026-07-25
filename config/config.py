from __future__ import annotations
from enum import Enum
import os
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class ModelConfig(BaseModel):
    provider: Literal["openai_compatible", "anthropic", "claude_code"] = "openai_compatible"
    name: str = "local-model"
    base_url: str | None = "http://localhost:8000/v1"
    api_key_env: str = "NYANSA_API_KEY"
    requires_api_key: bool = False
    temperature: float = Field(default=1, ge=0.0, le=2.0)
    context_window: int = 256_000
    max_output_tokens: int = Field(default=4096, ge=1)
    supports_stream_options: bool = True
    claude_code_command: str = "claude"
    claude_code_permission_mode: Literal[
        "default",
        "acceptEdits",
        "bypassPermissions",
        "plan",
        "auto",
    ] = "default"
    claude_code_max_turns: int | None = Field(default=None, ge=1)
    claude_code_extra_args: list[str] = Field(default_factory=list)


class ShellEnvironmentPolicy(BaseModel):
    ignore_default_excludes: bool = False
    exclude_patterns: list[str] = Field(
        default_factory=lambda: ["*KEY*", "*TOKEN*", "*SECRET*"]
    )
    set_vars: dict[str, str] = Field(default_factory=dict)


class ActorConfig(BaseModel):
    user_id: str = "local-user"
    organization_id: str = "local-org"
    role: str = "developer"


class AuditConfig(BaseModel):
    enabled: bool = True
    log_content: bool = False
    log_path: Path | None = None


class SensitiveDataPolicy(BaseModel):
    enabled: bool = True
    redact_before_model: bool = True
    redact_audit_events: bool = True


class RolePolicyConfig(BaseModel):
    enabled: bool = True
    allowed_tools: list[str] | None = None
    version: str | None = None


class UsagePolicyConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int | None = None
    requests_per_hour: int | None = None
    requests_per_day: int | None = None
    org_requests_per_day: int | None = None
    max_turns_per_session: int | None = None
    max_session_minutes: int | None = None
    max_context_tokens: int | None = None


class ControlPlaneConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://localhost:8080"
    token_env: str = "NYANSA_CONTROL_TOKEN"
    fail_closed: bool = True
    sync_actor: bool = True
    send_audit_events: bool = True
    send_attestations: bool = True


class MCPServerConfig(BaseModel):
    enabled: bool = True
    startup_timeout_sec: float = 10

    # stdio transport
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Path | None = None

    # http/sse transport
    url: str | None = None

    @model_validator(mode="after")
    def validate_transport(self) -> MCPServerConfig:
        has_command = self.command is not None
        has_url = self.url is not None

        if not has_command and not has_url:
            raise ValueError(
                "MCP Server must have either 'command' (stdio) or 'url' (http/sse)"
            )

        if has_command and has_url:
            raise ValueError(
                "MCP Server cannot have both 'command' (stdio) and 'url' (http/sse)"
            )

        return self


class ApprovalPolicy(str, Enum):
    ON_REQUEST = "on-request"
    ON_FAILURE = "on-failure"
    AUTO = "auto"
    AUTO_EDIT = "auto-edit"
    NEVER = "never"
    YOLO = "yolo"


class HookTrigger(str, Enum):
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    ON_ERROR = "on_error"


class HookConfig(BaseModel):
    name: str
    trigger: HookTrigger
    command: str | None = None  # python3 tests.py
    script: str | None = None  # *.sh
    timeout_sec: float = 30
    enabled: bool = True

    @model_validator(mode="after")
    def validate_hook(self) -> HookConfig:
        if not self.command and not self.script:
            raise ValueError("Hook must either have 'command' or 'script'")
        return self


class VerificationConfig(BaseModel):
    enabled: bool = True
    command: str | None = None
    timeout_seconds: float = Field(default=300, ge=1)
    max_attempts: int = Field(default=3, ge=0)


class SandboxConfig(BaseModel):
    enabled: bool = True
    backend: Literal["auto", "none", "bubblewrap", "sandbox_exec", "rlimit"] = "auto"
    confine_to_workspace: bool = True
    allow_network: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    require_proxy: bool = False
    proxy_url: str | None = None
    cpu_seconds: int = Field(default=120, ge=1)
    memory_mb: int = Field(default=4096, ge=64)
    file_size_mb: int = Field(default=1024, ge=1)
    max_processes: int = Field(default=256, ge=1)
    read_only_paths: list[str] = Field(default_factory=list)
    writable_paths: list[str] = Field(default_factory=list)


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    actor: ActorConfig = Field(default_factory=ActorConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    sensitive_data: SensitiveDataPolicy = Field(default_factory=SensitiveDataPolicy)
    role_policy: RolePolicyConfig = Field(default_factory=RolePolicyConfig)
    usage_policy: UsagePolicyConfig = Field(default_factory=UsagePolicyConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    cwd: Path = Field(default_factory=Path.cwd)
    shell_environment: ShellEnvironmentPolicy = Field(
        default_factory=ShellEnvironmentPolicy
    )
    hooks_enabled: bool = False
    hooks: list[HookConfig] = Field(default_factory=list)
    tool_discovery_enabled: bool = True
    approval: ApprovalPolicy = ApprovalPolicy.ON_REQUEST
    max_turns: int = 100
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    allowed_tools: list[str] | None = Field(
        None,
        description="If set, only these tools will be available to the agent",
    )

    developer_instructions: str | None = None
    user_instructions: str | None = None

    debug: bool = False

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.model.api_key_env) or os.environ.get("API_KEY")

    @property
    def base_url(self) -> str | None:
        return self.model.base_url or os.environ.get("BASE_URL")

    @property
    def model_name(self) -> str:
        return self.model.name

    @model_name.setter
    def model_name(self, value: str) -> None:
        self.model.name = value

    @property
    def temperature(self) -> float:
        return self.model.temperature

    @temperature.setter
    def temperature(self, value: float) -> None:
        self.model.temperature = value

    def validate(self) -> list[str]:
        errors: list[str] = []

        if self.model.requires_api_key and not self.api_key:
            message = (
                f"No model API key found. Set {self.model.api_key_env} in your .env "
                f"(or environment)."
            )
            if "openrouter" in (self.model.base_url or ""):
                message += (
                    " The default model runs on OpenRouter — get a free key at "
                    "https://openrouter.ai/settings/keys (free models work, but need a key)."
                )
            errors.append(message)

        local_endpoint = (self.model.base_url or "").startswith(
            ("http://localhost", "http://127.0.0.1")
        )
        hosted_model_id = "/" in self.model.name
        if local_endpoint and hosted_model_id:
            errors.append(
                "Model endpoint mismatch: BASE_URL points to a local server, but "
                f"NYANSA_MODEL/model is '{self.model.name}', which looks like a hosted "
                "provider model id. Set BASE_URL=https://openrouter.ai/api/v1 for "
                "OpenRouter, or choose a local model name such as qwen2.5-coder:7b."
            )

        if not self.cwd.exists():
            errors.append(f"Working directory does not exist: {self.cwd}")

        return errors

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
