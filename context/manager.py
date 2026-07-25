from datetime import datetime
from typing import Any
from client.response import TokenUsage
from config.config import Config
from prompts.system import get_system_prompt
from dataclasses import dataclass, field

from tools.base import Tool
from utils.text import count_tokens


@dataclass
class MessageItem:
    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_count: int | None = None
    pruned_at: datetime | None = None
    is_error: bool = False
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}

        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id

        if self.tool_calls:
            result["tool_calls"] = self.tool_calls

        # Always include `content`. Some providers (e.g. Poolside via OpenRouter)
        # reject a message that omits the field entirely, even though OpenAI treats
        # it as optional. An assistant message that is only tool calls uses null —
        # the OpenAI-canonical empty value; every other role keeps a string.
        if self.role == "assistant" and not self.content:
            result["content"] = None
        else:
            result["content"] = self.content

        return result


class ContextManager:
    PRUNE_PROTECT_RATIO = 0.25
    PRUNE_MINIMUM_RATIO = 0.10
    COMPRESSION_RATIO = 0.8

    def __init__(
        self,
        config: Config,
        user_memory: str | None,
        tools: list[Tool] | None,
    ) -> None:
        self._system_prompt = get_system_prompt(config, user_memory, tools)
        self.config = config
        self._model_name = self.config.model_name
        self._messages: list[MessageItem] = []
        self._latest_usage = TokenUsage()
        self.total_usage = TokenUsage()

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def add_user_message(self, content: str) -> None:
        item = MessageItem(
            role="user",
            content=content,
            token_count=count_tokens(
                content,
                self._model_name,
            ),
        )

        self._messages.append(item)

    def add_assistant_message(
        self,
        content: str,
        tool_calls: list[dict[str, any]] | None = None,
    ) -> None:
        item = MessageItem(
            role="assistant",
            content=content or "",
            token_count=count_tokens(
                content or "",
                self._model_name,
            ),
            tool_calls=tool_calls or [],
        )

        self._messages.append(item)

    def add_tool_result(
        self,
        tool_call_id: str,
        content: str,
        is_error: bool = False,
    ) -> None:
        item = MessageItem(
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            token_count=count_tokens(content, self._model_name),
            is_error=is_error,
        )

        self._messages.append(item)

    def get_messages(self) -> list[dict[str, Any]]:
        messages = []

        if self._system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": self._system_prompt,
                }
            )

        for item in self._messages:
            messages.append(item.to_dict())

        return messages

    @property
    def prune_protect_tokens(self) -> int:
        return max(
            4_000, int(self.config.model.context_window * self.PRUNE_PROTECT_RATIO)
        )

    @property
    def prune_minimum_tokens(self) -> int:
        return max(
            2_000, int(self.config.model.context_window * self.PRUNE_MINIMUM_RATIO)
        )

    def estimated_tokens(self) -> int:
        """Local token floor, used when the provider reports no usage or reports it late."""
        total = count_tokens(self._system_prompt or "", self._model_name)

        for item in self._messages:
            total += item.token_count or count_tokens(item.content, self._model_name)

        return total

    def needs_compression(self) -> bool:
        context_limit = self.config.model.context_window
        current_tokens = max(self._latest_usage.total_tokens, self.estimated_tokens())

        return current_tokens > (context_limit * self.COMPRESSION_RATIO)

    def set_latest_usage(self, usage: TokenUsage):
        self._latest_usage = usage

    def add_usage(self, usage: TokenUsage):
        self.total_usage += usage

    def replace_with_summary(self, summary: str) -> None:
        original_request = next(
            (msg for msg in self._messages if msg.role == "user"), None
        )

        self._messages = []

        if original_request is not None:
            original_request.pinned = True
            self._messages.append(original_request)

        continuation_content = f"""# Context Restoration (Previous Session Compacted)

        The previous conversation was compacted due to context length limits. Below is a detailed summary of the work done so far. 

        **CRITICAL: Actions listed under "COMPLETED ACTIONS" are already done. DO NOT repeat them.**

        ---

        {summary}

        ---

        Resume work from where we left off. Focus ONLY on the remaining tasks."""

        summary_item = MessageItem(
            role="user",
            content=continuation_content,
            token_count=count_tokens(continuation_content, self._model_name),
        )
        self._messages.append(summary_item)

        ack_content = """I've reviewed the context from the previous session. I understand:
- The original goal and what was requested
- Which actions are ALREADY COMPLETED (I will NOT repeat these)
- The current state of the project
- What still needs to be done

I'll continue with the REMAINING tasks only, starting from where we left off."""
        ack_item = MessageItem(
            role="assistant",
            content=ack_content,
            token_count=count_tokens(ack_content, self._model_name),
        )
        self._messages.append(ack_item)

        continue_content = (
            "Continue with the REMAINING work only. Do NOT repeat any completed actions. "
            "Proceed with the next step as described in the context above."
        )

        continue_item = MessageItem(
            role="user",
            content=continue_content,
            token_count=count_tokens(continue_content, self._model_name),
        )
        self._messages.append(continue_item)

    def emergency_trim(self, target_ratio: float = 0.6) -> int:
        """Last-resort recovery when compaction fails: drop the oldest exchanges.

        Cuts are only made at user messages, which never sit between an assistant
        tool_calls message and its tool results, so no result is left orphaned.
        """
        target = int(self.config.model.context_window * target_ratio)

        if self.estimated_tokens() <= target:
            return 0

        pinned = [msg for msg in self._messages if msg.pinned]
        rest = [msg for msg in self._messages if not msg.pinned]
        cut_points = [i for i, msg in enumerate(rest) if msg.role == "user" and i > 0]

        removed = 0

        for cut in cut_points:
            self._messages = pinned + rest[cut:]
            removed = cut

            if self.estimated_tokens() <= target:
                break

        return removed

    def prune_tool_outputs(self) -> int:
        user_message_count = sum(1 for msg in self._messages if msg.role == "user")

        if user_message_count < 2:
            return 0

        total_tokens = 0
        pruned_tokens = 0
        to_prune: list[MessageItem] = []

        for msg in reversed(self._messages):
            if msg.role == "tool" and msg.tool_call_id:
                if msg.pruned_at:
                    break

                tokens = msg.token_count or count_tokens(msg.content, self._model_name)
                total_tokens += tokens

                if msg.is_error or msg.pinned:
                    continue

                if total_tokens > self.prune_protect_tokens:
                    pruned_tokens += tokens
                    to_prune.append(msg)

        if pruned_tokens < self.prune_minimum_tokens:
            return 0

        pruned_count = 0

        for msg in to_prune:
            msg.content = "[Old tool result content cleared]"
            msg.token_count = count_tokens(msg.content, self._model_name)
            msg.pruned_at = datetime.now()
            pruned_count += 1

        return pruned_count

    def clear(self) -> None:
        self._messages = []
