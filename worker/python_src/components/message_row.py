"""Message row component for TUI."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Optional

from ..termio.tokenize import consume_escape_sequence
from .highlighted_code import (
    get_highlight_palette,
    highlight_code_fragment,
    normalize_highlight_theme,
    wrap_ansi,
)
from .diff_viewer import DiffViewerData, DiffViewerRenderer
from .markdown_table import parse_markdown_table, render_table, render_table_vertical
from .tool_row import ToolRowData, ToolRowRenderer

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_SEARCH_MATCH_STYLE = "\x1b[7m"
_SEARCH_ACTIVE_MATCH_STYLE = "\x1b[1;7m"


@dataclass
class MessageRowData:
    """Data for a single message row."""

    message_id: str = ""
    message_type: str = ""
    content: str = ""
    title: str | None = None
    status_text: str | None = None
    detail_lines: tuple[str, ...] = ()
    progress_fraction: float | None = None
    elapsed_ms: int | None = None
    progress_frame_index: int = 0
    timestamp: Optional[str] = None
    is_collapsed: bool = False
    is_selected: bool = False
    theme: str = "dark"
    search_query: str = ""
    search_match_offset: int = 0
    active_search_match_index: int | None = None


class MessageRowRenderer:
    """Renderer for individual message rows.

    Ported from src/components/MessageRow.tsx
    """

    def __init__(self, data: MessageRowData) -> None:
        self._data = data

    def render(self, width: int) -> str:
        """Render message row to string."""
        return "\n".join(self.render_lines(width))

    def render_lines(self, width: int) -> list[str]:
        lines = self._render_base_lines(width)
        query = self._data.search_query.strip()
        if not query:
            return lines
        return _highlight_search_matches_in_lines(
            lines,
            query,
            match_offset=max(self._data.search_match_offset, 0),
            active_match_index=self._data.active_search_match_index,
        )

    def count_search_matches(self, width: int, *, query: str | None = None) -> int:
        normalized_query = (query if query is not None else self._data.search_query).strip()
        if not normalized_query:
            return 0
        return _count_search_matches_in_lines(self._render_base_lines(width), normalized_query)

    def _render_base_lines(self, width: int) -> list[str]:
        """Render message row into terminal lines."""
        prefix = ""
        if self._data.is_selected:
            prefix = "[>] "
        elif self._data.is_collapsed:
            prefix = "[+] "

        if self._is_structured_progress_row():
            return self._render_progress_lines(width, prefix)

        content = self._data.content or ""
        available_width = max(width - len(prefix), 1)
        if _looks_like_markdown(content):
            content_lines = _render_markdown_lines(
                content,
                available_width,
                theme=self._data.theme,
            )
        else:
            content_lines = [_truncate_plain_text(content, available_width)]

        rendered: list[str] = []
        continuation_prefix = " " * len(prefix)
        for index, line in enumerate(content_lines):
            line_prefix = prefix if index == 0 else continuation_prefix
            if _contains_ansi(line):
                rendered.append(f"{line_prefix}{line}")
            else:
                rendered.append(_truncate_plain_text(f"{line_prefix}{line}", width))
        return rendered or [prefix.rstrip()]

    def _is_structured_progress_row(self) -> bool:
        return bool(
            self._data.message_type == "progress"
            or self._data.title
            or self._data.status_text
            or self._data.detail_lines
            or self._data.progress_fraction is not None
            or self._data.elapsed_ms is not None
        )

    def _render_progress_lines(self, width: int, prefix: str) -> list[str]:
        return ToolRowRenderer(
            ToolRowData(
                title=self._data.title,
                content=self._data.content,
                status_text=self._data.status_text,
                detail_lines=self._data.detail_lines,
                progress_fraction=self._data.progress_fraction,
                elapsed_ms=self._data.elapsed_ms,
                progress_frame_index=self._data.progress_frame_index,
                theme=self._data.theme,
            )
        ).render_lines(width, prefix=prefix)

    def has_content_after_index(self, index: int) -> bool:
        """Check if there's content after a given index."""
        return index < len(self._data.content)


def create_message_row(data: MessageRowData) -> MessageRowRenderer:
    """Factory function to create a message row renderer."""
    return MessageRowRenderer(data)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _contains_ansi(text: str) -> bool:
    return bool(ANSI_ESCAPE_RE.search(text))


def _visible_text(text: str) -> str:
    fragments: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            next_index = consume_escape_sequence(text, index)
            index = next_index if next_index > index else index + 1
            continue
        fragments.append(text[index])
        index += 1
    return "".join(fragments)


def _looks_like_markdown(content: str) -> bool:
    if "\n" in content:
        return True
    return bool(
        re.search(r"(^|\s)(#{1,6}\s|[-*+]\s|>\s|```|`[^`]+`)", content)
    )


def _truncate_plain_text(content: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(content) <= width:
        return content
    if width <= 3:
        return content[:width]
    return content[: width - 3] + "..."


def _render_markdown_lines(content: str, width: int, *, theme: str = "dark") -> list[str]:
    normalized_theme = normalize_highlight_theme(theme)
    palette = get_highlight_palette(normalized_theme)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    rendered: list[str] = []
    paragraph: list[str] = []
    in_code_block = False
    code_block_lines: list[str] = []
    code_language: str | None = None

    def _flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(part.strip() for part in paragraph if part.strip())
        paragraph.clear()
        if text:
            rendered.extend(_wrap_text(text, width))

    def _flush_code_block() -> None:
        nonlocal code_block_lines, code_language
        if not code_block_lines and not code_language:
            return
        rendered.extend(
            _render_code_block_lines(
                code_block_lines,
                width,
                language=code_language,
                theme=normalized_theme,
            )
        )
        code_block_lines = []
        code_language = None

    skip_until = 0
    for i, raw_line in enumerate(lines):
        if i < skip_until:
            continue
        stripped = raw_line.strip()
        if in_code_block:
            if stripped.startswith("```"):
                in_code_block = False
                _flush_code_block()
                continue
            code_block_lines.append(raw_line.rstrip("\n"))
            continue

        if stripped.startswith("```"):
            _flush_paragraph()
            in_code_block = True
            code_language = stripped[3:].strip() or None
            continue

        if not stripped:
            _flush_paragraph()
            if rendered and rendered[-1] != "":
                rendered.append("")
            continue

        heading_match = re.match(r"(#{1,6})\s+(.*)", stripped)
        if heading_match:
            _flush_paragraph()
            level = heading_match.group(1)
            heading = heading_match.group(2).strip()
            rendered.extend(
                _style_lines(_wrap_text(f"{level} {heading}", width), *palette["heading"])
            )
            continue

        quote_match = re.match(r">\s+(.*)", stripped)
        if quote_match:
            _flush_paragraph()
            rendered.extend(
                _style_lines(
                    _wrap_prefixed(quote_match.group(1), width, "│ "),
                    *palette["quote"],
                )
            )
            continue

        bullet_match = re.match(r"((?:[-*+])|\d+\.)\s+(.*)", stripped)
        if bullet_match:
            _flush_paragraph()
            bullet = bullet_match.group(1)
            item_text = bullet_match.group(2)
            rendered.extend(
                _wrap_hanging(item_text, width, first_prefix=f"{bullet} ", later_prefix="  ")
            )
            continue

        # Table detection: check if current line + next lines form a GFM table
        if "|" in stripped and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^[\s\|]*:?-+:?([\s\|]+:?-+:?)*[\s\|]*$", next_line):
                # Collect all consecutive table lines
                table_lines = []
                j = i
                while j < len(lines) and "|" in lines[j]:
                    table_lines.append(lines[j])
                    j += 1
                # Parse and render the table
                parsed = parse_markdown_table(table_lines)
                if parsed["headers"]:
                    rendered_table = render_table(parsed)
                    # Narrow terminal fallback (T3.2)
                    if max(len(line) for line in rendered_table.split("\n")) > width:
                        rendered_table = render_table_vertical(parsed, max_width=width)
                    rendered.extend(rendered_table.split("\n"))
                    skip_until = j
                    continue

        paragraph.append(stripped)

    _flush_paragraph()
    _flush_code_block()
    while rendered and rendered[-1] == "":
        rendered.pop()
    return rendered or [""]


def _wrap_text(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    return textwrap.wrap(
        text,
        width=max(width, 1),
        break_long_words=True,
        break_on_hyphens=False,
    ) or [text]


def _wrap_prefixed(text: str, width: int, prefix: str) -> list[str]:
    available_width = max(width - len(prefix), 1)
    wrapped = _wrap_text(text, available_width)
    return [f"{prefix}{line}" for line in wrapped]


def _wrap_hanging(
    text: str,
    width: int,
    *,
    first_prefix: str,
    later_prefix: str,
) -> list[str]:
    wrapped = textwrap.wrap(
        text,
        width=max(width - len(first_prefix), 1),
        break_long_words=True,
        break_on_hyphens=False,
    )
    if not wrapped:
        return [_truncate_plain_text(first_prefix.rstrip(), width)]
    lines = [f"{first_prefix}{wrapped[0]}"]
    for line in wrapped[1:]:
        lines.append(f"{later_prefix}{line}")
    return [_truncate_plain_text(line, width) for line in lines]


def _render_code_block_lines(
    code_lines: list[str],
    width: int,
    *,
    language: str | None,
    theme: str = "dark",
) -> list[str]:
    if (language or "").strip().lower() in {"diff", "patch"}:
        return DiffViewerRenderer(
            DiffViewerData(
                patch="\n".join(code_lines),
                theme=theme,
            )
        ).render_lines(width)

    normalized_theme = normalize_highlight_theme(theme)
    palette = get_highlight_palette(normalized_theme)
    rendered: list[str] = []
    if language:
        rendered.append(
            wrap_ansi(_truncate_plain_text(f"[{language}]", width), *palette["label"])
        )
    prefix = "│ "
    available_width = max(width - len(prefix), 1)
    for raw_line in code_lines or [""]:
        wrapped = _wrap_code_text(raw_line, available_width)
        for fragment in wrapped:
            styled_prefix = wrap_ansi(prefix, *palette["code_prefix"])
            rendered.append(
                f"{styled_prefix}{highlight_code_fragment(fragment, language, theme=normalized_theme)}"
            )
    return rendered


def _wrap_code_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    if text == "":
        return [""]
    return [text[index : index + width] for index in range(0, len(text), width)] or [""]


def _style_lines(lines: list[str], *codes: str) -> list[str]:
    return [wrap_ansi(line, *codes) if strip_ansi(line) else line for line in lines]


def _find_search_match_ranges(text: str, query: str) -> list[tuple[int, int]]:
    normalized_text = text.casefold()
    normalized_query = query.casefold()
    if not normalized_query:
        return []
    matches: list[tuple[int, int]] = []
    start = 0
    query_width = len(normalized_query)
    while True:
        index = normalized_text.find(normalized_query, start)
        if index < 0:
            return matches
        matches.append((index, index + query_width))
        start = index + query_width


def _update_sgr_state(current_style: str, sequence: str) -> str:
    if not sequence.endswith("m"):
        return current_style
    if sequence in {"\x1b[m", "\x1b[0m"}:
        return ""
    if sequence.startswith("\x1b[0;"):
        return f"\x1b[{sequence[4:]}"
    return f"{current_style}{sequence}"


def _highlight_search_matches_in_line(
    text: str,
    query: str,
    *,
    match_offset: int,
    active_match_index: int | None,
) -> tuple[str, int]:
    visible = _visible_text(text)
    matches = _find_search_match_ranges(visible, query)
    if not matches:
        return (text, 0)

    highlighted: list[str] = []
    match_index = 0
    next_match = matches[match_index]
    visible_index = 0
    current_style = ""
    current_match_style = ""
    in_match = False
    index = 0

    while index < len(text):
        if text[index] == "\x1b":
            next_index = consume_escape_sequence(text, index)
            if next_index <= index:
                next_index = index + 1
            sequence = text[index:next_index]
            highlighted.append(sequence)
            current_style = _update_sgr_state(current_style, sequence)
            if in_match:
                highlighted.append(current_match_style)
            index = next_index
            continue

        if visible_index == next_match[0]:
            global_match_index = match_offset + match_index
            current_match_style = (
                _SEARCH_ACTIVE_MATCH_STYLE
                if active_match_index is not None and global_match_index == active_match_index
                else _SEARCH_MATCH_STYLE
            )
            highlighted.append(current_match_style)
            in_match = True

        highlighted.append(text[index])
        index += 1
        visible_index += 1

        if visible_index == next_match[1]:
            highlighted.append("\x1b[0m")
            if current_style:
                highlighted.append(current_style)
            in_match = False
            match_index += 1
            if match_index >= len(matches):
                break
            next_match = matches[match_index]

    if index < len(text):
        highlighted.append(text[index:])

    return ("".join(highlighted), len(matches))


def _count_search_matches_in_lines(lines: list[str], query: str) -> int:
    return sum(len(_find_search_match_ranges(_visible_text(line), query)) for line in lines)


def _highlight_search_matches_in_lines(
    lines: list[str],
    query: str,
    *,
    match_offset: int,
    active_match_index: int | None,
) -> list[str]:
    highlighted: list[str] = []
    running_offset = match_offset
    for line in lines:
        rendered, count = _highlight_search_matches_in_line(
            line,
            query,
            match_offset=running_offset,
            active_match_index=active_match_index,
        )
        highlighted.append(rendered)
        running_offset += count
    return highlighted
