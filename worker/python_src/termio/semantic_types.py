from __future__ import annotations

from enum import Enum

from .ansi import CSI, ESC


class Color(Enum):
    """Named terminal colors with ANSI SGR codes."""

    black = 0
    red = 1
    green = 2
    yellow = 3
    blue = 4
    magenta = 5
    cyan = 6
    white = 7


class TextStyle(Enum):
    """Text style attributes with ANSI SGR codes."""

    bold = 1
    dim = 2
    italic = 3
    underline = 4
    blink = 5
    inverse = 7
    hidden = 8
    strikethrough = 9


class Action(Enum):
    """Terminal actions with CSI/ESC sequences."""

    # Screen actions
    clear_screen = "2J"
    erase_to_end_of_screen = "0J"
    erase_to_start_of_screen = "1J"
    erase_scrollback = "3J"

    # Line actions
    clear_line = "2K"
    erase_to_end_of_line = "0K"
    erase_to_start_of_line = "1K"

    # Cursor movement
    cursor_up = "A"
    cursor_down = "B"
    cursor_forward = "C"
    cursor_back = "D"
    cursor_position_absolute = "d"
    cursor_position_horizontal = "G"

    # Scrolling
    scroll_up = "S"
    scroll_down = "T"

    # Tab operations
    forward_tab = "I"
    backward_tab = "Z"

    # Line feed
    line_feed = "0M"
    reverse_line_feed = "1M"

    # Character attributes
    reset_attributes = "0m"
    bold_on = "1m"
    dim_on = "2m"
    italic_on = "3m"
    underline_on = "4m"
    blink_on = "5m"
    inverse_on = "7m"
    hidden_on = "8m"
    strikethrough_on = "9m"

    # Visibility
    show_cursor = "?25h"
    hide_cursor = "?25l"

    # Focus
    focus_in = "I"
    focus_out = "O"

    # Save/restore cursor
    save_cursor = "7"
    restore_cursor = "8"

    # Screen brightness (dim is 2, normal is 22)
    normal_intensity = "22m"
    normal_color = "39m"
    normal_background = "49m"


def color_to_sgr(color: Color, *, foreground: bool = True) -> str:
    """Convert a Color to its SGR sequence.

    Args:
        color: The Color enum value
        foreground: If True, sets foreground color; if False, sets background

    Returns:
        ANSI SGR sequence string
    """
    code = 30 + color.value if foreground else 40 + color.value
    return f"{CSI}{code}m"


def style_to_sgr(style: TextStyle, *, enable: bool = True) -> str:
    """Convert a TextStyle to its SGR sequence.

    Args:
        style: The TextStyle enum value
        enable: If True, enables the style; if False, disables it

    Returns:
        ANSI SGR sequence string
    """
    if enable:
        return f"{CSI}{style.value}m"
    else:
        # Most styles disable with value + 20, except bold/dim which use 22
        if style.value == 1:
            disable_code = 22
        elif style.value == 2:
            disable_code = 22
        else:
            disable_code = style.value + 20
        return f"{CSI}{disable_code}m"


def action_to_csi(action: Action, *, params: tuple[int, ...] = ()) -> str:
    """Convert an Action to its CSI sequence.

    Args:
        action: The Action enum value
        params: Optional parameters for the action

    Returns:
        ANSI CSI sequence string
    """
    seq = action.value
    if seq.startswith("?"):
        # Private mode sequence
        return f"{CSI}{seq}"
    elif seq in ("7", "8"):
        # ESC-based sequences
        return f"{ESC}{seq}"
    elif params:
        joined = ";".join(str(p) for p in params)
        return f"{CSI}{joined}{seq}"
    elif seq[0].isdigit():
        # Sequence with numeric prefix already handled
        return f"{CSI}{seq}"
    else:
        return f"{CSI}{seq}"
