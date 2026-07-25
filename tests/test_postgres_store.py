import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "set TEST_DATABASE_URL to run the Postgres-backed integration test",
)
class PostgresStoreIntegrationTests(unittest.TestCase):
    """Exercises the production store against a real Postgres.

    The rest of the suite runs against the SQLite base class; this proves the
    Postgres path (bootstrap → auth → audit chain → model setting) end to end.
    """

    def setUp(self):
        from control_plane.postgres_storage import PostgresControlPlaneStore

        self.url = os.environ["TEST_DATABASE_URL"]
        self.keydir = tempfile.TemporaryDirectory()
        os.environ["NYANSA_TOKEN_PEPPER"] = "integration-pepper"
        os.environ["NYANSA_ATTESTATION_KEY_SEED"] = "integration-seed"

        self.store = PostgresControlPlaneStore(
            self.url, key_dir=Path(self.keydir.name) / "state"
        )
        # Force schema creation, then reset to a clean slate for a repeatable run.
        self.store.is_bootstrapped()
        import psycopg

        with psycopg.connect(self.url) as conn:
            conn.execute("DELETE FROM control_plane_state")
            conn.commit()

    def tearDown(self):
        self.store.close()
        self.keydir.cleanup()
        os.environ.pop("NYANSA_TOKEN_PEPPER", None)
        os.environ.pop("NYANSA_ATTESTATION_KEY_SEED", None)

    def test_bootstrap_auth_audit_and_model_roundtrip(self):
        from control_plane.models import (
            BootstrapRequest,
            IngestAuditEventRequest,
            SetModelRequest,
        )

        self.assertFalse(self.store.is_bootstrapped())
        self.store.bootstrap(
            BootstrapRequest(
                organization_name="Org",
                root_user_id="root",
                root_token="root-token-integration-1",
            )
        )
        self.assertTrue(self.store.is_bootstrapped())

        user = self.store.authenticate("root-token-integration-1")
        self.assertEqual(user.id, "root")
        with self.assertRaises(Exception):
            self.store.authenticate("wrong-token")

        # Audit chain persists and verifies through Postgres.
        for i in range(3):
            self.store.ingest_audit_event(
                user, IngestAuditEventRequest(event_type="tool.start", details={"i": i})
            )
        verification = self.store.verify_audit_events_for_system()
        self.assertTrue(verification.valid)
        self.assertEqual(verification.event_count, 3)

        # Model setting round-trips.
        self.store.set_model_setting(
            user, SetModelRequest(model="openai/gpt-4o-mini")
        )
        actor = self.store.actor_for_user(user)
        self.assertEqual(actor.model_name, "openai/gpt-4o-mini")

    def test_state_survives_a_new_store_instance(self):
        from control_plane.models import BootstrapRequest
        from control_plane.postgres_storage import PostgresControlPlaneStore

        self.store.bootstrap(
            BootstrapRequest(
                organization_name="Org",
                root_user_id="root",
                root_token="root-token-integration-2",
            )
        )
        # A fresh store pointed at the same DB sees the persisted bootstrap.
        other = PostgresControlPlaneStore(
            self.url, key_dir=Path(self.keydir.name) / "state"
        )
        try:
            self.assertTrue(other.is_bootstrapped())
            self.assertEqual(other.authenticate("root-token-integration-2").id, "root")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
