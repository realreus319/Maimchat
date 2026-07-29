from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Iterable, List, Optional, TypeVar


T = TypeVar("T")


class RetryableError(Exception):
    def __init__(self, message: str, retry_after_ms: Optional[int] = None) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


@dataclass
class SerialBatchEventUploader(Generic[T]):
    max_batch_size: int
    max_queue_size: int
    base_delay_ms: int
    max_delay_ms: int
    jitter_ms: int
    max_consecutive_failures: Optional[int] = None
    pending: List[T] = field(default_factory=list)
    dropped_batch_count: int = 0
    consecutive_failures: int = 0

    def enqueue(self, events: Iterable[T]) -> None:
        items = list(events)
        if not items:
            return
        if len(self.pending) + len(items) > self.max_queue_size:
            raise ValueError("pending queue would exceed max_queue_size")
        self.pending.extend(items)

    def take_batch(self) -> List[T]:
        batch = self.pending[: self.max_batch_size]
        self.pending = self.pending[self.max_batch_size :]
        return batch

    def requeue_front(self, batch: Iterable[T]) -> None:
        items = list(batch)
        self.pending = items + self.pending

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> bool:
        self.consecutive_failures += 1
        if (
            self.max_consecutive_failures is not None
            and self.consecutive_failures >= self.max_consecutive_failures
        ):
            self.dropped_batch_count += 1
            self.consecutive_failures = 0
            return True
        return False

    def retry_delay(self, retry_after_ms: Optional[int] = None) -> int:
        target = retry_after_ms
        if target is None:
            target = min(
                self.base_delay_ms * (2 ** max(self.consecutive_failures - 1, 0)),
                self.max_delay_ms,
            )
        return max(self.base_delay_ms, min(target, self.max_delay_ms))
