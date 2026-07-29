from __future__ import annotations

from .ansi import CSI, ESC

# Cursor style constants
CURSOR_STYLES = {
    "block": 1,
    "underline": 3,
    "beam": 5,
    "bar": 6,
}

# DEC Private mode constants (DECSET/DECRST)
class DECSET:
    """DECSET mode constants for enable."""
    MOUSE_X10 = 9      # X10 mouse mode
    MOUSE_VT200 = 1000  # VT200 mouse mode
    MOUSE_VT200_HIGHLIGHT = 1001  # VT200 highlight mouse mode
    MOUSE_MOUSE_MOTION = 1002  # Button-event mouse mode
    MOUSE_FOCUS_MOUSE = 1003  # Any-event mouse mode (all motion)
    MOUSE_SGR = 1006   # SGR mouse mode
    MOUSE_URXVT = 1015  # URXVT mouse mode
    MOUSE_PIXEL = 1016  # Pixel position mouse mode
    SHOW_CURSOR = 25    # Show cursor
    ALT_SCREEN = 1049   # Alternate screen buffer
    BRACKETED_PASTE = 2004  # Bracketed paste mode
    FOCUS_REPORT = 1004  # Focus in/out reporting
    SYNCHRONIZED_UPDATE = 2026  # Synchronized update
    KITTEN_KEYBOARD = 2012  # Keyboard protocol mode


class DECRST:
    """DECRST mode constants for disable."""
    MOUSE_X10 = 9
    MOUSE_VT200 = 1000
    MOUSE_VT200_HIGHLIGHT = 1001
    MOUSE_MOUSE_MOTION = 1002
    MOUSE_FOCUS_MOUSE = 1003
    MOUSE_SGR = 1006
    MOUSE_URXVT = 1015
    MOUSE_PIXEL = 1016
    SHOW_CURSOR = 25
    ALT_SCREEN = 1049
    BRACKETED_PASTE = 2004
    FOCUS_REPORT = 1004
    SYNCHRONIZED_UPDATE = 2026
    KITTEN_KEYBOARD = 2012


def set_private_mode(mode: int) -> str:
    return f"{CSI}?{mode}h"


def reset_private_mode(mode: int) -> str:
    return f"{CSI}?{mode}l"


def enter_alt_screen() -> str:
    return set_private_mode(1049)


def exit_alt_screen() -> str:
    return reset_private_mode(1049)


def begin_synchronized_update() -> str:
    return set_private_mode(2026)


def end_synchronized_update() -> str:
    return reset_private_mode(2026)


def enable_bracketed_paste() -> str:
    return set_private_mode(2004)


def disable_bracketed_paste() -> str:
    return reset_private_mode(2004)


def enable_focus_reporting() -> str:
    return set_private_mode(1004)


def disable_focus_reporting() -> str:
    return reset_private_mode(1004)


def enable_mouse_tracking() -> str:
    return set_private_mode(1000)


def disable_mouse_tracking() -> str:
    return reset_private_mode(1000)


def enable_mouse_drag_tracking() -> str:
    return set_private_mode(1002)


def disable_mouse_drag_tracking() -> str:
    return reset_private_mode(1002)


def enable_all_motion_mouse_tracking() -> str:
    return set_private_mode(1003)


def disable_all_motion_mouse_tracking() -> str:
    return reset_private_mode(1003)


def enable_sgr_mouse_mode() -> str:
    return set_private_mode(1006)


def disable_sgr_mouse_mode() -> str:
    return reset_private_mode(1006)


# SS3 (Single Shift Select) sequences for cursor/app keypad movement
def ss3_up(lines: int = 1) -> str:
    return f"{ESC}A" * lines


def ss3_down(lines: int = 1) -> str:
    return f"{ESC}B" * lines


def ss3_right(columns: int = 1) -> str:
    return f"{ESC}C" * columns


def ss3_left(columns: int = 1) -> str:
    return f"{ESC}D" * columns


# X10 mouse encoding: CSI M x y (x and y are 1-indexed, 0-based encoded as 32 + value)
def encode_x10_mouse(row: int, col: int) -> tuple[int, int]:
    return (col + 32, row + 32)


def decode_x10_mouse(x: int, y: int) -> tuple[int, int]:
    return (y - 32, x - 32)


# =============================================================================
# Terminal Response Negotiation (DA1/DA2/XTVERSION)
# =============================================================================

# DA1: Primary Device Attributes response - VT100 with AVO (Advanced Video Option)
# Query: CSI c → Response: CSI ? 1 ; 2 c
def primary_device_attributes_response() -> str:
    """Generate DA1 (Primary Device Attributes) response: VT100 with AVO."""
    return f"{CSI}?1;2c"


# DA2: Secondary Device Attributes response - basic VT100
# Query: CSI > c → Response: CSI > 0 ; 0 ; 0 c
def secondary_device_attributes_response() -> str:
    """Generate DA2 (Secondary Device Attributes) response: basic VT100."""
    return f"{CSI}>0;0;0c"


# XTVERSION: Terminal version response using DCS (Device Control String)
# Query: CSI > q → Response: DCS > | terminal_info ST
# The terminal_info format is: "name version" e.g. "ghostty 1.0.0"
def xtversion_response(terminal_name: str = "", terminal_version: str = "") -> str:
    """Generate XTVERSION response with terminal name and version.

    Args:
        terminal_name: Terminal emulator name (e.g., "ghostty", "iTerm2", "xterm")
        terminal_version: Terminal version string (e.g., "1.0.0")

    Returns:
        DCS response string in format: P>|[name] [version] ST
    """
    from .ansi import ST

    identity = f"{terminal_name} {terminal_version}".strip()
    return f"{ESC}P>|{identity}{ST}"


class TerminalCapabilities:
    """Terminal capability response generator for DA1/DA2/XTVERSION queries.

    This class generates the appropriate escape sequence responses for
    terminal capability queries sent by the host.
    """

    def __init__(
        self,
        terminal_name: str = "",
        terminal_version: str = "",
        da1_response: str | None = None,
        da2_response: str | None = None,
        xtversion_response_str: str | None = None,
    ) -> None:
        self.terminal_name = terminal_name
        self.terminal_version = terminal_version
        # Allow override of default responses
        self._da1_response = da1_response
        self._da2_response = da2_response
        self._xtversion_response = xtversion_response_str

    @property
    def da1_response(self) -> str:
        """Primary Device Attributes (DA1) response."""
        if self._da1_response is not None:
            return self._da1_response
        return primary_device_attributes_response()

    @property
    def da2_response(self) -> str:
        """Secondary Device Attributes (DA2) response."""
        if self._da2_response is not None:
            return self._da2_response
        return secondary_device_attributes_response()

    @property
    def xtversion(self) -> str:
        """XTVERSION response string with terminal identity."""
        if self._xtversion_response is not None:
            return self._xtversion_response
        return xtversion_response(self.terminal_name, self.terminal_version)
