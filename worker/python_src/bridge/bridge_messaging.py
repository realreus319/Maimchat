from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Mapping, Optional


def _normalize_control_message_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_control_message_keys(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {}
    for key, item in value.items():
        if key == "requestId":
            key = "request_id"
        normalized[key] = _normalize_control_message_keys(item)
    return normalized


def is_sdk_message(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("type"), str)


def is_sdk_control_response(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("type") == "control_response"
        and "response" in value
    )


def is_sdk_control_request(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("type") == "control_request"
        and "request" in value
        and "request_id" in value
    )


def is_eligible_bridge_message(message: Mapping[str, Any]) -> bool:
    message_type = message.get("type")
    if message_type in {"user", "assistant"} and bool(message.get("isVirtual")):
        return False
    return message_type in {"user", "assistant"} or (
        message_type == "system" and message.get("subtype") == "local_command"
    )


def _strip_display_tags(text: str) -> str:
    out = []
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
            continue
        if char == ">" and depth > 0:
            depth -= 1
            continue
        if depth == 0:
            out.append(char)
    return "".join(out).strip()


def extract_title_text(message: Mapping[str, Any]) -> Optional[str]:
    if message.get("type") != "user":
        return None
    if (
        message.get("isMeta")
        or message.get("toolUseResult")
        or message.get("isCompactSummary")
    ):
        return None
    origin = message.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = message.get("message", {}).get("content")
    raw = None
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                raw = block.get("text")
                break
    if not isinstance(raw, str) or not raw:
        return None
    cleaned = _strip_display_tags(raw)
    return cleaned or None


class BoundedUUIDSet:
    def __init__(self, capacity: int = 256) -> None:
        self._capacity = max(1, capacity)
        self._items: Deque[str] = deque()
        self._seen: set[str] = set()

    def add(self, uuid: str) -> None:
        if uuid in self._seen:
            return
        self._items.append(uuid)
        self._seen.add(uuid)
        while len(self._items) > self._capacity:
            removed = self._items.popleft()
            self._seen.discard(removed)

    def has(self, uuid: str) -> bool:
        return uuid in self._seen


@dataclass(frozen=True)
class IngressDispatch:
    kind: str
    payload: Optional[Mapping[str, Any]] = None


def handle_ingress_message(
    data: str,
    recent_posted_uuids: BoundedUUIDSet,
    recent_inbound_uuids: BoundedUUIDSet,
) -> IngressDispatch:
    try:
        parsed = _normalize_control_message_keys(json.loads(data))
    except json.JSONDecodeError:
        return IngressDispatch(kind="ignored")

    if is_sdk_control_response(parsed):
        return IngressDispatch(kind="permission_response", payload=parsed)
    if is_sdk_control_request(parsed):
        return IngressDispatch(kind="control_request", payload=parsed)
    if not is_sdk_message(parsed):
        return IngressDispatch(kind="ignored")

    uuid = parsed.get("uuid") if isinstance(parsed.get("uuid"), str) else None
    if uuid and recent_posted_uuids.has(uuid):
        return IngressDispatch(kind="ignored")
    if uuid and recent_inbound_uuids.has(uuid):
        return IngressDispatch(kind="ignored")
    if parsed.get("type") != "user":
        return IngressDispatch(kind="ignored")
    if uuid:
        recent_inbound_uuids.add(uuid)
    return IngressDispatch(kind="inbound", payload=parsed)
