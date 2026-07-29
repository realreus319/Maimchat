from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


def coalesce_patches(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in {"external_metadata", "internal_metadata"}
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


@dataclass
class WorkerStateUploader:
    base_delay_ms: int = 500
    max_delay_ms: int = 8000
    jitter_ms: int = 1000
    pending: Optional[Dict[str, Any]] = None
    inflight: Optional[Dict[str, Any]] = None
    closed: bool = False
    failures: int = 0

    def enqueue(self, patch: Dict[str, Any]) -> None:
        if self.closed:
            return
        self.pending = (
            patch if self.pending is None else coalesce_patches(self.pending, patch)
        )

    def start_send(self) -> Optional[Dict[str, Any]]:
        if self.closed or self.pending is None:
            return None
        self.inflight = self.pending
        self.pending = None
        return self.inflight

    def mark_result(self, ok: bool) -> Optional[int]:
        if ok:
            self.inflight = None
            self.failures = 0
            return None
        self.failures += 1
        if self.pending is not None and self.inflight is not None:
            self.inflight = coalesce_patches(self.inflight, self.pending)
            self.pending = None
        exponential = min(
            self.base_delay_ms * (2 ** (self.failures - 1)), self.max_delay_ms
        )
        return exponential

    def close(self) -> None:
        self.closed = True
        self.pending = None
