"""Output buffer for TUI renderer."""

from __future__ import annotations

from typing import List, Optional, Tuple

from .screen import CellWidth, Screen, StylePool
from .terminal import iter_display_segments


class Output:
    """Output buffer that collects write/blit operations.

    Ported from src/ink/output.ts
    """

    def __init__(
        self,
        width: int,
        height: int,
        style_pool: StylePool,
        screen: Screen,
    ) -> None:
        self.width = width
        self.height = height
        self._style_pool = style_pool
        self._screen = screen
        self._current_x = 0
        self._current_y = 0
        self._current_style = style_pool.none

    def reset(self, width: int, height: int, screen: Screen) -> None:
        self.width = width
        self.height = height
        self._screen = screen
        self._current_x = 0
        self._current_y = 0
        self._current_style = self._style_pool.none

    def write(
        self, text: str, style_id: int = 0, hyperlink: Optional[str] = None
    ) -> None:
        """Write text at current cursor position."""
        for segment, segment_width in iter_display_segments(text):
            if segment == "\n":
                self._current_x = 0
                self._current_y += 1
                continue

            if segment_width <= 0:
                continue

            if segment_width > self.width:
                self._current_x = 0
                self._current_y += 1
                continue

            if self._current_y >= self.height:
                break

            if self._current_x + segment_width > self.width:
                self._current_x = 0
                self._current_y += 1
                if self._current_y >= self.height:
                    break

            self._screen.set_cell(
                self._current_x,
                self._current_y,
                segment,
                style_id if style_id != 0 else self._current_style,
                hyperlink,
                CellWidth.WIDE if segment_width == 2 else CellWidth.NARROW,
            )

            if segment_width == 2 and self._current_x + 1 < self.width:
                self._screen.set_cell(
                    self._current_x + 1,
                    self._current_y,
                    "",
                    style_id if style_id != 0 else self._current_style,
                    hyperlink,
                    CellWidth.SPACER_TAIL,
                )

            self._current_x += segment_width
            if self._current_x >= self.width:
                self._current_x = 0
                self._current_y += 1

    def set_cursor(self, x: int, y: int) -> None:
        """Set cursor position."""
        self._current_x = max(0, min(x, self.width - 1))
        self._current_y = max(0, min(y, self.height - 1))

    def get(self) -> Screen:
        """Get the rendered screen."""
        return self._screen
