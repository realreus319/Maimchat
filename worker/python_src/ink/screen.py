"""Terminal screen buffer and cell management for TUI renderer.

Ported from src/ink/screen.ts - Screen buffer with packed cell representation
for efficient memory usage and fast diffing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional, Set, Tuple, Union

from ..termio.osc import close_hyperlink, open_hyperlink


class CellWidth(IntEnum):
    """Cell width classification for handling double-wide characters."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


@dataclass(frozen=True)
class Rectangle:
    """2D rectangular region for damage tracking."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Size:
    """Screen dimensions."""

    width: int
    height: int


@dataclass(frozen=True)
class Point:
    """2D coordinate point."""

    x: int
    y: int


class CharPool:
    """Shared character string pool for interning characters.

    With a shared pool, interned char IDs are valid across screens,
    so blitRegion can copy IDs directly.
    """

    def __init__(self) -> None:
        self._strings: List[str] = [" ", ""]
        self._string_map: Dict[str, int] = {" ": 0, "": 1}
        self._ascii: List[int] = self._init_ascii()

    def _init_ascii(self) -> List[int]:
        table = [-1] * 128
        table[32] = 0
        return table

    def intern(self, char: str) -> int:
        if len(char) == 1:
            code = ord(char)
            if code < 128:
                cached = self._ascii[code]
                if cached != -1:
                    return cached
                index = len(self._strings)
                self._strings.append(char)
                self._ascii[code] = index
                return index

        existing = self._string_map.get(char)
        if existing is not None:
            return existing
        index = len(self._strings)
        self._strings.append(char)
        self._string_map[char] = index
        return index

    def get(self, index: int) -> str:
        if 0 <= index < len(self._strings):
            return self._strings[index]
        return " "


class HyperlinkPool:
    """Shared hyperlink string pool. Index 0 = no hyperlink."""

    def __init__(self) -> None:
        self._strings: List[str] = [""]
        self._string_map: Dict[str, int] = {}

    def intern(self, hyperlink: Optional[str]) -> int:
        if not hyperlink:
            return 0
        existing = self._string_map.get(hyperlink)
        if existing is not None:
            return existing
        index = len(self._strings)
        self._strings.append(hyperlink)
        self._string_map[hyperlink] = index
        return index

    def get(self, id: int) -> Optional[str]:
        if id == 0 or id >= len(self._strings):
            return None
        return self._strings[id]


class StylePool:
    """Shared style pool for ANSI codes.

    Bit 0 of the ID encodes whether the style has visible effect on spaces.
    """

    VISIBLE_ON_SPACE: Set[str] = {
        "\x1b[49m",
        "\x1b[27m",
        "\x1b[24m",
        "\x1b[29m",
        "\x1b[55m",
    }

    def __init__(self) -> None:
        self._ids: Dict[str, int] = {}
        self._styles: List[Tuple[str, ...]] = [()]
        self._transition_cache: Dict[int, str] = {}
        self.none = self.intern([])

    def _has_visible_space_effect(self, styles: Tuple[str, ...]) -> bool:
        for style in styles:
            if style in self.VISIBLE_ON_SPACE:
                return True
        return False

    def intern(self, styles: List[str]) -> int:
        key = "\0".join(styles) if styles else ""
        existing = self._ids.get(key)
        if existing is not None:
            return existing

        raw_id = len(self._styles)
        self._styles.append(tuple(styles))

        visible = len(styles) > 0 and self._has_visible_space_effect(tuple(styles))
        id = (raw_id << 1) | (1 if visible else 0)
        self._ids[key] = id
        return id

    def get(self, id: int) -> Tuple[str, ...]:
        return self._styles[(id >> 1) % len(self._styles)]

    def transition(self, from_id: int, to_id: int) -> str:
        if from_id == to_id:
            return ""
        key = from_id * 0x100000 + to_id
        cached = self._transition_cache.get(key)
        if cached is not None:
            return cached

        from_styles = set(self.get(from_id))
        to_styles = set(self.get(to_id))

        removed = from_styles - to_styles
        added = to_styles - from_styles

        result = ""
        for style in removed:
            if style == "\x1b[1m":
                result += "\x1b[22m"
            elif style == "\x1b[7m":
                result += "\x1b[27m"
            elif style == "\x1b[4m":
                result += "\x1b[24m"

        for style in added:
            result += style

        self._transition_cache[key] = result
        return result


@dataclass
class Cell:
    """View type returned by cell_at()."""

    char: str
    style_id: int
    width: CellWidth
    hyperlink: Optional[str]


class Screen:
    """Screen buffer with packed cell representation.

    Uses a packed array instead of Cell objects to eliminate GC pressure.
    For a 200x120 screen, this avoids allocating 24,000 objects.

    Cell data is stored as 2 Int32s per cell:
    - word0: char_id (32 bits)
    - word1: style_id[31:17] | hyperlink_id[16:2] | width[1:0]
    """

    STYLE_SHIFT = 17
    HYPERLINK_SHIFT = 2
    HYPERLINK_MASK = 0x7FFF
    WIDTH_MASK = 3

    def __init__(
        self,
        width: int,
        height: int,
        char_pool: CharPool,
        hyperlink_pool: HyperlinkPool,
        style_pool: StylePool,
    ) -> None:
        self.width = max(0, width)
        self.height = max(0, height)
        self.char_pool = char_pool
        self.hyperlink_pool = hyperlink_pool
        self.style_pool = style_pool
        self.empty_style_id = style_pool.none

        size = self.width * self.height
        self._cells: List[int] = [0] * (size * 2)
        self._no_select: bytearray = bytearray(size)
        self._soft_wrap: List[int] = [0] * height
        self.damage: Optional[Rectangle] = None

    def _pack_word1(self, style_id: int, hyperlink_id: int, width: int) -> int:
        return (
            (style_id << self.STYLE_SHIFT)
            | (hyperlink_id << self.HYPERLINK_SHIFT)
            | width
        )

    def _cell_index(self, x: int, y: int) -> int:
        return (y * self.width + x) * 2

    def set_cell(
        self,
        x: int,
        y: int,
        char: str,
        style_id: int = 0,
        hyperlink: Optional[str] = None,
        width: CellWidth = CellWidth.NARROW,
    ) -> None:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return

        char_id = self.char_pool.intern(char)
        hyperlink_id = self.hyperlink_pool.intern(hyperlink)

        idx = self._cell_index(x, y)
        self._cells[idx] = char_id
        self._cells[idx + 1] = self._pack_word1(style_id, hyperlink_id, width)

        self._update_damage(x, y)

    def _update_damage(self, x: int, y: int) -> None:
        if self.damage is None:
            self.damage = Rectangle(x, y, 1, 1)
        else:
            min_x = min(self.damage.x, x)
            min_y = min(self.damage.y, y)
            max_x = max(self.damage.x + self.damage.width - 1, x)
            max_y = max(self.damage.y + self.damage.height - 1, y)
            self.damage = Rectangle(
                min_x,
                min_y,
                max_x - min_x + 1,
                max_y - min_y + 1,
            )

    def cell_at(self, x: int, y: int) -> Cell:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return Cell(" ", self.empty_style_id, CellWidth.NARROW, None)

        idx = self._cell_index(x, y)
        char_id = self._cells[idx]
        word1 = self._cells[idx + 1]

        style_id = word1 >> self.STYLE_SHIFT
        hyperlink_id = (word1 >> self.HYPERLINK_SHIFT) & self.HYPERLINK_MASK
        width = CellWidth(word1 & self.WIDTH_MASK)

        char = self.char_pool.get(char_id)
        hyperlink = self.hyperlink_pool.get(hyperlink_id)

        return Cell(char, style_id, width, hyperlink)

    def is_empty_at(self, x: int, y: int) -> bool:
        if x < 0 or x >= self.width or y < 0 or y >= self.height:
            return True
        idx = self._cell_index(x, y)
        return self._cells[idx] == 0 and self._cells[idx + 1] == 0

    def clear(self) -> None:
        size = self.width * self.height
        self._cells = [0] * (size * 2)
        self._no_select = bytearray(size)
        self.damage = None

    def blit_region(
        self,
        src: Screen,
        src_x: int,
        src_y: int,
        width: int,
        height: int,
        dst_x: int,
        dst_y: int,
    ) -> None:
        """Copy a region from source screen to this screen."""
        for y in range(height):
            for x in range(width):
                sx, sy = src_x + x, src_y + y
                dx, dy = dst_x + x, dst_y + y

                if sx < 0 or sx >= src.width or sy < 0 or sy >= src.height:
                    continue
                if dx < 0 or dx >= self.width or dy < 0 or dy >= self.height:
                    continue

                src_cell = src.cell_at(sx, sy)
                self.set_cell(
                    dx,
                    dy,
                    src_cell.char,
                    src_cell.style_id,
                    src_cell.hyperlink,
                    src_cell.width,
                )
                self._update_damage(dx, dy)

    def diff(self, other: Screen) -> List[Tuple[int, int, Cell, Cell]]:
        """Compare this screen with another and return differences.

        Returns list of (x, y, this_cell, other_cell) tuples.
        """
        differences: List[Tuple[int, int, Cell, Cell]] = []

        min_width = min(self.width, other.width)
        min_height = min(self.height, other.height)

        for y in range(min_height):
            for x in range(min_width):
                this_cell = self.cell_at(x, y)
                other_cell = other.cell_at(x, y)

                if (
                    this_cell.char != other_cell.char
                    or this_cell.style_id != other_cell.style_id
                    or this_cell.width != other_cell.width
                    or this_cell.hyperlink != other_cell.hyperlink
                ):
                    differences.append((x, y, this_cell, other_cell))

        return differences

    def render_to_string(self, *, include_styles: bool = False) -> str:
        """Render screen content to string for testing/debugging."""
        if include_styles:
            return self._render_to_ansi_string()
        lines: List[str] = []
        for y in range(self.height):
            line_chars: List[str] = []
            for x in range(self.width):
                cell = self.cell_at(x, y)
                if cell.width in (CellWidth.SPACER_TAIL, CellWidth.SPACER_HEAD):
                    continue
                line_chars.append(cell.char)
            lines.append("".join(line_chars).rstrip())
        return "\n".join(lines)

    def _hyperlink_transition(
        self, from_hyperlink: Optional[str], to_hyperlink: Optional[str]
    ) -> str:
        if from_hyperlink == to_hyperlink:
            return ""
        if to_hyperlink:
            return open_hyperlink(to_hyperlink)
        return close_hyperlink()

    def _render_to_ansi_string(self) -> str:
        lines: List[str] = []
        empty_style_id = self.empty_style_id
        for y in range(self.height):
            line_parts: List[str] = []
            current_style = empty_style_id
            current_hyperlink: Optional[str] = None
            for x in range(self.width):
                cell = self.cell_at(x, y)
                if cell.width in (CellWidth.SPACER_TAIL, CellWidth.SPACER_HEAD):
                    continue
                transition = self.style_pool.transition(current_style, cell.style_id)
                if transition:
                    line_parts.append(transition)
                    current_style = cell.style_id
                hyperlink_transition = self._hyperlink_transition(
                    current_hyperlink,
                    cell.hyperlink,
                )
                if hyperlink_transition:
                    line_parts.append(hyperlink_transition)
                    current_hyperlink = cell.hyperlink
                line_parts.append(cell.char)
            if current_hyperlink is not None:
                line_parts.append(close_hyperlink())
            if current_style != empty_style_id:
                line_parts.append(self.style_pool.transition(current_style, empty_style_id))
            line = "".join(line_parts).rstrip()
            if current_style != empty_style_id and not line.endswith("\x1b[0m"):
                line += "\x1b[0m"
            lines.append(line)
        return "\n".join(lines)


def create_screen(
    width: int,
    height: int,
    style_pool: Optional[StylePool] = None,
    char_pool: Optional[CharPool] = None,
    hyperlink_pool: Optional[HyperlinkPool] = None,
) -> Screen:
    """Factory function to create a new screen with shared pools."""
    char_pool = char_pool or CharPool()
    hyperlink_pool = hyperlink_pool or HyperlinkPool()
    style_pool = style_pool or StylePool()
    return Screen(width, height, char_pool, hyperlink_pool, style_pool)
