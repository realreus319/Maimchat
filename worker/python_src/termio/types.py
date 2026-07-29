from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TerminalToken:
    """Parsed terminal token."""

    kind: str
    raw: str
    value: str = ""
    command: str = ""
    params: tuple[str, ...] = field(default_factory=tuple)
