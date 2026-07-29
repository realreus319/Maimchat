"""TUI root and render entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Optional

from .layout.engine import LayoutNode
from .renderer import RenderFrame, Renderer, create_renderer
from .screen import Cell, CharPool, HyperlinkPool, Rectangle, Screen, StylePool


@dataclass(frozen=True)
class RenderPatch:
    x: int
    y: int
    previous: Cell
    current: Cell


@dataclass
class Root:
    """Root of the TUI tree with double-buffer-style frame tracking."""

    node: LayoutNode
    renderer: Optional[Renderer] = None
    style_pool: Optional[StylePool] = None
    front_frame: Optional[RenderFrame] = None
    last_patches: tuple[RenderPatch, ...] = field(default_factory=tuple)
    last_damage: Optional[Rectangle] = None

    def __post_init__(self) -> None:
        if self.style_pool is None:
            self.style_pool = StylePool()
        if self.renderer is None:
            self.renderer = create_renderer(self.node, self.style_pool)

    def render_frame(
        self,
        width: int,
        height: int,
        is_tty: bool = False,
        *,
        dirty_layout_nodes: Collection[int] | None = None,
    ) -> RenderFrame:
        if self.renderer is None:
            if self.style_pool is None:
                self.style_pool = StylePool()
            self.renderer = create_renderer(self.node, self.style_pool)
        frame = self.renderer.render(
            width,
            height,
            is_tty,
            dirty_layout_nodes=dirty_layout_nodes,
        )
        previous = (
            self.front_frame.screen
            if self.front_frame is not None
            else _create_empty_screen_like(frame.screen)
        )
        self.last_patches = tuple(
            RenderPatch(x=x, y=y, previous=before, current=after)
            for x, y, before, after in previous.diff(frame.screen)
        )
        self.last_damage = _patches_to_damage(self.last_patches)
        frame.screen.damage = self.last_damage
        self.front_frame = frame
        return frame

    def diff(self) -> tuple[RenderPatch, ...]:
        return self.last_patches

    def render_to_string(
        self,
        width: int,
        height: int,
        *,
        is_tty: bool = False,
        include_styles: bool = False,
        dirty_layout_nodes: Collection[int] | None = None,
    ) -> str:
        frame = self.render_frame(
            width,
            height,
            is_tty=is_tty,
            dirty_layout_nodes=dirty_layout_nodes,
        )
        return frame.screen.render_to_string(include_styles=include_styles)


def _create_empty_screen_like(screen: Screen) -> Screen:
    return Screen(
        screen.width,
        screen.height,
        CharPool(),
        HyperlinkPool(),
        screen.style_pool,
    )


def _patches_to_damage(patches: tuple[RenderPatch, ...]) -> Optional[Rectangle]:
    if not patches:
        return None
    min_x = min(patch.x for patch in patches)
    min_y = min(patch.y for patch in patches)
    max_x = max(patch.x for patch in patches)
    max_y = max(patch.y for patch in patches)
    return Rectangle(
        min_x,
        min_y,
        max_x - min_x + 1,
        max_y - min_y + 1,
    )


def create_root(node: LayoutNode, style_pool: Optional[StylePool] = None) -> Root:
    """Create a root for the TUI tree."""
    return Root(node=node, style_pool=style_pool)


def render(
    root: Root,
    width: int,
    height: int,
    is_tty: bool = False,
    *,
    dirty_layout_nodes: Collection[int] | None = None,
) -> Optional[RenderFrame]:
    """Render the root to a frame and update patch tracking."""
    if root.renderer is None:
        return None
    return root.render_frame(width, height, is_tty, dirty_layout_nodes=dirty_layout_nodes)


__all__ = ["RenderPatch", "Root", "create_root", "render"]
