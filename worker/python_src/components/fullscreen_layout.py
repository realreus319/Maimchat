"""Fullscreen layout component for TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..ink.layout.engine import FlexDirection, LayoutNode


@dataclass
class FullscreenLayoutData:
    """Data for fullscreen layout rendering."""

    width: int = 80
    height: int = 24
    message_list_height: int = 20
    status_line_height: int = 1
    prompt_height: int = 3
    show_scrollbar: bool = True
    has_unseen_messages: bool = False
    unseen_count: int = 0


class FullscreenLayoutRenderer:
    """Renderer for fullscreen REPL layout.

    Ported from src/components/FullscreenLayout.tsx
    """

    def __init__(self, data: FullscreenLayoutData) -> None:
        self._data = data

    def create_layout_tree(self) -> LayoutNode:
        """Create the layout tree for fullscreen mode."""
        root = LayoutNode(
            width=float(self._data.width),
            height=float(self._data.height),
            direction=FlexDirection.COLUMN,
        )

        message_area = LayoutNode(
            flex_grow=1.0,
            direction=FlexDirection.COLUMN,
        )
        message_area.children = []
        root.children = [message_area]

        status_line = LayoutNode(
            height=float(self._data.status_line_height),
        )
        status_line.children = []
        root.children.append(status_line)

        prompt_area = LayoutNode(
            height=float(self._data.prompt_height),
        )
        prompt_area.children = []
        root.children.append(prompt_area)

        return root

    def render(self) -> str:
        """Render fullscreen layout to string."""
        lines: List[str] = []

        total_height = self._data.height
        status_height = self._data.status_line_height
        prompt_height = self._data.prompt_height
        message_height = total_height - status_height - prompt_height

        if message_height > 0:
            for _ in range(message_height):
                lines.append(" " * self._data.width)

        if self._data.has_unseen_messages and self._data.unseen_count > 0:
            divider = f"─── {self._data.unseen_count} new ───"
            padding = (self._data.width - len(divider)) // 2
            lines.append(" " * padding + divider)
        else:
            lines.append("-" * self._data.width)

        if status_height > 0:
            lines.append("[Status Line]"[: self._data.width])

        for _ in range(prompt_height):
            lines.append(" " * self._data.width)

        return "\n".join(lines[:total_height])

    def calculate_content_bounds(self) -> Tuple[int, int, int, int]:
        """Calculate the content area bounds (x, y, width, height)."""
        return (0, 0, self._data.width, self._data.message_list_height)

    def should_show_scrollbar(self) -> bool:
        """Check if scrollbar should be shown."""
        return self._data.show_scrollbar


def create_fullscreen_layout(data: FullscreenLayoutData) -> FullscreenLayoutRenderer:
    """Factory function to create a fullscreen layout renderer."""
    return FullscreenLayoutRenderer(data)
