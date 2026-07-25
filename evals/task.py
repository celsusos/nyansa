from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from textwrap import dedent


class Expectation(str, Enum):
    COMMAND_PASSES = "command_passes"
    NO_MUTATION = "no_mutation"


@dataclass(frozen=True)
class EvalTask:
    task_id: str
    category: str
    prompt: str
    files: dict[str, str]
    expectation: Expectation = Expectation.COMMAND_PASSES
    verify: str | None = None
    verify_files: dict[str, str] = field(default_factory=dict)
    max_turns: int | None = None
    timeout_sec: float | None = None

    def __post_init__(self) -> None:
        if self.expectation is Expectation.COMMAND_PASSES and not self.verify:
            raise ValueError(f"{self.task_id}: COMMAND_PASSES requires a verify command")
        for relative in (*self.files, *self.verify_files):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{self.task_id}: unsafe fixture path {relative!r}")


def fixture(text: str) -> str:
    return dedent(text).lstrip("\n")


def crlf(text: str) -> str:
    return fixture(text).replace("\n", "\r\n")


def write_fixture_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: newline translation would destroy CRLF fixtures.
        path.write_bytes(content.encode("utf-8"))


def snapshot_tree(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes: list[str] = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        if old is None:
            changes.append(f"created {relative}")
        elif new is None:
            changes.append(f"deleted {relative}")
        else:
            changes.append(f"modified {relative}")
    return changes
