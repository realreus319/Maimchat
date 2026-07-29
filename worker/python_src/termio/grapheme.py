from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterator


_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
_ZERO_WIDTH_CODEPOINTS = frozenset({
    0x200C,  # ZWNJ
    0x200D,  # ZWJ
    0x2060,  # word joiner
})


@dataclass(frozen=True)
class Grapheme:
    text: str
    width: int


def _is_zero_width(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in _ZERO_WIDTH_CODEPOINTS:
        return True
    if char in _VARIATION_SELECTORS:
        return True
    if 0xFE00 <= codepoint <= 0xFE0F:
        return True
    if 0xE0100 <= codepoint <= 0xE01EF:
        return True
    return bool(unicodedata.combining(char))


def _is_emoji_modifier(char: str) -> bool:
    codepoint = ord(char)
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _is_emoji_codepoint(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _should_extend_grapheme(cluster: str, next_char: str) -> bool:
    if not cluster:
        return False
    if cluster[-1] == "\u200d":
        return True
    if next_char == "\u200d":
        return True
    if _is_zero_width(next_char):
        return True
    if _is_emoji_modifier(next_char):
        return True
    return False


def _grapheme_width(grapheme_text: str) -> int:
    visible_chars = [part for part in grapheme_text if not _is_zero_width(part)]
    if not visible_chars:
        return 0
    for visible in visible_chars:
        if _is_emoji_codepoint(visible) or unicodedata.east_asian_width(visible) in {"W", "F"}:
            return 2
    return 1


def segmentGraphemes(text: str) -> Iterator[Grapheme]:
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\n":
            yield Grapheme(text="\n", width=0)
            index += 1
            continue

        cluster = char
        index += 1
        while index < len(text) and _should_extend_grapheme(cluster, text[index]):
            cluster += text[index]
            index += 1
        yield Grapheme(text=cluster, width=_grapheme_width(cluster))


def grapheme_width(text: str) -> int:
    return sum(g.width for g in segmentGraphemes(text))
