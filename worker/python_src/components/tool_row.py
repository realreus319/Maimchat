"""Structured tool progress row component for TUI."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .highlighted_code import (
    ANSI_RED,
    get_highlight_palette,
    normalize_highlight_theme,
    wrap_ansi,
)
from .progress_bar import ProgressBarData, ProgressBarRenderer
from .timer import TimerData, TimerRenderer


@dataclass
class ToolRowData:
    """Data for structured tool progress rows."""

    title: str | None = None
    content: str = ""
    status_text: str | None = None
    detail_lines: tuple[str, ...] = ()
    progress_fraction: float | None = None
    elapsed_ms: int | None = None
    progress_frame_index: int = 0
    theme: str = "dark"


class ToolRowRenderer:
    """Renderer for spinner/timer/progress/detail tool rows."""

    def __init__(self, data: ToolRowData) -> None:
        self._data = data

    def render(self, width: int, *, prefix: str = "") -> str:
        return "\n".join(self.render_lines(width, prefix=prefix))

    def render_lines(self, width: int, *, prefix: str = "") -> list[str]:
        normalized_theme = normalize_highlight_theme(self._data.theme)
        palette = get_highlight_palette(normalized_theme)
        continuation_prefix = " " * len(prefix)
        title = (self._data.title or self._data.content or "Progress").strip()
        status = (self._data.status_text or "").strip()
        spinner = _spinner_frame(self._data.progress_frame_index)
        header_parts = [spinner, title]
        if status:
            header_parts.append(status)
        if self._data.elapsed_ms is not None:
            timer = TimerRenderer(TimerData(elapsed_ms=self._data.elapsed_ms)).render()
            header_parts.append(timer)
        header_plain = " · ".join(part for part in header_parts if part)
        available_width = max(width - len(prefix), 1)
        styled_header = wrap_ansi(
            _truncate_plain_text(header_plain, available_width),
            *palette["command"],
        )
        rendered = [f"{prefix}{styled_header}"]

        bar_width = min(max(available_width, 10), 28)
        bar = ProgressBarRenderer(
            ProgressBarData(
                width=bar_width,
                fraction=self._data.progress_fraction,
                frame_index=self._data.progress_frame_index,
            )
        ).render()
        rendered.append(f"{continuation_prefix}{wrap_ansi(bar, *palette['label'])}")

        for detail in self._data.detail_lines:
            if not detail.strip():
                continue
            is_error = detail.lstrip().startswith("stderr:")
            codes = (ANSI_RED,) if is_error else palette["quote"]
            for line in _wrap_prefixed(detail, available_width, "│ "):
                rendered.append(
                    f"{continuation_prefix}{wrap_ansi(_truncate_plain_text(line, available_width), *codes)}"
                )
        return rendered or [prefix.rstrip()]


def create_tool_row(data: ToolRowData) -> ToolRowRenderer:
    """Factory function to create a tool row renderer."""

    return ToolRowRenderer(data)


def _truncate_plain_text(content: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(content) <= width:
        return content
    if width <= 3:
        return content[:width]
    return content[: width - 3] + "..."


def _wrap_prefixed(text: str, width: int, prefix: str) -> list[str]:
    available_width = max(width - len(prefix), 1)
    wrapped = textwrap.wrap(
        text,
        width=available_width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    return [
        f"{prefix}{line}" if index == 0 else f"{' ' * len(prefix)}{line}"
        for index, line in enumerate(wrapped)
    ]


def _spinner_frame(frame_index: int) -> str:
    frames = ("|", "/", "-", "\\")
    if frame_index < 0:
        return frames[0]
    return frames[frame_index % len(frames)]
