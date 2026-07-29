"""Structured diff viewer for TUI code blocks and tool results."""

from __future__ import annotations

from dataclasses import dataclass

from .highlighted_code import (
    get_highlight_palette,
    highlight_code_fragment,
    normalize_highlight_theme,
    wrap_ansi,
)


@dataclass(frozen=True)
class DiffViewerSummary:
    additions: int = 0
    deletions: int = 0
    hunks: int = 0
    files: int = 0


@dataclass
class DiffViewerData:
    patch: str
    theme: str = "dark"
    title: str = "diff"


class DiffViewerRenderer:
    """Render unified diffs with a compact summary header."""

    def __init__(self, data: DiffViewerData) -> None:
        self._data = data

    def render(self, width: int) -> str:
        return "\n".join(self.render_lines(width))

    def render_lines(self, width: int) -> list[str]:
        normalized_theme = normalize_highlight_theme(self._data.theme)
        palette = get_highlight_palette(normalized_theme)
        summary = summarize_diff(self._data.patch)

        header_parts = [f"[{self._data.title}]"]
        if summary.files:
            header_parts.append(f"files={summary.files}")
        header_parts.append(f"+{summary.additions}")
        header_parts.append(f"-{summary.deletions}")
        if summary.hunks:
            header_parts.append(f"hunks={summary.hunks}")

        rendered = [
            wrap_ansi(
                _truncate_plain_text(" ".join(header_parts), width),
                *palette["label"],
            )
        ]

        prefix = "│ "
        available_width = max(width - len(prefix), 1)
        styled_prefix = wrap_ansi(prefix, *palette["code_prefix"])
        raw_lines = self._data.patch.splitlines() or [""]
        for raw_line in raw_lines:
            for fragment in _wrap_code_text(raw_line, available_width):
                rendered.append(
                    f"{styled_prefix}{highlight_code_fragment(fragment, 'diff', theme=normalized_theme)}"
                )
        return rendered


def create_diff_viewer(data: DiffViewerData) -> DiffViewerRenderer:
    return DiffViewerRenderer(data)


def summarize_diff(patch: str) -> DiffViewerSummary:
    additions = 0
    deletions = 0
    hunks = 0
    files = 0
    saw_patch = False

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
            saw_patch = True
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
            saw_patch = True

    if files == 0 and saw_patch:
        files = 1

    return DiffViewerSummary(
        additions=additions,
        deletions=deletions,
        hunks=hunks,
        files=files,
    )


def _truncate_plain_text(content: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(content) <= width:
        return content
    if width <= 3:
        return content[:width]
    return content[: width - 3] + "..."


def _wrap_code_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    if text == "":
        return [""]
    return [text[index : index + width] for index in range(0, len(text), width)] or [""]


__all__ = [
    "DiffViewerData",
    "DiffViewerRenderer",
    "DiffViewerSummary",
    "create_diff_viewer",
    "summarize_diff",
]
