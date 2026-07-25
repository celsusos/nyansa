import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from control_plane.api import _resolve_safe_target, create_app
from control_plane.models import IngestAuditEventRequest
from control_plane.settings import ControlPlaneSettings
from control_plane.signing import AttestationSigner
from control_plane.storage import ControlPlaneStore


class ControlPlaneHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"
        self.store = ControlPlaneStore(self.state_path)
        self.client = TestClient(create_app(self.store))
        self.root_token = "root-token-for-hardening-tests"

    def tearDown(self):
        self.tmpdir.cleanup()

    def auth(self, token=None):
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

    def add_member(self, org_id, user_id, role, token):
        response = self.client.put(
            f"/organizations/{org_id}/members",
            headers=self.auth(),
            json={"user_id": user_id, "role": role, "token": token},
        )
        self.assertEqual(response.status_code, 200)

    # --- SSRF -----------------------------------------------------------------

    def test_model_check_blocks_cloud_metadata_address(self):
        self.bootstrap()

        response = self.client.post(
            "/setup/model-check",
            headers=self.auth(),
            json={
                "provider": "openai_compatible",
                "base_url": "http://169.254.169.254/v1",
                "model": "local-model",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("disallowed", response.json()["detail"])

    def test_model_check_allows_loopback_endpoint(self):
        # Loopback must stay reachable — local Ollama is the primary endpoint. The
        # connection itself fails (nothing listening), which is a 200 ok=False, not a
        # 400 from the SSRF guard.
        self.bootstrap()

        response = self.client.post(
            "/setup/model-check",
            headers=self.auth(),
            json={
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "local-model",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])

    def test_resolve_pins_to_the_validated_ip(self):
        target = _resolve_safe_target("http://127.0.0.1:11434/v1")

        # Connection goes to the literal IP; hostname is preserved for Host/SNI.
        self.assertEqual(target.connect_base, "http://127.0.0.1:11434/v1")
        self.assertEqual(target.host_header, "127.0.0.1:11434")
        self.assertEqual(target.sni, "127.0.0.1")

    def test_resolve_rejects_host_that_rebinds_to_metadata(self):
        # Simulate a hostname resolving to the cloud metadata address. Validating the
        # resolved address (not the name) and pinning it closes the rebinding vector.
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

        with patch("control_plane.api.socket.getaddrinfo", fake_getaddrinfo):
            with self.assertRaises(HTTPException) as ctx:
                _resolve_safe_target("http://evil.example.com/v1")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_production_model_check_requires_secret_before_bootstrap(self):
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(production=True, bootstrap_secret="boot-secret"),
            )
        )

        rejected = client.post(
            "/setup/model-check",
            json={"provider": "claude_code", "base_url": "local", "model": "sonnet"},
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = client.post(
            "/setup/model-check",
            headers={"X-Bootstrap-Secret": "boot-secret"},
            json={"provider": "claude_code", "base_url": "local", "model": "sonnet"},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_production_readiness_requires_durable_state_and_secrets(self):
        self.bootstrap()
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(production=True, bootstrap_secret="boot-secret"),
            )
        )

        response = client.get("/ready")

        self.assertEqual(response.status_code, 503)
        gaps = response.json()["detail"]["gaps"]
        self.assertIn("DATABASE_URL or NYANSA_DATABASE_URL", gaps)
        self.assertIn("NYANSA_TOKEN_PEPPER", gaps)
        self.assertIn("NYANSA_ATTESTATION_KEY or NYANSA_ATTESTATION_KEY_SEED", gaps)

    def test_production_readiness_accepts_postgres_and_persistent_secrets(self):
        self.bootstrap()
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    production=True,
                    bootstrap_secret="boot-secret",
                    database_url="postgresql://example",
                    token_pepper_present=True,
                    attestation_key_present=True,
                ),
            )
        )

        response = client.get("/ready")

        self.assertEqual(response.status_code, 200)

    def test_production_setup_status_requires_auth_after_bootstrap(self):
        self.bootstrap()
        client = TestClient(
            create_app(
                self.store,
                ControlPlaneSettings(
                    production=True,
                    bootstrap_secret="boot-secret",
                    database_url="postgresql://example",
                    token_pepper_present=True,
                    attestation_key_present=True,
                ),
            )
        )

        rejected = client.get("/setup/status")
        self.assertEqual(rejected.status_code, 401)

        accepted = client.get("/setup/status", headers=self.auth())
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("checks", accepted.json())

    def test_attestation_key_seed_is_stable_across_restarts(self):
        with patch.dict("os.environ", {"NYANSA_ATTESTATION_KEY_SEED": "stable-seed"}):
            first = AttestationSigner(Path(self.tmpdir.name) / "first.pem")
            second = AttestationSigner(Path(self.tmpdir.name) / "second.pem")

        self.assertEqual(first.public_key_pem(), second.public_key_pem())

    # --- Deprovisioning -------------------------------------------------------

    def test_removed_member_token_stops_authenticating(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        before = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-1"), params={"organization_id": org_id}
        )
        self.assertEqual(before.status_code, 200)

        removed = self.client.delete(
            f"/organizations/{org_id}/members/dev-1", headers=self.auth()
        )
        self.assertEqual(removed.status_code, 200)

        after = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-1"), params={"organization_id": org_id}
        )
        self.assertEqual(after.status_code, 401)

    def test_readding_member_reenables_account(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")
        self.client.delete(f"/organizations/{org_id}/members/dev-1", headers=self.auth())

        # Re-add with a fresh token.
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-2")

        reenabled = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-2"), params={"organization_id": org_id}
        )
        self.assertEqual(reenabled.status_code, 200)
        self.assertEqual(reenabled.json()["role"], "developer")

    # --- Server-derived actor (anti-spoofing) ---------------------------------

    def test_audit_actor_cannot_be_spoofed(self):
        org_id = self.bootstrap()

        response = self.client.post(
            "/audit/events",
            headers=self.auth(),
            json={
                "event_type": "tool.start",
                "actor": {"user_id": "attacker", "organization_id": "evil-org", "role": "root"},
                "details": {"tool_name": "read_file"},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actor"]["user_id"], "root")
        self.assertEqual(response.json()["actor"]["organization_id"], org_id)

    def test_attestation_actor_is_server_derived(self):
        self.bootstrap()

        response = self.client.post(
            "/attestations",
            headers=self.auth(),
            json={
                "repository": "acme/app",
                "commit_sha": "abc123",
                "model_id": "local-model",
                "file_hashes": {"a.py": "deadbeef"},
                "actor": {"user_id": "someone-else"},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["actor"]["user_id"], "root")

    # --- Model setting (UI-driven model change) -------------------------------

    def test_org_admin_sets_model_and_members_receive_it(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "owner-1", "org_owner", "owner-token-for-tests-1")
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        # Org admin sets the org's model from the approved catalog.
        set_resp = self.client.put(
            "/settings/model",
            headers=self.auth("owner-token-for-tests-1"),
            json={"model": "openai/gpt-4o-mini", "organization_id": org_id},
        )
        self.assertEqual(set_resp.status_code, 200)
        self.assertEqual(set_resp.json()["model"], "openai/gpt-4o-mini")

        # A developer sees it via /me, so their CLI applies it on startup.
        me = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-1"), params={"organization_id": org_id}
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["model_name"], "openai/gpt-4o-mini")
        self.assertEqual(me.json()["model_provider"], "openai_compatible")
        self.assertEqual(me.json()["model_base_url"], "https://openrouter.ai/api/v1")
        self.assertTrue(me.json()["model_requires_api_key"])

        # And via the dedicated read endpoint.
        got = self.client.get(
            "/settings/model",
            headers=self.auth("dev-token-for-tests-1"),
            params={"organization_id": org_id},
        )
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["model"], "openai/gpt-4o-mini")
        self.assertEqual(got.json()["catalog_entry"]["provider"], "openai_compatible")

    def test_non_admin_cannot_change_model(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        resp = self.client.put(
            "/settings/model",
            headers=self.auth("dev-token-for-tests-1"),
            json={"model": "openai/gpt-4o-mini", "organization_id": org_id},
        )
        self.assertEqual(resp.status_code, 403)

    def test_model_setting_rejects_empty_model(self):
        org_id = self.bootstrap()
        resp = self.client.put(
            "/settings/model",
            headers=self.auth(),
            json={"model": "", "organization_id": org_id},
        )
        self.assertEqual(resp.status_code, 422)

    def test_model_setting_rejects_unapproved_model(self):
        org_id = self.bootstrap()
        resp = self.client.put(
            "/settings/model",
            headers=self.auth(),
            json={"model": "unknown/vendor-model", "organization_id": org_id},
        )
        self.assertEqual(resp.status_code, 400)

    def test_root_can_manage_model_catalog_and_developer_cannot(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        denied = self.client.put(
            "/settings/model/catalog",
            headers=self.auth("dev-token-for-tests-1"),
            json={
                "id": "company/code-model",
                "label": "Company Code Model",
                "provider": "openai_compatible",
                "base_url": "https://models.example.com/v1",
                "requires_api_key": True,
                "enabled": True,
                "free": False,
            },
        )
        self.assertEqual(denied.status_code, 403)

        saved = self.client.put(
            "/settings/model/catalog",
            headers=self.auth(),
            json={
                "id": "company/code-model",
                "label": "Company Code Model",
                "provider": "openai_compatible",
                "base_url": "https://models.example.com/v1",
                "requires_api_key": True,
                "enabled": True,
                "free": False,
            },
        )
        self.assertEqual(saved.status_code, 200)

        set_resp = self.client.put(
            "/settings/model",
            headers=self.auth(),
            json={"model": "company/code-model", "organization_id": org_id},
        )
        self.assertEqual(set_resp.status_code, 200)
        me = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-1"), params={"organization_id": org_id}
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["model_base_url"], "https://models.example.com/v1")

    # --- Production governance ------------------------------------------------

    def test_org_admin_sets_network_policy_and_developer_receives_it(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "owner-1", "org_owner", "owner-token-for-tests-1")
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        response = self.client.put(
            "/network/policy",
            headers=self.auth("owner-token-for-tests-1"),
            json={
                "organization_id": org_id,
                "allow_network": True,
                "allowed_domains": ["github.com", "api.github.com"],
                "blocked_domains": ["paste.example"],
                "require_proxy": True,
                "proxy_url": "http://proxy.internal:8080",
            },
        )
        self.assertEqual(response.status_code, 200)

        me = self.client.get(
            "/me", headers=self.auth("dev-token-for-tests-1"), params={"organization_id": org_id}
        )
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["network_policy"]["allow_network"])
        self.assertEqual(me.json()["network_policy"]["allowed_domains"], ["api.github.com", "github.com"])

    def test_developer_cannot_update_network_policy(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        response = self.client.put(
            "/network/policy",
            headers=self.auth("dev-token-for-tests-1"),
            json={"organization_id": org_id, "allow_network": True},
        )

        self.assertEqual(response.status_code, 403)

    def test_integration_registry_is_admin_managed_and_auditor_visible(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "owner-1", "org_owner", "owner-token-for-tests-1")
        self.add_member(org_id, "auditor-1", "auditor", "auditor-token-for-tests-1")

        saved = self.client.put(
            "/integrations",
            headers=self.auth("owner-token-for-tests-1"),
            json={
                "provider": "github",
                "name": "Production GitHub",
                "organization_id": org_id,
                "enabled": True,
                "config": {"installation_id": "redacted"},
            },
        )
        self.assertEqual(saved.status_code, 200)

        visible = self.client.get(
            "/integrations",
            headers=self.auth("auditor-token-for-tests-1"),
            params={"organization_id": org_id},
        )
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json()["integrations"][0]["provider"], "github")

    def test_project_profiles_are_admin_managed_and_developer_visible(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "owner-1", "org_owner", "owner-token-for-tests-1")
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")

        denied = self.client.put(
            "/project-profiles",
            headers=self.auth("dev-token-for-tests-1"),
            json={
                "organization_id": org_id,
                "repository": "acme/app",
                "verification_commands": ["uv run python -m unittest discover -s tests"],
            },
        )
        self.assertEqual(denied.status_code, 403)

        saved = self.client.put(
            "/project-profiles",
            headers=self.auth("owner-token-for-tests-1"),
            json={
                "organization_id": org_id,
                "repository": "acme/app",
                "default_branch": "main",
                "verification_commands": [
                    "uv run python -m unittest discover -s tests",
                    "uv run python -m unittest discover -s tests",
                ],
                "allowed_domains": ["GitHub.com", "api.github.com"],
                "issue_tracker_url": "https://github.com/acme/app/issues",
                "secret_env_vars": ["NYANSA_API_KEY"],
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            saved.json()["verification_commands"],
            ["uv run python -m unittest discover -s tests"],
        )
        self.assertEqual(saved.json()["allowed_domains"], ["api.github.com", "github.com"])

        visible = self.client.get(
            "/project-profiles",
            headers=self.auth("dev-token-for-tests-1"),
            params={"organization_id": org_id, "repository": "acme/app"},
        )
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json()["project_profiles"][0]["repository"], "acme/app")

    def test_approval_request_can_be_decided_by_org_admin(self):
        org_id = self.bootstrap()
        self.add_member(org_id, "dev-1", "developer", "dev-token-for-tests-1")
        self.add_member(org_id, "owner-1", "org_owner", "owner-token-for-tests-1")

        created = self.client.post(
            "/approvals",
            headers=self.auth("dev-token-for-tests-1"),
            json={
                "organization_id": org_id,
                "request_type": "network",
                "target": "github.com",
                "reason": "Need repository metadata for a production fix",
            },
        )
        self.assertEqual(created.status_code, 200)

        decided = self.client.post(
            f"/approvals/{created.json()['id']}/decision",
            headers=self.auth("owner-token-for-tests-1"),
            json={"status": "approved"},
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["status"], "approved")

    def test_production_readiness_scores_remaining_gaps(self):
        org_id = self.bootstrap()

        response = self.client.get("/production/readiness", headers=self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertLess(response.json()["score"], 10)
        self.assertIn("network policy per org", response.json()["gaps"])

    # --- Concurrency (audit chain integrity) ----------------------------------

    def test_concurrent_audit_ingest_keeps_chain_valid(self):
        self.bootstrap()
        root_user = self.store.authenticate(self.root_token)
        total = 40

        def ingest(index):
            return self.store.ingest_audit_event(
                root_user,
                IngestAuditEventRequest(
                    event_type="tool.start", details={"i": index}
                ),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(ingest, range(total)))

        verification = self.store.verify_audit_events_for_system()
        self.assertTrue(verification.valid)
        self.assertEqual(verification.event_count, total)

    # --- Token lookup migration -----------------------------------------------

    def test_legacy_user_without_lookup_authenticates_and_migrates(self):
        self.bootstrap()

        # Simulate a pre-upgrade record: drop the lookup index.
        state = self.store._load()
        state.users["root"].token_lookup = None
        self.store._save(state)

        response = self.client.get("/me", headers=self.auth())
        self.assertEqual(response.status_code, 200)

        migrated = self.store._load()
        self.assertIsNotNone(migrated.users["root"].token_lookup)

    # --- Browser session CSRF -------------------------------------------------

    def test_cookie_authenticated_write_requires_same_origin(self):
        self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Grid Operator",
                "root_user_id": "root",
                "root_password": "correct-horse-password",
            },
        )
        login = self.client.post(
            "/auth/login",
            json={"user_id": "root", "password": "correct-horse-password"},
        )
        self.assertEqual(login.status_code, 200)
        session_token = login.json()["token"]
        self.client.cookies.set("nyansa_session", session_token)

        rejected = self.client.post("/cli/token", json={})
        self.assertEqual(rejected.status_code, 403)

        cross_site = self.client.post(
            "/cli/token",
            headers={"Origin": "https://evil.example"},
            json={},
        )
        self.assertEqual(cross_site.status_code, 403)

        same_origin = self.client.post(
            "/cli/token",
            headers={"Origin": "http://testserver"},
            json={},
        )
        self.assertEqual(same_origin.status_code, 200)

    def test_authorization_header_is_not_subject_to_cookie_csrf_guard(self):
        self.bootstrap()
        self.client.cookies.set("nyansa_session", "ambient-cookie-token")

        response = self.client.post(
            "/cli/token",
            headers=self.auth(),
            json={},
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
