from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def repository_name(cwd: Path) -> str:
    remote = _git(cwd, "config", "--get", "remote.origin.url")
    if remote:
        name = remote.rstrip("/")
        if name.endswith(".git"):
            name = name[:-4]
        if ":" in name and "/" in name:
            return "/".join(name.replace(":", "/").split("/")[-2:])
        return name.split("/")[-1] or name

    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel:
        return Path(toplevel).name

    return cwd.name


def commit_sha(cwd: Path) -> str | None:
    return _git(cwd, "rev-parse", "HEAD")


def branch(cwd: Path) -> str | None:
    name = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name == "HEAD" else name


def changed_files(cwd: Path, base_ref: str | None = None) -> list[str]:
    spec = f"{base_ref}...HEAD" if base_ref else "HEAD~1...HEAD"
    out = _git(cwd, "diff", "--name-only", spec)
    if not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
