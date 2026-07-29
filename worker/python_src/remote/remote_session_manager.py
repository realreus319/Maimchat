from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .session_websocket import SessionsWebSocket


@dataclass(frozen=True)
class RemoteSessionConfig:
    session_id: str
    get_access_token: Callable[[], str]
    org_uuid: str
    has_initial_prompt: bool = False
    viewer_only: bool = False


@dataclass(frozen=True)
class RemoteSessionEvent:
    kind: str
    payload: Optional[dict[str, Any]] = None


@dataclass
class RemoteSessionManager:
    config: RemoteSessionConfig
    websocket: SessionsWebSocket = field(init=False)
    pending_permission_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    received_messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.websocket = SessionsWebSocket(
            self.config.session_id,
            self.config.org_uuid,
            self.config.get_access_token,
        )

    @property
    def pending_request_count(self) -> int:
        return len(self.pending_permission_requests)

    def connect(self) -> None:
        self.websocket.connect()

    def handle_message(self, message: dict[str, Any]) -> RemoteSessionEvent:
        message_type = message.get("type")
        if message_type == "control_request":
            return self._handle_control_request(message)
        if message_type == "control_cancel_request":
            request_id = str(message.get("request_id", ""))
            pending = self.pending_permission_requests.pop(request_id, None)
            return RemoteSessionEvent(
                "permission_cancelled",
                {
                    "request_id": request_id,
                    "tool_use_id": pending.get("tool_use_id") if pending else None,
                },
            )
        if message_type == "control_response":
            return RemoteSessionEvent("ack", message)
        self.received_messages.append(message)
        return RemoteSessionEvent("message", message)

    def _handle_control_request(self, request: dict[str, Any]) -> RemoteSessionEvent:
        request_id = str(request.get("request_id", ""))
        inner = request.get("request")
        if not isinstance(inner, dict):
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": "Malformed control request",
                },
            }
            self.websocket.send_control_response(response)
            return RemoteSessionEvent("unsupported_control_request", response)
        if inner.get("subtype") == "can_use_tool":
            self.pending_permission_requests[request_id] = inner
            return RemoteSessionEvent(
                "permission_request",
                {"request_id": request_id, "request": inner},
            )
        response = {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": "Unsupported control request subtype: {}".format(
                    inner.get("subtype")
                ),
            },
        }
        self.websocket.send_control_response(response)
        return RemoteSessionEvent("unsupported_control_request", response)

    def respond_to_permission_request(
        self,
        request_id: str,
        result: dict[str, Any],
    ) -> bool:
        pending = self.pending_permission_requests.pop(request_id, None)
        if pending is None:
            return False
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": result,
            },
        }
        self.websocket.send_control_response(response)
        return True

    def send_message(self, content: dict[str, Any], uuid: Optional[str] = None) -> bool:
        payload = {"type": "user", "content": content}
        if uuid is not None:
            payload["uuid"] = uuid
        self.websocket.send_message(payload)
        return True
