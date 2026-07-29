from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


RECONNECT_DELAY_MS = 2000
MAX_RECONNECT_ATTEMPTS = 5
MAX_SESSION_NOT_FOUND_RETRIES = 3
PERMANENT_CLOSE_CODES = frozenset({4003})


@dataclass(frozen=True)
class ReconnectDecision:
    should_reconnect: bool
    delay_ms: Optional[int] = None
    close_reason: Optional[str] = None


@dataclass
class SessionsWebSocket:
    session_id: str
    org_uuid: str
    get_access_token: Callable[[], str]
    state: str = "closed"
    reconnect_attempts: int = 0
    session_not_found_retries: int = 0
    outbound_messages: list[dict[str, Any]] = field(default_factory=list)

    def connect(self) -> None:
        self.state = "connected"
        self.reconnect_attempts = 0
        self.session_not_found_retries = 0

    def handle_close(self, close_code: int) -> ReconnectDecision:
        self.state = "closed"
        if close_code in PERMANENT_CLOSE_CODES:
            return ReconnectDecision(False, close_reason="permanent_close")
        if close_code == 4001:
            self.session_not_found_retries += 1
            if self.session_not_found_retries > MAX_SESSION_NOT_FOUND_RETRIES:
                return ReconnectDecision(False, close_reason="session_not_found")
            self.state = "connecting"
            return ReconnectDecision(
                True,
                delay_ms=RECONNECT_DELAY_MS * self.session_not_found_retries,
                close_reason="session_not_found_retry",
            )
        self.reconnect_attempts += 1
        if self.reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
            return ReconnectDecision(False, close_reason="retry_budget_exhausted")
        self.state = "connecting"
        return ReconnectDecision(
            True, delay_ms=RECONNECT_DELAY_MS, close_reason="transient"
        )

    def send_control_response(self, response: dict[str, Any]) -> None:
        self.outbound_messages.append(response)

    def send_control_cancel_request(self, request_id: str) -> None:
        self.outbound_messages.append(
            {"type": "control_cancel_request", "request_id": request_id}
        )

    def send_message(self, message: dict[str, Any]) -> None:
        self.outbound_messages.append(message)
