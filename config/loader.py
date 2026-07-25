import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from config.config import Config
from utils.errors import ConfigError
import logging

logger = logging.getLogger(__name__)
CONFIG_FILE_NAME = "config.toml"

AGENT_MD_FILE = "AGENTS.md"
AGENT_MD_FALLBACK_FILE = "AGENT.md"


def get_config_dir() -> Path:
    return Path(user_config_dir("nyansa"))


def get_data_dir() -> Path:
    return Path(user_data_dir("nyansa"))


def get_system_config_path() -> Path:
    return get_config_dir() / CONFIG_FILE_NAME


def _parse_toml(path: Path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("Invalid TOML in {path}: {e}", config_file=str(path)) from e
    except (OSError, IOError) as e:
        raise ConfigError(
            "Failed to read config file {path}: {e}", config_file=str(path)
        ) from e


def _get_project_config(cwd: Path) -> Path | None:
    current = cwd.resolve()
    agent_dir = current / ".nyansa"

    if agent_dir.is_dir():
        config_file = agent_dir / CONFIG_FILE_NAME
        if config_file.is_file():
            return config_file

    return None


def _find_agent_md_in_dir(directory: Path) -> Path | None:
    for name in (AGENT_MD_FILE, AGENT_MD_FALLBACK_FILE):
        candidate = directory / name
        if candidate.is_file():
            return candidate

    return None


def _get_agent_md_files(cwd: Path) -> str | None:
    current = cwd.resolve()

    if not current.is_dir():
        return None

    found: list[Path] = []
    for directory in [current, *current.parents]:
        agent_md_file = _find_agent_md_in_dir(directory)
        if agent_md_file:
            found.append(agent_md_file)

        # Stop at the repository root; anything above it is out of scope.
        if (directory / ".git").exists():
            break

    if not found:
        return None

    # Root-first so that more-deeply-nested files appear last and take
    # precedence in the case of conflicting instructions.
    sections: list[str] = []
    for agent_md_file in reversed(found):
        try:
            content = agent_md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning(f"Skipping unreadable agent instructions: {agent_md_file}")
            continue

        if content.strip():
            sections.append(f"# From {agent_md_file}\n\n{content.strip()}")

    if not sections:
        return None

    return "\n\n".join(sections)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value

    return result


def _load_project_env(cwd: Path) -> None:
    """Load simple KEY=VALUE lines from project .env without overriding the shell."""
    env_path = cwd / ".env"
    if not env_path.is_file():
        return

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        logger.warning("Skipping unreadable .env: %s", env_path)
        return

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(cwd: Path | None) -> Config:
    cwd = cwd or Path.cwd()
    _load_project_env(cwd)

    system_path = get_system_config_path()

    config_dict: dict[str, Any] = {}

    if system_path.is_file():
        try:
            config_dict = _parse_toml(system_path)
        except ConfigError:
            logger.warning(f"Skipping invalid system config: {system_path}")

    project_path = _get_project_config(cwd)
    if project_path:
        try:
            project_config_dict = _parse_toml(project_path)
            config_dict = _merge_dicts(config_dict, project_config_dict)
        except ConfigError:
            logger.warning(f"Skipping invalid system config: {system_path}")

    if "cwd" not in config_dict:
        config_dict["cwd"] = cwd

    if "developer_instructions" not in config_dict:
        agent_md_content = _get_agent_md_files(cwd)
        if agent_md_content:
            config_dict["developer_instructions"] = agent_md_content

    try:
        config = Config(**config_dict)
    except Exception as e:
        raise ConfigError(f"Invalid configuration: {e}") from e

    _apply_model_env_overrides(config)

    return config


def _apply_model_env_overrides(config: Config) -> None:
    """Let `.env` drive the model, matching what the installer and README document.

    `NYANSA_MODEL` and `BASE_URL`/`NYANSA_BASE_URL` override the values from
    `.nyansa/config.toml` at load time. A control-plane model override (applied
    later during actor sync) still wins over both, so org governance is authoritative.
    """
    model_name = os.environ.get("NYANSA_MODEL")
    if model_name and model_name.strip():
        config.model.name = model_name.strip()

    base_url = os.environ.get("BASE_URL") or os.environ.get("NYANSA_BASE_URL")
    if base_url and base_url.strip():
        config.model.base_url = base_url.strip()

    local_base_url = config.model.base_url or ""
    if local_base_url.startswith(("http://localhost", "http://127.0.0.1")):
        config.model.requires_api_key = False
