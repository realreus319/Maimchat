from __future__ import annotations

from dataclasses import dataclass, field

from .ansi import BEL, ESC, OSC, ST
from .sgr import SgrState, apply_sgr, reset_sgr
from .types import TerminalToken


def _strip_string_terminator(payload: str) -> str:
    if payload.endswith(ST):
        return payload[: -len(ST)]
    if payload.endswith(BEL):
        return payload[:-1]
    return payload


def parse_control_sequence(raw: str) -> TerminalToken:
    if raw.startswith(f"{ESC}[") and len(raw) >= 3:
        body = raw[2:-1]
        params = tuple(part for part in body.split(";") if part) if body else ()
        return TerminalToken(
            kind="csi",
            raw=raw,
            command=raw[-1],
            params=params,
        )

    if raw.startswith(OSC):
        payload = _strip_string_terminator(raw[len(OSC) :])
        command, _, value = payload.partition(";")
        return TerminalToken(
            kind="osc",
            raw=raw,
            command=command,
            value=value,
        )

    if raw.startswith(f"{ESC}P"):
        payload = _strip_string_terminator(raw[2:])
        command = payload[:1]
        value = payload[1:] if payload else ""
        return TerminalToken(
            kind="dcs",
            raw=raw,
            command=command,
            value=value,
        )

    if raw.startswith(f"{ESC}X"):
        return TerminalToken(
            kind="sos",
            raw=raw,
            value=_strip_string_terminator(raw[2:]),
        )

    if raw.startswith(f"{ESC}^"):
        return TerminalToken(
            kind="pm",
            raw=raw,
            value=_strip_string_terminator(raw[2:]),
        )

    if raw.startswith(f"{ESC}_"):
        return TerminalToken(
            kind="apc",
            raw=raw,
            value=_strip_string_terminator(raw[2:]),
        )

    if raw.startswith(ESC):
        return TerminalToken(
            kind="esc",
            raw=raw,
            command=raw[1:],
        )

    return TerminalToken(kind="text", raw=raw, value=raw)


# ---------------------------------------------------------------------------
# Styled token – carries text content together with current style metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyledToken:
    """A terminal token enriched with the style state that was active when it
    was emitted.

    Attributes:
        kind: Token category (``"text"``, ``"csi"``, ``"osc"``, …).
        raw: The raw ANSI substring that produced this token.
        value: Decoded payload (text content for ``"text"`` tokens, etc.).
        command: Single-character command for CSI, or code string for OSC.
        params: Raw string parameters carried by the original token.
        style: Snapshot of the :class:`SgrState` at the moment the token was
            emitted.  For SGR (``m``) tokens this is the state *before* the
            SGR parameters are applied.
        in_link: Whether an OSC 8 hyperlink was open when this token was
            emitted.
        link_url: The URL of the currently open OSC 8 hyperlink, or ``""``.
    """

    kind: str
    raw: str
    value: str = ""
    command: str = ""
    params: tuple[str, ...] = field(default_factory=tuple)
    style: SgrState = field(default_factory=SgrState)
    in_link: bool = False
    link_url: str = ""


# ---------------------------------------------------------------------------
# TermioParser – stateful parser with SGR and hyperlink tracking
# ---------------------------------------------------------------------------


class TermioParser:
    """Stateful parser that tokenizes terminal output while tracking SGR
    style state and OSC 8 hyperlink state.

    Usage::

        parser = TermioParser()
        tokens = parser.feed("\\x1b[1mbold\\x1b[0m normal")
        for tok in tokens:
            print(tok.kind, tok.value, tok.style.bold)
    """

    def __init__(self) -> None:
        self.style = SgrState()
        self.in_link: bool = False
        self.link_url: str = ""

    def feed(self, data: bytes | str) -> tuple[StyledToken, ...]:
        """Parse *data*, update internal state, and return styled tokens.

        *data* may be ``str`` (typical) or ``bytes`` (decoded as UTF-8 with
        replacement).
        """
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        # Lazy import to avoid circular dependency: tokenize → parser → tokenize
        from .tokenize import tokenize_ansi

        results: list[StyledToken] = []
        for token in tokenize_ansi(data):
            styled = self._process_token(token)
            results.append(styled)
        return tuple(results)

    def reset(self) -> None:
        """Reset parser state (style, hyperlink) to defaults."""
        self.style = reset_sgr()
        self.in_link = False
        self.link_url = ""

    # -- internal helpers --------------------------------------------------

    def _snapshot(self, token: TerminalToken) -> StyledToken:
        return StyledToken(
            kind=token.kind,
            raw=token.raw,
            value=token.value,
            command=token.command,
            params=token.params,
            style=SgrState(
                bold=self.style.bold,
                dim=self.style.dim,
                italic=self.style.italic,
                underline=self.style.underline,
                blink=self.style.blink,
                inverse=self.style.inverse,
                hidden=self.style.hidden,
                strikethrough=self.style.strikethrough,
                foreground=self.style.foreground,
                background=self.style.background,
            ),
            in_link=self.in_link,
            link_url=self.link_url,
        )

    def _process_token(self, token: TerminalToken) -> StyledToken:
        # Snapshot state BEFORE applying this token's mutations, so text
        # tokens carry the style active at render time.
        styled = self._snapshot(token)

        if token.kind == "csi" and token.command == "m":
            self._apply_sgr_params(token.params)

        if token.kind == "osc":
            self._handle_osc(token)

        return styled

    def _apply_sgr_params(self, params: tuple[str, ...]) -> None:
        int_params: list[int] = []
        for p in params:
            try:
                int_params.append(int(p))
            except (ValueError, TypeError):
                int_params.append(0)
        apply_sgr(self.style, tuple(int_params))

    def _handle_osc(self, token: TerminalToken) -> None:
        if token.command != "8":
            return
        value = token.value
        _, _, url = value.partition(";")
        if url:
            self.in_link = True
            self.link_url = url
        else:
            self.in_link = False
            self.link_url = ""
