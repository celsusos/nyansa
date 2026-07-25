import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from control_plane.api import create_app
from control_plane.rate_limit import SlidingWindowRateLimiter
from control_plane.settings import ControlPlaneSettings
from control_plane.storage import ControlPlaneStore


def _settings(**overrides) -> ControlPlaneSettings:
    base = dict(database_url="test-injected")
    base.update(overrides)
    return ControlPlaneSettings(**base)


class RateLimitUnitTests(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60)
        now = 1000.0
        self.assertEqual([limiter.check("a", now)[0] for _ in range(3)], [True, True, True])
        allowed, retry_after = limiter.check("a", now)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_window_slides(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.check("a", 0.0)[0])
        self.assertTrue(limiter.check("a", 0.0)[0])
        self.assertFalse(limiter.check("a", 0.0)[0])
        # After the window passes, the bucket clears.
        self.assertTrue(limiter.check("a", 61.0)[0])

    def test_keys_are_independent(self):
        limiter = SlidingWindowRateLimiter(limit=1, window_seconds=60)
        self.assertTrue(limiter.check("a", 0.0)[0])
        self.assertTrue(limiter.check("b", 0.0)[0])
        self.assertFalse(limiter.check("a", 0.0)[0])

    def test_zero_limit_disables(self):
        limiter = SlidingWindowRateLimiter(limit=0)
        self.assertFalse(limiter.enabled)
        for _ in range(100):
            self.assertTrue(limiter.check("a")[0])


class RateLimitApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ControlPlaneStore(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_returns_429_with_retry_after(self):
        client = TestClient(
            create_app(self.store, _settings(rate_limit_per_minute=3)),
            raise_server_exceptions=True,
        )
        statuses = [client.get("/setup/status").status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 200])

        blocked = client.get("/setup/status")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_health_and_ready_are_exempt(self):
        client = TestClient(create_app(self.store, _settings(rate_limit_per_minute=1)))
        # Many health probes never trip the limiter.
        for _ in range(10):
            self.assertEqual(client.get("/health").status_code, 200)

    def test_hsts_emitted_only_behind_https(self):
        client = TestClient(create_app(self.store, _settings(rate_limit_per_minute=0)))
        plain = client.get("/health")
        self.assertNotIn("Strict-Transport-Security", plain.headers)

        behind_tls = client.get("/health", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Strict-Transport-Security", behind_tls.headers)


class TokenExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ControlPlaneStore(Path(self.tmp.name) / "state.json")
        self.client = TestClient(create_app(self.store, _settings()))
        self.root_token = "root-token-for-ttl-tests"
        self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Org",
                "root_user_id": "root",
                "root_token": self.root_token,
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.root_token}"}

    def _org_id(self):
        return self.client.get("/organizations", headers=self._auth()).json()["organizations"][0]["id"]

    def test_member_tokens_get_default_expiry(self):
        org_id = self._org_id()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self._auth(),
            json={"user_id": "dev-1", "role": "developer", "token": "dev-token-for-tests-1"},
        )
        # The stored member token now has a non-null expiry (default 720h).
        user = self.store._load().users["dev-1"]
        self.assertIsNotNone(user.token_expires_at)

    def test_root_token_stays_non_expiring(self):
        # Root is exempt so the operator is never locked out.
        root = self.store._load().users["root"]
        self.assertIsNone(root.token_expires_at)

    def test_explicit_expiry_is_respected(self):
        org_id = self._org_id()
        self.client.put(
            f"/organizations/{org_id}/members",
            headers=self._auth(),
            json={
                "user_id": "dev-2",
                "role": "developer",
                "token": "dev-token-for-tests-2",
                "token_expires_at": "2999-01-01T00:00:00+00:00",
            },
        )
        user = self.store._load().users["dev-2"]
        self.assertEqual(user.token_expires_at, "2999-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
