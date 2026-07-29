from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..ink.terminal import iter_display_segments, string_width


_CURSOR_MARKER = "_"
_PROMPT_PREFIX = "> "
_PROMPT_CONTINUATION_PREFIX = "  "


@dataclass
class PromptInputState:
    input_text: str = ""
    cursor_offset: int = 0
    mode: str = "default"
    submit_count: int = 0
    is_loading: bool = False
    help_open: bool = False
    history_entries: List[str] = field(default_factory=list)
    history_index: Optional[int] = None
    history_draft: str = ""


@dataclass
class PromptInputConfig:
    min_viewport_lines: int = 3
    footer_lines: int = 5
    max_height_percent: float = 0.5


@dataclass
class PromptInputData:
    state: PromptInputState = field(default_factory=PromptInputState)
    config: PromptInputConfig = field(default_factory=PromptInputConfig)
    placeholder: str = ""
    suggestions: List[str] = field(default_factory=list)
    show_footer: bool = True


class PromptInputRenderer:
    def __init__(self, data: PromptInputData) -> None:
        self._data = data

    def render(self, width: int) -> str:
        lines: List[str] = []

        input_line = self._render_input_line(width)
        lines.append(input_line)

        if self._data.show_footer:
            footer = self._render_footer(width)
            if footer:
                lines.append(footer)

        return "\n".join(lines)

    def _render_input_line(self, width: int) -> str:
        text = self._data.state.input_text
        cursor = self._data.state.cursor_offset
        cursor = max(0, min(cursor, len(text)))

        marked_text = text[:cursor] + _CURSOR_MARKER + text[cursor:]
        return "\n".join(
            _wrap_display_text(
                marked_text,
                width,
                first_prefix=_PROMPT_PREFIX,
                continuation_prefix=_PROMPT_CONTINUATION_PREFIX,
            )
        )

    def _render_footer(self, width: int) -> str:
        if not self._data.suggestions:
            return ""

        hint = " | ".join(self._data.suggestions[:3])
        return _truncate_display_text(hint, width)

    def handle_input(self, text: str) -> None:
        self._reset_history_navigation()
        pos = self._data.state.cursor_offset
        self._data.state.input_text = (
            self._data.state.input_text[:pos] + text + self._data.state.input_text[pos:]
        )
        self._data.state.cursor_offset = pos + len(text)

    def handle_backspace(self) -> None:
        if self._data.state.cursor_offset > 0:
            self._reset_history_navigation()
            pos = self._data.state.cursor_offset
            self._data.state.input_text = (
                self._data.state.input_text[: pos - 1]
                + self._data.state.input_text[pos:]
            )
            self._data.state.cursor_offset -= 1

    def set_history_entries(self, entries: List[str]) -> None:
        self._data.state.history_entries = [
            entry.strip() for entry in entries if entry.strip()
        ]
        self._reset_history_navigation()

    def handle_history_up(self) -> bool:
        state = self._data.state
        if not state.history_entries:
            return False

        if state.history_index is None:
            state.history_draft = state.input_text
            next_index = len(state.history_entries) - 1
        else:
            next_index = max(0, state.history_index - 1)

        state.history_index = next_index
        self._replace_input_text(state.history_entries[next_index])
        return True

    def handle_history_down(self) -> bool:
        state = self._data.state
        if state.history_index is None:
            return False

        next_index = state.history_index + 1
        if next_index >= len(state.history_entries):
            draft = state.history_draft
            self._reset_history_navigation()
            self._replace_input_text(draft)
            return True

        state.history_index = next_index
        self._replace_input_text(state.history_entries[next_index])
        return True

    def handle_submit(self) -> Tuple[bool, str]:
        if self._data.state.is_loading:
            return False, ""

        text = self._data.state.input_text.strip()
        if not text:
            return False, ""

        self._data.state.submit_count += 1
        self.add_history_entry(text)
        self._reset_history_navigation()
        self._data.state.input_text = ""
        self._data.state.cursor_offset = 0

        return True, text

    def add_history_entry(self, text: str) -> None:
        entry = text.strip()
        if not entry:
            return
        history = self._data.state.history_entries
        if history and history[-1] == entry:
            return
        history.append(entry)

    def move_cursor(self, delta: int) -> None:
        new_pos = self._data.state.cursor_offset + delta
        new_pos = max(0, min(new_pos, len(self._data.state.input_text)))
        self._data.state.cursor_offset = new_pos

    def toggle_help(self) -> bool:
        self._data.state.help_open = not self._data.state.help_open
        return self._data.state.help_open

    def set_loading(self, loading: bool) -> None:
        self._data.state.is_loading = loading

    def get_input_text(self) -> str:
        return self._data.state.input_text

    def _replace_input_text(self, text: str, cursor_offset: Optional[int] = None) -> None:
        self._data.state.input_text = text
        if cursor_offset is None:
            cursor_offset = len(text)
        self._data.state.cursor_offset = max(0, min(cursor_offset, len(text)))

    def _reset_history_navigation(self) -> None:
        self._data.state.history_index = None
        self._data.state.history_draft = ""


def _wrap_display_text(
    text: str,
    width: int,
    *,
    first_prefix: str,
    continuation_prefix: str,
) -> List[str]:
    terminal_width = max(width, 1)
    lines: List[str] = []
    prefix = first_prefix
    available_width = _available_content_width(terminal_width, prefix)
    parts: List[str] = []
    line_width = 0

    def flush_line() -> None:
        lines.append(f"{prefix}{''.join(parts)}")

    def start_continuation_line() -> None:
        nonlocal prefix, available_width, parts, line_width
        prefix = continuation_prefix
        available_width = _available_content_width(terminal_width, prefix)
        parts = []
        line_width = 0

    for segment, segment_width in iter_display_segments(text):
        if segment == "\n":
            flush_line()
            start_continuation_line()
            continue

        if parts and line_width + segment_width > available_width:
            flush_line()
            start_continuation_line()

        parts.append(segment)
        line_width += segment_width

    flush_line()
    return lines or [first_prefix]


def _available_content_width(width: int, prefix: str) -> int:
    return max(1, width - string_width(prefix))


def _truncate_display_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if string_width(text) <= width:
        return text

    ellipsis = "..."
    ellipsis_width = string_width(ellipsis)
    if width <= ellipsis_width:
        return _take_display_width(text, width)

    return f"{_take_display_width(text, width - ellipsis_width).rstrip()}..."


def _take_display_width(text: str, width: int) -> str:
    if width <= 0:
        return ""

    result: List[str] = []
    used_width = 0
    for segment, segment_width in iter_display_segments(text):
        if segment == "\n":
            break
        if used_width + segment_width > width:
            break
        result.append(segment)
        used_width += segment_width

    return "".join(result)


@dataclass
class PromptInteractionResult:
    submitted: bool = False
    text: str = ""
    cancelled: bool = False
    help_requested: bool = False


class PromptInteractionFlow:
    def __init__(
        self,
        renderer: PromptInputRenderer,
        on_submit: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._renderer = renderer
        self._on_submit = on_submit
        self._result = PromptInteractionResult()

    def process_key(self, key: str) -> bool:
        if key == "\n" or key == "return":
            success, text = self._renderer.handle_submit()
            if success:
                self._result.submitted = True
                self._result.text = text
                if self._on_submit:
                    self._on_submit(text)
                return False

        elif key == "\x1b" or key == "escape":
            self._result.cancelled = True
            return False

        elif key == "\x7f" or key == "backspace":
            self._renderer.handle_backspace()

        elif key == "left":
            self._renderer.move_cursor(-1)

        elif key == "right":
            self._renderer.move_cursor(1)

        elif key == "up" or key == "\x1b[A":
            self._renderer.handle_history_up()

        elif key == "down" or key == "\x1b[B":
            self._renderer.handle_history_down()

        elif key == "home":
            self._renderer._data.state.cursor_offset = 0

        elif key == "end":
            self._renderer._data.state.cursor_offset = len(
                self._renderer._data.state.input_text
            )

        elif key == "\t" or key == "tab":
            help_open = self._renderer.toggle_help()
            self._result.help_requested = help_open

        elif len(key) == 1 and key.isprintable():
            self._renderer.handle_input(key)

        return True

    def get_result(self) -> PromptInteractionResult:
        return self._result


def create_prompt_input(data: PromptInputData) -> PromptInputRenderer:
    return PromptInputRenderer(data)
