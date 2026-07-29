from __future__ import annotations

from threading import Lock


_warning_lock = Lock()
_compact_warning_suppressed = False


def suppress_compact_warning() -> None:
    global _compact_warning_suppressed
    with _warning_lock:
        _compact_warning_suppressed = True


def clear_compact_warning_suppression() -> None:
    global _compact_warning_suppressed
    with _warning_lock:
        _compact_warning_suppressed = False


def is_compact_warning_suppressed() -> bool:
    with _warning_lock:
        return _compact_warning_suppressed
