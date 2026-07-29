"""Terminal input parser for keyboard, mouse, paste, and response events."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Optional

_CTRL_KEY_NAMES = {
    9: "tab",
    10: "enter",
    13: "enter",
    27: "escape",
    32: "space",
}
_KITTY_KEY_NAMES = {
    57399: "0",
    57400: "1",
    57401: "2",
    57402: "3",
    57403: "4",
    57404: "5",
    57405: "6",
    57406: "7",
    57407: "8",
    57408: "9",
    57409: ".",
    57410: "/",
    57411: "*",
    57412: "-",
    57413: "+",
    57414: "enter",
    57415: "=",
}
_CSI_KEY_MAP = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "F": "end",
    "H": "home",
    "Z": "tab",
}
_CSI_TILDE_KEY_MAP = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
    "7": "home",
    "8": "end",
    "11": "f1",
    "12": "f2",
    "13": "f3",
    "14": "f4",
    "15": "f5",
    "17": "f6",
    "18": "f7",
    "19": "f8",
    "20": "f9",
    "21": "f10",
    "23": "f11",
    "24": "f12",
}
_SS3_KEY_MAP = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "P": "f1",
    "Q": "f2",
    "R": "f3",
    "S": "f4",
    "F": "end",
    "H": "home",
}
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"
_CSI_U_RE = re.compile(r"^\x1b\[(\d+)(?:;(\d+))?u$")
_MODIFY_OTHER_KEYS_RE = re.compile(r"^\x1b\[27;(\d+);(\d+)~$")
_CSI_NAVIGATION_RE = re.compile(r"^\x1b\[(?:(\d+)(?:;(\d+))?)?([A-DF-HZ])$")
_CSI_TILDE_RE = re.compile(r"^\x1b\[(\d+)(?:;(\d+))?~$")
_SS3_KEY_RE = re.compile(r"^\x1bO([A-DFHPQRS])$")
_SGR_MOUSE_RE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])$")
_DECRPM_RE = re.compile(r"^\x1b\[\?(\d+);(\d+)\$y$")
_DA1_RE = re.compile(r"^\x1b\[\?([\d;]*)c$")
_DA2_RE = re.compile(r"^\x1b\[>([\d;]*)c$")
_KITTY_FLAGS_RE = re.compile(r"^\x1b\[\?(\d+)u$")
_CURSOR_POSITION_RE = re.compile(r"^\x1b\[\??(\d+);(\d+)R$")
_FOCUS_RE = re.compile(r"^\x1b\[([IO])$")
_OSC_RESPONSE_RE = re.compile(r"^\x1b\](\d+);(.*?)(?:\x07|\x1b\\)$", re.DOTALL)
_XTVERSION_RE = re.compile(r"^\x1bP>\|(.*?)(?:\x07|\x1b\\)$", re.DOTALL)


@dataclass(frozen=True)
class ParsedTerminalInput:
    kind: str
    raw: str
    key: str = ""
    text: str = ""
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    super_key: bool = False
    response: str = ""
    params: tuple[str, ...] = ()
    event_type: str = ""
    x: int = 0
    y: int = 0
    button: int = -1


def _decode_modifier(value: int) -> tuple[bool, bool, bool, bool]:
    bitmask = max(value - 1, 0)
    shift = bool(bitmask & 1)
    alt = bool(bitmask & 2)
    ctrl = bool(bitmask & 4)
    super_key = bool(bitmask & 8)
    return ctrl, alt, shift, super_key


def _normalize_printable_key(char: str) -> tuple[str, bool]:
    if len(char) == 1 and char.isalpha() and char.upper() == char and char.lower() != char:
        return char.lower(), True
    return char, False


def _keycode_to_name(keycode: int) -> str:
    if keycode in _CTRL_KEY_NAMES:
        return _CTRL_KEY_NAMES[keycode]
    if keycode == 127:
        return "backspace"
    if keycode in _KITTY_KEY_NAMES:
        return _KITTY_KEY_NAMES[keycode]
    if 32 <= keycode <= 126:
        return chr(keycode).lower()
    try:
        return chr(keycode)
    except ValueError:
        return str(keycode)


def _parse_plain_key(raw: str) -> Optional[ParsedTerminalInput]:
    if not raw:
        return None
    if raw == "\x7f":
        return ParsedTerminalInput(kind="key", raw=raw, key="backspace")
    if raw in {"\r", "\n"}:
        return ParsedTerminalInput(kind="key", raw=raw, key="enter")
    if raw == "\t":
        return ParsedTerminalInput(kind="key", raw=raw, key="tab")
    if raw == "\x1b":
        return ParsedTerminalInput(kind="key", raw=raw, key="escape")
    if raw == " ":
        return ParsedTerminalInput(kind="key", raw=raw, key="space")
    if len(raw) == 1 and ord(raw) < 32:
        key_name = _CTRL_KEY_NAMES.get(ord(raw))
        if key_name is not None:
            return ParsedTerminalInput(kind="key", raw=raw, key=key_name, ctrl=True)
        return ParsedTerminalInput(
            kind="key",
            raw=raw,
            key=chr(ord(raw) + 96),
            ctrl=True,
        )
    if len(raw) == 1:
        key, shift = _normalize_printable_key(raw)
        return ParsedTerminalInput(kind="key", raw=raw, key=key, shift=shift)
    return None


def _parse_csi_u(raw: str) -> Optional[ParsedTerminalInput]:
    match = _CSI_U_RE.fullmatch(raw)
    if match is None:
        return None
    codepoint = int(match.group(1))
    modifier = int(match.group(2)) if match.group(2) else 1
    ctrl, alt, shift, super_key = _decode_modifier(modifier)
    key, implied_shift = _normalize_printable_key(_keycode_to_name(codepoint))
    return ParsedTerminalInput(
        kind="key",
        raw=raw,
        key=key,
        ctrl=ctrl,
        alt=alt,
        shift=shift or implied_shift,
        super_key=super_key,
    )


def _parse_modify_other_keys(raw: str) -> Optional[ParsedTerminalInput]:
    match = _MODIFY_OTHER_KEYS_RE.fullmatch(raw)
    if match is None:
        return None
    ctrl, alt, shift, super_key = _decode_modifier(int(match.group(1)))
    key, implied_shift = _normalize_printable_key(_keycode_to_name(int(match.group(2))))
    return ParsedTerminalInput(
        kind="key",
        raw=raw,
        key=key,
        ctrl=ctrl,
        alt=alt,
        shift=shift or implied_shift,
        super_key=super_key,
    )


def _parse_csi_navigation(raw: str) -> Optional[ParsedTerminalInput]:
    match = _CSI_NAVIGATION_RE.fullmatch(raw)
    if match is None:
        return None
    modifier = int(match.group(2)) if match.group(2) else 1
    key = _CSI_KEY_MAP.get(match.group(3))
    if key is None:
        return None
    ctrl, alt, shift, super_key = _decode_modifier(modifier)
    if key == "tab":
        shift = True
    return ParsedTerminalInput(
        kind="key",
        raw=raw,
        key=key,
        ctrl=ctrl,
        alt=alt,
        shift=shift,
        super_key=super_key,
    )


def _parse_csi_tilde_key(raw: str) -> Optional[ParsedTerminalInput]:
    match = _CSI_TILDE_RE.fullmatch(raw)
    if match is None:
        return None
    key = _CSI_TILDE_KEY_MAP.get(match.group(1))
    if key is None:
        return None
    modifier = int(match.group(2)) if match.group(2) else 1
    ctrl, alt, shift, super_key = _decode_modifier(modifier)
    return ParsedTerminalInput(
        kind="key",
        raw=raw,
        key=key,
        ctrl=ctrl,
        alt=alt,
        shift=shift,
        super_key=super_key,
    )


def _parse_ss3_key(raw: str) -> Optional[ParsedTerminalInput]:
    match = _SS3_KEY_RE.fullmatch(raw)
    if match is None:
        return None
    key = _SS3_KEY_MAP.get(match.group(1))
    if key is None:
        return None
    return ParsedTerminalInput(kind="key", raw=raw, key=key)


def _parse_sgr_mouse(raw: str) -> Optional[ParsedTerminalInput]:
    match = _SGR_MOUSE_RE.fullmatch(raw)
    if match is None:
        return None
    button_code = int(match.group(1))
    col = int(match.group(2))
    row = int(match.group(3))
    terminator = match.group(4)
    shift = bool(button_code & 0x04)
    alt = bool(button_code & 0x08)
    ctrl = bool(button_code & 0x10)

    if button_code & 0x40:
        return ParsedTerminalInput(
            kind="key",
            raw=raw,
            key="wheelup" if (button_code & 0x01) == 0 else "wheeldown",
            ctrl=ctrl,
            alt=alt,
            shift=shift,
        )

    if terminator == "m":
        event_type = "release"
    elif button_code & 0x20:
        event_type = "drag"
    else:
        event_type = "press"

    return ParsedTerminalInput(
        kind="mouse",
        raw=raw,
        ctrl=ctrl,
        alt=alt,
        shift=shift,
        event_type=event_type,
        x=col,
        y=row,
        button=button_code & 0x03,
    )


def _parse_x10_mouse(raw: str) -> Optional[ParsedTerminalInput]:
    if not raw.startswith("\x1b[M") or len(raw) != 6:
        return None
    button_code = ord(raw[3]) - 32
    col = ord(raw[4]) - 32
    row = ord(raw[5]) - 32
    if button_code < 0 or col <= 0 or row <= 0:
        return None

    shift = bool(button_code & 0x04)
    alt = bool(button_code & 0x08)
    ctrl = bool(button_code & 0x10)

    if button_code & 0x40:
        return ParsedTerminalInput(
            kind="key",
            raw=raw,
            key="wheelup" if (button_code & 0x01) == 0 else "wheeldown",
            ctrl=ctrl,
            alt=alt,
            shift=shift,
        )

    base_button = button_code & 0x03
    if base_button == 3:
        event_type = "release"
    elif button_code & 0x20:
        event_type = "drag"
    else:
        event_type = "press"

    return ParsedTerminalInput(
        kind="mouse",
        raw=raw,
        ctrl=ctrl,
        alt=alt,
        shift=shift,
        event_type=event_type,
        x=col,
        y=row,
        button=base_button,
    )


def _parse_terminal_response(raw: str) -> Optional[ParsedTerminalInput]:
    match = _DECRPM_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="decrpm",
            params=(match.group(1), match.group(2)),
        )

    match = _DA1_RE.fullmatch(raw)
    if match is not None:
        params = tuple(part for part in match.group(1).split(";") if part)
        return ParsedTerminalInput(kind="response", raw=raw, response="da1", params=params)

    match = _DA2_RE.fullmatch(raw)
    if match is not None:
        params = tuple(part for part in match.group(1).split(";") if part)
        return ParsedTerminalInput(kind="response", raw=raw, response="da2", params=params)

    match = _KITTY_FLAGS_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="kitty_keyboard",
            params=(match.group(1),),
        )

    match = _CURSOR_POSITION_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="cursor_position",
            params=(match.group(1), match.group(2)),
        )

    match = _FOCUS_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="focus_in" if match.group(1) == "I" else "focus_out",
        )

    match = _OSC_RESPONSE_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="osc",
            params=(match.group(1),),
            text=match.group(2),
        )

    match = _XTVERSION_RE.fullmatch(raw)
    if match is not None:
        return ParsedTerminalInput(
            kind="response",
            raw=raw,
            response="xtversion",
            text=match.group(1),
        )

    return None


def parse_terminal_input(raw: str) -> Optional[ParsedTerminalInput]:
    """Parse a terminal input chunk into a structured event."""
    if not raw:
        return None

    if raw.startswith(_BRACKETED_PASTE_START) and raw.endswith(_BRACKETED_PASTE_END):
        return ParsedTerminalInput(
            kind="paste",
            raw=raw,
            text=raw[len(_BRACKETED_PASTE_START) : -len(_BRACKETED_PASTE_END)],
        )
    if raw == _BRACKETED_PASTE_START:
        return ParsedTerminalInput(kind="paste_start", raw=raw)
    if raw == _BRACKETED_PASTE_END:
        return ParsedTerminalInput(kind="paste_end", raw=raw)

    for parser in (
        _parse_terminal_response,
        _parse_sgr_mouse,
        _parse_x10_mouse,
        _parse_csi_u,
        _parse_modify_other_keys,
        _parse_csi_navigation,
        _parse_csi_tilde_key,
        _parse_ss3_key,
    ):
        parsed = parser(raw)
        if parsed is not None:
            return parsed

    if raw.startswith("\x1b") and not raw.startswith(("\x1b[", "\x1b]", "\x1bP")):
        nested = parse_terminal_input(raw[1:])
        if nested is not None and nested.kind == "key":
            return replace(nested, raw=raw, alt=True)

    return _parse_plain_key(raw)


def parse_keypress(raw: str) -> Optional[ParsedTerminalInput]:
    """Parse only keyboard events, filtering out paste, mouse, and responses."""
    parsed = parse_terminal_input(raw)
    if parsed is None or parsed.kind != "key":
        return None
    return parsed


__all__ = ["ParsedTerminalInput", "parse_keypress", "parse_terminal_input"]
