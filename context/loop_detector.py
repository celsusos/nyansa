from __future__ import annotations

import difflib
import hashlib
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9]+")

_PAGINATION_ARGS = {
    "offset",
    "limit",
    "start",
    "end",
    "line",
    "line_number",
    "page",
    "cursor",
    "skip",
    "take",
    "head",
    "tail",
    "max_results",
    "max_lines",
    "timeout",
    "depth",
}

_PAGINATION_BUCKET = 100
_HASH_THRESHOLD = 200


def _normalize_text(text: str) -> str:
    return " ".join(_WORD_RE.findall(text.lower()))


def _normalize_arg(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return str(value)

    if isinstance(value, (int, float)):
        if key in _PAGINATION_ARGS:
            return f"~{int(value) // _PAGINATION_BUCKET}"
        return str(value)

    text = value if isinstance(value, str) else repr(value)
    normalized = _normalize_text(text)

    if len(normalized) > _HASH_THRESHOLD:
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]

    return normalized


@dataclass
class LoopDetection:
    message: str
    count: int
    severity: str

    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"

    def __str__(self) -> str:
        return self.message


@dataclass
class _Action:
    kind: str
    signature: str
    text: str = ""


class LoopDetector:
    def __init__(self):
        self.max_exact_repeats = 3
        self.max_cycle_length = 3
        self.max_detections = 3
        self.response_similarity = 0.85
        self.detection_count = 0
        self._history: deque[_Action] = deque(maxlen=20)

    def record_action(self, action_type: str, **details: Any):
        output = [action_type]
        text = ""

        if action_type == "tool_call":
            output.append(details.get("tool_name", ""))
            args = details.get("args", {})

            if isinstance(args, dict):
                for k in sorted(args.keys()):
                    output.append(f"{k}={_normalize_arg(k, args[k])}")
        elif action_type == "response":
            text = _normalize_text(details.get("text", ""))
            output.append(text)

        self._history.append(_Action(action_type, "|".join(output), text))

    def check_for_loop(self) -> LoopDetection | None:
        if len(self._history) < 2:
            return None

        if len(self._history) >= self.max_exact_repeats:
            recent = list(self._history)[-self.max_exact_repeats :]
            if all(self._matches(recent[0], action) for action in recent[1:]):
                return self._record_detection(
                    f"Same action repeated {self.max_exact_repeats} times"
                )

        if len(self._history) >= self.max_cycle_length * 2:
            history = list(self._history)

            for cycle_len in range(
                2, min(self.max_cycle_length + 1, len(history) // 2 + 1)
            ):
                recent = history[-cycle_len * 2 :]
                if all(
                    self._matches(a, b)
                    for a, b in zip(recent[:cycle_len], recent[cycle_len:])
                ):
                    return self._record_detection(
                        f"Detected repeating cycle of length {cycle_len}"
                    )

        return None

    def should_abort(self) -> bool:
        return self.detection_count >= self.max_detections

    def clear(self) -> None:
        self._history.clear()
        self.detection_count = 0

    def _matches(self, a: _Action, b: _Action) -> bool:
        if a.kind == "response" and b.kind == "response":
            if not a.text or not b.text:
                return a.text == b.text

            ratio = difflib.SequenceMatcher(None, a.text, b.text).ratio()
            return ratio >= self.response_similarity

        return a.signature == b.signature

    def _record_detection(self, message: str) -> LoopDetection:
        self.detection_count += 1
        severity = "critical" if self.should_abort() else "warning"

        if severity == "critical":
            message = (
                f"{message}. This loop has now been flagged "
                f"{self.detection_count} times without recovery."
            )

        return LoopDetection(
            message=message,
            count=self.detection_count,
            severity=severity,
        )
