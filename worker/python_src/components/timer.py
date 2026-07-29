"""Timer component for TUI."""

from __future__ import annotations

from dataclasses import dataclass


def format_duration(duration_ms: int | float | None) -> str:
    """Format elapsed milliseconds using compact terminal-friendly units."""

    if duration_ms is None:
        return "0ms"
    normalized = max(int(duration_ms), 0)
    if normalized < 1000:
        return f"{normalized}ms"
    if normalized < 10_000:
        value = normalized / 1000.0
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}s"
    total_seconds = normalized // 1000
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass
class TimerData:
    """Data for timer rendering."""

    elapsed_ms: int | float | None = None


class TimerRenderer:
    """Renderer for elapsed timer labels."""

    def __init__(self, data: TimerData) -> None:
        self._data = data

    def render(self) -> str:
        return format_duration(self._data.elapsed_ms)


def create_timer(data: TimerData) -> TimerRenderer:
    """Factory function to create a timer renderer."""

    return TimerRenderer(data)
