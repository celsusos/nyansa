from __future__ import annotations

import os
from typing import Any

import httpx

from config.config import Config


def _headers(config: Config) -> dict[str, str]:
    token = os.environ.get(config.control_plane.token_env)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def control_plane_enabled(config: Config) -> bool:
    return config.control_plane.enabled


def _request_error_message(config: Config, action: str, error: httpx.RequestError) -> str:
    return (
        f"Control plane {action} failed: could not reach "
        f"{config.control_plane.base_url.rstrip('/')}. Start the Docker stack with "
        "`make docker-up`, or set [control_plane].fail_closed = false for local "
        f"offline use. Details: {error}"
    )


async def control_plane_get(config: Config, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(
            f"{config.control_plane.base_url.rstrip('/')}{path}",
            headers=_headers(config),
            params=params,
        )
    response.raise_for_status()
    return response.json()


async def control_plane_post(
    config: Config,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            f"{config.control_plane.base_url.rstrip('/')}{path}",
            headers=_headers(config),
            json=payload,
        )
    response.raise_for_status()
    return response.json()


async def sync_actor_from_control_plane(config: Config) -> None:
    if not config.control_plane.enabled or not config.control_plane.sync_actor:
        return

    try:
        data = await control_plane_get(
            config,
            "/me",
            params={"organization_id": config.actor.organization_id},
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane actor sync failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "actor sync", error)) from error
        return

    config.actor.user_id = data["user_id"]
    if data.get("organization_id"):
        config.actor.organization_id = data["organization_id"]
    config.actor.role = data["role"]
    config.role_policy.allowed_tools = data.get("allowed_tools")
    if data.get("usage_policy"):
        config.usage_policy = config.usage_policy.__class__(**data["usage_policy"])
    # The organization's governed model choice wins over local model metadata.
    if data.get("model_name"):
        config.model.name = data["model_name"]
    if data.get("model_provider"):
        config.model.provider = data["model_provider"]
    if "model_base_url" in data:
        config.model.base_url = data["model_base_url"]
    if data.get("model_requires_api_key") is not None:
        config.model.requires_api_key = bool(data["model_requires_api_key"])
    if data.get("network_policy"):
        network_policy = data["network_policy"]
        config.sandbox.allow_network = bool(network_policy.get("allow_network", False))
        config.sandbox.allowed_domains = list(network_policy.get("allowed_domains") or [])
        config.sandbox.blocked_domains = list(network_policy.get("blocked_domains") or [])
        config.sandbox.require_proxy = bool(network_policy.get("require_proxy", False))
        config.sandbox.proxy_url = network_policy.get("proxy_url")


async def check_model_usage(
    config: Config,
    *,
    model: str,
    session_id: str | None = None,
    turn_number: int | None = None,
    context_tokens: int | None = None,
) -> None:
    if not config.control_plane.enabled:
        return

    try:
        await control_plane_post(
            config,
            "/usage/check",
            {
                "model": model,
                "organization_id": config.actor.organization_id,
                "session_id": session_id,
                "turn_number": turn_number,
                "context_tokens": context_tokens,
            },
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane usage check failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "usage check", error)) from error


async def register_agent_session(config: Config, session_id: str) -> None:
    if not control_plane_enabled(config):
        return
    try:
        await control_plane_post(
            config,
            "/sessions",
            {
                "session_id": session_id,
                "cwd": str(config.cwd),
                "model": config.model_name,
                "organization_id": config.actor.organization_id,
            },
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane session registration failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "session registration", error)) from error


async def heartbeat_agent_session(
    config: Config,
    session_id: str,
    *,
    turn_count: int,
    last_message_preview: str = "",
) -> None:
    if not control_plane_enabled(config):
        return
    try:
        await control_plane_post(
            config,
            f"/sessions/{session_id}/heartbeat",
            {
                "turn_count": turn_count,
                "last_message_preview": last_message_preview[:200],
                "model": config.model_name,
            },
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane session heartbeat failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "session heartbeat", error)) from error


async def record_agent_session_turn(
    config: Config,
    session_id: str,
    *,
    turn_number: int,
    message_preview: str = "",
) -> None:
    if not control_plane_enabled(config):
        return
    try:
        await control_plane_post(
            config,
            f"/sessions/{session_id}/turns",
            {
                "turn_number": turn_number,
                "message_preview": message_preview[:200],
                "model": config.model_name,
            },
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane session turn sync failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "session turn sync", error)) from error


async def end_agent_session(config: Config, session_id: str, status: str = "ended") -> None:
    if not control_plane_enabled(config):
        return
    try:
        await control_plane_post(
            config,
            f"/sessions/{session_id}/end",
            {"status": status},
        )
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane session end failed: {error.response.text}"
            ) from error
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "session end", error)) from error


async def send_attestation(config: Config, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not config.control_plane.enabled or not config.control_plane.send_attestations:
        return None

    try:
        return await control_plane_post(config, "/attestations", payload)
    except httpx.HTTPStatusError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(
                f"Control plane attestation failed: {error.response.text}"
            ) from error
        return None
    except httpx.RequestError as error:
        if config.control_plane.fail_closed:
            raise RuntimeError(_request_error_message(config, "attestation", error)) from error
        return None


def send_audit_event(config: Config, event: dict[str, Any]) -> None:
    if not config.control_plane.enabled or not config.control_plane.send_audit_events:
        return

    try:
        response = httpx.post(
            f"{config.control_plane.base_url.rstrip('/')}/audit/events",
            headers=_headers(config),
            json={
                "event_type": event["event_type"],
                "organization_id": config.actor.organization_id,
                # Identity is derived server-side from the token; sent only as a hint.
                "actor": event.get("actor", {}),
                "details": event.get("details", {}),
            },
            timeout=2,
        )
        if response.status_code >= 400 and config.control_plane.fail_closed:
            raise RuntimeError(f"Control plane audit ingest failed: {response.text}")
    except Exception:
        if config.control_plane.fail_closed:
            raise
