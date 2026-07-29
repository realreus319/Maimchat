"""Virtual message list component for TUI."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Optional, Tuple

from .error_boundary import render_component_error_lines
from .message_row import MessageRowData, MessageRowRenderer


@dataclass
class VirtualMessageListData:
    """Data for virtual message list rendering."""

    messages: List[MessageRowData] = field(default_factory=list)
    viewport_height: int = 24
    scroll_offset: int = 0
    sticky_prompt_height: int = 0
    follow_output: bool = True
    search_query: str = ""
    current_match_index: int = 0
    total_matches: int = 0
    total_height: int = 0


class VirtualMessageListRenderer:
    """Renderer for virtual message list with virtualization.

    Ported from src/components/VirtualMessageList.tsx
    """

    def __init__(self, data: VirtualMessageListData) -> None:
        self._data = data
        self._visible_range: Tuple[int, int] = (0, 0)

    def render(self, width: int, height: int) -> str:
        """Render virtual message list to string."""
        lines: List[str] = []
        query = self._data.search_query.strip()
        active_match_index = self._data.current_match_index if query else None
        render_height = max(height, 0)
        viewport_height = self._effective_viewport_height(render_height)
        active_match_message_index = (
            self._find_message_index_for_active_match(
                width=width,
                query=query,
                active_match_index=active_match_index,
            )
            if query
            else None
        )
        follow_tail = self._should_follow_tail() and active_match_message_index is None

        start_idx, end_idx = self._calculate_visible_range(
            width=width,
            height=render_height,
            active_match_message_index=active_match_message_index,
        )
        self._visible_range = (start_idx, end_idx)
        running_match_offset = (
            self._count_matches(self._data.messages[:start_idx], width, query)
            if query and start_idx > 0
            else 0
        )

        rendered_lines: List[str] = []
        for i in range(start_idx, min(end_idx, len(self._data.messages))):
            msg = self._data.messages[i]
            row_data = msg
            if query:
                local_matches = self._count_message_matches(msg, width, query)
                row_data = replace(
                    msg,
                    search_query=query,
                    search_match_offset=running_match_offset,
                    active_search_match_index=active_match_index,
                )
                running_match_offset += local_matches
            row_lines = self._render_message_lines(row_data, width)
            rendered_lines.extend(row_lines)
            if not follow_tail and len(rendered_lines) >= viewport_height:
                break

        if follow_tail and viewport_height > 0:
            lines.extend(rendered_lines[-viewport_height:])
        else:
            lines.extend(rendered_lines[:viewport_height])

        while len(lines) < render_height:
            lines.append(" " * width)

        return "\n".join(lines[:render_height])

    def _effective_viewport_height(self, height: int | None = None) -> int:
        requested_height = self._data.viewport_height if height is None else max(height, 0)
        visible_height = min(self._data.viewport_height, requested_height)
        return max(visible_height - max(self._data.sticky_prompt_height, 0), 0)

    def _should_follow_tail(self) -> bool:
        return bool(self._data.follow_output and self._data.scroll_offset <= 0)

    def _count_matches(
        self,
        messages: List[MessageRowData],
        width: int,
        query: str,
    ) -> int:
        return sum(
            self._count_message_matches(message, width, query)
            for message in messages
        )

    def _count_message_matches(
        self,
        message: MessageRowData,
        width: int,
        query: str,
    ) -> int:
        try:
            return MessageRowRenderer(
                replace(
                    message,
                    search_query="",
                    search_match_offset=0,
                    active_search_match_index=None,
                )
            ).count_search_matches(width, query=query)
        except Exception:
            return 0

    def _render_message_lines(
        self,
        message: MessageRowData,
        width: int,
    ) -> list[str]:
        try:
            return MessageRowRenderer(message).render_lines(width)
        except Exception as exc:
            return render_component_error_lines(
                component="MessageRow",
                error=exc,
                width=width,
            )

    def _measure_message_heights(self, width: int) -> tuple[int, ...]:
        return tuple(
            max(len(self._render_message_lines(message, width)), 1)
            for message in self._data.messages
        )

    def _find_message_index_for_active_match(
        self,
        *,
        width: int,
        query: str,
        active_match_index: int | None,
    ) -> int | None:
        if active_match_index is None or active_match_index < 0:
            return None
        running_match_offset = 0
        for index, message in enumerate(self._data.messages):
            local_matches = self._count_message_matches(message, width, query)
            if local_matches <= 0:
                continue
            if running_match_offset <= active_match_index < (
                running_match_offset + local_matches
            ):
                return index
            running_match_offset += local_matches
        return None

    def _calculate_range_for_anchor(
        self,
        *,
        anchor_index: int,
        message_heights: tuple[int, ...],
        viewport_height: int,
    ) -> tuple[int, int]:
        if not message_heights:
            return (0, 0)
        start = min(max(anchor_index, 0), len(message_heights) - 1)
        context_height = 0
        context_budget = max(viewport_height // 3, 0)
        while start > 0 and context_height + message_heights[start - 1] <= context_budget:
            start -= 1
            context_height += message_heights[start]

        end = start
        consumed_height = 0
        while end < len(message_heights) and consumed_height < viewport_height:
            consumed_height += message_heights[end]
            end += 1

        return (start, end)

    def _calculate_visible_range(
        self,
        *,
        width: int = 80,
        height: int | None = None,
        active_match_message_index: int | None = None,
    ) -> Tuple[int, int]:
        """Calculate the range of visible messages based on scroll."""
        messages = self._data.messages
        viewport_height = self._effective_viewport_height(height)
        scroll_offset = self._data.scroll_offset
        message_heights = self._measure_message_heights(width) if messages else ()
        self._data.total_height = sum(message_heights)

        if not messages:
            return (scroll_offset, scroll_offset)
        if viewport_height <= 0:
            start = min(max(scroll_offset, 0), max(len(messages) - 1, 0))
            return (start, start)

        if active_match_message_index is not None:
            return self._calculate_range_for_anchor(
                anchor_index=active_match_message_index,
                message_heights=message_heights,
                viewport_height=viewport_height,
            )

        if self._should_follow_tail():
            start = len(messages)
            consumed_height = 0
            while start > 0 and consumed_height < viewport_height:
                start -= 1
                consumed_height += message_heights[start]
            return (start, len(messages))

        start = min(max(scroll_offset, 0), len(messages) - 1)
        end = start
        consumed_height = 0
        while end < len(messages) and consumed_height < viewport_height:
            consumed_height += message_heights[end]
            end += 1

        return (start, end)

    def scroll_to(self, index: int) -> None:
        """Scroll to a specific message index."""
        self._data.follow_output = False
        self._data.scroll_offset = max(0, index)

    def scroll_by(self, delta: int) -> None:
        """Scroll by a delta amount."""
        anchor = (
            max(len(self._data.messages) - 1, 0)
            if self._should_follow_tail()
            else self._data.scroll_offset
        )
        self._data.follow_output = False
        self._data.scroll_offset = max(0, anchor + delta)

    def get_visible_range(self) -> Tuple[int, int]:
        """Get the currently visible message range."""
        return self._visible_range


def create_virtual_message_list(
    data: VirtualMessageListData,
) -> VirtualMessageListRenderer:
    """Factory function to create a virtual message list renderer."""
    return VirtualMessageListRenderer(data)
