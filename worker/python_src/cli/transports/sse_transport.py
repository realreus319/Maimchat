from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


RECONNECT_BASE_DELAY_MS = 1000
RECONNECT_MAX_DELAY_MS = 30000
RECONNECT_GIVE_UP_MS = 600000
LIVENESS_TIMEOUT_MS = 45000
PERMANENT_HTTP_CODES = frozenset({401, 403, 404})
POST_MAX_RETRIES = 10


def parse_sse_frames(buffer: str) -> dict[str, Any]:
    frames = []
    position = 0
    while True:
        idx = buffer.find("\n\n", position)
        if idx == -1:
            break
        raw_frame = buffer[position:idx]
        position = idx + 2
        if not raw_frame.strip():
            continue
        frame = {}
        is_comment = False
        for line in raw_frame.split("\n"):
            if line.startswith(":"):
                is_comment = True
                continue
            if ":" not in line:
                continue
            field, raw_value = line.split(":", 1)
            value = raw_value[1:] if raw_value.startswith(" ") else raw_value
            if field == "data":
                frame["data"] = (
                    frame["data"] + "\n" + value if "data" in frame else value
                )
            elif field in {"event", "id"}:
                frame[field] = value
        if frame or is_comment:
            frames.append(frame)
    return {"frames": frames, "remaining": buffer[position:]}


@dataclass(frozen=True)
class SSEDecision:
    should_reconnect: bool
    delay_ms: Optional[int]
    reason: str


@dataclass
class SSETransport:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    session_id: Optional[str] = None
    last_sequence_num: int = 0
    reconnect_attempts: int = 0
    reconnect_start_time_ms: Optional[int] = None
    state: str = "idle"

    def get_last_sequence_num(self) -> int:
        return self.last_sequence_num

    def record_event(self, sequence_num: int) -> bool:
        if sequence_num <= self.last_sequence_num:
            return False
        self.last_sequence_num = sequence_num
        return True

    def handle_disconnect(self, status_code: Optional[int], now_ms: int) -> SSEDecision:
        if status_code in PERMANENT_HTTP_CODES:
            self.state = "closed"
            return SSEDecision(False, None, "permanent_http_rejection")
        if self.reconnect_start_time_ms is None:
            self.reconnect_start_time_ms = now_ms
        elif now_ms - self.reconnect_start_time_ms > RECONNECT_GIVE_UP_MS:
            self.state = "closed"
            return SSEDecision(False, None, "reconnect_budget_exhausted")
        self.reconnect_attempts += 1
        self.state = "reconnecting"
        delay = min(
            RECONNECT_BASE_DELAY_MS * (2 ** (self.reconnect_attempts - 1)),
            RECONNECT_MAX_DELAY_MS,
        )
        return SSEDecision(True, delay, "transient_disconnect")

    def classify_post_failure(self, status_code: Optional[int], attempt: int) -> str:
        if status_code is not None and status_code in PERMANENT_HTTP_CODES:
            return "permanent"
        if (
            status_code is not None
            and status_code >= 400
            and status_code < 500
            and status_code != 429
        ):
            return "permanent"
        if attempt >= POST_MAX_RETRIES:
            return "give_up"
        return "retry"

    def is_liveness_timeout(self, silence_ms: int) -> bool:
        return silence_ms >= LIVENESS_TIMEOUT_MS
