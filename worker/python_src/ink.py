"""TUI rendering module - main entry point.

Ported from src/ink.ts
"""

from __future__ import annotations

from python_src.ink.ink import Ink, get_ink, render
from python_src.ink.layout.engine import (
    AlignItems,
    FlexDirection,
    JustifyContent,
    LayoutNode,
    calculate_layout,
)
from python_src.ink.output import Output
from python_src.ink.renderer import RenderFrame, Renderer, create_renderer
from python_src.ink.root import Root, create_root
from python_src.ink.screen import (
    Cell,
    CellWidth,
    CharPool,
    HyperlinkPool,
    Screen,
    StylePool,
)
from python_src.ink.parse_keypress import (
    ParsedTerminalInput,
    parse_keypress,
    parse_terminal_input,
)
from python_src.ink.terminal import (
    TerminalCapabilities,
    TerminalProbeRequest,
    get_terminal_size,
    is_extended_keys_supported,
    is_tty,
)

__all__ = [
    "AlignItems",
    "Cell",
    "CellWidth",
    "CharPool",
    "FlexDirection",
    "HyperlinkPool",
    "Ink",
    "JustifyContent",
    "LayoutNode",
    "Output",
    "ParsedTerminalInput",
    "RenderFrame",
    "Renderer",
    "Root",
    "Screen",
    "StylePool",
    "TerminalCapabilities",
    "TerminalProbeRequest",
    "calculate_layout",
    "create_renderer",
    "create_root",
    "get_ink",
    "get_terminal_size",
    "is_extended_keys_supported",
    "is_tty",
    "parse_keypress",
    "parse_terminal_input",
    "render",
]
