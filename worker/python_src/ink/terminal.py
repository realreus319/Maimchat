"""Terminal utilities for TUI renderer."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Iterator, Optional, Tuple

from .parse_keypress import ParsedTerminalInput, parse_terminal_input
from ..termio.dec import begin_synchronized_update, end_synchronized_update
from ..termio.tokenize import consume_escape_sequence, strip_control_sequences

_ESC = "\x1b"
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
_ZERO_WIDTH_CODEPOINTS = frozenset(
    {
        0x200C,  # ZWNJ
        0x200D,  # ZWJ
        0x2060,  # word joiner
    }
)
_TRUECOLOR_TERMINAL_MARKERS = frozenset(
    {
        "wezterm",
        "iterm2",
        "ghostty",
        "kitty",
        "alacritty",
        "foot",
        "rio",
        "warp",
        "contour",
        "vscode",
        "windows terminal",
    }
)
_SYNCHRONIZED_OUTPUT_TERMINAL_MARKERS = frozenset(
    {
        "wezterm",
        "iterm2",
        "ghostty",
        "kitty",
        "alacritty",
        "foot",
        "windows terminal",
    }
)
_EXTENDED_KEYS_TERMINAL_MARKERS = frozenset(
    {
        "wezterm",
        "iterm2",
        "ghostty",
        "kitty",
        "windows terminal",
    }
)
_HYPERLINK_TERMINAL_MARKERS = frozenset(
    {
        "wezterm",
        "iterm2",
        "ghostty",
        "kitty",
        "foot",
        "vscode",
        "windows terminal",
    }
)
_NATIVE_PROGRESS_BAR_TERMINAL_MARKERS = frozenset(
    {
        "iterm2",
        "ghostty",
    }
)


def _is_env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def strip_ansi_sequences(text: str) -> str:
    """Remove ANSI / OSC control sequences from text."""
    return strip_control_sequences(text)


def _is_zero_width(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in _ZERO_WIDTH_CODEPOINTS:
        return True
    if char in _VARIATION_SELECTORS:
        return True
    if 0xFE00 <= codepoint <= 0xFE0F:
        return True
    if 0xE0100 <= codepoint <= 0xE01EF:
        return True
    return bool(unicodedata.combining(char))


def _is_emoji_modifier(char: str) -> bool:
    codepoint = ord(char)
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _is_emoji_codepoint(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _should_extend_grapheme(cluster: str, next_char: str) -> bool:
    if not cluster:
        return False
    if cluster[-1] == "\u200d":
        return True
    if next_char == "\u200d":
        return True
    if _is_zero_width(next_char):
        return True
    if _is_emoji_modifier(next_char):
        return True
    return False


def iter_display_segments(text: str) -> Iterator[tuple[str, int]]:
    """Yield visible grapheme-ish display segments with terminal cell widths.

    ANSI / OSC control sequences are skipped and therefore contribute zero width.
    Newlines are returned as ``("\\n", 0)`` markers.
    """

    index = 0
    while index < len(text):
        char = text[index]
        if char == _ESC:
            index = consume_escape_sequence(text, index)
            continue
        if char == "\n":
            yield ("\n", 0)
            index += 1
            continue

        cluster = char
        index += 1
        while index < len(text) and _should_extend_grapheme(cluster, text[index]):
            cluster += text[index]
            index += 1
        yield (cluster, string_width(cluster))


def string_width(text: str) -> int:
    """Return terminal cell width for visible text.

    This intentionally approximates TS ``stringWidth`` behavior closely enough
    for TUI layout: ANSI / OSC control sequences are ignored, combining marks
    are zero-width, and East Asian wide / emoji graphemes occupy two cells.
    """

    cleaned = strip_ansi_sequences(text)
    width = 0
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if char == "\n":
            index += 1
            continue

        cluster = char
        index += 1
        while index < len(cleaned) and _should_extend_grapheme(cluster, cleaned[index]):
            cluster += cleaned[index]
            index += 1

        visible_chars = [part for part in cluster if not _is_zero_width(part)]
        if not visible_chars:
            continue

        cluster_width = 1
        for visible in visible_chars:
            if _is_emoji_codepoint(visible) or unicodedata.east_asian_width(visible) in {
                "W",
                "F",
            }:
                cluster_width = 2
                break
        width += cluster_width
    return width


def is_hyperlink_supported() -> bool:
    """Check if OSC8 hyperlinks are supported."""
    if _is_env_truthy(os.environ.get("NO_HYPERLINK")):
        return False
    if _is_env_truthy(os.environ.get("FORCE_HYPERLINK")):
        return True

    if os.environ.get("WT_SESSION"):
        return True

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"iterm.app", "wezterm", "vscode"}:
        return True

    if os.environ.get("VTE_VERSION"):
        return True

    term = os.environ.get("TERM", "").lower()
    return any(
        marker in term
        for marker in ("xterm-kitty", "kitty", "wezterm", "foot", "ghostty")
    )


def is_native_progress_bar_supported() -> bool:
    """Check if terminal supports OSC 9;4 native progress bars."""
    if _is_env_truthy(os.environ.get("NO_NATIVE_PROGRESS_BAR")):
        return False
    if _is_env_truthy(os.environ.get("FORCE_NATIVE_PROGRESS_BAR")):
        return True

    term = os.environ.get("TERM", "").lower()
    if "ghostty" in term:
        return True

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    return term_program in {"iterm.app"}


def get_terminal_size() -> Tuple[int, int]:
    """Get terminal dimensions as (columns, rows)."""
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        return size.columns, size.lines
    except (OSError, ValueError):
        return _get_terminal_size_fallback()


def _get_terminal_size_fallback() -> Tuple[int, int]:
    """Fallback terminal size detection."""
    import shutil

    try:
        return shutil.get_terminal_size()
    except Exception:
        return (80, 24)


def is_tty() -> bool:
    """Check if stdout is a TTY."""
    return sys.stdout.isatty()


class ColorDepth(IntEnum):
    """Detected terminal color depth."""

    NONE = 0
    BASIC = 16
    INDEXED = 256
    TRUECOLOR = 1 << 24


@dataclass(frozen=True)
class TerminalProbeRequest:
    """Terminal capability probe request and expected response type."""

    name: str
    request: str
    expected_response: str


def _parse_force_color_level(value: Optional[str]) -> Optional[ColorDepth]:
    if value is None:
        return None

    normalized = value.strip().lower()
    if normalized == "":
        return ColorDepth.BASIC
    if normalized in {"0", "false", "no", "off"}:
        return ColorDepth.NONE
    if normalized in {"true", "yes", "on"}:
        return ColorDepth.BASIC

    try:
        level = int(normalized)
    except ValueError:
        return ColorDepth.BASIC

    if level <= 0:
        return ColorDepth.NONE
    if level == 1:
        return ColorDepth.BASIC
    if level == 2:
        return ColorDepth.INDEXED
    return ColorDepth.TRUECOLOR


def detect_color_depth() -> ColorDepth:
    """Detect likely terminal color depth from environment hints."""
    forced = _parse_force_color_level(os.environ.get("FORCE_COLOR"))
    if forced is not None:
        return forced

    if _is_env_truthy(os.environ.get("NO_COLOR")):
        return ColorDepth.NONE

    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return ColorDepth.TRUECOLOR
    if "256" in colorterm:
        return ColorDepth.INDEXED

    if os.environ.get("WT_SESSION"):
        return ColorDepth.TRUECOLOR

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"iterm.app", "wezterm", "vscode"}:
        return ColorDepth.TRUECOLOR

    term = os.environ.get("TERM", "").lower()
    if any(
        marker in term
        for marker in ("truecolor", "direct", "kitty", "wezterm", "ghostty")
    ):
        return ColorDepth.TRUECOLOR
    if "256color" in term:
        return ColorDepth.INDEXED
    if term and term not in {"dumb", "unknown"}:
        return ColorDepth.BASIC
    return ColorDepth.NONE


def _coerce_color_depth(value: int) -> ColorDepth:
    if value >= int(ColorDepth.TRUECOLOR):
        return ColorDepth.TRUECOLOR
    if value >= int(ColorDepth.INDEXED):
        return ColorDepth.INDEXED
    if value >= int(ColorDepth.BASIC):
        return ColorDepth.BASIC
    return ColorDepth.NONE


def _merge_color_depth(current: ColorDepth, candidate: ColorDepth) -> ColorDepth:
    return _coerce_color_depth(max(int(current), int(candidate)))


def _parse_terminal_identity(text: str) -> tuple[str, str]:
    collapsed = " ".join(text.split()).strip()
    if not collapsed:
        return ("", "")

    match = re.match(r"^(.*?)[\s/_-]*([0-9][\w.+:-]*)\)?$", collapsed)
    if match is None:
        return (collapsed, "")

    name = match.group(1).rstrip(" (/_-")
    version = match.group(2).strip()
    if not name:
        return (collapsed, "")
    return (name, version)


def _with_color_depth(features: "TerminalFeatures", color_depth: ColorDepth) -> "TerminalFeatures":
    merged = _merge_color_depth(features.color_depth, color_depth)
    return replace(
        features,
        color_depth=merged,
        supports_256color=merged >= ColorDepth.INDEXED,
        supports_truecolor=merged >= ColorDepth.TRUECOLOR,
    )


def _apply_terminal_identity(
    features: "TerminalFeatures",
    *,
    terminal_name: str,
    terminal_version: str = "",
) -> "TerminalFeatures":
    updated = replace(
        features,
        terminal_name=terminal_name or features.terminal_name,
        terminal_version=terminal_version or features.terminal_version,
    )
    normalized = terminal_name.strip().lower()
    if not normalized:
        return updated

    if any(marker in normalized for marker in _TRUECOLOR_TERMINAL_MARKERS):
        updated = _with_color_depth(updated, ColorDepth.TRUECOLOR)
    elif updated.color_depth == ColorDepth.NONE:
        updated = _with_color_depth(updated, ColorDepth.BASIC)

    if any(marker in normalized for marker in _HYPERLINK_TERMINAL_MARKERS):
        updated = replace(updated, supports_hyperlinks=True)
    if any(marker in normalized for marker in _SYNCHRONIZED_OUTPUT_TERMINAL_MARKERS):
        updated = replace(updated, supports_synchronized_output=True)
    if any(marker in normalized for marker in _EXTENDED_KEYS_TERMINAL_MARKERS):
        updated = replace(updated, supports_extended_keys=True)
    if any(marker in normalized for marker in _NATIVE_PROGRESS_BAR_TERMINAL_MARKERS):
        updated = replace(updated, supports_native_progress_bar=True)
    return updated


def is_truecolor_supported() -> bool:
    """Check whether truecolor output is likely supported."""
    return detect_color_depth() >= ColorDepth.TRUECOLOR


def is_256color_supported() -> bool:
    """Check whether 256-color output is likely supported."""
    return detect_color_depth() >= ColorDepth.INDEXED


def is_extended_keys_supported() -> bool:
    """Check whether extended key reporting is likely supported."""
    if _is_env_truthy(os.environ.get("NO_EXTENDED_KEYS")):
        return False
    if _is_env_truthy(os.environ.get("FORCE_EXTENDED_KEYS")):
        return True

    term = os.environ.get("TERM", "").lower()
    if "tmux" in term or os.environ.get("TMUX"):
        return True
    if any(marker in term for marker in ("kitty", "ghostty")):
        return True
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("WT_SESSION"):
        return True

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    return term_program in {"iterm.app", "wezterm"}


def is_synchronized_output_supported() -> bool:
    """Check if terminal supports synchronized output."""
    term = os.environ.get("TERM", "")
    term_lower = term.lower()
    if any(
        marker in term_lower
        for marker in ("kitty", "wezterm", "ghostty", "foot", "alacritty")
    ):
        return True
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("WEZTERM_EXECUTABLE"):
        return True
    if os.environ.get("WT_SESSION"):
        return True

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in {"wezterm", "iterm.app"}:
        return True

    if "screen" in term_lower or "tmux" in term_lower:
        return bool(
            os.environ.get("KITTY_WINDOW_ID")
            or os.environ.get("WEZTERM_EXECUTABLE")
            or term_program in {"wezterm", "iterm.app"}
        )
    return False


def wrap_synchronized_output(text: str, *, enabled: bool) -> str:
    """Wrap text in DECSET 2026 synchronized-output markers."""
    if not enabled or not text:
        return text
    return f"{begin_synchronized_update()}{text}{end_synchronized_update()}"


@dataclass(frozen=True)
class TerminalFeatures:
    """Snapshot of detected terminal rendering features."""

    is_tty: bool
    supports_hyperlinks: bool
    supports_synchronized_output: bool
    color_depth: ColorDepth
    supports_256color: bool
    supports_truecolor: bool
    supports_native_progress_bar: bool = False
    supports_extended_keys: bool = False
    supports_unicode_width: bool = True
    terminal_name: str = ""
    terminal_version: str = ""
    primary_device_attributes: Tuple[str, ...] = ()
    secondary_device_attributes: Tuple[str, ...] = ()
    active_probe_applied: bool = False


class TerminalCapabilities:
    """Terminal capability detection."""

    _ACTIVE_PROBE_REQUESTS: tuple[TerminalProbeRequest, ...] = (
        TerminalProbeRequest(
            name="primary_device_attributes",
            request="\x1b[c",
            expected_response="da1",
        ),
        TerminalProbeRequest(
            name="secondary_device_attributes",
            request="\x1b[>c",
            expected_response="da2",
        ),
        TerminalProbeRequest(
            name="xtversion",
            request="\x1b[>q",
            expected_response="xtversion",
        ),
    )

    def __init__(self) -> None:
        self._width: Optional[int] = None
        self._height: Optional[int] = None
        color_depth = detect_color_depth()
        self.features = TerminalFeatures(
            is_tty=is_tty(),
            supports_hyperlinks=is_hyperlink_supported(),
            supports_synchronized_output=is_synchronized_output_supported(),
            color_depth=color_depth,
            supports_256color=color_depth >= ColorDepth.INDEXED,
            supports_truecolor=color_depth >= ColorDepth.TRUECOLOR,
            supports_native_progress_bar=is_native_progress_bar_supported(),
            supports_extended_keys=is_extended_keys_supported(),
        )
        self.is_tty = self.features.is_tty

    def build_active_probe_requests(self) -> tuple[TerminalProbeRequest, ...]:
        """Return the supported active terminal capability probes."""
        return self._ACTIVE_PROBE_REQUESTS

    def build_active_probe_payload(self) -> str:
        """Return the concatenated probe escape sequences."""
        return "".join(request.request for request in self._ACTIVE_PROBE_REQUESTS)

    def apply_active_probe_response(
        self,
        raw: str | ParsedTerminalInput,
    ) -> bool:
        """Apply a parsed terminal probe response and update detected features."""
        parsed = raw if isinstance(raw, ParsedTerminalInput) else parse_terminal_input(raw)
        if parsed is None or parsed.kind != "response":
            return False

        updated = self.features
        if parsed.response == "da1":
            updated = _with_color_depth(updated, ColorDepth.BASIC)
            updated = replace(
                updated,
                primary_device_attributes=parsed.params,
                active_probe_applied=True,
            )
        elif parsed.response == "da2":
            updated = _with_color_depth(updated, ColorDepth.BASIC)
            updated = replace(
                updated,
                secondary_device_attributes=parsed.params,
                terminal_version=updated.terminal_version or (
                    parsed.params[1] if len(parsed.params) > 1 else ""
                ),
                active_probe_applied=True,
            )
        elif parsed.response == "xtversion":
            terminal_name, terminal_version = _parse_terminal_identity(parsed.text)
            updated = replace(updated, active_probe_applied=True)
            updated = _apply_terminal_identity(
                updated,
                terminal_name=terminal_name or parsed.text.strip(),
                terminal_version=terminal_version,
            )
        else:
            return False

        changed = updated != self.features
        self.features = updated
        return changed

    def get_size(self) -> Tuple[int, int]:
        if self._width is None or self._height is None:
            self._width, self._height = get_terminal_size()
        return self._width, self._height

    def refresh_size(self) -> Tuple[int, int]:
        self._width, self._height = get_terminal_size()
        return self._width, self._height

    @property
    def width(self) -> int:
        if self._width is None:
            self._width, _ = get_terminal_size()
        return self._width

    @property
    def height(self) -> int:
        if self._height is None:
            _, self._height = get_terminal_size()
        return self._height

    @property
    def supports_hyperlinks(self) -> bool:
        return self.features.supports_hyperlinks

    @property
    def supports_synchronized_output(self) -> bool:
        return self.features.supports_synchronized_output

    @property
    def supports_truecolor(self) -> bool:
        return self.features.supports_truecolor

    @property
    def supports_256color(self) -> bool:
        return self.features.supports_256color

    @property
    def supports_native_progress_bar(self) -> bool:
        return self.features.supports_native_progress_bar

    @property
    def color_depth(self) -> ColorDepth:
        return self.features.color_depth

    @property
    def supports_extended_keys(self) -> bool:
        return self.features.supports_extended_keys

    @property
    def supports_unicode_width(self) -> bool:
        return self.features.supports_unicode_width

    @property
    def terminal_name(self) -> str:
        return self.features.terminal_name

    @property
    def terminal_version(self) -> str:
        return self.features.terminal_version

    @property
    def primary_device_attributes(self) -> Tuple[str, ...]:
        return self.features.primary_device_attributes

    @property
    def secondary_device_attributes(self) -> Tuple[str, ...]:
        return self.features.secondary_device_attributes

    @property
    def active_probe_applied(self) -> bool:
        return self.features.active_probe_applied

    def wrap_output(self, text: str) -> str:
        return wrap_synchronized_output(
            text,
            enabled=self.supports_synchronized_output,
        )
