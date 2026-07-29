from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any


def create_synthetic_assistant_message(
    request: dict[str, Any], request_id: str
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "id": "remote-{}".format(request_id),
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": request.get("tool_use_id"),
                    "name": request.get("tool_name"),
                    "input": request.get("input", {}),
                }
            ],
            "model": "",
            "stop_reason": None,
            "stop_sequence": None,
            "container": None,
            "context_management": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
        "requestId": None,
        "timestamp": None,
    }


@dataclass(frozen=True)
class ToolStub:
    name: str

    def user_facing_name(self) -> str:
        return self.name

    def render_tool_use_message(self, input_payload: dict[str, Any]) -> str:
        items = list(input_payload.items())[:3]
        return ", ".join(
            "{}: {}".format(
                key,
                value if isinstance(value, str) else json.dumps(value, sort_keys=True),
            )
            for key, value in items
        )

    def is_enabled(self) -> bool:
        return True

    def needs_permissions(self) -> bool:
        return True


def create_tool_stub(tool_name: str) -> ToolStub:
    return ToolStub(name=tool_name)
