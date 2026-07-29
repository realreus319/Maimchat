"""Helpers for lightweight TUI render error boundaries."""

from __future__ import annotations

import textwrap

from ..utils.error_display import (
    build_structured_error_display,
    format_compact_structured_error_text,
)


def render_component_error_lines(
    *,
    component: str,
    error: BaseException,
    width: int,
    max_lines: int = 4,
) -> list[str]:
    resolved_width = max(width, 1)
    resolved_max_lines = max(max_lines, 1)
    display = build_structured_error_display(
        error,
        title="Render Error",
        code="render_error",
        stage=component,
        log_error=False,
    )
    lines: list[str] = []
    for raw_line in format_compact_structured_error_text(display).splitlines():
        wrapped = textwrap.wrap(
            raw_line,
            width=resolved_width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        if wrapped:
            lines.extend(segment[:resolved_width] for segment in wrapped)
        else:
            lines.append("")
    if not lines:
        return ["Error: Render Error"]
    if len(lines) <= resolved_max_lines:
        return lines
    truncated = lines[:resolved_max_lines]
    last_line = truncated[-1]
    if resolved_width <= 3:
        truncated[-1] = "." * resolved_width
    elif len(last_line) >= resolved_width:
        truncated[-1] = last_line[: resolved_width - 3] + "..."
    else:
        truncated[-1] = f"{last_line[: max(resolved_width - 3, 0)]}..."
    return truncated


__all__ = ["render_component_error_lines"]
