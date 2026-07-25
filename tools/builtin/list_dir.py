from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from pydantic import BaseModel, Field

from tools.builtin.file_filter import DEFAULT_MAX_OUTPUT_TOKENS, cap_output
from utils.paths import resolve_path


class ListDirParams(BaseModel):
    path: str = Field(
        ".", description="Directory path to list (default: current directory)"
    )
    include_hidden: bool = Field(
        False,
        description="Whether to include hidden files and directories (default: false",
    )


class ListDirTool(Tool):
    name = "list_dir"
    description = "List contents of a directory"
    kind = ToolKind.READ
    schema = ListDirParams

    MAX_ENTRIES = 1000
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = ListDirParams(**invocation.params)

        dir_path = resolve_path(invocation.cwd, params.path)

        if not dir_path.exists() or not dir_path.is_dir():
            return ToolResult.error_result(f"Directory does not exist: {dir_path}")

        try:
            items = sorted(
                dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except Exception as e:
            return ToolResult.error_result(f"Error listing directory: {e}")

        if not params.include_hidden:
            items = [item for item in items if not item.name.startswith(".")]

        if not items:
            return ToolResult.success_result(
                "Directory is empty", metadata={"path": str(dir_path), "entries": 0}
            )

        shown = items[: self.MAX_ENTRIES]
        lines = []

        for item in shown:
            if item.is_dir():
                lines.append(f"{item.name}/")
            else:
                lines.append(item.name)

        if len(items) > self.MAX_ENTRIES:
            lines.append(
                f"\n[ENTRIES TRUNCATED: showing {self.MAX_ENTRIES} of {len(items)} "
                f"entries. List a more specific subdirectory, or use glob with a "
                f"pattern, to see the rest.]"
            )

        output, truncated = cap_output(
            "\n".join(lines),
            hint="List a more specific subdirectory to see the remaining entries.",
            max_tokens=self.MAX_OUTPUT_TOKENS,
        )

        return ToolResult.success_result(
            output,
            truncated=truncated or len(items) > self.MAX_ENTRIES,
            metadata={
                "path": str(dir_path),
                "entries": len(items),
                "shown": len(shown),
                "truncated": truncated,
            },
        )
