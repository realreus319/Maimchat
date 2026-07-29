from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _system_message(
    content: str,
    uuid: Optional[str],
    subtype: str = "informational",
    level: str = "info",
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "type": "system",
        "subtype": subtype,
        "content": content,
        "level": level,
        "uuid": uuid,
        "timestamp": _timestamp(),
    }
    payload.update(extra)
    return payload


@dataclass(frozen=True)
class ConvertedMessage:
    type: str
    message: Optional[dict[str, Any]] = None
    event: Optional[dict[str, Any]] = None


def convert_sdk_message(
    message: dict[str, Any],
    convert_tool_results: bool = False,
    convert_user_text_messages: bool = False,
) -> ConvertedMessage:
    message_type = message.get("type")
    if message_type == "assistant":
        return ConvertedMessage(
            "message",
            {
                "type": "assistant",
                "message": message.get("message"),
                "uuid": message.get("uuid"),
                "requestId": None,
                "timestamp": _timestamp(),
                "error": message.get("error"),
            },
        )
    if message_type == "user":
        content = message.get("message", {}).get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
        if convert_tool_results and is_tool_result:
            return ConvertedMessage(
                "message",
                {
                    "type": "user",
                    "message": {"content": content},
                    "toolUseResult": message.get("tool_use_result"),
                    "uuid": message.get("uuid"),
                    "timestamp": message.get("timestamp", _timestamp()),
                },
            )
        if convert_user_text_messages and not is_tool_result:
            return ConvertedMessage(
                "message",
                {
                    "type": "user",
                    "message": {"content": content},
                    "toolUseResult": message.get("tool_use_result"),
                    "uuid": message.get("uuid"),
                    "timestamp": message.get("timestamp", _timestamp()),
                },
            )
        return ConvertedMessage("ignored")
    if message_type == "stream_event":
        return ConvertedMessage("stream_event", event=message.get("event"))
    if message_type == "result":
        if message.get("subtype") == "success":
            return ConvertedMessage("ignored")
        errors = message.get("errors") or ["Unknown error"]
        return ConvertedMessage(
            "message",
            _system_message(
                ", ".join(str(item) for item in errors),
                message.get("uuid"),
                level="warning",
            ),
        )
    if message_type == "system":
        subtype = message.get("subtype")
        if subtype == "init":
            return ConvertedMessage(
                "message",
                _system_message(
                    "Remote session initialized (model: {})".format(
                        message.get("model")
                    ),
                    message.get("uuid"),
                ),
            )
        if subtype == "status":
            status = message.get("status")
            if not status:
                return ConvertedMessage("ignored")
            content = (
                "Compacting conversation…"
                if status == "compacting"
                else "Status: {}".format(status)
            )
            return ConvertedMessage(
                "message", _system_message(content, message.get("uuid"))
            )
        if subtype == "compact_boundary":
            return ConvertedMessage(
                "message",
                _system_message(
                    "Conversation compacted",
                    message.get("uuid"),
                    subtype="compact_boundary",
                    compactMetadata=message.get("compact_metadata"),
                ),
            )
        return ConvertedMessage("ignored")
    if message_type == "tool_progress":
        return ConvertedMessage(
            "message",
            _system_message(
                "Tool {} running for {}s…".format(
                    message.get("tool_name"), message.get("elapsed_time_seconds")
                ),
                message.get("uuid"),
                toolUseID=message.get("tool_use_id"),
            ),
        )
    if message_type in {"auth_status", "tool_use_summary"}:
        return ConvertedMessage("ignored")
    return ConvertedMessage("ignored")
