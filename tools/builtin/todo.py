from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from config.config import Config
from tools.base import Tool, ToolInvocation, ToolKind, ToolResult
from pydantic import BaseModel, Field

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

_STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED)
_PRIORITIES = ("high", "medium", "low")

_MARKERS = {
    STATUS_PENDING: " ",
    STATUS_IN_PROGRESS: "~",
    STATUS_COMPLETED: "x",
}


@dataclass
class TodoItem:
    id: str
    content: str
    status: str = STATUS_PENDING
    priority: str = "medium"
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "priority": self.priority,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TodoItem:
        return cls(
            id=str(data.get("id") or str(uuid.uuid4())[:8]),
            content=str(data.get("content", "")),
            status=(
                data.get("status")
                if data.get("status") in _STATUSES
                else STATUS_PENDING
            ),
            priority=(
                data.get("priority") if data.get("priority") in _PRIORITIES else "medium"
            ),
            depends_on=[str(dep) for dep in data.get("depends_on") or []],
        )

    def render(self) -> str:
        line = f"  [{_MARKERS[self.status]}] [{self.id}] ({self.priority}) {self.content}"
        if self.depends_on:
            line += f" (depends on: {', '.join(self.depends_on)})"
        return line


class TodosParams(BaseModel):
    action: str = Field(
        ...,
        description="Action: 'add', 'start', 'complete', 'list', 'clear' or 'plan'",
    )
    id: str | None = Field(None, description="Todo ID (for 'start' and 'complete')")
    content: str | None = Field(None, description="Todo content (for 'add')")
    priority: str | None = Field(
        None, description="Priority: 'high', 'medium' or 'low'. Defaults to 'medium'"
    )
    depends_on: list[str] | None = Field(
        None, description="IDs of todos that must be completed first"
    )
    todos: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "For 'plan': the full ordered task list, replacing any existing todos. "
            "Each entry accepts 'content' (required), 'id', 'status', 'priority' "
            "and 'depends_on'."
        ),
    )


class TodosTool(Tool):
    name = "todos"
    description = (
        "Manage an ordered task list for the current session. Use 'plan' to write a "
        "whole multi-step plan in one call, 'start' to mark the single task you are "
        "working on, and 'complete' when it is done. Completed tasks stay visible."
    )
    kind = ToolKind.MEMORY
    schema = TodosParams

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._todos: list[TodoItem] = []

    async def execute(self, invocation: ToolInvocation) -> ToolResult:
        params = TodosParams(**invocation.params)
        action = params.action.lower()

        if action == "add":
            return self._add(params)
        elif action == "plan":
            return self._plan(params)
        elif action == "start":
            return self._start(params)
        elif action == "complete":
            return self._complete(params)
        elif action == "list":
            return self._list()
        elif action == "clear":
            count = len(self._todos)
            self._todos.clear()
            return ToolResult.success_result(f"Cleared {count} todos")
        else:
            return ToolResult.error_result(f"Unknown action: {params.action}")

    def to_dict(self) -> dict[str, Any]:
        return {"todos": [todo.to_dict() for todo in self._todos]}

    def from_dict(self, data: dict[str, Any] | None) -> None:
        entries = (data or {}).get("todos") or []
        self._todos = [TodoItem.from_dict(entry) for entry in entries]

    def _add(self, params: TodosParams) -> ToolResult:
        if not params.content:
            return ToolResult.error_result("`content` required for 'add' action")

        priority = params.priority or "medium"
        if priority not in _PRIORITIES:
            return ToolResult.error_result(f"Invalid priority: {priority}")

        for dep in params.depends_on or []:
            if not self._find(dep):
                return ToolResult.error_result(f"Unknown dependency: {dep}")

        todo = TodoItem(
            id=str(uuid.uuid4())[:8],
            content=params.content,
            priority=priority,
            depends_on=list(params.depends_on or []),
        )
        self._todos.append(todo)

        return ToolResult.success_result(f"Added todo [{todo.id}]: {todo.content}")

    def _plan(self, params: TodosParams) -> ToolResult:
        if params.todos is None:
            return ToolResult.error_result("`todos` required for 'plan' action")

        items: list[TodoItem] = []
        seen: set[str] = set()

        for index, entry in enumerate(params.todos):
            if not isinstance(entry, dict) or not entry.get("content"):
                return ToolResult.error_result(
                    f"Todo at position {index} requires `content`"
                )
            if entry.get("status") is not None and entry["status"] not in _STATUSES:
                return ToolResult.error_result(f"Invalid status: {entry['status']}")
            if entry.get("priority") is not None and entry["priority"] not in _PRIORITIES:
                return ToolResult.error_result(f"Invalid priority: {entry['priority']}")

            item = TodoItem.from_dict(entry)
            if item.id in seen:
                return ToolResult.error_result(f"Duplicate todo id: {item.id}")

            seen.add(item.id)
            items.append(item)

        for item in items:
            for dep in item.depends_on:
                if dep not in seen:
                    return ToolResult.error_result(f"Unknown dependency: {dep}")

        active = [item for item in items if item.status == STATUS_IN_PROGRESS]
        if len(active) > 1:
            return ToolResult.error_result(
                "Only one todo may be 'in_progress' at a time"
            )

        self._todos = items

        return ToolResult.success_result(
            f"Replaced plan with {len(items)} todos\n{self._render()}"
        )

    def _start(self, params: TodosParams) -> ToolResult:
        if not params.id:
            return ToolResult.error_result("`id` required for 'start' action")

        todo = self._find(params.id)
        if not todo:
            return ToolResult.error_result(f"Todo not found: {params.id}")
        if todo.status == STATUS_COMPLETED:
            return ToolResult.error_result(f"Todo already completed: {params.id}")

        active = self._active()
        if active and active.id != todo.id:
            return ToolResult.error_result(
                f"Todo [{active.id}] is already in progress. Complete it first."
            )

        blocked = [
            dep
            for dep in todo.depends_on
            if not self._find(dep) or self._find(dep).status != STATUS_COMPLETED
        ]
        if blocked:
            return ToolResult.error_result(
                f"Todo [{todo.id}] is blocked by: {', '.join(blocked)}"
            )

        todo.status = STATUS_IN_PROGRESS

        return ToolResult.success_result(f"Started todo [{todo.id}]: {todo.content}")

    def _complete(self, params: TodosParams) -> ToolResult:
        if not params.id:
            return ToolResult.error_result("`id` required for 'complete' action")

        todo = self._find(params.id)
        if not todo:
            return ToolResult.error_result(f"Todo not found: {params.id}")

        todo.status = STATUS_COMPLETED

        return ToolResult.success_result(
            f"Completed todo [{todo.id}]: {todo.content}\n{self._render()}"
        )

    def _list(self) -> ToolResult:
        if not self._todos:
            return ToolResult.success_result("No todos")

        return ToolResult.success_result(self._render())

    def _render(self) -> str:
        counts = {status: 0 for status in _STATUSES}
        for todo in self._todos:
            counts[todo.status] += 1

        lines = ["Todos:"]
        lines.extend(todo.render() for todo in self._todos)
        lines.append(
            f"({counts[STATUS_COMPLETED]} completed, "
            f"{counts[STATUS_IN_PROGRESS]} in progress, "
            f"{counts[STATUS_PENDING]} pending)"
        )

        return "\n".join(lines)

    def _find(self, todo_id: str) -> TodoItem | None:
        for todo in self._todos:
            if todo.id == todo_id:
                return todo
        return None

    def _active(self) -> TodoItem | None:
        for todo in self._todos:
            if todo.status == STATUS_IN_PROGRESS:
                return todo
        return None
