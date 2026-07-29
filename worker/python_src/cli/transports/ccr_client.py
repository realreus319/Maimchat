from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DEFAULT_HEARTBEAT_INTERVAL_MS = 20000
STREAM_EVENT_FLUSH_INTERVAL_MS = 100
MAX_CONSECUTIVE_AUTH_FAILURES = 10


@dataclass
class StreamAccumulatorState:
    by_message: Dict[str, List[List[str]]] = field(default_factory=dict)
    scope_to_message: Dict[str, str] = field(default_factory=dict)


def create_stream_accumulator() -> StreamAccumulatorState:
    return StreamAccumulatorState()


def _scope_key(message: dict[str, Any]) -> str:
    return "{}:{}".format(
        message.get("session_id"), message.get("parent_tool_use_id") or ""
    )


def accumulate_stream_events(
    buffer: List[dict[str, Any]],
    state: StreamAccumulatorState,
) -> List[dict[str, Any]]:
    output: List[dict[str, Any]] = []
    touched: Dict[int, dict[str, Any]] = {}
    for message in buffer:
        event = message.get("event", {})
        event_type = event.get("type")
        if event_type == "message_start":
            message_id = event.get("message", {}).get("id")
            if isinstance(message_id, str):
                state.scope_to_message[_scope_key(message)] = message_id
                state.by_message[message_id] = []
            output.append(message)
            continue
        if (
            event_type == "content_block_delta"
            and event.get("delta", {}).get("type") == "text_delta"
        ):
            message_id = state.scope_to_message.get(_scope_key(message))
            blocks = state.by_message.get(message_id or "")
            if blocks is None:
                output.append(message)
                continue
            index = int(event.get("index", 0))
            while len(blocks) <= index:
                blocks.append([])
            chunks = blocks[index]
            chunks.append(str(event["delta"]["text"]))
            key = id(chunks)
            if key in touched:
                touched[key]["event"]["delta"]["text"] = "".join(chunks)
                continue
            snapshot = {
                "type": "stream_event",
                "uuid": message.get("uuid"),
                "session_id": message.get("session_id"),
                "parent_tool_use_id": message.get("parent_tool_use_id"),
                "event": {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": "".join(chunks)},
                },
            }
            touched[key] = snapshot
            output.append(snapshot)
            continue
        output.append(message)
    return output


def clear_stream_accumulator_for_message(
    state: StreamAccumulatorState,
    assistant: dict[str, Any],
) -> None:
    message_id = assistant.get("message", {}).get("id")
    if not isinstance(message_id, str):
        return
    state.by_message.pop(message_id, None)
    scope = _scope_key(assistant)
    if state.scope_to_message.get(scope) == message_id:
        state.scope_to_message.pop(scope, None)


@dataclass
class CCRClient:
    heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS
    consecutive_auth_failures: int = 0
    current_state: Optional[str] = None
    metadata_updates: List[dict[str, Any]] = field(default_factory=list)
    delivery_updates: List[tuple[str, str]] = field(default_factory=list)

    def classify_auth_failure(
        self, status_code: int, token_expired: bool = False
    ) -> str:
        if status_code not in {401, 403}:
            self.consecutive_auth_failures = 0
            return "ok"
        if token_expired:
            self.consecutive_auth_failures = MAX_CONSECUTIVE_AUTH_FAILURES
            return "expired_token"
        self.consecutive_auth_failures += 1
        if self.consecutive_auth_failures >= MAX_CONSECUTIVE_AUTH_FAILURES:
            return "give_up"
        return "retry"

    def record_success(self) -> None:
        self.consecutive_auth_failures = 0

    def report_state(self, state: str) -> None:
        self.current_state = state

    def report_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata_updates.append(metadata)

    def report_delivery(self, event_id: str, status: str) -> None:
        self.delivery_updates.append((event_id, status))
