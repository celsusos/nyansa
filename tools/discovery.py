from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Callable

from config.config import Config
from config.loader import get_config_dir, get_data_dir
from tools.base import Tool
from tools.registry import ToolRegistry
from tools.subagents import SubagentTool, load_subagent_definitions

logger = logging.getLogger(__name__)

TRUST_FILE_NAME = "trusted_tool_dirs.json"
TRUST_STORE_ENV_VAR = "NYANSA_TRUST_STORE"
TRUST_STORE_VERSION = 1

AGENT_DIR_NAME = ".nyansa"
TOOLS_SUBDIR = "tools"
SUBAGENTS_SUBDIR = "subagents"


class TrustStatus(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    CHANGED = "changed"


def get_trust_store_path() -> Path:
    override = os.environ.get(TRUST_STORE_ENV_VAR)
    if override:
        return Path(override)

    return get_data_dir() / TRUST_FILE_NAME


def _payload_files(directory: Path) -> list[Path]:
    agent_dir = directory / AGENT_DIR_NAME

    files = [
        path
        for path in sorted((agent_dir / TOOLS_SUBDIR).glob("*.py"))
        if path.is_file() and not path.name.startswith("__")
    ]
    files += [
        path
        for path in sorted((agent_dir / SUBAGENTS_SUBDIR).glob("*.toml"))
        if path.is_file()
    ]

    return files


def fingerprint_directory(directory: Path) -> str:
    digest = hashlib.sha256()
    root = directory.resolve()

    for path in _payload_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")

    return digest.hexdigest()


def load_trust_store() -> dict[str, Any]:
    path = get_trust_store_path()

    if not path.is_file():
        return {"version": TRUST_STORE_VERSION, "entries": {}}

    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring unreadable tool trust store {path}: {e}")
        return {"version": TRUST_STORE_VERSION, "entries": {}}

    if not isinstance(store, dict) or not isinstance(store.get("entries"), dict):
        logger.warning(f"Ignoring malformed tool trust store {path}")
        return {"version": TRUST_STORE_VERSION, "entries": {}}

    return store


def _save_trust_store(store: dict[str, Any]) -> None:
    path = get_trust_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")


def trust_status(directory: Path) -> TrustStatus:
    entry = load_trust_store()["entries"].get(str(directory.resolve()))

    if not isinstance(entry, dict):
        return TrustStatus.UNTRUSTED

    if entry.get("fingerprint") != fingerprint_directory(directory):
        return TrustStatus.CHANGED

    return TrustStatus.TRUSTED


def trust_directory(directory: Path) -> str:
    resolved = directory.resolve()
    fingerprint = fingerprint_directory(resolved)

    store = load_trust_store()
    store["entries"][str(resolved)] = {
        "fingerprint": fingerprint,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_trust_store(store)
    logger.info(f"Trusted project tools in {resolved}")

    return fingerprint


def revoke_directory(directory: Path) -> bool:
    resolved = str(directory.resolve())

    store = load_trust_store()
    if resolved not in store["entries"]:
        return False

    del store["entries"][resolved]
    _save_trust_store(store)
    logger.info(f"Revoked trust for project tools in {resolved}")

    return True


class ToolDiscoveryManager:
    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        trust_callback: Callable[[Path, TrustStatus], bool] | None = None,
    ):
        self.config = config
        self.registry = registry
        self.trust_callback = trust_callback

    def _load_tool_modules(self, file_path: Path) -> Any:
        module_name = f"discovered_tool_{file_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)

        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec from {file_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        spec.loader.exec_module(module)
        return module

    def _find_tool_classes(self, module: Any) -> list[Tool]:
        tools: list[Tool] = []

        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj)
                and issubclass(obj, Tool)
                and obj is not Tool
                and obj.__module__ == module.__name__
            ):
                tools.append(obj)

        return tools

    def is_trusted(self, directory: Path) -> bool:
        status = trust_status(directory)

        if status == TrustStatus.TRUSTED:
            return True

        if self.trust_callback is not None:
            try:
                approved = self.trust_callback(directory, status)
            except Exception:
                logger.exception(f"Trust prompt failed for {directory}")
                return False

            if approved:
                trust_directory(directory)
                return True

        if status == TrustStatus.CHANGED:
            logger.warning(
                f"Project tools in {directory} changed since they were approved. "
                f"Skipping them until they are approved again."
            )
        else:
            logger.info(
                f"Skipping untrusted project tools in {directory}. "
                f"Approve them explicitly to enable loading."
            )

        return False

    def _discover_tools(self, directory: Path) -> None:
        tool_dir = directory / AGENT_DIR_NAME / TOOLS_SUBDIR

        if not tool_dir.is_dir():
            return

        for py_file in sorted(tool_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue

            try:
                module = self._load_tool_modules(py_file)
                tool_classes = self._find_tool_classes(module)
            except Exception:
                logger.exception(f"Failed to load discovered tool file {py_file}")
                continue

            if not tool_classes:
                logger.debug(f"No Tool subclasses found in {py_file}")
                continue

            for tool_class in tool_classes:
                try:
                    self.registry.register(tool_class(self.config))
                except Exception:
                    logger.exception(
                        f"Failed to register discovered tool "
                        f"{tool_class.__name__} from {py_file}"
                    )

    def _discover_subagents(self, directory: Path) -> None:
        subagent_dir = directory / AGENT_DIR_NAME / SUBAGENTS_SUBDIR

        if not subagent_dir.is_dir():
            return

        for definition in load_subagent_definitions(subagent_dir):
            try:
                self.registry.register(SubagentTool(self.config, definition))
            except Exception:
                logger.exception(
                    f"Failed to register sub-agent '{definition.name}' "
                    f"from {subagent_dir}"
                )

    def discover_from_directory(
        self, directory: Path, implicit_trust: bool = False
    ) -> None:
        if not directory.is_dir():
            return

        if not (directory / AGENT_DIR_NAME).is_dir():
            return

        if not implicit_trust and not self.is_trusted(directory):
            return

        self._discover_tools(directory)
        self._discover_subagents(directory)

    def discover_all(self) -> None:
        if not getattr(self.config, "tool_discovery_enabled", True):
            logger.debug("Tool discovery is disabled by configuration")
            return

        self.discover_from_directory(self.config.cwd)
        self.discover_from_directory(get_config_dir(), implicit_trust=True)
