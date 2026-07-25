import json
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from control_plane.api import create_app
from control_plane.signing import AttestationSigner, verify_signature
from control_plane.storage import ControlPlaneStore

ROOT_TOKEN = "root-token-1234567890"


class AttestationApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "state.json"
        os.environ["NYANSA_CONTROL_PLANE_STATE"] = str(self.state_path)
        self.addCleanup(self._cleanup)

        self.store = ControlPlaneStore(self.state_path)
        self.client = TestClient(create_app(self.store))
        self.client.post(
            "/bootstrap",
            json={
                "organization_name": "Acme",
                "root_user_id": "root",
                "root_token": ROOT_TOKEN,
            },
        )
        self.headers = {"authorization": f"Bearer {ROOT_TOKEN}"}

    def _cleanup(self):
        os.environ.pop("NYANSA_CONTROL_PLANE_STATE", None)
        self._tmp.cleanup()

    def record(self, **overrides):
        body = {
            "repository": "acme/app",
            "commit_sha": "abc123",
            "model_id": "local-model",
            "file_hashes": {"a.py": "deadbeef"},
        }
        body.update(overrides)
        return self.client.post("/attestations", headers=self.headers, json=body)

    def test_record_is_signed_and_chained(self):
        first = self.record().json()
        second = self.record(commit_sha="def456").json()

        self.assertTrue(first["signature"])
        self.assertEqual(first["signing_key_id"], second["signing_key_id"])
        self.assertIsNone(first["previous_hash"])
        self.assertEqual(second["previous_hash"], first["entry_hash"])

    def test_carries_skill_and_policy_provenance(self):
        record = self.record(
            skill_id="pci-data-handling",
            skill_version="3",
            policy_version="2026-07",
            prompt_sha256="a" * 64,
        ).json()

        self.assertEqual(record["skill_id"], "pci-data-handling")
        self.assertEqual(record["skill_version"], "3")
        self.assertEqual(record["policy_version"], "2026-07")

    def test_verify_passes_for_untampered_chain(self):
        self.record()
        self.record(commit_sha="def456")

        result = self.client.get("/attestations/verify", headers=self.headers).json()
        self.assertTrue(result["valid"])
        self.assertEqual(result["attestation_count"], 2)

    def test_query_by_repository_and_commit(self):
        self.record(repository="acme/app", commit_sha="abc123")
        self.record(repository="acme/other", commit_sha="zzz999")

        by_commit = self.client.get(
            "/attestations?commit_sha=abc123", headers=self.headers
        ).json()["attestations"]
        by_repo = self.client.get(
            "/attestations?repository=acme/other", headers=self.headers
        ).json()["attestations"]

        self.assertEqual(len(by_commit), 1)
        self.assertEqual(by_commit[0]["commit_sha"], "abc123")
        self.assertEqual(len(by_repo), 1)
        self.assertEqual(by_repo[0]["repository"], "acme/other")

    def test_tampering_with_content_fails_verification(self):
        self.record()

        state = json.loads(self.state_path.read_text())
        state["attestations"][0]["file_hashes"] = {"a.py": "0000"}
        self.state_path.write_text(json.dumps(state))

        result = self.client.get("/attestations/verify", headers=self.headers).json()
        self.assertFalse(result["valid"])
        self.assertEqual(result["broken_index"], 0)

    def test_recomputed_hash_still_fails_without_the_private_key(self):
        """A DB editor who fixes entry_hash still can't produce a valid signature."""
        self.record()

        from control_plane.storage import attestation_entry_hash
        from control_plane.models import AttestationRecord

        state = json.loads(self.state_path.read_text())
        state["attestations"][0]["file_hashes"] = {"a.py": "forged"}
        forged = AttestationRecord(**state["attestations"][0])
        state["attestations"][0]["entry_hash"] = attestation_entry_hash(forged)
        self.state_path.write_text(json.dumps(state))

        result = self.client.get("/attestations/verify", headers=self.headers).json()
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "invalid or missing signature")

    def test_public_key_is_unauthenticated_and_verifies_a_signature(self):
        record = self.record().json()
        pk = self.client.get("/attestations/public-key").json()

        self.assertEqual(pk["algorithm"], "ed25519")
        self.assertTrue(
            verify_signature(pk["public_key_pem"], record["entry_hash"], record["signature"])
        )

    def test_reader_role_required(self):
        self.record()
        self.client.put(
            "/organizations/"
            + self._org_id()
            + "/members",
            headers=self.headers,
            json={"user_id": "dev", "role": "developer", "token": "dev-token-1234567890"},
        )
        dev_headers = {"authorization": "Bearer dev-token-1234567890"}

        resp = self.client.get(
            f"/attestations?organization_id={self._org_id()}", headers=dev_headers
        )
        self.assertEqual(resp.status_code, 403)

    def _org_id(self) -> str:
        orgs = self.client.get("/organizations", headers=self.headers).json()
        return orgs["organizations"][0]["id"]


class SignerTests(unittest.TestCase):
    def test_key_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as d:
            key_path = Path(d) / "key.pem"
            first = AttestationSigner(key_path)
            self.assertTrue(key_path.exists())
            self.assertEqual(oct(key_path.stat().st_mode)[-3:], "600")

            second = AttestationSigner(key_path)
            self.assertEqual(first.key_id(), second.key_id())

    def test_signature_roundtrips_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            signer = AttestationSigner(Path(d) / "key.pem")
            sig = signer.sign("hello")

            self.assertTrue(signer.verify("hello", sig))
            self.assertFalse(signer.verify("hell0", sig))

    def test_wrong_key_does_not_verify(self):
        with tempfile.TemporaryDirectory() as d:
            a = AttestationSigner(Path(d) / "a.pem")
            b = AttestationSigner(Path(d) / "b.pem")
            sig = a.sign("payload")

            self.assertFalse(verify_signature(b.public_key_pem(), "payload", sig))


if __name__ == "__main__":
    unittest.main()
