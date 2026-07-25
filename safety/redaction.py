from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from config.config import Config


@dataclass
class RedactionResult:
    value: Any
    redaction_count: int = 0


DEFAULT_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("assignment_secret", re.compile(r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY)[A-Z0-9_]*)\s*=\s*([^\s'\"`]+)")),
    ("json_secret", re.compile(r"(?i)([\"'](?:api[_-]?key|token|secret|password|private[_-]?key)[\"']\s*:\s*[\"'])([^\"']+)([\"'])")),
]


class SensitiveDataRedactor:
    def __init__(self, config: Config):
        self.config = config

    def redact_text(self, text: str) -> RedactionResult:
        if not self.config.sensitive_data.enabled:
            return RedactionResult(text, 0)

        redacted = text
        total = 0

        for name, pattern in DEFAULT_SECRET_PATTERNS:
            if name == "assignment_secret":
                redacted, count = pattern.subn(lambda m: f"{m.group(1)}=[REDACTED:{name}]", redacted)
            elif name == "json_secret":
                redacted, count = pattern.subn(lambda m: f"{m.group(1)}[REDACTED:{name}]{m.group(3)}", redacted)
            else:
                redacted, count = pattern.subn(f"[REDACTED:{name}]", redacted)
            total += count

        return RedactionResult(redacted, total)

    def redact_value(self, value: Any) -> RedactionResult:
        if isinstance(value, str):
            return self.redact_text(value)

        if isinstance(value, dict):
            total = 0
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                result = self.redact_value(item)
                redacted[key] = result.value
                total += result.redaction_count
            return RedactionResult(redacted, total)

        if isinstance(value, list):
            total = 0
            redacted_items = []
            for item in value:
                result = self.redact_value(item)
                redacted_items.append(result.value)
                total += result.redaction_count
            return RedactionResult(redacted_items, total)

        return RedactionResult(value, 0)
