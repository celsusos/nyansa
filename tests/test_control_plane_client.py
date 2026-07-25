import unittest
from unittest.mock import AsyncMock, patch

from config.config import Config
from control_plane.client import sync_actor_from_control_plane


class ControlPlaneClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_actor_sync_applies_governed_model_metadata(self):
        config = Config()
        config.control_plane.enabled = True
        config.control_plane.sync_actor = True
        config.actor.organization_id = "org-1"
        config.model.name = "local-model"
        config.model.provider = "openai_compatible"
        config.model.base_url = "http://localhost:11434/v1"
        config.model.requires_api_key = False

        with patch(
            "control_plane.client.control_plane_get",
            new=AsyncMock(
                return_value={
                    "user_id": "dev-1",
                    "organization_id": "org-1",
                    "role": "developer",
                    "allowed_tools": ["read_file"],
                    "usage_policy": None,
                    "model_name": "poolside/laguna-s-2.1:free",
                    "model_provider": "openai_compatible",
                    "model_base_url": "https://openrouter.ai/api/v1",
                    "model_requires_api_key": True,
                    "network_policy": {
                        "allow_network": True,
                        "allowed_domains": ["github.com"],
                        "blocked_domains": ["paste.example"],
                        "require_proxy": True,
                        "proxy_url": "http://proxy.internal:8080",
                    },
                }
            ),
        ):
            await sync_actor_from_control_plane(config)

        self.assertEqual(config.actor.user_id, "dev-1")
        self.assertEqual(config.actor.role, "developer")
        self.assertEqual(config.role_policy.allowed_tools, ["read_file"])
        self.assertEqual(config.model.name, "poolside/laguna-s-2.1:free")
        self.assertEqual(config.model.provider, "openai_compatible")
        self.assertEqual(config.model.base_url, "https://openrouter.ai/api/v1")
        self.assertTrue(config.model.requires_api_key)
        self.assertTrue(config.sandbox.allow_network)
        self.assertEqual(config.sandbox.allowed_domains, ["github.com"])
        self.assertEqual(config.sandbox.blocked_domains, ["paste.example"])
        self.assertTrue(config.sandbox.require_proxy)
        self.assertEqual(config.sandbox.proxy_url, "http://proxy.internal:8080")


if __name__ == "__main__":
    unittest.main()
