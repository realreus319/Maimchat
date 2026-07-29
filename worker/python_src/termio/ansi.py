from __future__ import annotations

ESC = "\x1b"
CSI = f"{ESC}["
DCS = f"{ESC}P"
OSC = f"{ESC}]"
ST = f"{ESC}\\"
BEL = "\a"

RESET = f"{CSI}0m"
CLEAR_SCREEN = f"{CSI}2J"
CURSOR_HOME = f"{CSI}H"
SHOW_CURSOR = f"{CSI}?25h"
HIDE_CURSOR = f"{CSI}?25l"
