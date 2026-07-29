"""Status line component for TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class StatusLineData:
    """Data for status line rendering."""

    model_id: str = ""
    model_display_name: str = ""
    current_dir: str = ""
    project_dir: str = ""
    added_dirs: Optional[List[str]] = None
    version: str = ""
    output_style: str = "default"
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    total_api_duration_ms: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_window_size: int = 0
    current_usage: int = 0
    used_percentage: float = 0.0
    remaining_percentage: float = 0.0
    exceeds_200k_tokens: bool = False
    permission_mode: str = "default"
    vim_mode: Optional[str] = None
    agent_name: Optional[str] = None
    remote_session_id: Optional[str] = None
    search_query: str = ""
    current_match_index: int = 0
    total_matches: int = 0
    selection_summary: str = ""
    status_line_text: Optional[str] = None


class StatusLineRenderer:
    """Renderer for status line component.

    Ported from src/components/StatusLine.tsx
    """

    def __init__(self, data: StatusLineData) -> None:
        self._data = data

    def render(self, width: int) -> str:
        """Render status line to string."""
        parts: List[str] = []

        if self._data.status_line_text:
            return self._data.status_line_text[:width]

        model_part = f"[{self._data.model_display_name or self._data.model_id}]"
        parts.append(model_part)

        if self._data.permission_mode and self._data.permission_mode != "default":
            parts.append(f"[{self._data.permission_mode}]")

        if self._data.current_dir:
            parts.append(self._data.current_dir)

        tokens_info = (
            f"{self._data.total_input_tokens + self._data.total_output_tokens} tokens"
        )
        parts.append(tokens_info)
        search_query = self._data.search_query.strip()
        if search_query:
            search_label = (
                search_query
                if len(search_query) <= 20
                else f"{search_query[:17]}..."
            )
            if self._data.total_matches > 0:
                parts.append(
                    f"/{search_label} "
                    f"{min(self._data.current_match_index + 1, self._data.total_matches)}"
                    f"/{self._data.total_matches}"
                )
            else:
                parts.append(f"/{search_label} 0/0")

        selection_summary = self._data.selection_summary.strip()
        if selection_summary:
            parts.append(selection_summary)

        result = " | ".join(parts)
        if len(result) > width:
            return result[: width - 3] + "..."
        return result

    def should_display(self, settings_status_line: Optional[str] = None) -> bool:
        """Check if status line should be displayed."""
        return settings_status_line is not None


def create_status_line(data: StatusLineData) -> StatusLineRenderer:
    """Factory function to create a status line renderer."""
    return StatusLineRenderer(data)
