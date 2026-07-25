from dataclasses import dataclass
from enum import Enum
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Literal
from config.config import ApprovalPolicy
from tools.base import ToolConfirmation

logger = logging.getLogger(__name__)

ConfirmationResponse = bool | Literal["always"]


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_CONFIRMATION = "needs_confirmation"


# Most restrictive first: used to combine several independent assessments.
_DECISION_SEVERITY = {
    ApprovalDecision.APPROVED: 0,
    ApprovalDecision.NEEDS_CONFIRMATION: 1,
    ApprovalDecision.REJECTED: 2,
}


@dataclass
class ApprovalContext:

    tool_name: str
    params: dict[str, Any]
    is_mutating: bool
    affected_paths: list[Path]
    command: str | None = None
    is_dangerous: bool = False


DANGEROUS_PATTERNS = [
    # File system destruction
    # NOTE: `rm` is handled by _is_dangerous_rm() below, which understands flag
    # ordering (-fr, -r -f, --force --recursive) instead of matching one spelling.
    r"rmdir\s+[/~]",
    # Disk operations
    r"dd\s+if=",
    r"mkfs",
    r"fdisk",
    r"parted",
    # System control
    r"shutdown",
    r"reboot",
    r"halt",
    r"poweroff",
    r"init\s+[06]",
    # Permission changes on root
    r"chmod\s+(-R\s+)?777\s+[/~]",
    r"chown\s+-R\s+.*\s+[/~]",
    # Network exposure
    r"nc\s+-l",
    r"netcat\s+-l",
    # Code execution from network
    r"curl\s+.*\|\s*(bash|sh)",
    r"wget\s+.*\|\s*(bash|sh)",
    # Fork bomb
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;",
]

# Patterns for safe commands (can be auto-approved)
SAFE_PATTERNS = [
    # Information commands
    r"^(ls|dir|pwd|cd|echo|cat|head|tail|less|more|wc)(\s|$)",
    r"^(find|locate|which|whereis|file|stat)(\s|$)",
    # Development tools (read-only)
    r"^git\s+(status|log|diff|show|branch|remote|tag)(\s|$)",
    r"^(npm|yarn|pnpm)\s+(list|ls|outdated)(\s|$)",
    r"^pip\s+(list|show|freeze)(\s|$)",
    r"^cargo\s+(tree|search)(\s|$)",
    # Text processing (usually safe)
    r"^(grep|awk|sed|cut|sort|uniq|tr|diff|comm)(\s|$)",
    # System info
    r"^(date|cal|uptime|whoami|id|groups|hostname|uname)(\s|$)",
    r"^(env|printenv|set)$",
    # Process info
    r"^(ps|top|htop|pgrep)(\s|$)",
]


# Every `rm` invocation in a command line, with the arguments that belong to it.
# Segments are split on shell separators so `X=/; rm -rf $X` is inspected as `rm -rf $X`.
RM_INVOCATION_RE = re.compile(r"\brm\b([^;&|\n]*)", re.IGNORECASE)

# An `rm` target we refuse to reason about. Anything rooted at / or ~, any bare
# glob, and any target that is only known at runtime (variable expansion or
# command substitution) - `rm -rf $X` is `rm -rf /` when X=/.
DANGEROUS_RM_TARGETS = [
    r"^[/~]",  # absolute or home-rooted
    r"[$`]",  # $VAR, ${VAR}, $(...), `...` - target unknown until runtime
    r"^\.{1,2}$",  # the working directory or its parent
    r"^\.{0,2}/?\*",  # bare globs: *, ./*, ../*
]


def _rm_flag_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if token.startswith("-") and token != "-"]


def _rm_is_destructive(flags: list[str]) -> bool:
    """True if the flags make `rm` recursive or forceful (in any spelling/order)."""
    for flag in flags:
        if flag.startswith("--"):
            if flag.lower() in {"--recursive", "--force", "--dir"}:
                return True
        elif re.search(r"[rRdf]", flag):
            # Bundled short flags: -rf, -fr, -r, -f, -d ...
            return True

    return False


def _is_dangerous_rm_target(target: str) -> bool:
    target = target.strip("\"'")
    if not target:
        return False

    for pattern in DANGEROUS_RM_TARGETS:
        if re.search(pattern, target):
            return True

    return False


def _is_dangerous_rm(command: str) -> bool:
    for match in RM_INVOCATION_RE.finditer(command):
        tokens = match.group(1).split()
        flags = _rm_flag_tokens(tokens)
        targets = [token for token in tokens if token not in flags]

        if not _rm_is_destructive(flags):
            continue

        for target in targets:
            if _is_dangerous_rm_target(target):
                return True

    return False


def is_dangerous_command(command: str) -> bool:
    if _is_dangerous_rm(command):
        return True

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True

    return False


def is_safe_command(command: str) -> bool:
    # SAFE_PATTERNS are anchored at the start of the string, so `ls; <anything>`
    # would otherwise be classified as safe on the strength of its first word.
    if re.search(r"[;&|`]|\$\(|\n", command):
        return False

    for pattern in SAFE_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True

    return False


class ApprovalManager:
    def __init__(
        self,
        approval_policy: ApprovalPolicy,
        cwd: Path,
        confirmation_callback: Callable[
            [ToolConfirmation], ConfirmationResponse
        ] | None = None,
    ) -> None:
        self.approval_policy = approval_policy
        self.cwd = cwd
        self.confirmation_callback = confirmation_callback
        self._session_approvals: set[str] = set()
        # ON_FAILURE runs unattended until something fails, then escalates to
        # confirmation for the rest of the session.
        self.failure_escalated = False

    def record_failure(self) -> None:
        """Escalate ON_FAILURE to confirmation after a failed tool call."""
        if self.approval_policy == ApprovalPolicy.ON_FAILURE and not self.failure_escalated:
            logger.info(
                "Tool failure recorded: on-failure policy now requires confirmation"
            )

        self.failure_escalated = True

    def reset_failure_escalation(self) -> None:
        self.failure_escalated = False

    def _unattended_decision(self) -> ApprovalDecision:
        """Decision for an operation that no policy has already resolved."""
        if self.approval_policy == ApprovalPolicy.ON_FAILURE and not self.failure_escalated:
            return ApprovalDecision.APPROVED

        return ApprovalDecision.NEEDS_CONFIRMATION

    def _is_within_cwd(self, path: Path) -> bool:
        # Resolve both sides: `is_relative_to` is textual, so `cwd/../etc/passwd`
        # and symlinks pointing outside the workspace would otherwise pass.
        try:
            resolved = path.resolve()
            root = self.cwd.resolve()
        except OSError:
            return False

        return resolved == root or resolved.is_relative_to(root)

    def _assess_command_safety(self, command: str) -> ApprovalDecision:
        # The dangerous-command check runs before any policy short-circuit, so
        # even YOLO cannot silently run `rm -rf /`.
        if is_dangerous_command(command):
            if self.approval_policy == ApprovalPolicy.YOLO:
                return ApprovalDecision.NEEDS_CONFIRMATION
            return ApprovalDecision.REJECTED

        if self.approval_policy == ApprovalPolicy.YOLO:
            return ApprovalDecision.APPROVED

        if self.approval_policy == ApprovalPolicy.NEVER:
            if is_safe_command(command):
                return ApprovalDecision.APPROVED
            return ApprovalDecision.REJECTED

        if self.approval_policy == ApprovalPolicy.AUTO:
            return ApprovalDecision.APPROVED

        if is_safe_command(command):
            return ApprovalDecision.APPROVED

        # AUTO_EDIT auto-approves edits but not arbitrary commands.
        if self.approval_policy == ApprovalPolicy.AUTO_EDIT:
            return ApprovalDecision.NEEDS_CONFIRMATION

        return self._unattended_decision()

    def _assess_path_safety(self, paths: list[Path]) -> ApprovalDecision:
        outside = [path for path in paths if not self._is_within_cwd(path)]

        if self.approval_policy == ApprovalPolicy.YOLO:
            return ApprovalDecision.APPROVED

        if self.approval_policy == ApprovalPolicy.NEVER:
            # NEVER must not ask, so writes outside the workspace are refused.
            if outside:
                return ApprovalDecision.REJECTED
            return ApprovalDecision.APPROVED

        if outside:
            # Writing outside the workspace always needs a human, even in AUTO.
            return ApprovalDecision.NEEDS_CONFIRMATION

        if self.approval_policy in {ApprovalPolicy.AUTO, ApprovalPolicy.AUTO_EDIT}:
            return ApprovalDecision.APPROVED

        # ON_REQUEST (the default) confirms every write, including in-cwd ones.
        return self._unattended_decision()

    def _assess_dangerous_flag(self) -> ApprovalDecision:
        if self.approval_policy == ApprovalPolicy.YOLO:
            return ApprovalDecision.APPROVED

        if self.approval_policy == ApprovalPolicy.NEVER:
            return ApprovalDecision.REJECTED

        return ApprovalDecision.NEEDS_CONFIRMATION

    def _assess_mutation(self) -> ApprovalDecision:
        """Fallback for mutating tools with no command and no affected paths."""
        if self.approval_policy in {
            ApprovalPolicy.YOLO,
            ApprovalPolicy.AUTO,
            ApprovalPolicy.AUTO_EDIT,
        }:
            return ApprovalDecision.APPROVED

        if self.approval_policy == ApprovalPolicy.NEVER:
            return ApprovalDecision.REJECTED

        return self._unattended_decision()

    async def check_approval(self, context: ApprovalContext) -> ApprovalDecision:
        if not context.is_mutating:
            return ApprovalDecision.APPROVED

        decisions: list[ApprovalDecision] = []

        if context.command:
            decisions.append(self._assess_command_safety(context.command))

        if context.affected_paths:
            decisions.append(self._assess_path_safety(context.affected_paths))

        if context.is_dangerous:
            decisions.append(self._assess_dangerous_flag())

        if not decisions:
            decisions.append(self._assess_mutation())

        # The most restrictive assessment wins.
        return max(decisions, key=lambda decision: _DECISION_SEVERITY[decision])

    def _confirmation_key(self, confirmation: ToolConfirmation) -> str:
        data = {
            "tool_name": confirmation.tool_name,
            "params": confirmation.params,
            "command": confirmation.command,
            "affected_paths": sorted(str(path) for path in confirmation.affected_paths),
        }
        return json.dumps(data, sort_keys=True, default=str)

    def request_confirmation(self, confirmation: ToolConfirmation) -> bool:
        key = self._confirmation_key(confirmation)
        if key in self._session_approvals:
            return True

        # Fail closed: with no way to ask a human, an operation that requires
        # confirmation is denied rather than silently approved.
        if self.confirmation_callback is None:
            logger.warning(
                "Denying '%s': confirmation required but no confirmation_callback is wired",
                confirmation.tool_name,
            )
            return False

        try:
            response = self.confirmation_callback(confirmation)
        except Exception:
            logger.exception(
                "Confirmation callback failed for '%s'; denying operation",
                confirmation.tool_name,
            )
            return False

        if response == "always":
            if not confirmation.is_dangerous:
                self._session_approvals.add(key)
            return True

        return bool(response)
