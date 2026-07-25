from pathlib import Path
from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from pydantic import BaseModel, Field

from tools.builtin.file_filter import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ExclusionFilter,
    cap_output,
    rank_paths,
)
from utils.paths import resolve_path


class GlobParams(BaseModel):
    pattern: str = Field(..., description="Glob pattern to match")
    path: str = Field(
        ".", description="Directory to search in (default: current directory)"
    )


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern. Supports ** for recursive matching. "
        "Results are sorted with shallower, recently modified project files first; "
        "version control, dependency and build directories as well as paths ignored "
        "by .gitignore are skipped."
    )
    kind = ToolKind.READ
    schema = GlobParams

    MAX_RESULTS = 1000
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = GlobParams(**invocation.params)

        search_path = resolve_path(invocation.cwd, params.path)

        if not search_path.exists() or not search_path.is_dir():
            return ToolResult.error_result(f"Directory does not exist: {search_path}")

        try:
            matches = self._find_files(search_path, params.pattern)
        except Exception as e:
            return ToolResult.error_result(f"Error searching: {e}")

        if not matches:
            return ToolResult.success_result(
                f"No files found matching pattern '{params.pattern}'",
                metadata={
                    "path": str(search_path),
                    "matches": 0,
                },
            )

        shown = matches[: self.MAX_RESULTS]
        output_lines = [
            str(self._display_path(file_path, invocation.cwd)) for file_path in shown
        ]

        if len(matches) > self.MAX_RESULTS:
            output_lines.append(
                f"\n[RESULTS TRUNCATED: showing the top {self.MAX_RESULTS} of "
                f"{len(matches)} matches. Use a more specific pattern or path to see "
                f"the rest.]"
            )

        output, truncated = cap_output(
            "\n".join(output_lines),
            hint="Use a more specific pattern or path to narrow the result set.",
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        return ToolResult.success_result(
            output,
            truncated=truncated or len(matches) > self.MAX_RESULTS,
            metadata={
                "path": str(search_path),
                "matches": len(matches),
                "shown": len(shown),
                "truncated": truncated,
            },
        )

    def _display_path(self, file_path: Path, cwd: Path) -> Path:
        try:
            return file_path.relative_to(cwd)
        except ValueError:
            return file_path

    def _find_files(self, search_path: Path, pattern: str) -> list[Path]:
        exclusions = ExclusionFilter(search_path)

        files = [
            path
            for path in search_path.glob(pattern)
            if path.is_file() and not exclusions.is_excluded(path, is_dir=False)
        ]

        return rank_paths(files, search_path)
