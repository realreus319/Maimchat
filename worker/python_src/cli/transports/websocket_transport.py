from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


DEFAULT_BASE_RECONNECT_DELAY = 1000
DEFAULT_MAX_RECONNECT_DELAY = 30000
DEFAULT_RECONNECT_GIVE_UP_MS = 600000
SLEEP_DETECTION_THRESHOLD_MS = DEFAULT_MAX_RECONNECT_DELAY * 2
PERMANENT_CLOSE_CODES = frozenset({1002, 4001, 4003})


@dataclass(frozen=True)
class TransportDecision:
    should_reconnect: bool
    delay_ms: Optional[int]
    state: str
    reason: str


@dataclass
class WebSocketTransport:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    session_id: Optional[str] = None
    auto_reconnect: bool = True
    state: str = "idle"
    reconnect_attempts: int = 0
    reconnect_start_time_ms: Optional[int] = None
    last_reconnect_attempt_time_ms: Optional[int] = None
    last_sent_id: Optional[str] = None
    buffered_messages: list[dict[str, Any]] = field(default_factory=list)

    def is_connected_status(self) -> bool:
        return self.state == "connected"

    def get_state_label(self) -> str:
        return self.state

    def connect(self) -> None:
        self.state = "connected"
        self.reconnect_attempts = 0
        self.reconnect_start_time_ms = None
        self.last_reconnect_attempt_time_ms = None

    def write(self, message: dict[str, Any]) -> None:
        self.buffered_messages.append(message)
        if isinstance(message.get("uuid"), str):
            self.last_sent_id = str(message["uuid"])

    def handle_disconnect(self, close_code: int, now_ms: int) -> TransportDecision:
        if close_code in PERMANENT_CLOSE_CODES or not self.auto_reconnect:
            self.state = "closed"
            return TransportDecision(False, None, self.state, "permanent_close")
        if (
            self.last_reconnect_attempt_time_ms is not None
            and now_ms - self.last_reconnect_attempt_time_ms
            > SLEEP_DETECTION_THRESHOLD_MS
        ):
            self.reconnect_attempts = 0
            self.reconnect_start_time_ms = now_ms
        if self.reconnect_start_time_ms is None:
            self.reconnect_start_time_ms = now_ms
        elif now_ms - self.reconnect_start_time_ms > DEFAULT_RECONNECT_GIVE_UP_MS:
            self.state = "closed"
            return TransportDecision(
                False, None, self.state, "reconnect_budget_exhausted"
            )
        self.reconnect_attempts += 1
        self.last_reconnect_attempt_time_ms = now_ms
        self.state = "reconnecting"
        delay = min(
            DEFAULT_BASE_RECONNECT_DELAY * (2 ** (self.reconnect_attempts - 1)),
            DEFAULT_MAX_RECONNECT_DELAY,
        )
        return TransportDecision(True, delay, self.state, "transient_close")
