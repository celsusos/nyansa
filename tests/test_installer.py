import tempfile
import unittest
from pathlib import Path

from scripts.installer import (
    Plan,
    _read_env,
    _resolve_persistent,
    _write_config_toml,
    _write_env,
    _write_join_config,
)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_env_upserts_and_preserves_other_lines(self):
        env_path = self.cwd / ".env"
        env_path.write_text("# comment\nFOO=bar\nNYANSA_API_KEY=old\n", encoding="utf-8")

        _write_env(env_path, {"NYANSA_API_KEY": "new", "BASE_URL": "http://x/v1"})

        values = _read_env(env_path)
        self.assertEqual(values["FOO"], "bar")            # untouched
        self.assertEqual(values["NYANSA_API_KEY"], "new")  # updated in place
        self.assertEqual(values["BASE_URL"], "http://x/v1")  # appended
        self.assertIn("# comment", env_path.read_text(encoding="utf-8"))

    def test_resolve_persistent_reuses_secrets_and_generates_missing(self):
        env_path = self.cwd / ".env"
        env_path.write_text(
            "NYANSA_TOKEN_PEPPER=keep-this-pepper\n"
            "NYANSA_CONTROL_TOKEN=keep-this-token\n"
            "NYANSA_DATABASE_URL=postgresql://u:p@db:5432/x\n",
            encoding="utf-8",
        )
        plan = Plan(cwd=self.cwd)

        _resolve_persistent(plan)

        # Existing secrets are preserved so re-runs never invalidate stored hashes.
        self.assertEqual(plan.secrets["NYANSA_TOKEN_PEPPER"], "keep-this-pepper")
        self.assertEqual(plan.secrets["NYANSA_CONTROL_TOKEN"], "keep-this-token")
        # Missing ones are generated fresh and non-trivial.
        self.assertTrue(plan.secrets["NYANSA_BOOTSTRAP_SECRET"])
        self.assertTrue(plan.secrets["NYANSA_ATTESTATION_KEY_SEED"])
        self.assertNotEqual(
            plan.secrets["NYANSA_BOOTSTRAP_SECRET"],
            plan.secrets["NYANSA_ATTESTATION_KEY_SEED"],
        )
        # The persisted database URL is adopted on re-run.
        self.assertEqual(plan.database_url, "postgresql://u:p@db:5432/x")

    def test_guided_model_choice_wins_over_stale_env_model_endpoint(self):
        env_path = self.cwd / ".env"
        env_path.write_text(
            "BASE_URL=http://localhost:11434/v1\n"
            "NYANSA_MODEL=qwen2.5-coder:7b\n",
            encoding="utf-8",
        )
        plan = Plan(
            cwd=self.cwd,
            base_url="https://openrouter.ai/api/v1",
            model_name="openai/gpt-4o-mini",
            reuse_existing_model_config=False,
        )

        _resolve_persistent(plan)

        self.assertEqual(plan.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(plan.model_name, "openai/gpt-4o-mini")

    def test_resolve_persistent_generates_all_when_no_env(self):
        plan = Plan(cwd=self.cwd)

        _resolve_persistent(plan)

        core = (
            "NYANSA_CONTROL_TOKEN",
            "NYANSA_BOOTSTRAP_SECRET",
            "NYANSA_TOKEN_PEPPER",
            "NYANSA_ATTESTATION_KEY_SEED",
        )
        for key in core:
            self.assertTrue(plan.secrets[key])
        # Every generated secret is distinct (no accidental reuse across keys).
        values = list(plan.secrets.values())
        self.assertEqual(len(values), len(set(values)))

    def test_write_config_toml_local_standalone(self):
        plan = Plan(cwd=self.cwd, org_id="org-123", model_name="qwen2.5-coder:7b")
        plan.govern_cli = False
        plan.production = False

        _write_config_toml(plan)

        text = (self.cwd / ".nyansa" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('provider = "openai_compatible"', text)
        self.assertIn('name = "qwen2.5-coder:7b"', text)
        self.assertIn('organization_id = "org-123"', text)
        self.assertIn("enabled = false", text)      # CLI runs standalone
        self.assertIn("fail_closed = false", text)   # local fails open

    def test_write_config_toml_governed_production(self):
        plan = Plan(cwd=self.cwd, org_id="org-9")
        plan.govern_cli = True
        plan.production = True
        plan.provider = "claude_code"
        plan.base_url = None

        _write_config_toml(plan)

        text = (self.cwd / ".nyansa" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('provider = "claude_code"', text)
        # The model section omits base_url for claude_code; only [control_plane] has one.
        model_section = text.split("[actor]", 1)[0]
        self.assertNotIn("base_url =", model_section)
        self.assertIn("enabled = true", text)  # governed
        self.assertIn("fail_closed = true", text)  # production fails closed

    def test_write_config_toml_backs_up_existing(self):
        config_dir = self.cwd / ".nyansa"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("old = true\n", encoding="utf-8")
        plan = Plan(cwd=self.cwd, org_id="org-1")

        _write_config_toml(plan)

        self.assertTrue((config_dir / "config.toml.bak").exists())
        self.assertIn("old = true", (config_dir / "config.toml.bak").read_text())


    def test_join_config_uses_actor_role_and_enables_control_plane(self):
        actor = {
            "user_id": "maya",
            "organization_id": "org-42",
            "role": "developer",
            "model_name": "poolside/laguna-s-2.1:free",
        }
        _write_join_config(self.cwd, "https://cp.example.com", actor, "poolside/laguna-s-2.1:free")

        text = (self.cwd / ".nyansa" / "config.toml").read_text(encoding="utf-8")
        # Actor comes from the login, not hardcoded root.
        self.assertIn('user_id = "maya"', text)
        self.assertIn('organization_id = "org-42"', text)
        self.assertIn('role = "developer"', text)
        # The CLI is governed and points at the joined control plane.
        self.assertIn("enabled = true", text)
        self.assertIn('base_url = "https://cp.example.com"', text)
        self.assertIn("fail_closed = true", text)
        self.assertIn('name = "poolside/laguna-s-2.1:free"', text)


if __name__ == "__main__":
    unittest.main()
