from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from authlib.jose import JsonWebKey, jwt
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from control_plane.rate_limit import SlidingWindowRateLimiter

from control_plane.models import (
    BootstrapRequest,
    CliTokenRequest,
    CliTokenResponse,
    CliTokenSummary,
    CreateApprovalRequest,
    CreateOrganizationRequest,
    DecideApprovalRequest,
    GateCheckRequest,
    IngestAuditEventRequest,
    InternEscalationRequest,
    LoginRequest,
    LoginResponse,
    ModelConnectivityRequest,
    ModelConnectivityResponse,
    EndSessionRequest,
    OrgRolePolicyRequest,
    RecordAttestationRequest,
    RegisterSessionRequest,
    RotateTokenRequest,
    RoleName,
    RoleDashboardResponse,
    SetModelRequest,
    SessionHeartbeatRequest,
    SessionTurnRequest,
    UpdateNetworkPolicyRequest,
    UpdateRolePolicyRequest,
    UpdateUsagePolicyRequest,
    UpsertIntegrationRequest,
    UpsertModelCatalogRequest,
    UpsertMemberRequest,
    UpsertProjectProfileRequest,
    UsageCheckRequest,
    UserRecord,
)
from control_plane.settings import ControlPlaneSettings, load_settings
from control_plane.store_factory import (
    MissingDatabaseUrl,
    create_store_from_env,
)
from control_plane.storage import ControlPlaneError, ControlPlaneStore

logger = logging.getLogger("control_plane")

# Liveness/readiness probes must never be rate limited (orchestrators poll them).
_RATE_LIMIT_EXEMPT = {"/health", "/ready"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MICROSOFT_SESSION_COOKIE = "nyansa_session"
MICROSOFT_STATE_COOKIE = "nyansa_ms_state"


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring a single proxy hop's X-Forwarded-For.

    Trust the left-most forwarded address only when a proxy set the header; uvicorn
    must run with --proxy-headers/--forwarded-allow-ips for this to be meaningful.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _reject_cross_site_cookie_request(request: Request) -> JSONResponse | None:
    """Block unsafe cookie-authenticated requests from other origins.

    The API primarily uses bearer tokens in the Authorization header. Microsoft
    SSO also creates an HttpOnly session cookie, which browsers attach
    automatically; for those ambient credentials, require a same-origin
    Origin/Referer on state-changing methods.
    """
    if request.method not in _UNSAFE_METHODS:
        return None
    if MICROSOFT_SESSION_COOKIE not in request.cookies:
        return None
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return None

    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return JSONResponse(status_code=403, content={"detail": "Missing CSRF origin"})

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    expected = f"{scheme}://{request.headers.get('host', '')}"
    if _same_origin(source, expected):
        return None

    return JSONResponse(status_code=403, content={"detail": "Invalid CSRF origin"})


def _same_origin(source: str, expected: str) -> bool:
    parsed_source = urlparse(source)
    parsed_expected = urlparse(expected)
    return (
        parsed_source.scheme == parsed_expected.scheme
        and parsed_source.hostname == parsed_expected.hostname
        and (parsed_source.port or _default_port(parsed_source.scheme))
        == (parsed_expected.port or _default_port(parsed_expected.scheme))
    )


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def create_app(
    store: ControlPlaneStore | None = None,
    settings: ControlPlaneSettings | None = None,
) -> FastAPI:
    store_instance = store or create_store_from_env()
    settings_instance = settings or load_settings()
    if store is not None and settings is None and not settings_instance.database_url:
        settings_instance = settings_instance.model_copy(
            update={"database_url": "test-injected"}
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            close = getattr(app.state.store, "close", None)
            if callable(close):
                close()

    app = FastAPI(
        title="Nyansa Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.store = store_instance
    app.state.settings = settings_instance
    app.state.web_dist = _web_dist_from_env()
    app.state.rate_limiter = SlidingWindowRateLimiter(
        settings_instance.rate_limit_per_minute
    )

    if (app.state.web_dist / "assets").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=app.state.web_dist / "assets"),
            name="agent-ui-assets",
        )

    @app.middleware("http")
    async def gateway(request, call_next):
        csrf_response = _reject_cross_site_cookie_request(request)
        if csrf_response is not None:
            return csrf_response

        # Rate limit first (cheap) so floods never reach handlers or the store.
        limiter: SlidingWindowRateLimiter = app.state.rate_limiter
        client_ip = _client_ip(request)
        if limiter.enabled and request.url.path not in _RATE_LIMIT_EXEMPT:
            allowed, retry_after = limiter.check(client_ip)
            if not allowed:
                logger.warning("rate_limited ip=%s path=%s", client_ip, request.url.path)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests"},
                    headers={"Retry-After": str(int(retry_after))},
                )

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started) * 1000)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        # Only meaningful over TLS; emit when the proxy terminated HTTPS.
        if request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

        # Structured, secret-free access log for observability.
        logger.info(
            "request method=%s path=%s status=%s dur_ms=%s ip=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            client_ip,
        )
        return response

    def get_store() -> ControlPlaneStore:
        return app.state.store

    def get_settings() -> ControlPlaneSettings:
        return app.state.settings

    @app.get("/", response_class=HTMLResponse)
    def dashboard(store: ControlPlaneStore = Depends(get_store)):
        index_path = app.state.web_dist / "index.html"
        if index_path.exists():
            return FileResponse(index_path)

        status = "Bootstrapped" if store.is_bootstrapped() else "Needs Bootstrap"
        readiness = store.verify_audit_events_for_system()
        audit_status = "Valid" if readiness.valid else "Invalid"
        audit_class = "ok" if readiness.valid else "bad"
        boot_class = "ok" if store.is_bootstrapped() else "warn"
        return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nyansa Control Plane</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f8fa; color: #20242a; }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 40px 24px; }}
    h1 {{ font-size: 28px; margin: 0 0 6px; }}
    p {{ color: #5b6472; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 28px 0; }}
    .card {{ background: white; border: 1px solid #dfe3ea; border-radius: 8px; padding: 18px; }}
    .label {{ font-size: 12px; color: #687386; text-transform: uppercase; letter-spacing: .04em; }}
    .value {{ font-size: 22px; margin-top: 8px; font-weight: 650; }}
    .ok {{ color: #16784c; }}
    .warn {{ color: #9a6700; }}
    .bad {{ color: #b42318; }}
    code {{ background: #eef1f5; padding: 2px 5px; border-radius: 4px; }}
    ul {{ line-height: 1.8; }}
  </style>
</head>
<body>
  <main>
    <h1>Nyansa Control Plane</h1>
    <p>Central authority for organizations, members, roles, policies, and audit integrity.</p>
    <section class="grid">
      <div class="card"><div class="label">Bootstrap</div><div class="value {boot_class}">{status}</div></div>
      <div class="card"><div class="label">Audit Chain</div><div class="value {audit_class}">{audit_status}</div></div>
      <div class="card"><div class="label">Audit Events</div><div class="value">{readiness.event_count}</div></div>
    </section>
    <section class="card">
      <h2>Operator Endpoints</h2>
      <ul>
        <li><code>GET /health</code> and <code>GET /ready</code></li>
        <li><code>POST /bootstrap</code></li>
        <li><code>GET /me</code></li>
        <li><code>GET /organizations</code>, <code>POST /organizations</code></li>
        <li><code>GET/PUT/DELETE /organizations/&lt;id&gt;/members</code></li>
        <li><code>GET/PUT /policies/roles</code></li>
        <li><code>GET/POST /audit/events</code> and <code>GET /audit/verify</code></li>
      </ul>
    </section>
  </main>
</body>
</html>
"""

    @app.get("/app/{path:path}", include_in_schema=False)
    def spa_fallback(path: str):
        index_path = app.state.web_dist / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="UI build not found")
        return FileResponse(index_path)

    def current_user(
        authorization: str | None = Header(None),
        session_cookie: str | None = Cookie(None, alias=MICROSOFT_SESSION_COOKIE),
        store: ControlPlaneStore = Depends(get_store),
    ) -> UserRecord:
        token = session_cookie
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()

        try:
            return store.authenticate(token)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    def setup_user_or_unbootstrapped(
        authorization: str | None = Header(None),
        session_cookie: str | None = Cookie(None, alias=MICROSOFT_SESSION_COOKIE),
        store: ControlPlaneStore = Depends(get_store),
    ) -> UserRecord | None:
        if not store.is_bootstrapped():
            return None
        return current_user(
            authorization=authorization,
            session_cookie=session_cookie,
            store=store,
        )

    @app.get("/health")
    def health(store: ControlPlaneStore = Depends(get_store)):
        database = _database_health(store)
        return {
            "ok": database.get("ok", False),
            "bootstrapped": store.is_bootstrapped(),
            "database": database,
        }

    @app.get("/ready")
    def ready(
        store: ControlPlaneStore = Depends(get_store),
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        production_gaps = _production_gaps(settings)
        database_health = _database_health(store)
        if production_gaps:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Production configuration is incomplete",
                    "gaps": production_gaps,
                },
            )
        database = _database_health(store)
        if not database.get("ok", False):
            raise HTTPException(
                status_code=503,
                detail={"message": "Database is unavailable", "database": database},
            )
        if not store.is_bootstrapped():
            raise HTTPException(status_code=503, detail="Control plane is not bootstrapped")
        audit_state = store.verify_audit_events_for_system()
        if not audit_state.valid:
            raise HTTPException(status_code=503, detail="Audit chain verification failed")
        return {"ok": True, "audit": audit_state.model_dump(mode="json")}

    @app.get("/setup/status")
    def setup_status(
        authorization: str | None = Header(None),
        session_cookie: str | None = Cookie(None, alias=MICROSOFT_SESSION_COOKIE),
        store: ControlPlaneStore = Depends(get_store),
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        if settings.production and store.is_bootstrapped():
            current_user(
                authorization=authorization,
                session_cookie=session_cookie,
                store=store,
            )

        audit_state = store.verify_audit_events_for_system()
        llm_base_url = os.environ.get("BASE_URL") or os.environ.get("NYANSA_BASE_URL")
        llm_api_key_present = bool(
            os.environ.get("NYANSA_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("API_KEY")
        )
        control_token_present = bool(os.environ.get("NYANSA_CONTROL_TOKEN"))
        production_gaps = _production_gaps(settings)
        database_health = _database_health(store)

        checks = [
            {
                "key": "bootstrap",
                "label": "Root user and first organization",
                "ok": store.is_bootstrapped(),
                "severity": "critical",
            },
            {
                "key": "production_secret",
                "label": "Production bootstrap secret",
                "ok": (not settings.production) or bool(settings.bootstrap_secret),
                "severity": "critical",
            },
            {
                "key": "postgres",
                "label": "Postgres durable state",
                "ok": database_health.get("ok", False)
                and database_health.get("backend") == "postgres",
                "severity": "critical",
            },
            {
                "key": "token_pepper",
                "label": "Persistent token pepper",
                "ok": (not settings.production) or settings.token_pepper_present,
                "severity": "critical",
            },
            {
                "key": "attestation_key",
                "label": "Persistent attestation signing key",
                "ok": (not settings.production) or settings.attestation_key_present,
                "severity": "critical",
            },
            {
                "key": "llm_endpoint",
                "label": "Model endpoint configured",
                "ok": bool(llm_base_url),
                "severity": "critical",
            },
            {
                "key": "llm_secret",
                "label": "Model credential present when required",
                "ok": llm_api_key_present or (llm_base_url or "").startswith("http://localhost"),
                "severity": "critical",
            },
            {
                "key": "control_token",
                "label": "CLI control-plane token environment",
                "ok": control_token_present,
                "severity": "warning",
            },
            {
                "key": "audit_integrity",
                "label": "Audit chain integrity",
                "ok": audit_state.valid,
                "severity": "critical",
            },
        ]

        return {
            "ok": all(check["ok"] for check in checks if check["severity"] == "critical"),
            "checks": checks,
            "production": settings.production,
            "state_backend": database_health.get("backend", "unknown"),
            "production_gaps": production_gaps,
            "database": database_health,
            "configured_model_base_url": llm_base_url,
            "model_api_key_present": llm_api_key_present,
            "microsoft_sign_in_enabled": _microsoft_configured(settings),
            "control_token_present": control_token_present,
            "audit": audit_state.model_dump(mode="json"),
        }

    @app.post("/setup/model-check", response_model=ModelConnectivityResponse)
    async def check_model_connectivity(
        request: ModelConnectivityRequest,
        x_bootstrap_secret: str | None = Header(None),
        user: UserRecord | None = Depends(setup_user_or_unbootstrapped),
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        # Before bootstrap there is no user to authenticate. In production, gate that
        # window behind the bootstrap secret so the outbound-request capability of
        # this endpoint is never exposed unauthenticated.
        if user is None and settings.production:
            if not settings.bootstrap_secret or x_bootstrap_secret != settings.bootstrap_secret:
                raise HTTPException(
                    status_code=403,
                    detail="Model check requires the bootstrap secret before bootstrap",
                )
        # Resolve and validate the target exactly once, then connect to that pinned
        # IP — so DNS cannot rebind to a blocked address between check and connect.
        target: _PinnedTarget | None = None
        if request.provider != "claude_code":
            target = _resolve_safe_target(request.base_url)
        started = time.perf_counter()

        try:
            if request.provider == "claude_code":
                return ModelConnectivityResponse(
                    ok=True,
                    provider=request.provider,
                    message=(
                        "Claude Code mode uses each developer's local Claude Code "
                        "login. Copy the generated config, install Claude Code, and "
                        "run `claude` locally to authenticate."
                    ),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            assert target is not None  # always set for non-claude_code providers
            if request.provider == "anthropic":
                response = await _check_anthropic_model(request, target)
            else:
                response = await _check_openai_compatible_model(request, target)
        except httpx.HTTPError as error:
            return ModelConnectivityResponse(
                ok=False,
                provider=request.provider,
                message=f"Connection failed: {error}",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code < 400:
            return ModelConnectivityResponse(
                ok=True,
                provider=request.provider,
                message="Model endpoint responded successfully",
                status_code=response.status_code,
                latency_ms=latency_ms,
            )

        return ModelConnectivityResponse(
            ok=False,
            provider=request.provider,
            message=_safe_provider_error(response),
            status_code=response.status_code,
            latency_ms=latency_ms,
        )

    @app.post("/bootstrap")
    def bootstrap(
        request: BootstrapRequest,
        x_bootstrap_secret: str | None = Header(None),
        store: ControlPlaneStore = Depends(get_store),
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        if settings.production:
            if not settings.bootstrap_secret:
                raise HTTPException(
                    status_code=503,
                    detail="NYANSA_BOOTSTRAP_SECRET is required in production",
                )
            if x_bootstrap_secret != settings.bootstrap_secret:
                raise HTTPException(status_code=403, detail="Invalid bootstrap secret")
        try:
            return store.bootstrap(request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/auth/login", response_model=LoginResponse)
    def login(
        request: LoginRequest,
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.login(request.user_id, request.password)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/auth/logout")
    def logout(settings: ControlPlaneSettings = Depends(get_settings)):
        response = {"ok": True}
        redirect = JSONResponse(response)
        redirect.delete_cookie(MICROSOFT_SESSION_COOKIE)
        return redirect

    @app.get("/auth/microsoft/start")
    def microsoft_start(
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        _require_microsoft_config(settings)
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        authorization_endpoint = _microsoft_authorization_endpoint(settings)
        params = {
            "client_id": settings.microsoft_client_id,
            "response_type": "code",
            "redirect_uri": settings.microsoft_redirect_uri,
            "response_mode": "query",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
        }
        response = RedirectResponse(f"{authorization_endpoint}?{urlencode(params)}")
        response.set_cookie(
            MICROSOFT_STATE_COOKIE,
            f"{state}.{nonce}",
            httponly=True,
            secure=settings.production,
            samesite="lax",
            max_age=600,
        )
        return response

    @app.get("/auth/microsoft/callback")
    async def microsoft_callback(
        code: str | None = Query(None),
        state: str | None = Query(None),
        error: str | None = Query(None),
        error_description: str | None = Query(None),
        state_cookie: str | None = Cookie(None, alias=MICROSOFT_STATE_COOKIE),
        store: ControlPlaneStore = Depends(get_store),
        settings: ControlPlaneSettings = Depends(get_settings),
    ):
        _require_microsoft_config(settings)
        if error:
            raise HTTPException(status_code=400, detail=error_description or error)
        if not code or not state or not state_cookie or "." not in state_cookie:
            raise HTTPException(status_code=400, detail="Invalid Microsoft sign-in state")
        expected_state, nonce = state_cookie.split(".", 1)
        if not secrets.compare_digest(state, expected_state):
            raise HTTPException(status_code=400, detail="Invalid Microsoft sign-in state")

        claims = await _exchange_microsoft_code(settings, code, nonce)
        email = _microsoft_email(claims)
        if not email:
            raise HTTPException(status_code=403, detail="Microsoft account did not provide an email")
        domain = email.rsplit("@", 1)[-1].lower()
        if settings.microsoft_allowed_domains and domain not in settings.microsoft_allowed_domains:
            raise HTTPException(status_code=403, detail="Microsoft email domain is not allowed")

        subject = _microsoft_subject(claims)
        display_name = str(claims.get("name") or email)
        try:
            login_result = store.login_microsoft(
                subject=subject,
                email=email,
                display_name=display_name,
                default_role=settings.microsoft_default_role,
                auto_provision=settings.microsoft_auto_provision,
                domain_org_map=settings.microsoft_domain_org_map,
            )
        except ControlPlaneError as storage_error:
            raise HTTPException(
                status_code=storage_error.status_code,
                detail=storage_error.detail,
            ) from storage_error

        response = RedirectResponse("/")
        response.delete_cookie(MICROSOFT_STATE_COOKIE)
        response.set_cookie(
            MICROSOFT_SESSION_COOKIE,
            login_result["token"],
            httponly=True,
            secure=settings.production,
            samesite="lax",
            max_age=12 * 60 * 60,
        )
        return response

    @app.post("/cli/token", response_model=CliTokenResponse)
    def issue_cli_token(
        request: CliTokenRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.issue_cli_token(user, request.name, request.token_expires_at)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/cli/tokens", response_model=list[CliTokenSummary])
    def list_cli_tokens(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.list_cli_tokens(user)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.delete("/cli/tokens/{token_id}", response_model=CliTokenSummary)
    def revoke_cli_token(
        token_id: str,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.revoke_cli_token(user, token_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/me")
    def me(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.actor_for_user(user, organization_id).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/organizations")
    def list_organizations(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        return {"organizations": store.list_organizations(user)}

    @app.post("/organizations")
    def create_organization(
        request: CreateOrganizationRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.create_organization(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/organizations/{organization_id}/members")
    def list_members(
        organization_id: str,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"members": store.list_members(user, organization_id)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/organizations/{organization_id}/members")
    def upsert_member(
        organization_id: str,
        request: UpsertMemberRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.upsert_member(user, organization_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.delete("/organizations/{organization_id}/members/{user_id}")
    def remove_member(
        organization_id: str,
        user_id: str,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            store.remove_member(user, organization_id, user_id)
            return {"ok": True}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/role-dashboard", response_model=RoleDashboardResponse)
    def role_dashboard(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.role_dashboard(user, organization_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/organizations/{organization_id}/policies/roles")
    def list_org_role_policies(
        organization_id: str,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"role_policies": store.list_org_role_policies(user, organization_id)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/organizations/{organization_id}/policies/roles")
    def update_org_role_policy(
        organization_id: str,
        request: OrgRolePolicyRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.update_org_role_policy(user, organization_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/policies/roles")
    def list_role_policies(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"role_policies": store.list_role_policies(user)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/policies/roles/{role}")
    def update_role_policy(
        role: RoleName,
        request: UpdateRolePolicyRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.update_role_policy(user, role, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/usage/policies")
    def list_usage_policies(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"usage_policies": store.list_usage_policies(user)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/usage/policies/{role}")
    def update_usage_policy(
        role: RoleName,
        request: UpdateUsagePolicyRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.update_usage_policy(user, role, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/usage/check")
    def check_usage(
        request: UsageCheckRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.check_usage(user, request).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/usage/summary")
    def usage_summary(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.usage_summary(user, organization_id=organization_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/settings/model")
    def get_model_setting(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.get_model_setting(user, organization_id=organization_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/settings/model/catalog")
    def list_model_catalog(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"models": store.list_model_catalog(user)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/settings/model/catalog")
    def upsert_model_catalog_entry(
        request: UpsertModelCatalogRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.upsert_model_catalog_entry(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/settings/model")
    def set_model_setting(
        request: SetModelRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.set_model_setting(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/sessions")
    def register_session(
        request: RegisterSessionRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.register_session(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/sessions")
    def list_sessions(
        organization_id: str | None = Query(None),
        status: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {
                "sessions": store.list_agent_sessions(
                    user,
                    organization_id=organization_id,
                    status=status,
                    limit=limit,
                )
            }
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/sessions/{session_id}/heartbeat")
    def heartbeat_session(
        session_id: str,
        request: SessionHeartbeatRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.heartbeat_session(user, session_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/sessions/{session_id}/turns")
    def record_session_turn(
        session_id: str,
        request: SessionTurnRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.record_session_turn(user, session_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/sessions/{session_id}/end")
    def end_session(
        session_id: str,
        request: EndSessionRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.end_session(user, session_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/sessions/{session_id}/revoke")
    def revoke_session(
        session_id: str,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.revoke_session(user, session_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/organizations/{organization_id}/members/{user_id}/rotate-token")
    def rotate_member_token(
        organization_id: str,
        user_id: str,
        request: RotateTokenRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.rotate_member_token(user, organization_id, user_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/audit/events")
    def ingest_audit_event(
        request: IngestAuditEventRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        return store.ingest_audit_event(user, request)

    @app.get("/audit/events")
    def list_audit_events(
        organization_id: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {
                "events": store.list_audit_events(
                    user,
                    organization_id=organization_id,
                    limit=limit,
                )
            }
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/audit/verify")
    def verify_audit_events(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.verify_audit_events(user).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/intern/escalations")
    def request_intern_escalation(
        request: InternEscalationRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.request_intern_escalation(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/audit/export")
    def export_audit_package(
        organization_id: str | None = Query(None),
        limit: int = Query(500, ge=1, le=2000),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.export_audit_package(user, organization_id, limit)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/network/policy")
    def get_network_policy(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.get_network_policy(user, organization_id)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/network/policy")
    def update_network_policy(
        request: UpdateNetworkPolicyRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.update_network_policy(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/integrations")
    def list_integrations(
        organization_id: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {"integrations": store.list_integrations(user, organization_id)}
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/integrations")
    def upsert_integration(
        request: UpsertIntegrationRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.upsert_integration(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/project-profiles")
    def list_project_profiles(
        organization_id: str | None = Query(None),
        repository: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {
                "project_profiles": store.list_project_profiles(
                    user,
                    organization_id=organization_id,
                    repository=repository,
                )
            }
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.put("/project-profiles")
    def upsert_project_profile(
        request: UpsertProjectProfileRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.upsert_project_profile(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/approvals")
    def create_approval_request(
        request: CreateApprovalRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.create_approval_request(user, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/approvals")
    def list_approval_requests(
        organization_id: str | None = Query(None),
        status: str | None = Query(None),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {
                "approvals": store.list_approval_requests(
                    user, organization_id=organization_id, status=status
                )
            }
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/approvals/{approval_id}/decision")
    def decide_approval_request(
        approval_id: str,
        request: DecideApprovalRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.decide_approval_request(user, approval_id, request)
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/production/readiness")
    def production_readiness(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.production_readiness(user).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.post("/attestations")
    def record_attestation(
        request: RecordAttestationRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        return store.record_attestation(user, request)

    @app.get("/attestations")
    def list_attestations(
        organization_id: str | None = Query(None),
        repository: str | None = Query(None),
        commit_sha: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return {
                "attestations": store.list_attestations(
                    user,
                    organization_id=organization_id,
                    repository=repository,
                    commit_sha=commit_sha,
                    limit=limit,
                )
            }
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/attestations/verify")
    def verify_attestations(
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.verify_attestations(user).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    @app.get("/attestations/public-key")
    def attestation_public_key(
        store: ControlPlaneStore = Depends(get_store),
    ):
        return store.attestation_public_key()

    @app.post("/gate/check")
    def gate_check(
        request: GateCheckRequest,
        user: UserRecord = Depends(current_user),
        store: ControlPlaneStore = Depends(get_store),
    ):
        try:
            return store.evaluate_gate(user, request).model_dump(mode="json")
        except ControlPlaneError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error

    return app


def _production_gaps(settings: ControlPlaneSettings) -> list[str]:
    gaps: list[str] = []
    if settings.production and not settings.bootstrap_secret:
        gaps.append("NYANSA_BOOTSTRAP_SECRET")
    if not settings.database_url:
        gaps.append("DATABASE_URL or NYANSA_DATABASE_URL")
    if settings.production and not settings.token_pepper_present:
        gaps.append("NYANSA_TOKEN_PEPPER")
    if settings.production and not settings.attestation_key_present:
        gaps.append("NYANSA_ATTESTATION_KEY or NYANSA_ATTESTATION_KEY_SEED")
    if settings.microsoft_enabled and not _microsoft_configured(settings):
        gaps.append("NYANSA_MICROSOFT_CLIENT_ID/SECRET/REDIRECT_URI")
    return gaps


def _database_health(store: ControlPlaneStore) -> dict[str, object]:
    health_check = getattr(store, "health_check", None)
    if not callable(health_check):
        return {"ok": True, "backend": "test-injected"}

    try:
        return health_check()
    except Exception as error:
        return {
            "ok": False,
            "backend": "postgres",
            "error": str(error),
        }


def _microsoft_configured(settings: ControlPlaneSettings) -> bool:
    return bool(
        settings.microsoft_enabled
        and settings.microsoft_client_id
        and settings.microsoft_client_secret
        and settings.microsoft_redirect_uri
    )


def _require_microsoft_config(settings: ControlPlaneSettings) -> None:
    if not _microsoft_configured(settings):
        raise HTTPException(status_code=503, detail="Microsoft sign-in is not configured")


def _microsoft_authority(settings: ControlPlaneSettings) -> str:
    tenant = settings.microsoft_tenant.strip().strip("/") or "organizations"
    return f"https://login.microsoftonline.com/{tenant}/v2.0"


def _microsoft_authorization_endpoint(settings: ControlPlaneSettings) -> str:
    return f"{_microsoft_authority(settings)}/oauth2/v2.0/authorize"


async def _exchange_microsoft_code(
    settings: ControlPlaneSettings,
    code: str,
    nonce: str,
) -> dict[str, Any]:
    authority = _microsoft_authority(settings)
    async with httpx.AsyncClient(timeout=10) as client:
        metadata_response = await client.get(f"{authority}/.well-known/openid-configuration")
        metadata_response.raise_for_status()
        metadata = metadata_response.json()

        token_response = await client.post(
            metadata["token_endpoint"],
            data={
                "client_id": settings.microsoft_client_id,
                "client_secret": settings.microsoft_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.microsoft_redirect_uri,
                "scope": "openid profile email",
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        token_response.raise_for_status()
        token_body = token_response.json()

        jwks_response = await client.get(metadata["jwks_uri"])
        jwks_response.raise_for_status()
        key_set = JsonWebKey.import_key_set(jwks_response.json())

    claims = jwt.decode(token_body["id_token"], key_set)
    claims.validate(leeway=60)
    audience = claims.get("aud")
    valid_audience = (
        settings.microsoft_client_id in audience
        if isinstance(audience, list)
        else audience == settings.microsoft_client_id
    )
    if not valid_audience:
        raise HTTPException(status_code=401, detail="Invalid Microsoft token audience")
    if claims.get("nonce") != nonce:
        raise HTTPException(status_code=401, detail="Invalid Microsoft token nonce")
    issuer = str(claims.get("iss") or "")
    if not issuer.startswith("https://login.microsoftonline.com/") or not issuer.endswith("/v2.0"):
        raise HTTPException(status_code=401, detail="Invalid Microsoft token issuer")
    return dict(claims)


def _microsoft_subject(claims: dict[str, Any]) -> str:
    tenant_id = str(claims.get("tid") or "")
    object_id = str(claims.get("oid") or "")
    if tenant_id and object_id:
        return f"{tenant_id}:{object_id}"
    subject = str(claims.get("sub") or "")
    if subject:
        return subject
    raise HTTPException(status_code=401, detail="Microsoft token is missing subject")


def _microsoft_email(claims: dict[str, Any]) -> str:
    email = str(
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    ).strip().lower()
    return email if "@" in email else ""


def _web_dist_from_env() -> Path:
    value = os.environ.get("NYANSA_WEB_DIST")
    if value:
        return Path(value)
    return Path(__file__).resolve().parents[1] / "agent-ui" / "dist" / "spa"


@dataclass(frozen=True)
class _PinnedTarget:
    """A connectivity target resolved to a single validated IP.

    ``connect_base`` points at the literal IP so httpx connects there without
    re-resolving DNS. ``host_header``/``sni`` carry the original hostname so
    virtual-hosted servers route correctly and TLS still validates against the
    certificate's hostname, not the IP.
    """

    connect_base: str
    host_header: str
    sni: str


def _resolve_safe_target(value: str) -> _PinnedTarget:
    """Resolve, SSRF-validate, and pin a connectivity target to one IP.

    Blocks link-local (which includes the cloud metadata endpoint
    169.254.169.254 / fe80::), multicast, reserved, and unspecified addresses.
    Loopback and private ranges stay allowed on purpose: local Ollama and internal
    model gateways are the primary supported endpoints. Every resolved address is
    checked, so a host that resolves to both a public and a blocked address is
    rejected. The first validated address is pinned for the actual connection,
    closing the DNS-rebinding TOCTOU because httpx never re-resolves the hostname.
    """
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="base_url must be an http or https URL")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="base_url host could not be resolved")

    pinned_ip: str | None = None
    for info in infos:
        candidate = info[4][0]
        ip = ipaddress.ip_address(candidate)
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(
                status_code=400,
                detail="base_url resolves to a disallowed network address",
            )
        if pinned_ip is None:
            pinned_ip = candidate

    if pinned_ip is None:
        raise HTTPException(status_code=400, detail="base_url host could not be resolved")

    literal = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    connect_base = f"{parsed.scheme}://{literal}:{port}{parsed.path}"
    return _PinnedTarget(
        connect_base=connect_base,
        host_header=parsed.netloc,
        sni=parsed.hostname,
    )


async def _check_openai_compatible_model(
    request: ModelConnectivityRequest,
    target: _PinnedTarget,
) -> httpx.Response:
    headers = {"Host": target.host_header}
    if request.api_key:
        headers["Authorization"] = f"Bearer {request.api_key}"
    async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
        return await client.get(
            f"{target.connect_base.rstrip('/')}/models",
            headers=headers,
            extensions={"sni_hostname": target.sni},
        )


async def _check_anthropic_model(
    request: ModelConnectivityRequest,
    target: _PinnedTarget,
) -> httpx.Response:
    if not request.api_key:
        raise HTTPException(status_code=400, detail="Claude connectivity checks require an API key")
    async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
        return await client.post(
            f"{target.connect_base.rstrip('/')}/v1/messages",
            headers={
                "Host": target.host_header,
                "x-api-key": request.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": request.model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            extensions={"sni_hostname": target.sni},
        )


def _safe_provider_error(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text[:300]
    return f"Endpoint returned HTTP {response.status_code}: {detail}"


def _missing_database_app(error: MissingDatabaseUrl) -> FastAPI:
    app = FastAPI(title="Nyansa Control Plane", version="0.1.0")

    @app.get("/health")
    def health():
        return {
            "ok": False,
            "bootstrapped": False,
            "database": {
                "ok": False,
                "backend": "postgres",
                "error": str(error),
            },
        }

    @app.get("/ready")
    def ready():
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Postgres database is required",
                "gaps": ["DATABASE_URL or NYANSA_DATABASE_URL"],
            },
        )

    return app


try:
    app = create_app()
except MissingDatabaseUrl as error:
    app = _missing_database_app(error)
