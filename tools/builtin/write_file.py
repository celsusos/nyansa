from pathlib import Path

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


def read_existing_content(path: Path) -> tuple[str | None, str | None]:
    """Read a file that is about to be overwritten.

    Returns ``(content, error)``. A ``None`` content means the previous content
    is genuinely unknown - callers must not substitute an empty string for it,
    because that renders as a diff claiming the file was empty.
    """
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "existing file is not valid UTF-8 text"
    except OSError as e:
        return None, f"existing file could not be read: {e}"


class WriteFileParams(BaseModel):
    path: str = Field(
        ...,
        description="Path to the file to write (relative to working directory or absolute)",
    )
    content: str = Field(..., description="Content to write to the file")
    create_directories: bool = Field(
        True, description="Create parent directories if they don't exist"
    )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write content to a file. Creates the file if it doesn't exist, "
        "or overwrites if it does. Parent directories are created automatically. "
        "Use this for creating new files or completely replacing file contents. "
        "For partial modifications, use the edit tool instead."
    )
    kind = ToolKind.WRITE
    schema = WriteFileParams

    async def get_confirmation(
        self, invocation: ToolInvocation
    ) -> ToolConfirmation | None:
        params = WriteFileParams(**invocation.params)
        path = resolve_path(invocation.cwd, params.path)

        is_new_file = not path.exists()

        old_content = ""
        read_error = None
        if not is_new_file:
            old_content, read_error = read_existing_content(path)

        # Without the previous content there is no honest diff to show, so show
        # none rather than one that pretends the file was empty.
        diff = None
        if old_content is not None:
            diff = FileDiff(
                path=path,
                old_content=old_content,
                new_content=params.content,
                is_new_file=is_new_file,
            )

        action = "Created" if is_new_file else "Updated"
        description = f"{action} file: {path}"

        if read_error:
            description += (
                f"\nWARNING: {read_error}. "
                "The current contents cannot be shown and will be overwritten."
            )

        return ToolConfirmation(
            tool_name=self.name,
            params=invocation.params,
            description=description,
            diff=diff,
            affected_paths=[path],
            is_dangerous=not is_new_file,
        )

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = WriteFileParams(**invocation.params)
        path = resolve_path(invocation.cwd, params.path)

        is_new_file = not path.exists()
        old_content = ""
        read_error = None

        if not is_new_file:
            old_content, read_error = read_existing_content(path)

        try:
            if params.create_directories:
                ensure_parent_directory(path)
            elif not path.parent.exists():
                return ToolResult.error_result(
                    f"Parent directory does not exist: {path.parent}"
                )

            path.write_text(params.content, encoding="utf-8")

            action = "Created" if is_new_file else "Updated"
            line_count = len(params.content.splitlines())

            diff = None
            if old_content is not None:
                diff = FileDiff(
                    path=path,
                    old_content=old_content,
                    new_content=params.content,
                    is_new_file=is_new_file,
                )

            output = f"{action} {path} {line_count} lines"

            if read_error:
                output += (
                    f"\nWARNING: {read_error}, so no diff of the replaced "
                    "contents is available."
                )

            return ToolResult.success_result(
                output,
                diff=diff,
                metadata={
                    "path": str(path),
                    "is_new_file": is_new_file,
                    "lines": line_count,
                    "bytes": len(params.content.encode("utf-8")),
                    "old_content_available": old_content is not None,
                },
            )
        except OSError as e:
            return ToolResult.error_result(f"Failed to write file: {e}")
