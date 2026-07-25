from pathlib import Path
from pydantic import BaseModel, Field

from context.symbols import extract_symbols, language_for, read_source
from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from tools.builtin.file_filter import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ExclusionFilter,
    cap_output,
    rank_paths,
)
from utils.paths import resolve_path


class RepoMapParams(BaseModel):
    path: str = Field(
        ".", description="Directory to map (default: the working directory)"
    )
    max_files: int = Field(
        200, description="Maximum number of files to include", ge=1, le=2000
    )
    symbols: bool = Field(
        True,
        description="Include top-level classes and functions per file. "
        "Set false for a structure-only listing.",
    )


class RepoMapTool(Tool):
    name = "repo_map"
    description = (
        "Get a structural overview of a codebase: source files ranked by relevance, "
        "each with its top-level classes, functions and types. Use this FIRST when "
        "working in an unfamiliar repository - it is far cheaper than reading files "
        "one by one, and tells you where to look. Supports Python, JavaScript, "
        "TypeScript, Go, Rust, Java, Kotlin, Ruby, PHP and C-family sources."
    )
    kind = ToolKind.READ
    schema = RepoMapParams

    MAX_SCAN = 20_000
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = RepoMapParams(**invocation.params)
        root = resolve_path(invocation.cwd, params.path)

        if not root.exists() or not root.is_dir():
            return ToolResult.error_result(f"Directory does not exist: {root}")

        files, scan_capped = self._collect_files(root)

        if not files:
            return ToolResult.success_result(
                f"No recognised source files found under {root}",
                metadata={"path": str(root), "files": 0},
            )

        ranked = rank_paths(files, root)
        shown = ranked[: params.max_files]

        sections: list[str] = []
        symbol_total = 0

        for path in shown:
            rendered, count = self._render_file(path, root, params.symbols)
            symbol_total += count
            sections.append(rendered)

        notices = []
        if scan_capped:
            notices.append(
                f"Scan stopped at {self.MAX_SCAN} files; the map is incomplete. "
                "Map a subdirectory for full coverage."
            )
        if len(ranked) > len(shown):
            notices.append(
                f"Showing the {len(shown)} most relevant of {len(ranked)} files. "
                "Raise max_files or map a subdirectory to see more."
            )

        body = "\n".join(sections)
        output = "\n\n".join(notices + [body]) if notices else body
        capped, truncated = cap_output(
            output,
            "Map a subdirectory, or set symbols=false for a structure-only listing.",
            self.MAX_OUTPUT_TOKENS,
        )

        return ToolResult.success_result(
            capped,
            truncated=truncated,
            metadata={
                "path": str(root),
                "files": len(shown),
                "files_found": len(ranked),
                "symbols": symbol_total,
            },
        )

    def _collect_files(self, root: Path) -> tuple[list[Path], bool]:
        exclusions = ExclusionFilter(root)
        files: list[Path] = []
        capped = False

        for path in root.rglob("*"):
            if len(files) >= self.MAX_SCAN:
                capped = True
                break

            if not path.is_file() or language_for(path) is None:
                continue

            if exclusions.is_excluded(path, is_dir=False):
                continue

            files.append(path)

        return files, capped

    def _render_file(self, path: Path, root: Path, include_symbols: bool) -> tuple[str, int]:
        try:
            display = path.relative_to(root)
        except ValueError:
            display = path

        if not include_symbols:
            return str(display), 0

        source = read_source(path)
        if source is None:
            return f"{display}\n  [unreadable]", 0

        symbols = extract_symbols(path, source)
        if not symbols:
            return str(display), 0

        lines = [str(display)] + [f"  {symbol.render()}" for symbol in symbols]

        return "\n".join(lines), len(symbols)
