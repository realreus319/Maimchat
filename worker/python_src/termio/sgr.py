from __future__ import annotations

from dataclasses import dataclass

from .csi import sequence


# Underline style constants (SGR 4 subparameter)
UNDERLINE_STYLES = {
    "single": 1,
    "double": 2,
    "curly": 3,
    "dotted": 4,
    "dashed": 5,
}


# Stroke color constants (SGR 38/48)
STROKE_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}


@dataclass
class SgrState:
    """Tracks cumulative SGR (Select Graphic Rendition) style state.

    Represents the current text styling as SGR sequences are applied
    sequentially. Call ``apply_sgr`` to update an instance based on a
    parsed parameter list.
    """

    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    blink: bool = False
    inverse: bool = False
    hidden: bool = False
    strikethrough: bool = False
    foreground: tuple[int, ...] | None = None
    background: tuple[int, ...] | None = None


def reset_sgr() -> SgrState:
    """Return a fresh ``SgrState`` with all attributes at their defaults."""
    return SgrState()


def apply_sgr(state: SgrState, params: tuple[int, ...]) -> None:
    """Apply a sequence of SGR parameters to *state*, mutating it in place.

    *params* is a tuple of raw integer parameters as extracted from an
    ``ESC [ … m`` sequence.  Each parameter is consumed according to
    standard ECMA-48 / VT500 SGR semantics.
    """
    # An empty parameter list is equivalent to a single 0 (reset).
    if not params:
        _reset_state(state)
        return

    i = 0
    while i < len(params):
        p = params[i]

        if p == 0:
            _reset_state(state)
        elif p == 1:
            state.bold = True
        elif p == 2:
            state.dim = True
        elif p == 3:
            state.italic = True
        elif p == 4:
            state.underline = True
        elif p == 5:
            state.blink = True
        elif p == 7:
            state.inverse = True
        elif p == 8:
            state.hidden = True
        elif p == 9:
            state.strikethrough = True
        elif p == 22:
            state.bold = False
            state.dim = False
        elif p == 23:
            state.italic = False
        elif p == 24:
            state.underline = False
        elif p == 25:
            state.blink = False
        elif p == 27:
            state.inverse = False
        elif p == 28:
            state.hidden = False
        elif p == 29:
            state.strikethrough = False
        elif 30 <= p <= 37:
            # Standard foreground color (30 + color index)
            state.foreground = (p - 30,)
        elif p == 38:
            # Extended foreground color
            i, color = _parse_extended_color(params, i)
            state.foreground = color
        elif p == 39:
            # Default foreground
            state.foreground = None
        elif 40 <= p <= 47:
            # Standard background color (40 + color index)
            state.background = (p - 40,)
        elif p == 48:
            # Extended background color
            i, color = _parse_extended_color(params, i)
            state.background = color
        elif p == 49:
            # Default background
            state.background = None
        elif 90 <= p <= 97:
            # Bright foreground color (90 + color index)
            state.foreground = (p - 90 + 8,)
        elif 100 <= p <= 107:
            # Bright background color (100 + color index)
            state.background = (p - 100 + 8,)

        i += 1


def _reset_state(state: SgrState) -> None:
    """Reset all attributes of *state* to defaults."""
    state.bold = False
    state.dim = False
    state.italic = False
    state.underline = False
    state.blink = False
    state.inverse = False
    state.hidden = False
    state.strikethrough = False
    state.foreground = None
    state.background = None


def _parse_extended_color(
    params: tuple[int, ...], index: int
) -> tuple[int, tuple[int, ...]]:
    """Parse an extended color sequence starting at *index*.

    Returns (new_index, color_tuple).  The caller should use *new_index*
    as the current position (it points to the last consumed parameter).
    """
    if index + 1 < len(params) and params[index + 1] == 5:
        # 256-color: 38;5;n or 48;5;n
        if index + 2 < len(params):
            return (index + 2, (5, params[index + 2]))
        return (index + 1, (5, 0))

    if index + 1 < len(params) and params[index + 1] == 2:
        # True-color: 38;2;r;g;b or 48;2;r;g;b
        if index + 4 < len(params):
            return (index + 4, (2, params[index + 2], params[index + 3], params[index + 4]))
        # Partial true-color — consume whatever is available
        available = params[index + 2 :]
        return (len(params) - 1, (2, *available))

    # Malformed — just skip
    return (index, ())


def reset() -> str:
    return sequence("m", 0)


def bold() -> str:
    return sequence("m", 1)


def underline() -> str:
    return sequence("m", 4)


def underline_style(style: int | str) -> str:
    if isinstance(style, str):
        style = UNDERLINE_STYLES[style]
    return sequence("m", 4, style)


def foreground_256(color: int) -> str:
    return sequence("m", 38, 5, color)


def foreground_rgb(red: int, green: int, blue: int) -> str:
    return sequence("m", 38, 2, red, green, blue)


def background_256(color: int) -> str:
    return sequence("m", 48, 5, color)


def background_rgb(red: int, green: int, blue: int) -> str:
    return sequence("m", 48, 2, red, green, blue)
