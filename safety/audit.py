from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from config.config import Config
from control_plane.client import send_audit_event
from config.loader import get_data_dir
from safety.redaction import SensitiveDataRedactor


class AuditLogger:
    CONTENT_KEYS = {
        "content",
        "diff",
        "message",
        "new_string",
        "old_string",
        "output",
        "response",
    }

    def __init__(self, config: Config):
        self.config = config
        self.redactor = SensitiveDataRedactor(config)

    def _path(self) -> Path:
        path = self.config.audit.log_path
        if path is None:
            path = get_data_dir() / "audit" / "events.jsonl"

        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        return path

    def _summarize_content(self, value: Any, key: str | None = None) -> Any:
        if self.config.audit.log_content:
            return value

        if isinstance(value, str) and key in self.CONTENT_KEYS:
            return {
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                "bytes": len(value.encode("utf-8")),
            }

        if isinstance(value, dict):
            return {
                item_key: self._summarize_content(item, str(item_key))
                for item_key, item in value.items()
            }

        if isinstance(value, list):
            return [self._summarize_content(item, key) for item in value]

        return value

    def record(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        if not self.config.audit.enabled:
            return

        safe_details = self._summarize_content(details or {})
        if self.config.sensitive_data.redact_audit_events:
            safe_details = self.redactor.redact_value(safe_details).value

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "actor": {
                "user_id": self.config.actor.user_id,
                "organization_id": self.config.actor.organization_id,
                "role": self.config.actor.role,
            },
            "details": safe_details,
        }

        path = self._path()
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, sort_keys=True) + "\n")
        os.chmod(path, 0o600)
        send_audit_event(self.config, event)
