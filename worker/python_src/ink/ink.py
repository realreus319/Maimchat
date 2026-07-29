"""Main TUI rendering module."""

from __future__ import annotations

from typing import Optional

from .layout.engine import LayoutNode
from .root import Root, create_root
from .screen import CharPool, HyperlinkPool, Screen, StylePool
from .terminal import TerminalCapabilities, get_terminal_size


class Ink:
    """Main TUI rendering controller.

    Ported from src/ink/ink.tsx
    """

    def __init__(self) -> None:
        self._terminal = TerminalCapabilities()
        self._style_pool = StylePool()
        self._char_pool = CharPool()
        self._hyperlink_pool = HyperlinkPool()
        self._root: Optional[Root] = None
        self._screen: Optional[Screen] = None

    def create_root(self, node: LayoutNode) -> Root:
        """Create a root node for the TUI tree."""
        self._root = create_root(node, style_pool=self._style_pool)
        return self._root

    def render(self, width: Optional[int] = None, height: Optional[int] = None) -> str:
        """Render the current TUI tree to a string."""
        if width is None or height is None:
            width, height = get_terminal_size()

        if self._root is None:
            return ""
        return self._root.render_to_string(
            width,
            height,
            is_tty=self._terminal.is_tty,
        )

    def clear(self) -> None:
        """Clear the screen."""
        if self._screen:
            self._screen.clear()


# Global instance
_ink_instance: Optional[Ink] = None


def get_ink() -> Ink:
    """Get or create the global Ink instance."""
    global _ink_instance
    if _ink_instance is None:
        _ink_instance = Ink()
    return _ink_instance


def render(node: LayoutNode) -> str:
    """Render a node to string."""
    ink = get_ink()
    root = ink.create_root(node)
    return ink.render()


# Re-export core components
from .layout.engine import FlexDirection
from .renderer import RenderFrame
from .terminal import is_tty

__all__ = [
    "Ink",
    "LayoutNode",
    "FlexDirection",
    "RenderFrame",
    "Screen",
    "StylePool",
    "CharPool",
    "HyperlinkPool",
    "get_ink",
    "render",
    "is_tty",
]
