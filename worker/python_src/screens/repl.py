"""REPL screen for TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..components.error_boundary import render_component_error_lines
from ..components.fullscreen_layout import (
    FullscreenLayoutData,
    FullscreenLayoutRenderer,
)
from ..components.message_row import MessageRowData
from ..components.messages import MessageListData, MessageListRenderer
from ..components.status_line import StatusLineData, StatusLineRenderer
from ..components.virtual_message_list import (
    VirtualMessageListData,
    VirtualMessageListRenderer,
)


@dataclass
class REPLScreenData:
    """Data for REPL screen rendering."""

    width: int = 80
    height: int = 24
    messages: List[MessageRowData] = field(default_factory=list)
    status_line: Optional[StatusLineData] = None
    fullscreen: bool = True
    show_virtual_list: bool = True
    search_query: str = ""
    current_match_index: int = 0
    total_matches: int = 0
    scroll_offset: int = 0
    follow_output: bool = True


class REPLRenderer:
    """Renderer for REPL screen.

    Ported from src/screens/REPL.tsx
    """

    def __init__(self, data: REPLScreenData) -> None:
        self._data = data

    def render(self) -> str:
        """Render REPL screen to string."""
        if self._data.fullscreen:
            return self._render_fullscreen()
        else:
            return self._render_standard()

    def _render_fullscreen(self) -> str:
        """Render in fullscreen mode."""
        if self._data.show_virtual_list and (
            len(self._data.messages) > 50 or not self._data.follow_output
        ):
            return self._render_virtual()

        layout_data = FullscreenLayoutData(
            width=self._data.width,
            height=self._data.height,
        )
        layout = FullscreenLayoutRenderer(layout_data)

        message_list_data = MessageListData(
            messages=self._data.messages,
            search_query=self._data.search_query,
            current_match_index=self._data.current_match_index,
            total_matches=self._data.total_matches,
        )
        message_list = MessageListRenderer(message_list_data)

        lines: List[str] = []

        msg_height = self._data.height - 4
        if msg_height > 0:
            try:
                msg_content = message_list.render(self._data.width, msg_height)
            except Exception as exc:
                msg_content = self._render_component_fallback(
                    component="MessageList",
                    error=exc,
                    height=msg_height,
                )
            lines.append(msg_content)

        if self._data.status_line:
            status = StatusLineRenderer(self._data.status_line)
            try:
                status_line = status.render(self._data.width)
            except Exception as exc:
                status_line = self._render_component_fallback(
                    component="StatusLine",
                    error=exc,
                    height=1,
                )
            lines.append("-" * self._data.width)
            lines.append(status_line)

        return "\n".join(lines)

    def _render_virtual(self) -> str:
        """Render with virtual message list."""
        virtual_data = VirtualMessageListData(
            messages=self._data.messages,
            viewport_height=self._data.height - 4,
            scroll_offset=self._data.scroll_offset,
            follow_output=self._data.follow_output,
            search_query=self._data.search_query,
            current_match_index=self._data.current_match_index,
            total_matches=self._data.total_matches,
        )
        virtual = VirtualMessageListRenderer(virtual_data)

        try:
            content = virtual.render(self._data.width, self._data.height - 4)
        except Exception as exc:
            content = self._render_component_fallback(
                component="VirtualMessageList",
                error=exc,
                height=max(self._data.height - 4, 1),
            )

        lines: List[str] = [content]

        if self._data.status_line:
            status = StatusLineRenderer(self._data.status_line)
            try:
                status_line = status.render(self._data.width)
            except Exception as exc:
                status_line = self._render_component_fallback(
                    component="StatusLine",
                    error=exc,
                    height=1,
                )
            lines.append("-" * self._data.width)
            lines.append(status_line)

        return "\n".join(lines)

    def _render_standard(self) -> str:
        """Render in standard (non-fullscreen) mode."""
        message_list_data = MessageListData(
            messages=self._data.messages,
            search_query=self._data.search_query,
            current_match_index=self._data.current_match_index,
            total_matches=self._data.total_matches,
        )
        message_list = MessageListRenderer(message_list_data)

        lines: List[str] = []

        try:
            content = message_list.render(self._data.width, self._data.height - 2)
        except Exception as exc:
            content = self._render_component_fallback(
                component="MessageList",
                error=exc,
                height=max(self._data.height - 2, 1),
            )
        lines.append(content)

        if self._data.status_line:
            status = StatusLineRenderer(self._data.status_line)
            try:
                status_line = status.render(self._data.width)
            except Exception as exc:
                status_line = self._render_component_fallback(
                    component="StatusLine",
                    error=exc,
                    height=1,
                )
            lines.append("-" * self._data.width)
            lines.append(status_line)

        return "\n".join(lines)

    def get_message_count(self) -> int:
        """Get the total message count."""
        return len(self._data.messages)

    def _render_component_fallback(
        self,
        *,
        component: str,
        error: BaseException,
        height: int,
    ) -> str:
        lines = render_component_error_lines(
            component=component,
            error=error,
            width=self._data.width,
            max_lines=height,
        )
        padded = lines[:height]
        while len(padded) < height:
            padded.append(" " * self._data.width)
        return "\n".join(padded[:height])


def create_repl_screen(data: REPLScreenData) -> REPLRenderer:
    """Factory function to create a REPL screen renderer."""
    return REPLRenderer(data)
