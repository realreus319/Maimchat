"""Renderer for TUI output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Optional

from .layout.engine import LayoutEngine, LayoutNode
from .output import Output
from .screen import CellWidth, CharPool, HyperlinkPool, Screen, StylePool
from .terminal import iter_display_segments


@dataclass
class RenderFrame:
    """A rendered frame with screen and metadata."""

    screen: Screen
    cursor_x: int
    cursor_y: int
    cursor_visible: bool


@dataclass(frozen=True)
class CachedSubtree:
    """Cached subtree render for lightweight dirty-subtree reuse."""

    width: int
    height: int
    signature: tuple[object, ...]
    screen: Screen


@dataclass(frozen=True)
class DeclaredCursor:
    """Absolute cursor position declared by a layout subtree."""

    x: int
    y: int
    visible: bool


class Renderer:
    """Renderer that produces frames from layout tree.

    Ported from src/ink/renderer.ts
    """

    def __init__(
        self,
        root: LayoutNode,
        style_pool: StylePool,
    ) -> None:
        self._root = root
        self._style_pool = style_pool
        self._output: Optional[Output] = None
        self._char_pool = CharPool()
        self._hyperlink_pool = HyperlinkPool()
        self._layout_engine = LayoutEngine()
        self._node_cache: dict[int, CachedSubtree] = {}
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_reused_cells = 0
        self.last_layout_cache_hits = 0
        self.last_layout_cache_misses = 0
        self.last_layout_nodes_reused = 0

    def render(
        self,
        width: int,
        height: int,
        is_tty: bool = False,
        *,
        dirty_layout_nodes: Collection[int] | None = None,
    ) -> RenderFrame:
        """Render the current layout tree to a frame."""
        if width <= 0 or height <= 0:
            screen = Screen(
                0, 0, self._char_pool, self._hyperlink_pool, self._style_pool
            )
            return RenderFrame(screen, 0, 0, True)

        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_reused_cells = 0
        active_ids = self._collect_active_nodes(self._root)
        self._layout_engine.prune_cache(active_ids)
        self._layout_engine.calculate_layout(self._root, float(width), float(height))
        self.last_layout_cache_hits = self._layout_engine.last_cache_hits
        self.last_layout_cache_misses = self._layout_engine.last_cache_misses
        self.last_layout_nodes_reused = self._layout_engine.last_nodes_reused

        screen = Screen(
            width, height, self._char_pool, self._hyperlink_pool, self._style_pool
        )

        if self._output is None:
            self._output = Output(width, height, self._style_pool, screen)
        else:
            self._output.reset(width, height, screen)

        self._prune_cache(active_ids)
        dirty_node_ids = frozenset(dirty_layout_nodes or ())
        self._render_cached_subtree(
            self._root,
            screen,
            0,
            0,
            dirty_layout_nodes=dirty_node_ids if dirty_node_ids else None,
        )
        declared_cursor = self._resolve_declared_cursor(self._root)
        if declared_cursor is None:
            cursor_x = 0
            cursor_y = height
            cursor_visible = not is_tty or height == 0
        else:
            cursor_x = max(0, min(width - 1, declared_cursor.x)) if width > 0 else 0
            cursor_y = max(0, min(height - 1, declared_cursor.y)) if height > 0 else 0
            cursor_visible = declared_cursor.visible

        return RenderFrame(
            screen=screen,
            cursor_x=cursor_x,
            cursor_y=cursor_y,
            cursor_visible=cursor_visible,
        )

    def _resolve_declared_cursor(
        self,
        node: LayoutNode,
        *,
        origin_x: int = 0,
        origin_y: int = 0,
    ) -> DeclaredCursor | None:
        declared_cursor: DeclaredCursor | None = None
        local_cursor_x = getattr(node, "cursor_x", None)
        local_cursor_y = getattr(node, "cursor_y", None)
        local_cursor_visible = getattr(node, "cursor_visible", None)

        if (
            local_cursor_x is not None
            or local_cursor_y is not None
            or local_cursor_visible is not None
        ):
            declared_cursor = DeclaredCursor(
                x=origin_x + int(local_cursor_x or 0),
                y=origin_y + int(local_cursor_y or 0),
                visible=True if local_cursor_visible is None else bool(local_cursor_visible),
            )

        for child in node.children or []:
            child_cursor = self._resolve_declared_cursor(
                child,
                origin_x=origin_x + int(child.computed_x),
                origin_y=origin_y + int(child.computed_y),
            )
            if child_cursor is not None:
                declared_cursor = child_cursor

        return declared_cursor

    def _render_cached_subtree(
        self,
        node: LayoutNode,
        target: Screen,
        origin_x: int,
        origin_y: int,
        *,
        dirty_layout_nodes: frozenset[int] | None = None,
    ) -> None:
        """Render a node, reusing cached subtree output when unchanged."""
        width = int(node.computed_width)
        height = int(node.computed_height)
        if width <= 0 or height <= 0:
            return

        cached = self._node_cache.get(id(node))
        if (
            dirty_layout_nodes is not None
            and id(node) not in dirty_layout_nodes
            and cached is not None
            and cached.width == width
            and cached.height == height
        ):
            target.blit_region(cached.screen, 0, 0, width, height, origin_x, origin_y)
            self.last_cache_hits += 1
            self.last_reused_cells += width * height
            return

        signature = self._node_signature(node)
        if (
            cached is not None
            and cached.width == width
            and cached.height == height
            and cached.signature == signature
        ):
            target.blit_region(cached.screen, 0, 0, width, height, origin_x, origin_y)
            self.last_cache_hits += 1
            self.last_reused_cells += width * height
            return

        subtree = Screen(
            width,
            height,
            self._char_pool,
            self._hyperlink_pool,
            self._style_pool,
        )
        self._render_node_absolute(
            node,
            subtree,
            0,
            0,
            dirty_layout_nodes=dirty_layout_nodes,
        )
        self._node_cache[id(node)] = CachedSubtree(
            width=width,
            height=height,
            signature=signature,
            screen=subtree,
        )
        target.blit_region(subtree, 0, 0, width, height, origin_x, origin_y)
        self.last_cache_misses += 1

    def _render_node_absolute(
        self,
        node: LayoutNode,
        screen: Screen,
        origin_x: int,
        origin_y: int,
        *,
        dirty_layout_nodes: frozenset[int] | None = None,
    ) -> None:
        """Render a single node into a screen at an absolute origin."""
        width = int(node.computed_width)
        height = int(node.computed_height)
        self._render_text(node, screen, origin_x, origin_y, width, height)
        for child in node.children or []:
            self._render_cached_subtree(
                child,
                screen,
                origin_x + int(child.computed_x),
                origin_y + int(child.computed_y),
                dirty_layout_nodes=dirty_layout_nodes,
            )

    def _render_text(
        self,
        node: LayoutNode,
        screen: Screen,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> None:
        text = getattr(node, "text", None)
        if not text or width <= 0 or height <= 0:
            return

        style_id = self._resolve_style_id(node)
        hyperlink = getattr(node, "hyperlink", None)
        cursor_x = 0
        cursor_y = 0

        for segment, segment_width in iter_display_segments(text):
            if cursor_y >= height:
                break

            if segment == "\n":
                cursor_x = 0
                cursor_y += 1
                continue

            if segment_width <= 0:
                continue

            if segment_width > width:
                cursor_x = 0
                cursor_y += 1
                continue

            if cursor_x + segment_width > width:
                cursor_x = 0
                cursor_y += 1
                if cursor_y >= height:
                    break

            screen.set_cell(
                x + cursor_x,
                y + cursor_y,
                segment,
                style_id=style_id,
                hyperlink=hyperlink,
                width=CellWidth.WIDE if segment_width == 2 else CellWidth.NARROW,
            )

            if segment_width == 2 and cursor_x + 1 < width:
                screen.set_cell(
                    x + cursor_x + 1,
                    y + cursor_y,
                    "",
                    style_id=style_id,
                    hyperlink=hyperlink,
                    width=CellWidth.SPACER_TAIL,
                )

            cursor_x += segment_width
            if cursor_x >= width:
                cursor_x = 0
                cursor_y += 1

    def _node_signature(self, node: LayoutNode) -> tuple[object, ...]:
        return (
            getattr(node, "text", None),
            tuple(getattr(node, "styles", None) or ()),
            getattr(node, "style_id", None),
            getattr(node, "hyperlink", None),
            tuple(
                (
                    int(child.computed_x),
                    int(child.computed_y),
                    int(child.computed_width),
                    int(child.computed_height),
                    self._node_signature(child),
                )
                for child in (node.children or [])
            ),
        )

    def _collect_active_nodes(self, node: LayoutNode) -> set[int]:
        active = {id(node)}
        for child in node.children or []:
            active.update(self._collect_active_nodes(child))
        return active

    def _prune_cache(self, active_ids: set[int]) -> None:
        stale = [node_id for node_id in self._node_cache if node_id not in active_ids]
        for node_id in stale:
            del self._node_cache[node_id]

    def _resolve_style_id(self, node: LayoutNode) -> int:
        explicit_style_id = getattr(node, "style_id", None)
        if isinstance(explicit_style_id, int):
            return explicit_style_id
        styles = getattr(node, "styles", None) or []
        if not styles:
            return self._style_pool.none
        return self._style_pool.intern(list(styles))


def create_renderer(
    root: LayoutNode, style_pool: Optional[StylePool] = None
) -> Renderer:
    """Factory function to create a renderer."""
    return Renderer(root, style_pool or StylePool())
