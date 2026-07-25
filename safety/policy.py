from __future__ import annotations

from config.config import Config


ROLE_TOOL_ALLOWLISTS: dict[str, set[str] | None] = {
    "root": None,
    "platform_owner": None,
    "org_owner": None,
    "security_admin": {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "todos",
        "memory",
        "web_search",
        "web_fetch",
        "subagent_codebase_investigator",
        "subagent_code_reviewer",
    },
    "developer": {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "todos",
        "memory",
        "edit",
        "edit_file",
        "repo_map",
        "write_file",
        "subagent_codebase_investigator",
        "subagent_code_reviewer",
    },
    "intern": {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "todos",
        "subagent_codebase_investigator",
    },
    "auditor": {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "subagent_codebase_investigator",
    },
}


class RolePolicy:
    def __init__(self, config: Config):
        self.config = config

    def is_tool_allowed(self, tool_name: str) -> bool:
        if not self.config.role_policy.enabled:
            return True

        if self.config.role_policy.allowed_tools is not None:
            return tool_name in set(self.config.role_policy.allowed_tools)

        allowlist = ROLE_TOOL_ALLOWLISTS.get(self.config.actor.role)
        if allowlist is None:
            return True

        return tool_name in allowlist
