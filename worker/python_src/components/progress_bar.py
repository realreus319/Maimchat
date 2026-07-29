"""Progress bar component for TUI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProgressBarData:
    """Data for progress bar rendering."""

    width: int = 20
    fraction: float | None = None
    frame_index: int = 0
    fill_char: str = "="
    empty_char: str = "-"
    head_char: str = ">"


class ProgressBarRenderer:
    """Renderer for determinate and indeterminate progress bars."""

    def __init__(self, data: ProgressBarData) -> None:
        self._data = data

    def render(self) -> str:
        width = max(self._data.width, 3)
        inner_width = max(width - 2, 1)
        fraction = self._data.fraction
        if fraction is not None:
            normalized = max(0.0, min(float(fraction), 1.0))
            filled = min(inner_width, int(round(normalized * inner_width)))
            body = (
                (self._data.fill_char * filled)
                + (self._data.empty_char * (inner_width - filled))
            )[:inner_width]
            return f"[{body}]"

        segment_width = max(1, min(inner_width, max(2, inner_width // 4)))
        max_start = max(inner_width - segment_width, 0)
        start = self._data.frame_index % (max_start + 1 if max_start else 1)
        cells = [self._data.empty_char] * inner_width
        for index in range(start, min(start + segment_width, inner_width)):
            cells[index] = self._data.fill_char
        head = min(start + segment_width - 1, inner_width - 1)
        if cells:
            cells[head] = self._data.head_char
        return f"[{''.join(cells)}]"


def create_progress_bar(data: ProgressBarData) -> ProgressBarRenderer:
    """Factory function to create a progress bar renderer."""

    return ProgressBarRenderer(data)
