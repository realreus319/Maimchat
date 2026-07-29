"""Message list component for TUI."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Optional

from .error_boundary import render_component_error_lines
from .message_row import MessageRowData, MessageRowRenderer


@dataclass
class MessageListData:
    """Data for message list rendering."""

    messages: List[MessageRowData] = field(default_factory=list)
    visible_range: Optional[tuple] = None
    search_query: str = ""
    current_match_index: int = 0
    total_matches: int = 0


class MessageListRenderer:
    """Renderer for message list component.

    Ported from src/components/Messages.tsx
    """

    def __init__(self, data: MessageListData) -> None:
        self._data = data

    def render(self, width: int, height: int) -> str:
        """Render message list to string."""
        lines: List[str] = []
        query = self._data.search_query.strip()
        active_match_index = self._data.current_match_index if query else None
        running_match_offset = 0

        messages = self._data.messages
        visible_range = self._data.visible_range
        if visible_range is None and query and height > 0:
            visible_range = self._calculate_active_match_visible_range(
                width=width,
                height=height,
                query=query,
                active_match_index=active_match_index,
            )

        if visible_range:
            start, end = visible_range
            if query and start > 0:
                running_match_offset = self._count_matches(
                    self._data.messages[:start],
                    width,
                    query,
                )
            messages = messages[start:end]

        for msg_data in messages:
            row_data = msg_data
            if query:
                local_matches = self._count_message_matches(msg_data, width, query)
                row_data = replace(
                    msg_data,
                    search_query=query,
                    search_match_offset=running_match_offset,
                    active_search_match_index=active_match_index,
                )
                running_match_offset += local_matches
            row_lines = self._render_message_lines(row_data, width)
            for line in row_lines:
                lines.append(line)
                if len(lines) >= height:
                    break
            if len(lines) >= height:
                break

        while len(lines) < height:
            lines.append(" " * width)

        return "\n".join(lines[:height])

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

    def _calculate_active_match_visible_range(
        self,
        *,
        width: int,
        height: int,
        query: str,
        active_match_index: int | None,
    ) -> tuple[int, int] | None:
        target_index = self._find_message_index_for_active_match(
            width=width,
            query=query,
            active_match_index=active_match_index,
        )
        if target_index is None:
            return None

        message_heights = self._measure_message_heights(width)
        start = min(max(target_index, 0), len(message_heights) - 1)
        context_height = 0
        context_budget = max(height // 3, 0)
        while start > 0 and context_height + message_heights[start - 1] <= context_budget:
            start -= 1
            context_height += message_heights[start]

        end = start
        consumed_height = 0
        while end < len(message_heights) and consumed_height < height:
            consumed_height += message_heights[end]
            end += 1

        return (start, end)

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

    def get_visible_count(self) -> int:
        """Get number of visible messages."""
        if self._data.visible_range:
            start, end = self._data.visible_range
            return end - start
        return len(self._data.messages)

    def get_message_at_index(self, index: int) -> Optional[MessageRowData]:
        """Get message data at a given index."""
        if 0 <= index < len(self._data.messages):
            return self._data.messages[index]
        return None


def create_message_list(data: MessageListData) -> MessageListRenderer:
    """Factory function to create a message list renderer."""
    return MessageListRenderer(data)
