"""Layout engine for TUI renderer using flexbox layout."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Tuple

Length = float | str


class FlexDirection(Enum):
    COLUMN = "column"
    ROW = "row"


class JustifyContent(Enum):
    START = "flex-start"
    END = "flex-end"
    CENTER = "center"
    SPACE_BETWEEN = "space-between"
    SPACE_AROUND = "space-around"


class AlignItems(Enum):
    START = "flex-start"
    END = "flex-end"
    CENTER = "center"
    STRETCH = "stretch"
    BASELINE = "baseline"


@dataclass
class LayoutNode:
    """A node in the layout tree."""

    width: Optional[Length] = None
    height: Optional[Length] = None
    min_width: Optional[float] = None
    max_width: Optional[float] = None
    min_height: Optional[float] = None
    max_height: Optional[float] = None
    flex_grow: float = 0.0
    flex_shrink: float = 1.0
    flex_basis: Optional[Length] = None
    direction: FlexDirection = FlexDirection.COLUMN
    justify_content: JustifyContent = JustifyContent.START
    align_items: AlignItems = AlignItems.STRETCH
    align_self: Optional[AlignItems] = None
    baseline_offset: Optional[float] = None
    order: int = 0
    aspect_ratio: Optional[float] = None
    flex_wrap: bool = False
    padding_top: float = 0.0
    padding_bottom: float = 0.0
    padding_left: float = 0.0
    padding_right: float = 0.0
    margin_top: float = 0.0
    margin_bottom: float = 0.0
    margin_left: float = 0.0
    margin_right: float = 0.0
    computed_x: float = 0.0
    computed_y: float = 0.0
    computed_width: float = 0.0
    computed_height: float = 0.0
    text: Optional[str] = None
    styles: Optional[List[str]] = None
    hyperlink: Optional[str] = None
    cursor_x: Optional[float] = None
    cursor_y: Optional[float] = None
    cursor_visible: Optional[bool] = None
    children: Optional[List[LayoutNode]] = None
    parent: Optional[LayoutNode] = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []
        if self.styles is None:
            self.styles = []
        for child in self.children:
            child.parent = self


@dataclass
class Rectangle:
    """2D rectangle with position and size."""

    x: float
    y: float
    width: float
    height: float


@dataclass
class Point:
    """2D point."""

    x: float
    y: float


@dataclass
class Size:
    """2D size."""

    width: float
    height: float


@dataclass(frozen=True)
class _CachedLayoutSnapshot:
    """Relative layout snapshot for lightweight measurement-cache reuse."""

    computed_x: float
    computed_y: float
    computed_width: float
    computed_height: float
    children: tuple["_CachedLayoutSnapshot", ...] = field(default_factory=tuple)

    @property
    def node_count(self) -> int:
        return 1 + sum(child.node_count for child in self.children)


@dataclass(frozen=True)
class _CachedLayoutEntry:
    """Memoized layout result for a node under a specific container size/signature."""

    available_width: float
    available_height: float
    signature: tuple[Any, ...]
    snapshot: _CachedLayoutSnapshot


class LayoutEngine:
    """Simplified flexbox layout engine.

    Ported from src/ink/layout/engine.ts and src/ink/layout/yoga.ts
    """

    def __init__(self) -> None:
        self._layout_cache: dict[int, _CachedLayoutEntry] = {}
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_nodes_reused = 0

    def calculate_layout(self, root: LayoutNode, width: float, height: float) -> None:
        """Calculate layout for the entire tree."""
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_nodes_reused = 0
        self._layout_node(root, width, height)

    def prune_cache(self, active_ids: set[int]) -> None:
        """Drop stale memoized layout entries for nodes no longer in the tree."""
        stale = [node_id for node_id in self._layout_cache if node_id not in active_ids]
        for node_id in stale:
            del self._layout_cache[node_id]

    def _layout_node(
        self, node: LayoutNode, available_width: float, available_height: float
    ) -> None:
        cached = self._get_cached_layout(node, available_width, available_height)
        if cached is not None:
            self._restore_cached_layout(node, cached.snapshot)
            self.last_cache_hits += 1
            self.last_nodes_reused += cached.snapshot.node_count
            return

        node.computed_x = 0
        node.computed_y = 0

        width = (
            self._resolve_length(node.width, available_width)
            if node.width is not None
            else max(0, available_width - node.margin_left - node.margin_right)
        )
        height = (
            self._resolve_length(node.height, available_height)
            if node.height is not None
            else max(0, available_height - node.margin_top - node.margin_bottom)
        )
        if node.aspect_ratio is not None and node.aspect_ratio > 0:
            if node.width is not None and node.height is None:
                height = width / node.aspect_ratio
            elif node.height is not None and node.width is None:
                width = height * node.aspect_ratio
        node.computed_width = self._clamp_dimension(
            width,
            minimum=node.min_width,
            maximum=node.max_width,
        )
        node.computed_height = self._clamp_dimension(
            height,
            minimum=node.min_height,
            maximum=node.max_height,
        )

        if not node.children:
            self._store_cached_layout(node, available_width, available_height)
            return

        content_width = node.computed_width - node.padding_left - node.padding_right
        content_height = node.computed_height - node.padding_top - node.padding_bottom

        if node.direction == FlexDirection.COLUMN:
            self._layout_column(node, content_width, content_height)
        else:
            self._layout_row(node, content_width, content_height)
        self._store_cached_layout(node, available_width, available_height)

    def _layout_column(self, node: LayoutNode, width: float, height: float) -> None:
        children = sorted(node.children or [], key=lambda child: child.order)
        total_flex_grow = sum(child.flex_grow for child in children if child.flex_grow > 0)
        total_margins = sum(child.margin_top + child.margin_bottom for child in children)
        total_fixed_height = sum(
            self._clamp_dimension(
                child.height if child.height is not None else (child.flex_basis or 0),
                container_size=height,
                minimum=child.min_height,
                maximum=child.max_height,
            )
            for child in children
            if child.flex_grow <= 0
        )
        available_flex_height = max(0, height - total_fixed_height - total_margins)
        child_sizes: list[tuple[LayoutNode, float, float]] = []

        for child in children:
            child_align = child.align_self or node.align_items
            child_height = self._resolve_child_main_size(
                child,
                available_flex_height,
                total_flex_grow,
                axis="column",
                container_main=height,
                container_cross=width,
            )
            child_width = self._resolve_child_cross_size(
                child,
                available_cross=width,
                resolved_main=child_height,
                axis="column",
                align=child_align,
                container_cross=width,
            )
            child_sizes.append((child, child_width, child_height))

        if node.flex_wrap and child_sizes:
            self._layout_wrapped_columns(node, width, child_sizes)
            return

        total_outer_height = sum(
            child_height + child.margin_top + child.margin_bottom
            for child, _, child_height in child_sizes
        )
        start_offset, gap = self._resolve_justification(
            available_space=height,
            used_space=total_outer_height,
            item_count=len(child_sizes),
            justify=node.justify_content,
        )

        current_y = node.padding_top + start_offset

        for child, child_width, child_height in child_sizes:
            child_align = child.align_self or node.align_items
            child.computed_height = child_height
            child.computed_width = child_width
            child.computed_x = (
                node.padding_left
                + child.margin_left
                + self._resolve_cross_alignment_offset(
                    available_cross=width,
                    child_cross=child_width,
                    leading_margin=child.margin_left,
                    trailing_margin=child.margin_right,
                    align=child_align,
                )
            )
            child.computed_y = current_y + child.margin_top

            # Save computed position before recursive call
            saved_x = child.computed_x
            saved_y = child.computed_y
            self._layout_child(child)
            # Restore computed position after recursive call
            child.computed_x = saved_x
            child.computed_y = saved_y

            current_y += (
                child.computed_height + child.margin_top + child.margin_bottom + gap
            )

    def _layout_row(self, node: LayoutNode, width: float, height: float) -> None:
        children = sorted(node.children or [], key=lambda child: child.order)
        total_flex_grow = sum(child.flex_grow for child in children if child.flex_grow > 0)
        total_margins = sum(child.margin_left + child.margin_right for child in children)
        total_fixed_width = sum(
            self._clamp_dimension(
                child.width if child.width is not None else (child.flex_basis or 0),
                container_size=width,
                minimum=child.min_width,
                maximum=child.max_width,
            )
            for child in children
            if child.flex_grow <= 0
        )
        available_flex_width = max(0, width - total_fixed_width - total_margins)
        child_sizes: list[tuple[LayoutNode, float, float]] = []

        for child in children:
            child_align = child.align_self or node.align_items
            child_width = self._resolve_child_main_size(
                child,
                available_flex_width,
                total_flex_grow,
                axis="row",
                container_main=width,
                container_cross=height,
            )
            child_height = self._resolve_child_cross_size(
                child,
                available_cross=height,
                resolved_main=child_width,
                axis="row",
                align=child_align,
                container_cross=height,
            )
            child_sizes.append((child, child_width, child_height))

        if node.flex_wrap and child_sizes:
            self._layout_wrapped_rows(node, width, child_sizes)
            return

        total_outer_width = sum(
            child_width + child.margin_left + child.margin_right
            for child, child_width, _ in child_sizes
        )
        start_offset, gap = self._resolve_justification(
            available_space=width,
            used_space=total_outer_width,
            item_count=len(child_sizes),
            justify=node.justify_content,
        )

        current_x = node.padding_left + start_offset
        line_baseline = self._resolve_row_line_baseline(child_sizes, node)

        for child, child_width, child_height in child_sizes:
            child_align = child.align_self or node.align_items
            child.computed_width = child_width
            child.computed_height = child_height
            child.computed_x = current_x + child.margin_left
            child.computed_y = (
                node.padding_top
                + child.margin_top
                + self._resolve_cross_alignment_offset(
                    available_cross=height,
                    child_cross=child_height,
                    leading_margin=child.margin_top,
                    trailing_margin=child.margin_bottom,
                    align=child_align,
                    baseline_position=line_baseline,
                    child_baseline=self._resolve_child_baseline(child, child_height),
                )
            )

            saved_x = child.computed_x
            saved_y = child.computed_y
            self._layout_child(child)
            child.computed_x = saved_x
            child.computed_y = saved_y

            current_x += child.computed_width + child.margin_left + child.margin_right + gap

    def _resolve_child_main_size(
        self,
        child: LayoutNode,
        available_main: float,
        total_flex_grow: float,
        *,
        axis: str,
        container_main: float,
        container_cross: float,
    ) -> float:
        explicit = self._resolve_length(
            child.width if axis == "row" else child.height,
            container_main,
        )
        cross_explicit = self._resolve_length(
            child.height if axis == "row" else child.width,
            container_cross,
        )
        flex_basis = self._resolve_length(child.flex_basis, container_main)
        minimum = child.min_width if axis == "row" else child.min_height
        maximum = child.max_width if axis == "row" else child.max_height

        if child.flex_grow > 0 and total_flex_grow > 0:
            base = (child.flex_grow / total_flex_grow) * available_main
        elif explicit is not None:
            base = explicit
        elif flex_basis is not None:
            base = flex_basis
        elif (
            child.aspect_ratio is not None
            and child.aspect_ratio > 0
            and cross_explicit is not None
        ):
            base = (
                cross_explicit * child.aspect_ratio
                if axis == "row"
                else cross_explicit / child.aspect_ratio
            )
        else:
            base = 0.0
        return self._clamp_dimension(base, minimum=minimum, maximum=maximum)

    def _resolve_child_cross_size(
        self,
        child: LayoutNode,
        available_cross: float,
        resolved_main: float,
        *,
        axis: str,
        align: AlignItems,
        container_cross: float,
    ) -> float:
        explicit = self._resolve_length(
            child.height if axis == "row" else child.width,
            container_cross,
        )
        minimum = child.min_height if axis == "row" else child.min_width
        maximum = child.max_height if axis == "row" else child.max_width
        leading_margin = child.margin_top if axis == "row" else child.margin_left
        trailing_margin = child.margin_bottom if axis == "row" else child.margin_right

        if explicit is not None:
            base = explicit
        elif child.aspect_ratio is not None and child.aspect_ratio > 0 and resolved_main > 0:
            base = (
                resolved_main / child.aspect_ratio
                if axis == "row"
                else resolved_main * child.aspect_ratio
            )
        else:
            base = max(0, available_cross - leading_margin - trailing_margin)
            if align == AlignItems.STRETCH:
                base = max(0, available_cross - leading_margin - trailing_margin)
        return self._clamp_dimension(base, minimum=minimum, maximum=maximum)

    def _resolve_cross_alignment_offset(
        self,
        *,
        available_cross: float,
        child_cross: float,
        leading_margin: float,
        trailing_margin: float,
        align: AlignItems,
        baseline_position: float | None = None,
        child_baseline: float | None = None,
    ) -> float:
        if (
            align == AlignItems.BASELINE
            and baseline_position is not None
            and child_baseline is not None
        ):
            return max(0.0, baseline_position - leading_margin - child_baseline)
        free_space = max(
            0,
            available_cross - child_cross - leading_margin - trailing_margin,
        )
        if align == AlignItems.END:
            return free_space
        if align == AlignItems.CENTER:
            return free_space / 2
        return 0.0

    def _resolve_justification(
        self,
        *,
        available_space: float,
        used_space: float,
        item_count: int,
        justify: JustifyContent,
    ) -> tuple[float, float]:
        free_space = max(0.0, available_space - used_space)
        if item_count <= 0:
            return (0.0, 0.0)
        if justify == JustifyContent.END:
            return (free_space, 0.0)
        if justify == JustifyContent.CENTER:
            return (free_space / 2, 0.0)
        if justify == JustifyContent.SPACE_BETWEEN and item_count > 1:
            return (0.0, free_space / (item_count - 1))
        if justify == JustifyContent.SPACE_AROUND:
            gap = free_space / item_count if item_count > 0 else 0.0
            return (gap / 2, gap)
        return (0.0, 0.0)

    def _resolve_child_baseline(self, child: LayoutNode, child_cross: float) -> float:
        if child.baseline_offset is not None:
            return min(max(0.0, child.baseline_offset), child_cross)
        return max(0.0, child_cross)

    def _resolve_row_line_baseline(
        self,
        child_sizes: list[tuple[LayoutNode, float, float]],
        node: LayoutNode,
    ) -> float | None:
        has_baseline_alignment = any(
            (child.align_self or node.align_items) == AlignItems.BASELINE
            for child, _, _ in child_sizes
        )
        if not has_baseline_alignment:
            return None

        baseline = None
        for child, _, child_height in child_sizes:
            child_baseline = self._resolve_child_baseline(child, child_height)
            candidate = child.margin_top + child_baseline
            baseline = candidate if baseline is None else max(baseline, candidate)
        return baseline

    def _clamp_dimension(
        self,
        value: float | str,
        *,
        container_size: Optional[float] = None,
        minimum: Optional[float],
        maximum: Optional[float],
    ) -> float:
        resolved = self._resolve_length(value, container_size or 0.0)
        if resolved is None:
            resolved = 0.0
        clamped = max(0.0, resolved)
        if minimum is not None:
            clamped = max(clamped, minimum)
        if maximum is not None:
            clamped = min(clamped, maximum)
        return clamped

    def _resolve_length(
        self,
        value: Optional[Length],
        container_size: float,
    ) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.endswith("%"):
                percentage = float(stripped[:-1])
                return container_size * (percentage / 100.0)
            return float(stripped)
        return float(value)

    def _layout_child(self, child: LayoutNode) -> None:
        original_width = child.width
        original_height = child.height
        if isinstance(child.width, str):
            child.width = child.computed_width
        if isinstance(child.height, str):
            child.height = child.computed_height
        try:
            self._layout_node(child, child.computed_width, child.computed_height)
        finally:
            child.width = original_width
            child.height = original_height

    def _layout_wrapped_rows(
        self,
        node: LayoutNode,
        width: float,
        child_sizes: list[tuple[LayoutNode, float, float]],
    ) -> None:
        lines: list[list[tuple[LayoutNode, float, float]]] = []
        current_line: list[tuple[LayoutNode, float, float]] = []
        current_width = 0.0

        for child, child_width, child_height in child_sizes:
            outer_width = child_width + child.margin_left + child.margin_right
            if current_line and current_width + outer_width > width:
                lines.append(current_line)
                current_line = []
                current_width = 0.0
            current_line.append((child, child_width, child_height))
            current_width += outer_width

        if current_line:
            lines.append(current_line)

        current_y = node.padding_top

        for line in lines:
            line_width = sum(
                child_width + child.margin_left + child.margin_right
                for child, child_width, _ in line
            )
            line_height = max(
                child_height + child.margin_top + child.margin_bottom
                for child, _, child_height in line
            )
            start_offset, gap = self._resolve_justification(
                available_space=width,
                used_space=line_width,
                item_count=len(line),
                justify=node.justify_content,
            )
            current_x = node.padding_left + start_offset
            line_baseline = self._resolve_row_line_baseline(line, node)

            for child, child_width, child_height in line:
                child_align = child.align_self or node.align_items
                child.computed_width = child_width
                child.computed_height = child_height
                child.computed_x = current_x + child.margin_left
                child.computed_y = (
                    current_y
                    + child.margin_top
                    + self._resolve_cross_alignment_offset(
                        available_cross=line_height,
                        child_cross=child_height,
                        leading_margin=child.margin_top,
                        trailing_margin=child.margin_bottom,
                        align=child_align,
                        baseline_position=line_baseline,
                        child_baseline=self._resolve_child_baseline(child, child_height),
                    )
                )
                saved_x = child.computed_x
                saved_y = child.computed_y
                self._layout_child(child)
                child.computed_x = saved_x
                child.computed_y = saved_y
                current_x += child_width + child.margin_left + child.margin_right + gap

            current_y += line_height

    def _layout_wrapped_columns(
        self,
        node: LayoutNode,
        height: float,
        child_sizes: list[tuple[LayoutNode, float, float]],
    ) -> None:
        columns: list[list[tuple[LayoutNode, float, float]]] = []
        current_column: list[tuple[LayoutNode, float, float]] = []
        current_height = 0.0

        for child, child_width, child_height in child_sizes:
            outer_height = child_height + child.margin_top + child.margin_bottom
            if current_column and current_height + outer_height > height:
                columns.append(current_column)
                current_column = []
                current_height = 0.0
            current_column.append((child, child_width, child_height))
            current_height += outer_height

        if current_column:
            columns.append(current_column)

        current_x = node.padding_left

        for column in columns:
            column_height = sum(
                child_height + child.margin_top + child.margin_bottom
                for child, _, child_height in column
            )
            column_width = max(
                child_width + child.margin_left + child.margin_right
                for child, child_width, _ in column
            )
            start_offset, gap = self._resolve_justification(
                available_space=height,
                used_space=column_height,
                item_count=len(column),
                justify=node.justify_content,
            )
            current_y = node.padding_top + start_offset

            for child, child_width, child_height in column:
                child_align = child.align_self or node.align_items
                child.computed_width = child_width
                child.computed_height = child_height
                child.computed_x = (
                    current_x
                    + child.margin_left
                    + self._resolve_cross_alignment_offset(
                        available_cross=column_width,
                        child_cross=child_width,
                        leading_margin=child.margin_left,
                        trailing_margin=child.margin_right,
                        align=child_align,
                    )
                )
                child.computed_y = current_y + child.margin_top
                saved_x = child.computed_x
                saved_y = child.computed_y
                self._layout_child(child)
                child.computed_x = saved_x
                child.computed_y = saved_y
                current_y += child_height + child.margin_top + child.margin_bottom + gap

            current_x += column_width

    def _layout_signature(self, node: LayoutNode) -> tuple[Any, ...]:
        return (
            node.width,
            node.height,
            node.min_width,
            node.max_width,
            node.min_height,
            node.max_height,
            node.flex_grow,
            node.flex_shrink,
            node.flex_basis,
            node.direction.value,
            node.justify_content.value,
            node.align_items.value,
            node.align_self.value if node.align_self is not None else None,
            node.baseline_offset,
            node.order,
            node.aspect_ratio,
            node.flex_wrap,
            node.padding_top,
            node.padding_bottom,
            node.padding_left,
            node.padding_right,
            node.margin_top,
            node.margin_bottom,
            node.margin_left,
            node.margin_right,
            tuple(self._layout_signature(child) for child in (node.children or [])),
        )

    def _capture_layout(self, node: LayoutNode) -> _CachedLayoutSnapshot:
        return _CachedLayoutSnapshot(
            computed_x=node.computed_x,
            computed_y=node.computed_y,
            computed_width=node.computed_width,
            computed_height=node.computed_height,
            children=tuple(self._capture_layout(child) for child in (node.children or [])),
        )

    def _restore_cached_layout(
        self,
        node: LayoutNode,
        snapshot: _CachedLayoutSnapshot,
    ) -> None:
        node.computed_x = snapshot.computed_x
        node.computed_y = snapshot.computed_y
        node.computed_width = snapshot.computed_width
        node.computed_height = snapshot.computed_height
        for child, child_snapshot in zip(node.children or [], snapshot.children):
            self._restore_cached_layout(child, child_snapshot)

    def _get_cached_layout(
        self,
        node: LayoutNode,
        available_width: float,
        available_height: float,
    ) -> _CachedLayoutEntry | None:
        cached = self._layout_cache.get(id(node))
        signature = self._layout_signature(node)
        if (
            cached is None
            or cached.available_width != available_width
            or cached.available_height != available_height
            or cached.signature != signature
        ):
            self.last_cache_misses += 1
            return None
        return cached

    def _store_cached_layout(
        self,
        node: LayoutNode,
        available_width: float,
        available_height: float,
    ) -> None:
        self._layout_cache[id(node)] = _CachedLayoutEntry(
            available_width=available_width,
            available_height=available_height,
            signature=self._layout_signature(node),
            snapshot=self._capture_layout(node),
        )


def calculate_layout(root: LayoutNode, width: float, height: float) -> None:
    """Calculate layout for a tree of nodes."""
    engine = LayoutEngine()
    engine.calculate_layout(root, width, height)
