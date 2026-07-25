import os
from pathlib import Path
import tempfile
import unittest

from agent.provenance import hash_text
from control_plane.models import GateCheckRequest, RecordAttestationRequest
from control_plane.storage import ControlPlaneStore


def _bootstrap(store: ControlPlaneStore):
    from control_plane.models import BootstrapRequest

    store.bootstrap(
        BootstrapRequest(
            organization_name="Acme",
            root_user_id="root",
            root_token="root-token-1234567890",
        )
    )


class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "state.json"
        os.environ["NYANSA_CONTROL_PLANE_STATE"] = str(self.state_path)
        self.addCleanup(self._cleanup)

        self.store = ControlPlaneStore(self.state_path)
        _bootstrap(self.store)
        self.user = self.store.authenticate("root-token-1234567890")

    def _cleanup(self):
        os.environ.pop("NYANSA_CONTROL_PLANE_STATE", None)
        self._tmp.cleanup()

    def attest(self, **overrides):
        body = {
            "repository": "acme/app",
            "commit_sha": "c0ffee",
            "model_id": "local-model",
            "file_hashes": {"app.py": hash_text("good")},
        }
        body.update(overrides)
        return self.store.record_attestation(self.user, RecordAttestationRequest(**body))

    def gate(self, **kwargs):
        defaults = {"repository": "acme/app", "commit_sha": "c0ffee"}
        defaults.update(kwargs)
        return self.store.evaluate_gate(self.user, GateCheckRequest(**defaults))

    def test_passes_when_changed_files_are_attested(self):
        self.attest()
        result = self.gate(file_hashes={"app.py": hash_text("good")})

        self.assertTrue(result.passed)
        self.assertEqual(result.matched_attestations, 1)
        self.assertEqual(result.uncovered_files, [])

    def test_fails_when_no_attestation_for_commit(self):
        self.attest(commit_sha="other")
        result = self.gate(file_hashes={"app.py": hash_text("good")})

        self.assertFalse(result.passed)
        self.assertTrue(any("no attestation" in r for r in result.reasons))

    def test_fails_on_unattested_file(self):
        self.attest()
        result = self.gate(
            file_hashes={"app.py": hash_text("good"), "secret.py": hash_text("sneaky")}
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.uncovered_files, ["secret.py"])

    def test_fails_on_hash_mismatch(self):
        self.attest()
        result = self.gate(file_hashes={"app.py": hash_text("tampered after attestation")})

        self.assertFalse(result.passed)
        self.assertEqual(result.mismatched_files, ["app.py"])

    def test_fails_when_chain_is_tampered(self):
        self.attest()
        import json

        state = json.loads(self.state_path.read_text())
        state["attestations"][0]["file_hashes"] = {"app.py": "forged"}
        self.state_path.write_text(json.dumps(state))

        result = self.gate(file_hashes={"app.py": "forged"})
        self.assertFalse(result.passed)
        self.assertFalse(result.chain_valid)

    def test_required_policy_version_enforced(self):
        self.attest(policy_version="2026-06")
        result = self.gate(
            file_hashes={"app.py": hash_text("good")},
            require_policy_version="2026-07",
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("policy version" in r for r in result.reasons))

    def test_commit_omitted_matches_any_commit_for_repo(self):
        self.attest(commit_sha="abc")
        result = self.store.evaluate_gate(
            self.user,
            GateCheckRequest(repository="acme/app", file_hashes={"app.py": hash_text("good")}),
        )
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
