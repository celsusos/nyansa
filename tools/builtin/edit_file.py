import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tools.base import (
    FileDiff,
    Tool,
    ToolConfirmation,
    ToolInvocation,
    ToolKind,
    ToolResult,
)
from pydantic import BaseModel, Field

from utils.paths import ensure_parent_directory, resolve_path

DEFAULT_LINE_ENDING = "\n"

# Fuzzy matching scans every candidate window in the file. Skip it on very large
# files rather than burning seconds on a best-effort fallback.
MAX_FUZZY_SCAN_LINES = 20_000

# Upper bound on how much of a close-match region we echo back in an error.
MAX_ERROR_DIFF_LINES = 40


def detect_line_ending(content: str) -> str:
    """Return the dominant line ending of a raw (untranslated) file body."""
    if "\r\n" in content:
        return "\r\n"

    return DEFAULT_LINE_ENDING


def normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(content: str, line_ending: str) -> str:
    if line_ending == DEFAULT_LINE_ENDING:
        return content

    return content.replace("\n", line_ending)


def read_text_preserving_newlines(path: Path) -> str:
    """Read a file without universal-newline translation."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_preserving_newlines(path: Path, content: str) -> None:
    """Write a file without translating newlines to the platform default."""
    path.write_text(content, encoding="utf-8", newline="")


def leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def base_indent(text: str) -> str:
    for line in text.split("\n"):
        if line.strip():
            return leading_whitespace(line)

    return ""


def reindent(text: str, old_indent: str, new_indent: str) -> str:
    """Re-anchor a block of text from one base indentation onto another."""
    if old_indent == new_indent:
        return text

    lines = []

    for line in text.split("\n"):
        if not line.strip():
            lines.append("")
        elif not old_indent:
            lines.append(new_indent + line)
        elif line.startswith(old_indent):
            lines.append(new_indent + line[len(old_indent) :])
        else:
            lines.append(line)

    return "\n".join(lines)


def line_starts(content: str) -> list[int]:
    """Character offset of the first character of every line."""
    offsets = []
    position = 0

    for line in content.split("\n"):
        offsets.append(position)
        position += len(line) + 1

    return offsets


@dataclass
class TextMatch:
    start: int
    end: int
    indent: str = ""


@dataclass
class EditOutcome:
    new_content: str | None = None
    replaced_count: int = 0
    strategy: str = "exact"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def find_exact_matches(content: str, old_string: str) -> list[TextMatch]:
    matches = []
    start = content.find(old_string)

    while start != -1:
        end = start + len(old_string)
        matches.append(TextMatch(start=start, end=end))
        start = content.find(old_string, end)

    return matches


def _find_line_matches(
    content: str,
    old_string: str,
    key: Callable[[str], str],
) -> list[TextMatch]:
    """Find line-aligned windows whose lines are equal under ``key``."""
    old_lines = old_string.split("\n")
    old_keys = [key(line) for line in old_lines]

    if not any(old_keys):
        # Nothing but whitespace to anchor on - far too weak to match safely.
        return []

    content_lines = content.split("\n")

    if len(content_lines) > MAX_FUZZY_SCAN_LINES:
        return []

    offsets = line_starts(content)
    window = len(old_lines)
    matches = []

    for start in range(0, len(content_lines) - window + 1):
        candidate = content_lines[start : start + window]

        if [key(line) for line in candidate] != old_keys:
            continue

        text = "\n".join(candidate)
        matches.append(
            TextMatch(
                start=offsets[start],
                end=offsets[start] + len(text),
                indent=base_indent(text),
            )
        )

    return matches


def find_indentation_tolerant_matches(content: str, old_string: str) -> list[TextMatch]:
    """Match line blocks ignoring leading/trailing whitespace per line."""
    return _find_line_matches(content, old_string, lambda line: line.strip())


def find_whitespace_normalized_matches(content: str, old_string: str) -> list[TextMatch]:
    """Match line blocks ignoring all runs of whitespace within each line."""
    return _find_line_matches(content, old_string, lambda line: " ".join(line.split()))


class EditParams(BaseModel):
    path: str = Field(
        ...,
        description="Path to the file to edit (relative to working directory or absolute path)",
    )
    old_string: str = Field(
        "",
        description="The text to find and replace. Should match the file exactly including whitespace and indentation; differences in indentation, surrounding whitespace and line endings are tolerated as a fallback when the match stays unambiguous. For new files, leave this empty.",
    )
    new_string: str = Field(
        ...,
        description="The text to replace old_string with. Can be empty to delete text",
    )
    replace_all: bool = Field(
        False, description="Replace all occurrences of old_string (default: false)"
    )


class EditTool(Tool):
    name = "edit"
    description = (
        "Edit a file by replacing text. The old_string should match the file "
        "exactly (including whitespace and indentation) and must be unique in the "
        "file unless replace_all is true. If no exact match exists the tool "
        "retries while ignoring indentation and whitespace differences, but only "
        "applies the edit when exactly one region matches. Line endings are "
        "handled automatically and the file's existing style is preserved. "
        "Use this for precise, surgical edits. "
        "For creating new files or complete rewrites, use write_file instead."
    )
    kind = ToolKind.WRITE
    schema = EditParams

    async def get_confirmation(
        self,
        invocation: ToolInvocation,
    ) -> ToolConfirmation | None:
        params = EditParams(**invocation.params)
        path = resolve_path(invocation.cwd, params.path)

        is_new_file = not path.exists()

        if is_new_file:
            diff = FileDiff(
                path=path,
                old_content="",
                new_content=params.new_string,
                is_new_file=True,
            )

            return ToolConfirmation(
                tool_name=self.name,
                params=invocation.params,
                description=f"Create new file: {path}",
                diff=diff,
                affected_paths=[path],
            )

        old_content = normalize_line_endings(read_text_preserving_newlines(path))

        outcome = self._apply_edit(
            old_content,
            normalize_line_endings(params.old_string),
            normalize_line_endings(params.new_string),
            params.replace_all,
            path,
        )

        # A failing edit still gets a confirmation so the caller sees why; the
        # diff is omitted rather than showing a no-op or a guessed change.
        diff = None
        if outcome.new_content is not None:
            diff = FileDiff(
                path=path,
                old_content=old_content,
                new_content=outcome.new_content,
            )

        description = f"Edit file: {path}"
        if outcome.new_content is not None and outcome.strategy != "exact":
            description += f" (matched by {outcome.strategy})"

        return ToolConfirmation(
            tool_name=self.name,
            params=invocation.params,
            description=description,
            diff=diff,
            affected_paths=[path],
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = EditParams(**invocation.params)
        path = resolve_path(invocation.cwd, params.path)

        if not path.exists():
            if params.old_string:
                return ToolResult.error_result(
                    f"File does not exist: {path}. To create a new file, use an empty old_string."
                )

            ensure_parent_directory(path)
            write_text_preserving_newlines(path, params.new_string)

            line_count = len(params.new_string.splitlines())

            return ToolResult.success_result(
                f"Created {path} {line_count} lines",
                diff=FileDiff(
                    path=path,
                    old_content="",
                    new_content=params.new_string,
                    is_new_file=True,
                ),
                metadata={
                    "path": str(path),
                    "is_new_file": True,
                    "lines": line_count,
                },
            )

        try:
            raw_content = read_text_preserving_newlines(path)
        except UnicodeDecodeError as e:
            return ToolResult.error_result(
                f"Cannot edit {path}: file is not valid UTF-8 text ({e})."
            )
        except OSError as e:
            return ToolResult.error_result(f"Failed to read file {path}: {e}")

        line_ending = detect_line_ending(raw_content)
        old_content = normalize_line_endings(raw_content)

        if not params.old_string:
            return ToolResult.error_result(
                "old_string is empty but file exists. Provide old_string to edit, or use write_file to overwrite."
            )

        outcome = self._apply_edit(
            old_content,
            normalize_line_endings(params.old_string),
            normalize_line_endings(params.new_string),
            params.replace_all,
            path,
        )

        if outcome.new_content is None:
            return ToolResult.error_result(
                outcome.error or f"Failed to edit {path}",
                metadata=outcome.metadata,
            )

        new_content = outcome.new_content

        if new_content == old_content:
            return ToolResult.error_result(
                "No change made - old_string equals new_string"
            )

        try:
            write_text_preserving_newlines(
                path, restore_line_endings(new_content, line_ending)
            )
        except OSError as e:
            return ToolResult.error_result(f"failed to write file: {e}")

        old_lines = len(old_content.splitlines())
        new_lines = len(new_content.splitlines())
        line_diff = new_lines - old_lines

        diff_msg = ""

        if line_diff > 0:
            diff_msg = f" (+{line_diff} lines)"
        elif line_diff < 0:
            diff_msg = f" ({line_diff} lines)"

        match_msg = ""
        if outcome.strategy != "exact":
            match_msg = f" [matched by {outcome.strategy}]"

        return ToolResult.success_result(
            f"Edited {path}: replaced {outcome.replaced_count} occurrence(s){diff_msg}{match_msg}",
            diff=FileDiff(path=path, old_content=old_content, new_content=new_content),
            metadata={
                "path": str(path),
                "replaced_count": outcome.replaced_count,
                "line_diff": line_diff,
                "match_strategy": outcome.strategy,
                "line_ending": "crlf" if line_ending == "\r\n" else "lf",
            },
        )

    def _apply_edit(
        self,
        content: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
        path: Path,
    ) -> EditOutcome:
        """Resolve old_string against content and produce the edited content.

        Exact matching is tried first. Only if it finds nothing do we fall back
        to whitespace-tolerant strategies, and a fallback that matches more than
        one region is reported as ambiguous instead of guessing.
        """
        if not old_string:
            return EditOutcome(error="old_string is empty")

        exact_matches = find_exact_matches(content, old_string)

        if exact_matches:
            return self._build_outcome(
                content,
                exact_matches,
                [new_string] * len(exact_matches),
                replace_all,
                path,
                "exact",
            )

        strategies = (
            ("indentation", find_indentation_tolerant_matches),
            ("whitespace", find_whitespace_normalized_matches),
        )

        old_base = base_indent(old_string)

        for strategy, finder in strategies:
            matches = finder(content, old_string)

            if not matches:
                continue

            replacements = [
                reindent(new_string, old_base, match.indent) for match in matches
            ]

            return self._build_outcome(
                content,
                matches,
                replacements,
                replace_all,
                path,
                strategy,
            )

        return EditOutcome(error=self._no_match_message(old_string, content, path))

    def _build_outcome(
        self,
        content: str,
        matches: list[TextMatch],
        replacements: list[str],
        replace_all: bool,
        path: Path,
        strategy: str,
    ) -> EditOutcome:
        if len(matches) > 1 and not replace_all:
            return EditOutcome(
                strategy=strategy,
                error=self._ambiguous_message(content, matches, path, strategy),
                metadata={
                    "occurrence_count": len(matches),
                    "match_strategy": strategy,
                },
            )

        if not replace_all:
            matches = matches[:1]
            replacements = replacements[:1]

        new_content = content
        for match, replacement in zip(reversed(matches), reversed(replacements)):
            new_content = (
                new_content[: match.start] + replacement + new_content[match.end :]
            )

        return EditOutcome(
            new_content=new_content,
            replaced_count=len(matches),
            strategy=strategy,
            metadata={"match_strategy": strategy},
        )

    def _line_number(self, content: str, offset: int) -> int:
        return content.count("\n", 0, offset) + 1

    def _ambiguous_message(
        self,
        content: str,
        matches: list[TextMatch],
        path: Path,
        strategy: str,
    ) -> str:
        locations = ", ".join(
            f"line {self._line_number(content, match.start)}" for match in matches[:5]
        )

        if len(matches) > 5:
            locations += ", ..."

        qualifier = (
            "" if strategy == "exact" else f" (after {strategy}-tolerant matching)"
        )

        return (
            f"old_string found {len(matches)} times in {path}{qualifier}: {locations}. "
            f"Either: \n"
            f"1. Provide more context to make the match unique or\n"
            f"2. Set replace_all=true to replace all occurrences"
        )

    def _no_match_error(self, old_string: str, content: str, path: Path) -> ToolResult:
        return ToolResult.error_result(
            self._no_match_message(old_string, content, path)
        )

    def _no_match_message(self, old_string: str, content: str, path: Path) -> str:
        """Build an actionable not-found error anchored on the closest region."""
        error_msg = f"old_string not found in {path}."

        closest = self._find_closest_region(content, old_string)

        if closest is None:
            return error_msg + (
                " The file contains nothing resembling old_string. Re-read the file "
                "with the read_file tool and copy the target text from its output."
            )

        start_line, region, ratio = closest

        error_msg += (
            f"\n\nClosest match is at line {start_line} "
            f"({int(ratio * 100)}% similar). Diff (- your old_string, + the file):\n"
        )

        old_lines = old_string.split("\n")[:MAX_ERROR_DIFF_LINES]
        region_lines = region.split("\n")[:MAX_ERROR_DIFF_LINES]

        diff = difflib.unified_diff(
            old_lines,
            region_lines,
            fromfile="old_string",
            tofile=f"{path}:{start_line}",
            lineterm="",
            n=3,
        )

        error_msg += "\n".join(diff)
        error_msg += (
            "\n\nCopy the '+' side verbatim into old_string, including indentation "
            "and any trailing whitespace."
        )

        return error_msg

    def _find_closest_region(
        self,
        content: str,
        old_string: str,
    ) -> tuple[int, str, float] | None:
        """Find the line window in content most similar to old_string."""
        content_lines = content.split("\n")

        if not content_lines or content == "":
            return None

        if len(content_lines) > MAX_FUZZY_SCAN_LINES:
            return None

        old_lines = old_string.split("\n")
        window = min(len(old_lines), len(content_lines))

        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(old_string)

        best_ratio = 0.0
        best_start = None

        for start in range(0, len(content_lines) - window + 1):
            candidate = "\n".join(content_lines[start : start + window])

            matcher.set_seq1(candidate)

            if matcher.real_quick_ratio() <= best_ratio:
                continue
            if matcher.quick_ratio() <= best_ratio:
                continue

            ratio = matcher.ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_start = start

        if best_start is None or best_ratio < 0.3:
            return None

        region = "\n".join(content_lines[best_start : best_start + window])

        return best_start + 1, region, best_ratio
