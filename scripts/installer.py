"""Interactive first-run installer for Nyansa.

`nyansa-init` takes a fresh checkout to a fully working setup in one guided
pass: it configures the model endpoint, generates the control-plane secrets,
bootstraps the first organization and root account in-process (no server needs to
be running), seeds optional roles, and writes the CLI's `.env` and
`.nyansa/config.toml`. Every prompt has a default; `--quick` accepts them all.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from control_plane.models import BootstrapRequest, UpsertMemberRequest
from control_plane.storage import ControlPlaneError
from control_plane.store_factory import create_store_from_env

console = Console()

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
# Free, tool-capable coding model. Free models rotate and are rate-limited; swap
# via https://openrouter.ai/models?max_price=0 if it ever stops working.
DEFAULT_OPENROUTER_MODEL = "poolside/laguna-s-2.1:free"
DEFAULT_CONTROL_PLANE_URL = "http://localhost:8080"
# Dev and production both run on Postgres so the builds match. This is the local
# dev default; the installer can start a matching container automatically.
DEV_DATABASE_URL = "postgresql://nyansa:nyansa@localhost:5432/nyansa"
DEV_PG_CONTAINER = "nyansa-postgres"
SEED_ROLES = ("developer", "intern", "auditor")


@dataclass
class Plan:
    cwd: Path
    production: bool = True
    docker: bool = True
    start_server: bool = True
    control_plane_url: str = DEFAULT_CONTROL_PLANE_URL
    # model
    provider: str = "openai_compatible"
    model_name: str = DEFAULT_OPENROUTER_MODEL
    base_url: str | None = DEFAULT_OPENROUTER_URL
    requires_api_key: bool = True
    api_key: str = ""
    reuse_existing_model_config: bool = True
    # control plane
    govern_cli: bool = True
    org_name: str = "Local Org"
    root_user_id: str = "root"
    org_domains: list[str] = field(default_factory=list)
    database_url: str = DEV_DATABASE_URL
    seed_roles: bool = False
    # generated / results
    secrets: dict[str, str] = field(default_factory=dict)
    seeded_tokens: dict[str, str] = field(default_factory=dict)
    org_id: str = ""
    already_bootstrapped: bool = False


def _gen(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

def _collect(plan: Plan, quick: bool) -> Plan:
    if quick:
        return plan

    console.print(
        Panel.fit(
            "This sets up the agent end to end: model endpoint, control-plane\n"
            "secrets, your first organization and [b]root[/b] account, and the CLI config.\n"
            "Press Enter to accept the default shown in [dim]brackets[/dim].",
            title="Nyansa · first-run setup",
            border_style="cyan",
        )
    )

    mode = Prompt.ask("\nDeployment mode", choices=["docker", "local", "server"], default="docker")
    plan.docker = mode == "docker"
    plan.start_server = mode == "docker"
    plan.production = mode in {"docker", "server"}

    _collect_model(plan)
    _collect_control_plane(plan)
    return plan


def _collect_model(plan: Plan) -> None:
    plan.reuse_existing_model_config = False
    console.print("\n[b]Model endpoint[/b]")
    choice = Prompt.ask(
        "Provider",
        choices=["default", "openrouter", "ollama", "openai", "claude-code"],
        default="default",
    )

    if choice in {"default", "openrouter"}:
        plan.provider = "openai_compatible"
        plan.base_url = DEFAULT_OPENROUTER_URL
        plan.model_name = Prompt.ask("  Model", default=DEFAULT_OPENROUTER_MODEL)
        plan.requires_api_key = True
        plan.api_key = Prompt.ask(
            "  OpenRouter API key (leave blank to set later)",
            password=True,
            default="",
        )
    elif choice == "ollama":
        plan.provider = "openai_compatible"
        plan.base_url = Prompt.ask("  Ollama base URL", default=DEFAULT_OLLAMA_URL)
        plan.model_name = Prompt.ask("  Model", default=DEFAULT_OLLAMA_MODEL)
        plan.requires_api_key = False
        plan.api_key = "local"
    elif choice == "openai":
        plan.provider = "openai_compatible"
        plan.base_url = Prompt.ask(
            "  OpenAI-compatible base URL", default=DEFAULT_OPENROUTER_URL
        )
        plan.model_name = Prompt.ask("  Model", default=DEFAULT_OPENROUTER_MODEL)
        plan.requires_api_key = True
        plan.api_key = Prompt.ask("  API key", password=True, default="")
    else:  # claude-code
        plan.provider = "claude_code"
        plan.base_url = None
        plan.model_name = Prompt.ask("  Claude model", default="sonnet")
        plan.requires_api_key = False
        plan.api_key = "local"


def _collect_control_plane(plan: Plan) -> None:
    console.print("\n[b]Control plane[/b] (organizations, roles, audit, usage caps)")
    plan.org_name = Prompt.ask("  Organization name", default=plan.org_name)
    raw_domains = Prompt.ask(
        "  Organization email domains (comma-separated, optional)",
        default="",
    )
    plan.org_domains = [
        domain.strip().lower().lstrip("@")
        for domain in raw_domains.split(",")
        if domain.strip()
    ]
    plan.root_user_id = Prompt.ask("  Root user id", default=plan.root_user_id)
    plan.control_plane_url = Prompt.ask(
        "  Control-plane URL",
        default=plan.control_plane_url,
    )

    # Dev and production both run on Postgres so the builds stay identical.
    if plan.docker:
        plan.database_url = "postgresql://nyansa:nyansa@postgres:5432/nyansa"
    elif plan.production:
        plan.database_url = Prompt.ask(
            "  Postgres DATABASE_URL",
            default="postgresql://nyansa:CHANGE_ME@localhost:5432/nyansa",
        )
    else:
        plan.database_url = Prompt.ask("  Postgres DATABASE_URL", default=plan.database_url)

    plan.govern_cli = Confirm.ask("  Route the local CLI through the control plane now?", default=True)
    plan.seed_roles = Confirm.ask(
        "  Seed sample developer / intern / auditor accounts?", default=False
    )


def _resolve_persistent(plan: Plan) -> None:
    """Reuse any secrets and paths already in `.env`, generating only what's missing.

    This makes re-runs safe: the token pepper and attestation key seed must stay
    stable, or existing token-lookup indexes and signatures would no longer match.
    """
    existing = _read_env(plan.cwd / ".env")

    for key, length in (
        ("NYANSA_CONTROL_TOKEN", 32),
        ("NYANSA_ROOT_PASSWORD", 24),
        ("NYANSA_BOOTSTRAP_SECRET", 36),
        ("NYANSA_TOKEN_PEPPER", 32),
        ("NYANSA_ATTESTATION_KEY_SEED", 32),
        ("POSTGRES_PASSWORD", 32),
    ):
        plan.secrets[key] = existing.get(key) or _gen(length)

    if existing.get("NYANSA_DATABASE_URL"):
        plan.database_url = existing["NYANSA_DATABASE_URL"]
    if existing.get("DATABASE_URL"):
        plan.database_url = existing["DATABASE_URL"]
    if plan.reuse_existing_model_config and existing.get("BASE_URL"):
        plan.base_url = existing["BASE_URL"]
    if plan.reuse_existing_model_config and existing.get("NYANSA_MODEL"):
        plan.model_name = existing["NYANSA_MODEL"]
    if existing.get("NYANSA_API_KEY"):
        plan.api_key = existing["NYANSA_API_KEY"]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def _apply_runtime_env(plan: Plan) -> None:
    """Export the secrets and paths the in-process bootstrap must use.

    The store reads NYANSA_TOKEN_PEPPER / NYANSA_ATTESTATION_KEY_SEED from
    the environment, so they must be set before the store is created — and they
    are written to `.env` so the server later reads the identical values.
    """
    os.environ.update(plan.secrets)
    if plan.database_url:
        os.environ["NYANSA_DATABASE_URL"] = plan.database_url
        os.environ["DATABASE_URL"] = plan.database_url
    if plan.base_url:
        os.environ["BASE_URL"] = plan.base_url
    os.environ["NYANSA_API_KEY"] = plan.api_key
    os.environ.setdefault("POSTGRES_DB", "nyansa")
    os.environ.setdefault("POSTGRES_USER", "nyansa")


def _pg_can_connect(database_url: str, timeout: int = 3) -> bool:
    return _pg_connect_error(database_url, timeout=timeout) is None


def _pg_connect_error(database_url: str, timeout: int = 3) -> str | None:
    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=timeout):
            return None
    except Exception as error:
        return str(error)


def _ensure_postgres(plan: Plan, assume_yes: bool) -> None:
    """Make sure Postgres is reachable, offering to start a local dev container.

    Dev and production both run on Postgres. When the configured database points at
    localhost and isn't up, the installer can start a matching container so a
    first-time dev setup stays a single command.
    """
    if _pg_can_connect(plan.database_url):
        return

    host = urlparse(plan.database_url).hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise click.ClickException(
            f"Cannot reach Postgres at '{host}'. Start it and re-run `nyansa-init`."
        )
    if not shutil.which("docker"):
        raise click.ClickException(
            "Postgres is not reachable and Docker is unavailable.\n"
            f"Start Postgres for {plan.database_url} and re-run `nyansa-init`."
        )
    if not assume_yes and not Confirm.ask(
        f"  Postgres is not running — start a local container '{DEV_PG_CONTAINER}'?",
        default=True,
    ):
        raise click.ClickException("Postgres is required. Start it and re-run.")

    _start_postgres_container(plan)


def _start_postgres_container(plan: Plan) -> None:
    parsed = urlparse(plan.database_url)
    env = {
        "POSTGRES_USER": parsed.username or "nyansa",
        "POSTGRES_PASSWORD": parsed.password or "nyansa",
        "POSTGRES_DB": (parsed.path or "/nyansa").lstrip("/") or "nyansa",
        "PGDATA": "/var/lib/postgresql/18/docker",
    }
    port = parsed.port or 5432

    console.print(f"  Starting Postgres container [b]{DEV_PG_CONTAINER}[/b] …")
    started = subprocess.run(
        ["docker", "start", DEV_PG_CONTAINER], capture_output=True, text=True
    )
    if started.returncode != 0:
        run = subprocess.run(
            [
                "docker", "run", "-d", "--name", DEV_PG_CONTAINER,
                "-e", f"POSTGRES_USER={env['POSTGRES_USER']}",
                "-e", f"POSTGRES_PASSWORD={env['POSTGRES_PASSWORD']}",
                "-e", f"POSTGRES_DB={env['POSTGRES_DB']}",
                "-e", f"PGDATA={env['PGDATA']}",
                "-p", f"{port}:5432",
                "mirror.gcr.io/library/postgres:18",
            ],
            capture_output=True, text=True,
        )
        if run.returncode != 0:
            raise click.ClickException(f"Failed to start Postgres: {run.stderr.strip()}")

    for _ in range(60):
        if _pg_can_connect(plan.database_url, timeout=2):
            console.print("  [green]Postgres is ready.[/green]")
            return
        time.sleep(1)

    error = _pg_connect_error(plan.database_url, timeout=2)
    hint = (
        f"Postgres did not become ready in time. Last connection error: {error}"
        if error
        else "Postgres did not become ready in time."
    )
    if "does not exist" in (error or ""):
        hint += (
            "\nThe local dev container may have been initialized with different "
            f"credentials. To reset local dev data, run: docker rm -f {DEV_PG_CONTAINER}"
            "\nThen re-run `nyansa-init`."
        )
    raise click.ClickException(hint)


def _bootstrap(plan: Plan) -> None:
    store = create_store_from_env()

    if store.is_bootstrapped():
        plan.already_bootstrapped = True
        plan.org_id = next(iter(store._load().organizations))
        console.print(
            "[yellow]Control plane is already bootstrapped — keeping the existing "
            "organization and root account.[/yellow]"
        )
        # Reuse an existing root token from .env if present; otherwise the operator
        # must rotate it (the stored token is a one-way hash and cannot be recovered).
        existing = _read_env(plan.cwd / ".env").get("NYANSA_CONTROL_TOKEN")
        if existing:
            plan.secrets["NYANSA_CONTROL_TOKEN"] = existing
        return

    try:
        result = store.bootstrap(
            BootstrapRequest(
                organization_name=plan.org_name,
                organization_domains=plan.org_domains,
                root_user_id=plan.root_user_id,
                root_display_name="Root User",
                root_password=plan.secrets["NYANSA_ROOT_PASSWORD"],
                root_token=plan.secrets["NYANSA_CONTROL_TOKEN"],
            )
        )
    except ControlPlaneError as error:
        raise click.ClickException(f"Bootstrap failed: {error.detail}") from error

    plan.org_id = result["organization"]["id"]

    if plan.seed_roles:
        _seed_roles(store, plan)


def _compose_database_url(plan: Plan) -> str:
    password = plan.secrets["POSTGRES_PASSWORD"]
    return f"postgresql://nyansa:{password}@postgres:5432/nyansa"


def _compose_up(plan: Plan) -> None:
    if not shutil.which("docker"):
        raise click.ClickException("Docker is required for Docker install mode.")

    console.print("\n[b]Starting Docker stack[/b]")
    env = {**os.environ, "COMPOSE_BAKE": os.environ.get("COMPOSE_BAKE", "true")}
    cleanup = subprocess.run(
        ["docker", "compose", "down", "--remove-orphans"],
        cwd=plan.cwd,
        env=env,
    )
    if cleanup.returncode != 0:
        raise click.ClickException("Docker Compose cleanup failed.")

    result = subprocess.run(
        ["docker", "compose", "up", "--build", "-d"],
        cwd=plan.cwd,
        env=env,
    )
    if result.returncode != 0:
        raise click.ClickException("Docker Compose failed.")


def _wait_for_server(plan: Plan, timeout_seconds: int = 120) -> None:
    console.print(f"  Waiting for {plan.control_plane_url} …")
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{plan.control_plane_url.rstrip('/')}/health", timeout=3)
            if response.status_code == 200:
                body = response.json()
                database = body.get("database", {})
                if database.get("ok"):
                    console.print("  [green]Control plane is online.[/green]")
                    return
                last_error = str(database.get("error") or body)
        except httpx.HTTPError as error:
            last_error = str(error)
        time.sleep(2)
    raise click.ClickException(f"Control plane did not become healthy: {last_error}")


def _bootstrap_via_api(plan: Plan) -> None:
    base_url = plan.control_plane_url.rstrip("/")
    status = httpx.get(f"{base_url}/health", timeout=5).json()
    if status.get("bootstrapped"):
        plan.already_bootstrapped = True
        actor_response = httpx.get(
            f"{base_url}/me",
            headers={"authorization": f"Bearer {plan.secrets['NYANSA_CONTROL_TOKEN']}"},
            timeout=5,
        )
        if actor_response.status_code != 200:
            raise click.ClickException(
                "Control plane is already bootstrapped, but NYANSA_CONTROL_TOKEN "
                "could not authenticate. Set a valid root/service token in .env and re-run."
            )
        actor = actor_response.json()
        plan.org_id = actor.get("organization_id", "")
        console.print(
            "[yellow]Control plane is already bootstrapped — keeping the existing "
            "organization and root account.[/yellow]"
        )
        return

    response = httpx.post(
        f"{base_url}/bootstrap",
        headers={
            "content-type": "application/json",
            "x-bootstrap-secret": plan.secrets["NYANSA_BOOTSTRAP_SECRET"],
        },
        json={
            "organization_name": plan.org_name,
            "organization_domains": plan.org_domains,
            "root_user_id": plan.root_user_id,
            "root_display_name": "Root User",
            "root_password": plan.secrets["NYANSA_ROOT_PASSWORD"],
            "root_token": plan.secrets["NYANSA_CONTROL_TOKEN"],
        },
        timeout=10,
    )
    if response.status_code >= 400:
        raise click.ClickException(f"Bootstrap failed: {response.status_code} {response.text}")
    plan.org_id = response.json()["organization"]["id"]

    # Reuse the Postgres store for optional role seeding; it points at the same DB
    # when run inside a local non-Docker install. For Docker, role seeding should
    # be done via API endpoints after the root account exists.
    if plan.seed_roles and not plan.docker:
        _seed_roles(create_store_from_env(), plan)


def _verify_server(plan: Plan) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{plan.control_plane_url.rstrip('/')}/ready", timeout=5)
        if response.status_code == 200:
            return True, "ready"
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as error:
        return False, str(error)


def _seed_roles(store, plan: Plan) -> None:
    root_user = store._load().users[plan.root_user_id]
    for role in SEED_ROLES:
        token = _gen(24)
        store.upsert_member(
            root_user,
            plan.org_id,
            UpsertMemberRequest(
                user_id=f"{role}-1",
                role=role,
                display_name=f"Sample {role.title()}",
                token=token,
            ),
        )
        plan.seeded_tokens[f"{role}-1 ({role})"] = token


# --------------------------------------------------------------------------- #
# Config file writing
# --------------------------------------------------------------------------- #

def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Upsert managed keys, preserving unrelated lines and comments."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining and out and out[-1].strip():
        out.append("")
    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def _write_env_file(plan: Plan) -> None:
    env_path = plan.cwd / ".env"
    _backup(env_path)
    updates = {
        "NYANSA_API_KEY": plan.api_key,
        "NYANSA_MODEL": plan.model_name,
        "POSTGRES_DB": "nyansa",
        "POSTGRES_USER": "nyansa",
        "POSTGRES_PASSWORD": plan.secrets["POSTGRES_PASSWORD"],
        **plan.secrets,
    }
    if plan.base_url:
        updates["BASE_URL"] = plan.base_url
    if plan.database_url:
        updates["NYANSA_DATABASE_URL"] = plan.database_url
        updates["DATABASE_URL"] = plan.database_url
    if plan.production:
        updates["NYANSA_PRODUCTION"] = "true"
    _write_env(env_path, updates)


def _write_config_toml(plan: Plan) -> None:
    config_dir = plan.cwd / ".nyansa"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    _backup(config_path)

    lines = ['approval = "on-request"', "", "[model]"]
    lines.append(f'provider = "{plan.provider}"')
    lines.append(f'name = "{plan.model_name}"')
    if plan.base_url:
        lines.append(f'base_url = "{plan.base_url}"')
    lines.append('api_key_env = "NYANSA_API_KEY"')
    lines.append(f"requires_api_key = {'true' if plan.requires_api_key else 'false'}")

    lines += [
        "",
        "[actor]",
        f'user_id = "{plan.root_user_id}"',
        f'organization_id = "{plan.org_id}"',
        'role = "root"',
        "",
        "[control_plane]",
        f"enabled = {'true' if plan.govern_cli else 'false'}",
        f'base_url = "{plan.control_plane_url}"',
        'token_env = "NYANSA_CONTROL_TOKEN"',
        # Locally, fail open if the server is down so the CLI still runs; in
        # production, fail closed so governance is never silently bypassed.
        f"fail_closed = {'true' if plan.production else 'false'}",
        "sync_actor = true",
        "send_audit_events = true",
        "",
        "[audit]",
        "enabled = true",
        "log_content = false",
        "",
        "[sensitive_data]",
        "enabled = true",
        "redact_before_model = true",
        "redact_audit_events = true",
        "",
        "[role_policy]",
        "enabled = true",
    ]
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Join an existing deployment (teammate onboarding)
# --------------------------------------------------------------------------- #

def _http_detail(error: httpx.HTTPStatusError) -> str:
    try:
        body = error.response.json()
        detail = body.get("detail")
        return detail if isinstance(detail, str) else str(body)
    except Exception:
        return error.response.text[:200] or str(error)


def _join_flow(cwd: Path, url: str) -> None:
    """Connect this machine to a control plane an admin already set up.

    Signs in with the developer's username/password, exchanges the short-lived
    session for a long-lived CLI token, and writes `.env` + `.nyansa/config.toml`
    — no bootstrap, no Docker, no Postgres. One command instead of six manual steps.
    """
    url = url.rstrip("/")
    console.print(
        Panel.fit(
            f"Connect this machine to your team's control plane at\n[b]{url}[/b]\n"
            "Sign in with the username and password your admin gave you.",
            title="Nyansa · join your team",
            border_style="cyan",
        )
    )

    try:
        health = httpx.get(f"{url}/health", timeout=10).json()
    except httpx.HTTPError as error:
        raise click.ClickException(f"Can't reach the control plane at {url}: {error}")
    if not health.get("bootstrapped"):
        raise click.ClickException(
            "That control plane isn't set up yet — ask your admin to run first-time setup."
        )

    user_id = Prompt.ask("  Username")
    password = Prompt.ask("  Password", password=True)

    try:
        response = httpx.post(
            f"{url}/auth/login",
            json={"user_id": user_id, "password": password},
            timeout=15,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise click.ClickException(f"Sign-in failed: {_http_detail(error)}")
    except httpx.HTTPError as error:
        raise click.ClickException(f"Sign-in failed: {error}")

    login = response.json()
    session_token = login["token"]
    actor = login.get("actor", {})

    try:
        cli_response = httpx.post(
            f"{url}/cli/token",
            headers={"Authorization": f"Bearer {session_token}"},
            json={},
            timeout=15,
        )
        cli_response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise click.ClickException(f"Could not issue a CLI token: {_http_detail(error)}")
    except httpx.HTTPError as error:
        raise click.ClickException(f"Could not issue a CLI token: {error}")

    cli_token = cli_response.json()["token"]

    # The org may pin a model; otherwise fall back to the free default. The endpoint
    # and key stay local (never stored in the control plane).
    model_name = actor.get("model_name") or DEFAULT_OPENROUTER_MODEL
    api_key = Prompt.ask(
        "  Model API key (OpenRouter; leave blank to set later)",
        password=True,
        default="",
    )

    env_updates = {
        "NYANSA_CONTROL_TOKEN": cli_token,
        "BASE_URL": DEFAULT_OPENROUTER_URL,
        "NYANSA_MODEL": model_name,
    }
    if api_key:
        env_updates["NYANSA_API_KEY"] = api_key
    _backup(cwd / ".env")
    _write_env(cwd / ".env", env_updates)
    _write_join_config(cwd, url, actor, model_name)

    verified = False
    try:
        verified = (
            httpx.get(
                f"{url}/me",
                headers={"Authorization": f"Bearer {cli_token}"},
                timeout=10,
            ).status_code
            == 200
        )
    except httpx.HTTPError:
        verified = False

    body = [
        f"[b]Signed in as:[/b] {user_id}  ([dim]{actor.get('role', '?')}[/dim])",
        f"[b]Organization:[/b] {actor.get('organization_id', '?')}",
        "",
        "[b]Run the agent:[/b]  source .env && uv run nyansa",
        "[b]See what you can do:[/b]  type /capabilities inside the agent",
    ]
    if not api_key:
        body += [
            "",
            "[yellow]⚠ No model API key set — add NYANSA_API_KEY to .env before your first prompt.[/yellow]",
            "  Free OpenRouter key: https://openrouter.ai/settings/keys",
        ]
    console.print(
        Panel(
            "\n".join(body),
            title="Joined" if verified else "Joined — verify sign-in manually",
            border_style="green" if verified else "yellow",
        )
    )


def _write_join_config(cwd: Path, url: str, actor: dict, model_name: str) -> None:
    config_dir = cwd / ".nyansa"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    _backup(config_path)
    org_id = actor.get("organization_id") or ""
    lines = [
        'approval = "on-request"',
        "",
        "[model]",
        'provider = "openai_compatible"',
        f'name = "{model_name}"',
        f'base_url = "{DEFAULT_OPENROUTER_URL}"',
        'api_key_env = "NYANSA_API_KEY"',
        "requires_api_key = true",
        "",
        "[actor]",
        f'user_id = "{actor.get("user_id", "")}"',
        f'organization_id = "{org_id}"',
        f'role = "{actor.get("role", "developer")}"',
        "",
        "[control_plane]",
        "enabled = true",
        f'base_url = "{url}"',
        'token_env = "NYANSA_CONTROL_TOKEN"',
        # Governed by default: if the control plane is unreachable the CLI stops
        # rather than running without policy. Set false for offline work.
        "fail_closed = true",
        "sync_actor = true",
        "send_audit_events = true",
        "",
        "[audit]",
        "enabled = true",
        "log_content = false",
    ]
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Verification + summary
# --------------------------------------------------------------------------- #

def _check_model(plan: Plan) -> tuple[bool, str]:
    if plan.provider == "claude_code":
        if shutil.which("claude"):
            return True, "claude binary found on PATH"
        return False, "claude binary not found — install Claude Code and run `claude`"
    if not plan.base_url:
        return False, "no base URL configured"
    try:
        headers = {}
        if plan.requires_api_key and plan.api_key:
            headers["Authorization"] = f"Bearer {plan.api_key}"
        response = httpx.get(
            f"{plan.base_url.rstrip('/')}/models", headers=headers, timeout=4
        )
        if response.status_code < 400:
            return True, f"reachable (HTTP {response.status_code})"
        return False, f"endpoint returned HTTP {response.status_code}"
    except httpx.HTTPError as error:
        return False, f"unreachable: {error}"


def _verify(plan: Plan) -> None:
    from config.loader import load_config

    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Result")

    if plan.docker or plan.start_server:
        ok_server, server_detail = _verify_server(plan)
        table.add_row("Server readiness", _ok(ok_server, server_detail))
    else:
        table.add_row("Postgres reachable", _ok(_pg_can_connect(plan.database_url)))

    if plan.docker or plan.start_server:
        try:
            health = httpx.get(f"{plan.control_plane_url.rstrip('/')}/health", timeout=5).json()
            booted = bool(health.get("bootstrapped"))
        except httpx.HTTPError:
            booted = False
        table.add_row("Control plane bootstrapped", _ok(booted))
        table.add_row("Audit chain integrity", _ok(ok_server if "ok_server" in locals() else False))
    else:
        store = create_store_from_env()
        booted = store.is_bootstrapped()
        table.add_row("Control plane bootstrapped", _ok(booted))

        audit = store.verify_audit_events_for_system()
        table.add_row("Audit chain integrity", _ok(audit.valid))

    ok_model, detail = _check_model(plan)
    table.add_row("Model endpoint", _ok(ok_model, detail))

    config = load_config(cwd=plan.cwd)
    errors = config.validate()
    table.add_row("CLI config loads", _ok(not errors, "; ".join(errors)))

    console.print("\n")
    console.print(table)


def _ok(value: bool, detail: str = "") -> str:
    mark = "[green]✓[/green]" if value else "[red]✗[/red]"
    return f"{mark} {detail}".rstrip()


def _print_summary(plan: Plan) -> None:
    body = [
        f"[b]Organization:[/b] {plan.org_name}  ([dim]{plan.org_id}[/dim])",
        f"[b]Organization domains:[/b] {', '.join(plan.org_domains) if plan.org_domains else '(none)'}",
        f"[b]Root user:[/b] {plan.root_user_id}",
        "",
        "[b]Secrets[/b] (also written to .env — store them safely, tokens cannot be recovered):",
        f"  root login   user={plan.root_user_id} password={plan.secrets['NYANSA_ROOT_PASSWORD']}",
        f"  root token   NYANSA_CONTROL_TOKEN = {plan.secrets['NYANSA_CONTROL_TOKEN']}",
    ]
    if not plan.already_bootstrapped:
        body.append(
            f"  bootstrap    NYANSA_BOOTSTRAP_SECRET = {plan.secrets['NYANSA_BOOTSTRAP_SECRET']}"
        )
    for label, token in plan.seeded_tokens.items():
        body.append(f"  {label}: {token}")

    body += [
        "",
        "[b]Run the CLI:[/b]  source .env && uv run nyansa",
        f"[b]Open the UI:[/b]  {plan.control_plane_url}",
    ]
    if not plan.docker:
        body.append("[b]Start the control plane + UI:[/b]  make dev")
    if plan.govern_cli and not plan.docker:
        body.append(
            "[yellow]The CLI is set to route through the control plane — start it "
            "(make run-api) before running the agent.[/yellow]"
        )
    if plan.requires_api_key and not plan.api_key:
        body += [
            "",
            "[yellow]⚠ No model API key set yet — the agent can't answer until you add one.[/yellow]",
            "  Get a key at https://openrouter.ai/settings/keys, then put it in .env:",
            "    NYANSA_API_KEY=<your-key>",
        ]
    console.print(Panel("\n".join(body), title="Setup complete", border_style="green"))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

@click.command()
@click.option(
    "--quick",
    "-q",
    is_flag=True,
    help="Accept production Docker defaults (Postgres + OpenRouter-compatible model).",
)
@click.option(
    "--cwd",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Project directory to configure (default: current directory).",
)
@click.option(
    "--database-url",
    default=None,
    help="Postgres connection string to use (overrides prompt and .env).",
)
@click.option(
    "--join",
    "join_url",
    default=None,
    metavar="URL",
    help="Join an existing control plane instead of setting one up (for teammates).",
)
def main(quick: bool, cwd: Path | None, database_url: str | None, join_url: str | None) -> None:
    project_dir = (cwd or Path.cwd()).resolve()

    # Teammate path: connect this machine to a control plane an admin already runs.
    if join_url:
        try:
            _join_flow(project_dir, join_url)
        except click.ClickException:
            raise
        except KeyboardInterrupt:
            console.print("\n[red]Cancelled.[/red]")
            raise SystemExit(1)
        return

    plan = Plan(cwd=project_dir)
    if database_url:
        plan.database_url = database_url

    try:
        _collect(plan, quick)
        _resolve_persistent(plan)
        if plan.docker:
            plan.database_url = _compose_database_url(plan)
        # An explicit --database-url wins over whatever .env carried.
        if database_url:
            plan.database_url = database_url
        _apply_runtime_env(plan)
        _write_env_file(plan)
        if plan.docker or plan.start_server:
            _compose_up(plan)
            _wait_for_server(plan)
            _bootstrap_via_api(plan)
        else:
            _ensure_postgres(plan, assume_yes=quick)
            _bootstrap(plan)
        _write_config_toml(plan)
        _verify(plan)
        _print_summary(plan)
    except click.ClickException:
        raise
    except KeyboardInterrupt:
        console.print("\n[red]Setup cancelled.[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
