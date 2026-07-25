import os
from pathlib import Path
import re
from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from pydantic import BaseModel, Field

from tools.builtin.file_filter import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ExclusionFilter,
    cap_output,
    rank_paths,
)
from utils.paths import is_binary_file, resolve_path


class GrepParams(BaseModel):
    pattern: str = Field(..., description="Regular expression pattern to search for")
    path: str = Field(
        ".", description="File or directory to search in (default: current directory)"
    )
    case_insensitive: bool = Field(
        False,
        description="Case-insensitive search (default: false)",
    )


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search for a regex pattern in file contents. Returns matching lines with file "
        "paths and line numbers. Skips version control, dependency and build "
        "directories as well as paths ignored by .gitignore."
    )
    kind = ToolKind.READ
    schema = GrepParams

    MAX_FILES = 500
    MAX_SCAN = 20000
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = GrepParams(**invocation.params)

        search_path = resolve_path(invocation.cwd, params.path)

        if not search_path.exists():
            return ToolResult.error_result(f"Path does not exist: {search_path}")

        try:
            flags = re.IGNORECASE if params.case_insensitive else 0
            pattern = re.compile(params.pattern, flags)
        except re.error as e:
            return ToolResult.error_result(f"Invalid regex pattern: {e}")

        if search_path.is_dir():
            files, files_capped = self._find_files(search_path)
        else:
            files, files_capped = [search_path], False

        output_lines = []
        matches = 0

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            lines = content.splitlines()
            file_matches = False

            for i, line in enumerate(lines, start=1):
                if pattern.search(line):
                    matches += 1
                    if not file_matches:
                        output_lines.append(
                            f"=== {self._display_path(file_path, invocation.cwd)} ==="
                        )
                        file_matches = True

                    output_lines.append(f"{i}:{line}")

            if file_matches:
                output_lines.append("")

        metadata = {
            "path": str(search_path),
            "matches": matches,
            "files_searched": len(files),
            "files_capped": files_capped,
        }

        cap_notice = (
            f"[SEARCH INCOMPLETE: only the first {self.MAX_FILES} files were searched "
            f"(ranked by path depth and recency). Other files were NOT searched, so "
            f"this result may be missing matches. Narrow the search with a more "
            f"specific path or pattern.]"
        )

        if not output_lines:
            message = f"No matches found for pattern '{params.pattern}'"
            if files_capped:
                message = f"{message}\n\n{cap_notice}"

            return ToolResult.success_result(message, metadata=metadata)

        output = "\n".join(output_lines)

        if files_capped:
            output = f"{cap_notice}\n\n{output}"

        output, truncated = cap_output(
            output,
            hint=(
                "Re-run with a narrower path or a more specific pattern to see the "
                "remaining matches."
            ),
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        metadata["truncated"] = truncated

        return ToolResult.success_result(
            output,
            truncated=truncated,
            metadata=metadata,
        )

    def _display_path(self, file_path: Path, cwd: Path) -> Path:
        try:
            return file_path.relative_to(cwd)
        except ValueError:
            return file_path

    def _find_files(self, search_path: Path) -> tuple[list[Path], bool]:
        exclusions = ExclusionFilter(search_path)
        files = []
        scan_capped = False

        for root, dirs, filenames in os.walk(search_path):
            root_path = Path(root)
            dirs[:] = exclusions.filter_dirs(root_path, dirs)

            for filename in filenames:
                if filename.startswith("."):
                    continue

                file_path = root_path / filename
                if exclusions.is_excluded(file_path, is_dir=False):
                    continue

                if not is_binary_file(file_path):
                    files.append(file_path)

            if len(files) >= self.MAX_SCAN:
                scan_capped = True
                break

        ranked = rank_paths(files, search_path)

        return ranked[: self.MAX_FILES], scan_capped or len(ranked) > self.MAX_FILES
