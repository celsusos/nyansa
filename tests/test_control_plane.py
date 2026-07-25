import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from control_plane.api import create_app
from control_plane.settings import ControlPlaneSettings
from control_plane.storage import ControlPlaneStore


class ControlPlaneApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"
        self.store = ControlPlaneStore(self.state_path)
        self.client = TestClient(create_app(self.store))
        self.root_token = "root-token-for-tests"

    def tearDown(self):
        self.tmpdir.cleanup()

    def auth(self, token: str | None = None):
        return {"Authorization": f"Bearer {token or self.root_token}"}

    def bootstrap(self):
        response = self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_token": self.root_token,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["organization"]["id"]

    def test_bootstrap_creates_root_actor(self):
        org_id = self.bootstrap()

        response = self.client.get("/me", headers=self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], "root")
        self.assertEqual(response.json()["organization_id"], org_id)
        self.assertEqual(response.json()["role"], "root")

        state = self.store._load()
        self.assertTrue(state.users["root"].token_hash.startswith("pbkdf2_sha256$"))

    def test_bootstrap_root_can_login_with_password_and_issue_cli_token(self):
        response = self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_password": "correct-horse-password",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["root_token"])

        login_response = self.client.post(
            "/auth/login",
            json={"user_id": "root", "password": "correct-horse-password"},
        )
        self.assertEqual(login_response.status_code, 200)
        session_token = login_response.json()["token"]

        me_response = self.client.get("/me", headers=self.auth(session_token))
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["role"], "root")

        cli_response = self.client.post(
            "/cli/token",
            headers=self.auth(session_token),
            json={},
        )
        self.assertEqual(cli_response.status_code, 200)
        cli_token = cli_response.json()["token"]
        self.assertNotEqual(cli_token, session_token)

        cli_me_response = self.client.get("/me", headers=self.auth(cli_token))
        self.assertEqual(cli_me_response.status_code, 200)
        self.assertEqual(cli_me_response.json()["user_id"], "root")

    def test_user_can_list_and_revoke_own_cli_tokens(self):
        self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_password": "correct-horse-password",
            },
        )
        login_response = self.client.post(
            "/auth/login",
            json={"user_id": "root", "password": "correct-horse-password"},
        )
        session_token = login_response.json()["token"]

        issued = self.client.post(
            "/cli/token",
            headers=self.auth(session_token),
            json={"name": "Laptop"},
        )
        self.assertEqual(issued.status_code, 200)
        token_id = issued.json()["id"]
        cli_token = issued.json()["token"]

        tokens = self.client.get("/cli/tokens", headers=self.auth(session_token))
        self.assertEqual(tokens.status_code, 200)
        self.assertEqual(tokens.json()[0]["id"], token_id)
        self.assertEqual(tokens.json()[0]["name"], "Laptop")
        self.assertNotIn("token_hash", tokens.text)

        before_revoke = self.client.get("/me", headers=self.auth(cli_token))
        self.assertEqual(before_revoke.status_code, 200)

        revoked = self.client.delete(
            f"/cli/tokens/{token_id}",
            headers=self.auth(session_token),
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertIsNotNone(revoked.json()["revoked_at"])

        after_revoke = self.client.get("/me", headers=self.auth(cli_token))
        self.assertEqual(after_revoke.status_code, 401)

    def test_member_can_login_with_password_without_manual_token(self):
        root_org_id = self.bootstrap()
        response = self.client.put(
            f"/organizations/{root_org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "developer-1",
                "display_name": "Developer One",
                "role": "developer",
                "password": "developer-password",
            },
        )
        self.assertEqual(response.status_code, 200)

        login_response = self.client.post(
            "/auth/login",
            json={"user_id": "developer-1", "password": "developer-password"},
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(login_response.json()["actor"]["role"], "developer")

        cli_response = self.client.post(
            "/cli/token",
            headers=self.auth(login_response.json()["token"]),
            json={},
        )
        self.assertEqual(cli_response.status_code, 200)
        self.assertTrue(cli_response.json()["token"])

    def test_microsoft_sign_in_auto_provisions_and_sets_cookie_session(self):
        org_id = self.bootstrap()
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    microsoft_enabled=True,
                    microsoft_client_id="client-id",
                    microsoft_client_secret="client-secret",
                    microsoft_redirect_uri="http://testserver/auth/microsoft/callback",
                    microsoft_allowed_domains=["example.com"],
                ),
            ),
            follow_redirects=False,
        )

        start_response = client.get("/auth/microsoft/start")
        self.assertEqual(start_response.status_code, 307)
        self.assertIn("login.microsoftonline.com", start_response.headers["location"])

        with patch(
            "control_plane.api._exchange_microsoft_code",
            return_value={
                "tid": "tenant-1",
                "oid": "object-1",
                "preferred_username": "dev@example.com",
                "name": "Dev Example",
            },
        ):
            callback_response = client.get(
                "/auth/microsoft/callback?code=abc&state="
                + start_response.cookies["nyansa_ms_state"].split(".", 1)[0]
            )

        self.assertEqual(callback_response.status_code, 307)
        self.assertIn("nyansa_session", callback_response.cookies)

        me_response = client.get("/me")
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["user_id"], "dev@example.com")
        self.assertEqual(me_response.json()["organization_id"], org_id)
        self.assertEqual(me_response.json()["role"], "developer")

    def test_microsoft_sign_in_rejects_unapproved_domain(self):
        self.bootstrap()
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    microsoft_enabled=True,
                    microsoft_client_id="client-id",
                    microsoft_client_secret="client-secret",
                    microsoft_redirect_uri="http://testserver/auth/microsoft/callback",
                    microsoft_allowed_domains=["example.com"],
                ),
            ),
            follow_redirects=False,
        )
        start_response = client.get("/auth/microsoft/start")

        with patch(
            "control_plane.api._exchange_microsoft_code",
            return_value={
                "tid": "tenant-1",
                "oid": "object-1",
                "preferred_username": "dev@other.example",
                "name": "Dev Example",
            },
        ):
            callback_response = client.get(
                "/auth/microsoft/callback?code=abc&state="
                + start_response.cookies["nyansa_ms_state"].split(".", 1)[0]
            )

        self.assertEqual(callback_response.status_code, 403)

    def test_microsoft_multi_org_requires_domain_mapping(self):
        self.bootstrap()
        self.client.post("/organizations", headers=self.auth(), json={"name": "Second Org"})
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    microsoft_enabled=True,
                    microsoft_client_id="client-id",
                    microsoft_client_secret="client-secret",
                    microsoft_redirect_uri="http://testserver/auth/microsoft/callback",
                    microsoft_allowed_domains=["example.com"],
                ),
            ),
            follow_redirects=False,
        )
        start_response = client.get("/auth/microsoft/start")

        with patch(
            "control_plane.api._exchange_microsoft_code",
            return_value={
                "tid": "tenant-1",
                "oid": "object-1",
                "preferred_username": "dev@example.com",
                "name": "Dev Example",
            },
        ):
            callback_response = client.get(
                "/auth/microsoft/callback?code=abc&state="
                + start_response.cookies["nyansa_ms_state"].split(".", 1)[0]
            )

        self.assertEqual(callback_response.status_code, 409)

    def test_microsoft_domain_map_routes_user_to_matching_org(self):
        self.bootstrap()
        org_response = self.client.post(
            "/organizations",
            headers=self.auth(),
            json={"name": "Engineering", "domains": ["example.com"]},
        )
        org_id = org_response.json()["id"]
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    microsoft_enabled=True,
                    microsoft_client_id="client-id",
                    microsoft_client_secret="client-secret",
                    microsoft_redirect_uri="http://testserver/auth/microsoft/callback",
                    microsoft_allowed_domains=["example.com"],
                ),
            ),
            follow_redirects=False,
        )
        start_response = client.get("/auth/microsoft/start")

        with patch(
            "control_plane.api._exchange_microsoft_code",
            return_value={
                "tid": "tenant-1",
                "oid": "object-1",
                "preferred_username": "dev@example.com",
                "name": "Dev Example",
            },
        ):
            callback_response = client.get(
                "/auth/microsoft/callback?code=abc&state="
                + start_response.cookies["nyansa_ms_state"].split(".", 1)[0]
            )

        self.assertEqual(callback_response.status_code, 307)
        me_response = client.get("/me")
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["organization_id"], org_id)

    def test_microsoft_env_domain_map_routes_by_org_name(self):
        self.bootstrap()
        org_response = self.client.post(
            "/organizations",
            headers=self.auth(),
            json={"name": "Mapped Org"},
        )
        org_id = org_response.json()["id"]
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    microsoft_enabled=True,
                    microsoft_client_id="client-id",
                    microsoft_client_secret="client-secret",
                    microsoft_redirect_uri="http://testserver/auth/microsoft/callback",
                    microsoft_allowed_domains=["example.com"],
                    microsoft_domain_org_map={"example.com": "Mapped Org"},
                ),
            ),
            follow_redirects=False,
        )
        start_response = client.get("/auth/microsoft/start")

        with patch(
            "control_plane.api._exchange_microsoft_code",
            return_value={
                "tid": "tenant-1",
                "oid": "object-1",
                "preferred_username": "dev@example.com",
                "name": "Dev Example",
            },
        ):
            callback_response = client.get(
                "/auth/microsoft/callback?code=abc&state="
                + start_response.cookies["nyansa_ms_state"].split(".", 1)[0]
            )

        self.assertEqual(callback_response.status_code, 307)
        me_response = client.get("/me")
        self.assertEqual(me_response.json()["organization_id"], org_id)

    def test_root_can_create_org_and_add_intern_member(self):
        self.bootstrap()

        org_response = self.client.post(
            "/organizations",
            headers=self.auth(),
            json={"name": "ECG Engineering"},
        )
        self.assertEqual(org_response.status_code, 200)
        org_id = org_response.json()["id"]

        member_response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "intern-1",
                "display_name": "Intern One",
                "role": "intern",
                "token": "intern-token-for-tests",
            },
        )
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(member_response.json()["role"], "intern")

        actor_response = self.client.get(
            "/me",
            headers=self.auth("intern-token-for-tests"),
            params={"organization_id": org_id},
        )
        self.assertEqual(actor_response.status_code, 200)
        self.assertEqual(actor_response.json()["role"], "intern")

    def test_org_owner_can_update_org_restricted_role_policy(self):
        org_id = self.bootstrap()
        owner_response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "owner-1",
                "role": "org_owner",
                "token": "owner-token-for-tests",
            },
        )
        self.assertEqual(owner_response.status_code, 200)

        policy_response = self.client.put(
            f"/organizations/{org_id}/policies/roles",
            headers=self.auth("owner-token-for-tests"),
            json={"role": "intern", "allowed_tools": ["read_file", "grep"]},
        )
        self.assertEqual(policy_response.status_code, 200)

        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "intern-1",
                "role": "intern",
                "token": "intern-token-for-tests",
            },
        )
        actor_response = self.client.get(
            "/me",
            headers=self.auth("intern-token-for-tests"),
            params={"organization_id": org_id},
        )
        self.assertEqual(actor_response.status_code, 200)
        self.assertEqual(actor_response.json()["allowed_tools"], ["grep", "read_file"])

    def test_developer_cannot_update_org_role_policy(self):
        org_id = self.bootstrap()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "developer-1",
                "role": "developer",
                "token": "developer-token-for-tests",
            },
        )

        policy_response = self.client.put(
            f"/organizations/{org_id}/policies/roles",
            headers=self.auth("developer-token-for-tests"),
            json={"role": "intern", "allowed_tools": ["read_file"]},
        )
        self.assertEqual(policy_response.status_code, 403)

    def test_developer_default_tools_exclude_shell_network_and_mcp(self):
        org_id = self.bootstrap()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "developer-1",
                "role": "developer",
                "token": "developer-token-for-tests",
            },
        )

        actor_response = self.client.get(
            "/me",
            headers=self.auth("developer-token-for-tests"),
            params={"organization_id": org_id},
        )

        self.assertEqual(actor_response.status_code, 200)
        allowed = set(actor_response.json()["allowed_tools"])
        self.assertIn("write_file", allowed)
        self.assertIn("edit_file", allowed)
        self.assertNotIn("shell", allowed)
        self.assertNotIn("web_fetch", allowed)
        self.assertNotIn("web_search", allowed)
        self.assertNotIn("mcp", allowed)

    def test_intern_can_request_escalation_visible_on_security_dashboard(self):
        org_id = self.bootstrap()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={"user_id": "intern-1", "role": "intern", "token": "intern-token-for-tests"},
        )
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "security-1",
                "role": "security_admin",
                "token": "security-token-for-tests",
            },
        )

        escalation = self.client.post(
            "/intern/escalations",
            headers=self.auth("intern-token-for-tests"),
            json={
                "organization_id": org_id,
                "requested_tool": "web_fetch",
                "reason": "Need to inspect upstream advisory details",
            },
        )
        self.assertEqual(escalation.status_code, 200)

        dashboard = self.client.get(
            "/role-dashboard",
            headers=self.auth("security-token-for-tests"),
            params={"organization_id": org_id},
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["pending_escalations"], 1)

    def test_auditor_can_export_org_audit_package(self):
        org_id = self.bootstrap()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={"user_id": "auditor-1", "role": "auditor", "token": "auditor-token-for-tests"},
        )
        self.client.post(
            "/audit/events",
            headers=self.auth(),
            json={
                "organization_id": org_id,
                "event_type": "test.event",
                "details": {"ok": True},
            },
        )

        export_response = self.client.get(
            "/audit/export",
            headers=self.auth("auditor-token-for-tests"),
            params={"organization_id": org_id},
        )
        self.assertEqual(export_response.status_code, 200)
        self.assertTrue(export_response.json()["audit"]["valid"])
        self.assertEqual(len(export_response.json()["events"]), 1)

    def test_org_admin_can_rotate_member_token(self):
        self.bootstrap()
        org_response = self.client.post(
            "/organizations",
            headers=self.auth(),
            json={"name": "GRIDCo Engineering"},
        )
        self.assertEqual(org_response.status_code, 200)
        org_id = org_response.json()["id"]

        member_response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "developer-1",
                "display_name": "Developer One",
                "role": "developer",
                "token": "old-developer-token",
            },
        )
        self.assertEqual(member_response.status_code, 200)

        rotate_response = self.client.post(
            f"/organizations/{org_id}/members/developer-1/rotate-token",
            headers=self.auth(),
            json={"token": "new-developer-token"},
        )
        self.assertEqual(rotate_response.status_code, 200)

        old_actor_response = self.client.get(
            "/me",
            headers=self.auth("old-developer-token"),
            params={"organization_id": org_id},
        )
        self.assertEqual(old_actor_response.status_code, 401)

        new_actor_response = self.client.get(
            "/me",
            headers=self.auth("new-developer-token"),
            params={"organization_id": org_id},
        )
        self.assertEqual(new_actor_response.status_code, 200)
        self.assertEqual(new_actor_response.json()["role"], "developer")

    def test_root_can_update_role_policy_and_actor_receives_allowed_tools(self):
        self.bootstrap()
        org_response = self.client.post(
            "/organizations",
            headers=self.auth(),
            json={"name": "Policy Test Org"},
        )
        self.assertEqual(org_response.status_code, 200)
        org_id = org_response.json()["id"]

        policy_response = self.client.put(
            "/policies/roles/intern",
            headers=self.auth(),
            json={"allowed_tools": ["read_file", "grep"]},
        )
        self.assertEqual(policy_response.status_code, 200)
        self.assertEqual(policy_response.json()["allowed_tools"], ["grep", "read_file"])

        member_response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "policy-intern",
                "role": "intern",
                "token": "policy-intern-token",
            },
        )
        self.assertEqual(member_response.status_code, 200)

        actor_response = self.client.get(
            "/me",
            headers=self.auth("policy-intern-token"),
            params={"organization_id": org_id},
        )

        self.assertEqual(actor_response.status_code, 200)
        self.assertEqual(actor_response.json()["allowed_tools"], ["grep", "read_file"])

    def test_root_can_update_usage_policy_and_usage_check_enforces_limit(self):
        org_id = self.bootstrap()

        update_response = self.client.put(
            "/usage/policies/developer",
            headers=self.auth(),
            json={
                "enabled": True,
                "requests_per_minute": 1,
                "requests_per_hour": 10,
                "requests_per_day": 10,
                "org_requests_per_day": 10,
                "max_turns_per_session": 5,
                "max_session_minutes": 10,
                "max_context_tokens": 32000,
            },
        )
        self.assertEqual(update_response.status_code, 200)

        member_response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "quota-dev",
                "role": "developer",
                "token": "quota-dev-token-for-tests",
            },
        )
        self.assertEqual(member_response.status_code, 200)

        headers = self.auth("quota-dev-token-for-tests")
        first = self.client.post(
            "/usage/check",
            headers=headers,
            json={"model": "cohere/north-mini-code:free", "organization_id": org_id},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/usage/check",
            headers=headers,
            json={"model": "cohere/north-mini-code:free", "organization_id": org_id},
        )
        self.assertEqual(second.status_code, 429)

    def test_actor_response_includes_usage_policy(self):
        self.bootstrap()

        response = self.client.get("/me", headers=self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertIn("usage_policy", response.json())
        self.assertEqual(response.json()["usage_policy"]["max_context_tokens"], 256000)

    def test_usage_summary_requires_org_scope_for_non_platform_admin(self):
        org_id = self.bootstrap()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={
                "user_id": "owner-1",
                "role": "org_owner",
                "token": "owner-token-for-tests",
            },
        )

        missing_scope = self.client.get(
            "/usage/summary",
            headers=self.auth("owner-token-for-tests"),
        )
        scoped = self.client.get(
            "/usage/summary",
            headers=self.auth("owner-token-for-tests"),
            params={"organization_id": org_id},
        )

        self.assertEqual(missing_scope.status_code, 400)
        self.assertEqual(scoped.status_code, 200)

    def test_session_registry_respects_role_hierarchy(self):
        org_id = self.bootstrap()
        for user_id, role, token in (
            ("developer-1", "developer", "developer-token-for-tests"),
            ("owner-1", "org_owner", "owner-token-for-tests"),
            ("auditor-1", "auditor", "auditor-token-for-tests"),
        ):
            response = self.client.put(
                f"/organizations/{org_id}/members",
                headers=self.auth(),
                json={"user_id": user_id, "role": role, "token": token},
            )
            self.assertEqual(response.status_code, 200)

        register = self.client.post(
            "/sessions",
            headers=self.auth("developer-token-for-tests"),
            json={
                "session_id": "session-1",
                "organization_id": org_id,
                "cwd": "/repo",
                "model": "cohere/north-mini-code:free",
            },
        )
        self.assertEqual(register.status_code, 200)

        developer_list = self.client.get(
            "/sessions",
            headers=self.auth("developer-token-for-tests"),
        )
        auditor_list = self.client.get(
            "/sessions",
            headers=self.auth("auditor-token-for-tests"),
            params={"organization_id": org_id},
        )
        developer_revoke = self.client.post(
            "/sessions/session-1/revoke",
            headers=self.auth("developer-token-for-tests"),
        )
        owner_revoke = self.client.post(
            "/sessions/session-1/revoke",
            headers=self.auth("owner-token-for-tests"),
        )

        self.assertEqual(developer_list.status_code, 200)
        self.assertEqual(len(developer_list.json()["sessions"]), 1)
        self.assertEqual(auditor_list.status_code, 200)
        self.assertEqual(len(auditor_list.json()["sessions"]), 1)
        self.assertEqual(developer_revoke.status_code, 403)
        self.assertEqual(owner_revoke.status_code, 200)
        self.assertEqual(owner_revoke.json()["status"], "revoked")

    def test_revoked_session_heartbeat_is_rejected(self):
        org_id = self.bootstrap()
        self.client.post(
            "/sessions",
            headers=self.auth(),
            json={
                "session_id": "session-revoked",
                "organization_id": org_id,
                "cwd": "/repo",
                "model": "local-model",
            },
        )
        self.client.post("/sessions/session-revoked/revoke", headers=self.auth())

        heartbeat = self.client.post(
            "/sessions/session-revoked/heartbeat",
            headers=self.auth(),
            json={"turn_count": 1, "last_message_preview": "hello"},
        )

        self.assertEqual(heartbeat.status_code, 423)

    def test_invalid_token_is_rejected(self):
        self.bootstrap()

        response = self.client.get("/organizations", headers=self.auth("bad-token"))

        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_rejected(self):
        self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_token": self.root_token,
                "root_token_expires_at": "2000-01-01T00:00:00+00:00",
            },
        )

        response = self.client.get("/me", headers=self.auth())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Token expired")

    def test_authenticated_user_can_ingest_audit_event(self):
        org_id = self.bootstrap()

        response = self.client.post(
            "/audit/events",
            headers=self.auth(),
            json={
                "event_type": "tool.start",
                "actor": {"user_id": "root", "organization_id": org_id, "role": "root"},
                "details": {"tool_name": "read_file"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event_type"], "tool.start")

        list_response = self.client.get(
            "/audit/events",
            headers=self.auth(),
            params={"organization_id": org_id},
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["events"][0]["event_type"], "tool.start")

        verify_response = self.client.get("/audit/verify", headers=self.auth())
        self.assertEqual(verify_response.status_code, 200)
        self.assertTrue(verify_response.json()["valid"])

    def test_audit_verify_detects_tampered_event(self):
        org_id = self.bootstrap()
        for tool_name in ("read_file", "grep"):
            response = self.client.post(
                "/audit/events",
                headers=self.auth(),
                json={
                    "event_type": "tool.start",
                    "actor": {
                        "user_id": "root",
                        "organization_id": org_id,
                        "role": "root",
                    },
                    "details": {"tool_name": tool_name},
                },
            )
            self.assertEqual(response.status_code, 200)

        state = self.store._load()
        state.audit_events[0].details["tool_name"] = "tampered"
        self.store._save(state)

        verify_response = self.client.get("/audit/verify", headers=self.auth())

        self.assertEqual(verify_response.status_code, 200)
        self.assertFalse(verify_response.json()["valid"])
        self.assertEqual(verify_response.json()["broken_index"], 0)

    def test_production_bootstrap_requires_bootstrap_secret(self):
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    production=True,
                    bootstrap_secret="bootstrap-secret",
                ),
            )
        )

        rejected = client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_token": self.root_token,
            },
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = client.post(
            "/bootstrap",
            headers={"X-Bootstrap-Secret": "bootstrap-secret"},
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_token": self.root_token,
            },
        )
        self.assertEqual(accepted.status_code, 200)

    def test_readiness_and_security_headers(self):
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Nyansa Control Plane", dashboard.text)

        not_ready = self.client.get("/ready")
        self.assertEqual(not_ready.status_code, 503)

        self.bootstrap()
        ready = self.client.get("/ready")

        self.assertEqual(ready.status_code, 200)
        self.assertTrue(ready.json()["audit"]["valid"])
        self.assertEqual(ready.headers["X-Frame-Options"], "DENY")

    def test_setup_status_guides_unbootstrapped_user(self):
        response = self.client.get("/setup/status")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["state_backend"], "test-injected")
        self.assertIn("bootstrap", {check["key"] for check in body["checks"]})

    def test_setup_status_is_visible_before_login_after_bootstrap(self):
        self.bootstrap()

        response = self.client.get("/setup/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("checks", response.json())

    def test_model_check_requires_auth_after_bootstrap(self):
        self.bootstrap()

        response = self.client.post(
            "/setup/model-check",
            json={
                "provider": "openai_compatible",
                "base_url": "http://localhost:11434/v1",
                "model": "local-model",
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_model_check_rejects_invalid_url(self):
        response = self.client.post(
            "/setup/model-check",
            json={
                "provider": "openai_compatible",
                "base_url": "file:///etc/passwd",
                "model": "local-model",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_serves_quasar_build_when_available(self):
        dist = Path(self.tmpdir.name) / "web" / "spa"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><div id=\"q-app\"></div>", encoding="utf-8")

        with patch.dict("os.environ", {"NYANSA_WEB_DIST": str(dist)}):
            client = TestClient(create_app(self.store))

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("q-app", response.text)


if __name__ == "__main__":
    unittest.main()
