from __future__ import annotations
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from config.config import SandboxConfig

logger = logging.getLogger(__name__)

try:
    import resource
except ImportError:
    resource = None

Backend = str

MB = 1024 * 1024

MACOS_WRITABLE = ("/tmp", "/private/tmp", "/private/var/folders", "/dev")


@dataclass
class SandboxedCommand:
    argv: list[str]
    preexec_fn: Callable[[], None] | None
    backend: Backend
    description: str


def _preexec_supported() -> bool:
    return resource is not None and sys.platform != "win32"


def bubblewrap_available() -> bool:
    return shutil.which("bwrap") is not None


@lru_cache(maxsize=1)
def sandbox_exec_available() -> bool:
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return False

    try:
        result = subprocess.run(
            [binary, "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    if result.returncode != 0:
        logger.warning(
            "sandbox-exec is present but unusable: %s",
            (result.stderr or result.stdout).strip(),
        )
        return False

    return True


def resolve_backend(policy: SandboxConfig) -> Backend:
    if not policy.enabled or policy.backend == "none":
        return "none"

    if policy.backend == "auto":
        if bubblewrap_available():
            return "bubblewrap"
        if sandbox_exec_available():
            return "sandbox_exec"
        return "rlimit"

    if policy.backend == "bubblewrap":
        if bubblewrap_available():
            return "bubblewrap"
        logger.warning(
            "Sandbox backend 'bubblewrap' requested but 'bwrap' is not on PATH; "
            "falling back to rlimit-only sandboxing (no filesystem or network isolation)"
        )
        return "rlimit"

    if policy.backend == "sandbox_exec":
        if sandbox_exec_available():
            return "sandbox_exec"
        logger.warning(
            "Sandbox backend 'sandbox_exec' requested but 'sandbox-exec' is not "
            "available; falling back to rlimit-only sandboxing (no filesystem or "
            "network isolation)"
        )
        return "rlimit"

    return "rlimit"


def _set_limit(which: int, value: int) -> None:
    try:
        soft, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, hard))
    except (OSError, ValueError):
        pass


def build_rlimit_preexec(policy: SandboxConfig) -> Callable[[], None] | None:
    if not _preexec_supported():
        return None

    cpu = policy.cpu_seconds
    fsize = policy.file_size_mb * MB
    nproc = policy.max_processes
    mem = policy.memory_mb * MB

    def apply() -> None:
        _set_limit(resource.RLIMIT_CPU, cpu)
        _set_limit(resource.RLIMIT_FSIZE, fsize)
        _set_limit(resource.RLIMIT_CORE, 0)
        if hasattr(resource, "RLIMIT_NPROC"):
            _set_limit(resource.RLIMIT_NPROC, nproc)
        if sys.platform == "darwin":
            if hasattr(resource, "RLIMIT_DATA"):
                _set_limit(resource.RLIMIT_DATA, mem)
        elif hasattr(resource, "RLIMIT_AS"):
            _set_limit(resource.RLIMIT_AS, mem)
        elif hasattr(resource, "RLIMIT_DATA"):
            _set_limit(resource.RLIMIT_DATA, mem)

    return apply


def bubblewrap_command(
    policy: SandboxConfig, argv: list[str], cwd: Path
) -> list[str]:
    wrapper = ["bwrap"]

    if policy.confine_to_workspace:
        wrapper += ["--ro-bind", "/", "/", "--bind", str(cwd), str(cwd)]
        for path in policy.writable_paths:
            wrapper += ["--bind", path, path]
    else:
        wrapper += ["--bind", "/", "/"]

    for path in policy.read_only_paths:
        wrapper += ["--ro-bind", path, path]

    wrapper += [
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--die-with-parent",
        "--chdir",
        str(cwd),
    ]

    if not policy.allow_network:
        wrapper += ["--unshare-net"]

    return wrapper + argv


def _sbpl_quote(path: str) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def sbpl_profile(policy: SandboxConfig, cwd: Path) -> str:
    lines = ["(version 1)", "(allow default)"]

    if policy.confine_to_workspace:
        writable = [str(cwd), str(cwd.resolve())]
        writable += list(policy.writable_paths)
        writable += list(MACOS_WRITABLE)

        subpaths = "\n".join(f"  (subpath {_sbpl_quote(p)})" for p in writable)
        lines.append("(deny file-write*)")
        lines.append(f"(allow file-write*\n{subpaths})")

    if not policy.allow_network:
        lines.append("(deny network*)")

    return "\n".join(lines)


def sandbox_exec_command(
    policy: SandboxConfig, argv: list[str], cwd: Path
) -> list[str]:
    return ["sandbox-exec", "-p", sbpl_profile(policy, cwd)] + argv


def _describe(policy: SandboxConfig, backend: Backend) -> str:
    net = "network" if policy.allow_network else "no-network"
    if backend in ("bubblewrap", "sandbox_exec"):
        fs = "workspace-confined" if policy.confine_to_workspace else "unconfined-fs"
        return f"{backend} ({fs}, {net}, rlimits)"
    if backend == "rlimit":
        return "rlimit (resource limits only, no filesystem or network isolation)"
    return "sandbox disabled"


def build_sandboxed_command(
    policy: SandboxConfig, argv: list[str], cwd: Path
) -> SandboxedCommand:
    backend = resolve_backend(policy)
    argv = list(argv)

    if backend == "none":
        return SandboxedCommand(argv, None, backend, _describe(policy, backend))

    preexec = build_rlimit_preexec(policy)

    if backend == "bubblewrap":
        wrapped = bubblewrap_command(policy, argv, cwd)
    elif backend == "sandbox_exec":
        wrapped = sandbox_exec_command(policy, argv, cwd)
    else:
        wrapped = argv

    return SandboxedCommand(wrapped, preexec, backend, _describe(policy, backend))
