from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .serial_batch_event_uploader import SerialBatchEventUploader
from .websocket_transport import WebSocketTransport


BATCH_FLUSH_INTERVAL_MS = 100


@dataclass
class HybridTransport(WebSocketTransport):
    uploader: SerialBatchEventUploader[dict[str, Any]] = field(
        default_factory=lambda: SerialBatchEventUploader(
            max_batch_size=500,
            max_queue_size=100000,
            base_delay_ms=500,
            max_delay_ms=8000,
            jitter_ms=1000,
        )
    )
    stream_event_buffer: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dropped_batch_count(self) -> int:
        return self.uploader.dropped_batch_count

    def write(self, message: dict[str, Any]) -> None:
        if message.get("type") == "stream_event":
            self.stream_event_buffer.append(message)
            return
        batch = self.take_stream_events()
        batch.append(message)
        self.uploader.enqueue(batch)

    def write_batch(self, messages: list[dict[str, Any]]) -> None:
        batch = self.take_stream_events()
        batch.extend(messages)
        self.uploader.enqueue(batch)

    def take_stream_events(self) -> list[dict[str, Any]]:
        buffered = list(self.stream_event_buffer)
        self.stream_event_buffer.clear()
        return buffered

    def flush(self) -> list[dict[str, Any]]:
        batch = self.take_stream_events()
        if batch:
            self.uploader.enqueue(batch)
        return self.uploader.take_batch()

    def close(self) -> None:
        self.stream_event_buffer.clear()
        self.state = "closed"
