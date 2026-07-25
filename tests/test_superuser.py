import tempfile
import unittest
from pathlib import Path

from control_plane.models import BootstrapRequest, UpsertMemberRequest
from control_plane.storage import ControlPlaneError, ControlPlaneStore


class CreateSuperuserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ControlPlaneStore(Path(self.tmp.name) / "state.json")
        self.store.bootstrap(
            BootstrapRequest(
                organization_name="Org",
                root_user_id="root",
                root_token="root-token-superuser-tests",
            )
        )
        self.root = self.store.authenticate("root-token-superuser-tests")
        self.org_id = next(iter(self.store._load().organizations))

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_a_working_platform_superuser(self):
        self.store.create_superuser(
            self.root,
            user_id="admin-2",
            token="admin-2-token-1234567890",
            platform_role="platform_owner",
            display_name="Second Admin",
            organization_id=self.org_id,
        )

        user = self.store.authenticate("admin-2-token-1234567890")
        self.assertEqual(user.id, "admin-2")
        self.assertEqual(user.platform_role, "platform_owner")

        # Superuser power is real: a platform-only action doesn't raise.
        self.store.list_role_policies(user)
        actor = self.store.actor_for_user(user)
        self.assertEqual(actor.platform_role, "platform_owner")

    def test_non_superuser_cannot_create_superuser(self):
        self.store.upsert_member(
            self.root,
            self.org_id,
            UpsertMemberRequest(user_id="dev-1", role="developer", token="dev-token-123456"),
        )
        developer = self.store.authenticate("dev-token-123456")

        with self.assertRaises(ControlPlaneError) as ctx:
            self.store.create_superuser(
                developer, user_id="sneaky", token="sneaky-token-123456"
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_rejects_non_platform_role(self):
        with self.assertRaises(ControlPlaneError) as ctx:
            self.store.create_superuser(
                self.root,
                user_id="x",
                token="x-token-1234567890",
                platform_role="developer",  # not a platform role
            )
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
