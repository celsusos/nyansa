import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.loader import load_config


class ConfigEnvOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_project_model(self, name: str, base_url: str) -> None:
        agent_dir = self.cwd / ".nyansa"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "config.toml").write_text(
            f'[model]\nname = "{name}"\nbase_url = "{base_url}"\n', encoding="utf-8"
        )

    def test_env_overrides_model_name_and_base_url(self):
        with patch.dict(
            "os.environ",
            {"NYANSA_MODEL": "openai/gpt-4o-mini", "BASE_URL": "https://openrouter.ai/api/v1"},
        ):
            config = load_config(cwd=self.cwd)

        self.assertEqual(config.model.name, "openai/gpt-4o-mini")
        self.assertEqual(config.model.base_url, "https://openrouter.ai/api/v1")

    def test_env_wins_over_project_config(self):
        self._write_project_model("local-toml-model", "http://localhost:1234/v1")

        with patch.dict("os.environ", {"NYANSA_MODEL": "anthropic/claude-3.5-sonnet"}):
            config = load_config(cwd=self.cwd)

        # .env-style override beats the config.toml value.
        self.assertEqual(config.model.name, "anthropic/claude-3.5-sonnet")
        # base_url untouched by env falls back to the project config.
        self.assertEqual(config.model.base_url, "http://localhost:1234/v1")

    def test_project_config_used_when_env_absent(self):
        self._write_project_model("qwen2.5-coder:7b", "http://localhost:11434/v1")

        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("NYANSA_MODEL", None)
            os.environ.pop("BASE_URL", None)
            os.environ.pop("NYANSA_BASE_URL", None)
            config = load_config(cwd=self.cwd)

        self.assertEqual(config.model.name, "qwen2.5-coder:7b")
        self.assertEqual(config.model.base_url, "http://localhost:11434/v1")

    def test_blank_env_is_ignored(self):
        self._write_project_model("qwen2.5-coder:7b", "http://localhost:11434/v1")

        with patch.dict("os.environ", {"NYANSA_MODEL": "  ", "BASE_URL": ""}):
            config = load_config(cwd=self.cwd)

        self.assertEqual(config.model.name, "qwen2.5-coder:7b")
        self.assertEqual(config.model.base_url, "http://localhost:11434/v1")

    def test_project_env_loaded_without_sourcing_shell(self):
        (self.cwd / ".env").write_text(
            "NYANSA_MODEL=poolside/laguna-s-2.1:free\n"
            "BASE_URL=https://openrouter.ai/api/v1\n"
            "NYANSA_API_KEY=test-key\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(cwd=self.cwd)
            self.assertEqual(config.model.name, "poolside/laguna-s-2.1:free")
            self.assertEqual(config.model.base_url, "https://openrouter.ai/api/v1")
            self.assertEqual(config.api_key, "test-key")

    def test_local_endpoint_does_not_require_api_key(self):
        agent_dir = self.cwd / ".nyansa"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "config.toml").write_text(
            "[model]\n"
            'name = "qwen2.5-coder:7b"\n'
            'base_url = "http://localhost:11434/v1"\n'
            "requires_api_key = true\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(cwd=self.cwd)

        self.assertFalse(config.model.requires_api_key)
        self.assertEqual(config.validate(), [])

    def test_hosted_model_on_local_endpoint_reports_mismatch(self):
        agent_dir = self.cwd / ".nyansa"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "config.toml").write_text(
            "[model]\n"
            'name = "openai/gpt-4o-mini"\n'
            'base_url = "http://localhost:11434/v1"\n',
            encoding="utf-8",
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(cwd=self.cwd)

        self.assertIn("Model endpoint mismatch", "\n".join(config.validate()))


if __name__ == "__main__":
    unittest.main()
