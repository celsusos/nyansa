import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from agent.provenance import hash_text
from config.config import Config
from control_plane.client import send_attestation
from control_plane.storage import ControlPlaneStore


class EmissionGateTests(unittest.TestCase):
    def make_config(self, **cp) -> Config:
        config = Config()
        for key, value in cp.items():
            setattr(config.control_plane, key, value)
        return config

    def test_disabled_control_plane_is_a_noop(self):
        config = self.make_config(enabled=False)
        result = asyncio.run(send_attestation(config, {"repository": "x"}))
        self.assertIsNone(result)

    def test_send_attestations_flag_off_is_a_noop(self):
        config = self.make_config(enabled=True, send_attestations=False)
        result = asyncio.run(send_attestation(config, {"repository": "x"}))
        self.assertIsNone(result)


class EmissionEndToEndTests(unittest.TestCase):
    """The client posts to a real in-process store, and the chain verifies."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "state.json"
        os.environ["NYANSA_CONTROL_PLANE_STATE"] = str(self.state_path)
        self.addCleanup(self._cleanup)

        self.store = ControlPlaneStore(self.state_path)
        self.store.bootstrap(_bootstrap_request())

    def _cleanup(self):
        os.environ.pop("NYANSA_CONTROL_PLANE_STATE", None)
        self._tmp.cleanup()

    def _root(self):
        return self.store.authenticate("root-token-1234567890")

    def test_recorded_attestation_captures_provenance_and_verifies(self):
        from control_plane.models import RecordAttestationRequest

        payload = RecordAttestationRequest(
            repository="acme/app",
            commit_sha="c0ffee",
            branch="main",
            model_id="local-model",
            prompt_sha256=hash_text("fix the bug"),
            file_hashes={"app.py": hash_text("new content")},
            policy_version="2026-07",
            skill_id="pci-data-handling",
            skill_version="3",
        )
        recorded = self.store.record_attestation(self._root(), payload)

        self.assertEqual(recorded["repository"], "acme/app")
        self.assertEqual(recorded["file_hashes"]["app.py"], hash_text("new content"))
        self.assertEqual(recorded["skill_id"], "pci-data-handling")
        self.assertTrue(recorded["signature"])

        verification = self.store.verify_attestations_for_system()
        self.assertTrue(verification.valid)
        self.assertEqual(verification.attestation_count, 1)


def _bootstrap_request():
    from control_plane.models import BootstrapRequest

    return BootstrapRequest(
        organization_name="Acme",
        root_user_id="root",
        root_token="root-token-1234567890",
    )


if __name__ == "__main__":
    unittest.main()
