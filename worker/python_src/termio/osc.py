from __future__ import annotations

import base64
import platform
import shutil
from enum import IntEnum
from pathlib import Path
from urllib.parse import quote

from .ansi import DCS, ESC, OSC, ST


def sequence(command: str, payload: str = "") -> str:
    return f"{OSC}{command};{payload}{ST}"


def set_title(title: str) -> str:
    return sequence("0", title)


def set_icon_name(name: str) -> str:
    return sequence("1", name)


def set_window_title(title: str) -> str:
    return sequence("2", title)


def set_current_directory(path: str | Path, *, host: str = "") -> str:
    if isinstance(path, str) and path.startswith("file://"):
        uri = path
    else:
        resolved = Path(path).expanduser().resolve()
        quoted_path = quote(resolved.as_posix(), safe="/")
        uri = f"file://{host}{quoted_path}"
    return sequence("7", uri)


def open_hyperlink(url: str) -> str:
    return sequence("8;", url)


def close_hyperlink() -> str:
    return sequence("8;", "")


def hyperlink(label: str, url: str) -> str:
    return f"{open_hyperlink(url)}{label}{close_hyperlink()}"


def set_clipboard(text: str, *, clipboard: str = "c") -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return sequence("52", f"{clipboard};{encoded}")


def clear_clipboard(*, clipboard: str = "c") -> str:
    return sequence("52", f"{clipboard};")


def query_clipboard(*, clipboard: str = "c") -> str:
    return sequence("52", f"{clipboard};?")


class ProgressState(IntEnum):
    """OSC 9;4 progress bar states."""

    CLEAR = 0
    NORMAL = 1
    ERROR = 2
    INDETERMINATE = 3
    WARNING = 4


def set_progress_bar(state: ProgressState, percent: int | None = None) -> str:
    if state in {ProgressState.NORMAL, ProgressState.WARNING} and percent is None:
        raise ValueError("percent is required for normal and warning progress states")
    if state == ProgressState.INDETERMINATE and percent is not None:
        raise ValueError("indeterminate progress does not accept a percent value")
    if percent is not None:
        percent = max(0, min(100, int(percent)))
        return sequence("9", f"4;{int(state)};{percent}")
    return sequence("9", f"4;{int(state)}")


def clear_progress_bar() -> str:
    return sequence("9", "4")


def parse_osc(params: str) -> dict[str, int | str]:
    """Parse an OSC parameter string into its code and data components.

    Examples:
        ``"0;title"`` → ``{"code": 0, "data": "title"}``
        ``"52;c;aGVsbG8="`` → ``{"code": 52, "data": "c;aGVsbG8="}``
    """
    separator = params.find(";")
    if separator == -1:
        return {"code": int(params), "data": ""}
    code_str = params[:separator]
    data = params[separator + 1 :]
    return {"code": int(code_str), "data": data}


def wrap_for_multiplexer(sequence: str) -> str:
    """Wrap an escape sequence for tmux passthrough using DCS.

    Wraps the given *sequence* in a Device Control String so that tmux
    forwards it to the underlying terminal::

        ESC P tmux ; ESC \\ <sequence> ESC \\
    """
    return f"{DCS}tmux;{ESC}\\{sequence}{ST}"


def get_clipboard_command() -> str:
    """Return the name of the platform-appropriate clipboard tool.

    Returns ``"pbcopy"`` on macOS, ``"xclip"`` on Linux, and ``"clip"``
    on Windows.  Falls back to ``"xclip"`` for unknown platforms.
    """
    system = platform.system()
    if system == "Darwin":
        return "pbcopy"
    if system == "Windows":
        return "clip"
    return "xclip"
