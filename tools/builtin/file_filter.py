from __future__ import annotations

import re
from pathlib import Path

from utils.text import count_tokens, truncate_text

DEFAULT_MAX_OUTPUT_TOKENS = 25000

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    "vendor",
    "third_party",
    "__pycache__",
    ".venv",
    "venv",
    "site-packages",
    ".tox",
    ".nox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".parcel-cache",
    ".turbo",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".gradle",
    ".terraform",
    ".idea",
    ".vscode",
    "coverage",
    "htmlcov",
}

VENDOR_DIRS = {
    "node_modules",
    "bower_components",
    "vendor",
    "third_party",
    ".venv",
    "venv",
    "site-packages",
    ".tox",
    ".nox",
    ".eggs",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".gradle",
}


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    length = len(pattern)

    while i < length:
        char = pattern[i]

        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i):
            out.append("/.*")
            i += 3
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < length and pattern[j] in "!^":
                j += 1
            if j < length and pattern[j] == "]":
                j += 1
            while j < length and pattern[j] != "]":
                j += 1

            if j >= length:
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        elif char == "\\" and i + 1 < length:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(char))
            i += 1

    return "".join(out)


class _GitignoreRule:
    def __init__(self, base: Path, pattern: str) -> None:
        self.base = base
        self.negated = pattern.startswith("!")

        if self.negated:
            pattern = pattern[1:]

        self.dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")

        anchored = "/" in pattern
        if pattern.startswith("/"):
            pattern = pattern[1:]
            anchored = True

        prefix = "" if anchored else "(?:.*/)?"
        body = prefix + _translate(pattern)

        self._self_re = re.compile(f"^{body}$")
        self._child_re = re.compile(f"^{body}/.*$")

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        if self._child_re.match(rel_path):
            return True

        if self.dir_only and not is_dir:
            return False

        return bool(self._self_re.match(rel_path))


def _parse_gitignore(path: Path) -> list[_GitignoreRule]:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []

    rules = []
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        try:
            rules.append(_GitignoreRule(path.parent, line))
        except re.error:
            continue

    return rules


class ExclusionFilter:
    """Shared directory/gitignore exclusion logic for grep, glob and list_dir."""

    def __init__(self, root: Path, use_gitignore: bool = True) -> None:
        self.root = root
        self.enabled = not _is_vendored_path(root)
        self.use_gitignore = use_gitignore and self.enabled

        self._boundary = _find_repo_root(root) or root
        self._dir_rules: dict[Path, list[_GitignoreRule]] = {}
        self._chains: dict[Path, list[_GitignoreRule]] = {}

    def _rules_in(self, directory: Path) -> list[_GitignoreRule]:
        cached = self._dir_rules.get(directory)
        if cached is None:
            gitignore = directory / ".gitignore"
            cached = _parse_gitignore(gitignore) if gitignore.is_file() else []
            self._dir_rules[directory] = cached

        return cached

    def _chain_for(self, directory: Path) -> list[_GitignoreRule]:
        cached = self._chains.get(directory)
        if cached is not None:
            return cached

        if directory == self._boundary or self._boundary not in directory.parents:
            chain = list(self._rules_in(directory))
        else:
            chain = list(self._chain_for(directory.parent)) + self._rules_in(directory)

        self._chains[directory] = chain
        return chain

    def is_excluded(self, path: Path, is_dir: bool | None = None) -> bool:
        if not self.enabled:
            return False

        if is_dir is None:
            is_dir = path.is_dir()

        if any(part in EXCLUDED_DIRS for part in _relative_parts(path, self.root)):
            return True

        if not self.use_gitignore:
            return False

        parent = path.parent
        ignored = False

        for rule in self._chain_for(parent):
            try:
                rel = path.relative_to(rule.base).as_posix()
            except ValueError:
                continue

            if rule.matches(rel, is_dir):
                ignored = not rule.negated

        return ignored

    def filter_dirs(self, parent: Path, names: list[str]) -> list[str]:
        return [
            name for name in names if not self.is_excluded(parent / name, is_dir=True)
        ]


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _is_vendored_path(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


def _find_repo_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent

    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate

    return None


def rank_key(path: Path, root: Path) -> tuple:
    parts = _relative_parts(path, root)
    vendored = 1 if any(part in VENDOR_DIRS for part in parts) else 0

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return (vendored, len(parts), -mtime, "/".join(parts))


def rank_paths(paths: list[Path], root: Path) -> list[Path]:
    return sorted(paths, key=lambda p: rank_key(p, root))


def cap_output(
    output: str,
    hint: str,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> tuple[str, bool]:
    if count_tokens(output) <= max_tokens:
        return output, False

    suffix = (
        f"\n\n[OUTPUT TRUNCATED: exceeded the {max_tokens} token limit for this tool. "
        f"Results below this point were dropped. {hint}]"
    )

    return truncate_text(output, max_tokens, suffix=suffix), True
