"""Main entry point for TUI module."""

from __future__ import annotations

from .ink import Ink, get_ink, render
from .layout.engine import FlexDirection, LayoutNode, calculate_layout
from .events import (
    ClickEvent,
    EventDispatcher,
    EventPhase,
    EventPriority,
    FocusEvent,
    KeyboardEvent,
    MouseEvent,
    TerminalEvent,
)
from .focus_manager import FocusManager
from .parse_keypress import ParsedTerminalInput, parse_keypress, parse_terminal_input
from .reconciler import ComponentNode, Reconciler, create_reconciler
from .renderer import RenderFrame, create_renderer
from .root import RenderPatch, Root, create_root
from .screen import Cell, CellWidth, CharPool, HyperlinkPool, Screen, StylePool
from .terminal import (
    TerminalCapabilities,
    TerminalProbeRequest,
    get_terminal_size,
    is_extended_keys_supported,
    is_hyperlink_supported,
    is_synchronized_output_supported,
    is_truecolor_supported,
    is_tty,
    iter_display_segments,
    string_width,
    strip_ansi_sequences,
    wrap_synchronized_output,
)

__all__ = [
    "ClickEvent",
    "ComponentNode",
    "create_reconciler",
    "Ink",
    "LayoutNode",
    "FlexDirection",
    "EventDispatcher",
    "EventPhase",
    "EventPriority",
    "FocusEvent",
    "FocusManager",
    "KeyboardEvent",
    "MouseEvent",
    "ParsedTerminalInput",
    "RenderFrame",
    "RenderPatch",
    "Reconciler",
    "Root",
    "Screen",
    "Cell",
    "CellWidth",
    "StylePool",
    "CharPool",
    "HyperlinkPool",
    "TerminalEvent",
    "TerminalCapabilities",
    "TerminalProbeRequest",
    "calculate_layout",
    "create_renderer",
    "create_root",
    "get_ink",
    "get_terminal_size",
    "is_extended_keys_supported",
    "is_hyperlink_supported",
    "is_synchronized_output_supported",
    "is_truecolor_supported",
    "is_tty",
    "iter_display_segments",
    "parse_keypress",
    "parse_terminal_input",
    "render",
    "string_width",
    "strip_ansi_sequences",
    "wrap_synchronized_output",
]
