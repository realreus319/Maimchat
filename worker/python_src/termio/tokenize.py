from __future__ import annotations

from collections.abc import Iterator
from enum import IntEnum

from .ansi import BEL, ESC
from .parser import parse_control_sequence
from .types import TerminalToken


# ---------------------------------------------------------------------------
# Tokenizer state machine (9-state FSM for terminal output tokenization)
# ---------------------------------------------------------------------------


class TokenizerState(IntEnum):
    """9-state FSM states for tokenizing terminal output.

    States follow the VT500 terminal state machine model:
    - GROUND: normal text processing
    - ESCAPE: ESC received, awaiting next byte to determine target state
    - CSI_ENTRY/CSI_PARAM/CSI_INTERMEDIATE: CSI sequence parsing
    - OSC_STRING: collecting OSC payload until BEL or ST
    - DCS_ENTRY: DCS parameter/intermediate collection
    - DCS_STRING: collecting DCS payload until ST
    - SOS_PM_APC: collecting SOS/PM/APC payload until ST
    """

    GROUND = 0
    ESCAPE = 1
    CSI_ENTRY = 2
    CSI_PARAM = 3
    CSI_INTERMEDIATE = 4
    OSC_STRING = 5
    DCS_ENTRY = 6
    DCS_STRING = 7
    SOS_PM_APC = 8


def _find_st(buf: str) -> int:
    """Find start index of ST (ESC \\) in *buf*, or -1 if absent."""
    idx = buf.find(ESC)
    while idx != -1:
        if idx + 1 < len(buf) and buf[idx + 1] == "\\":
            return idx
        idx = buf.find(ESC, idx + 1)
    return -1


# ---------------------------------------------------------------------------
# Backward-compatible helpers
# ---------------------------------------------------------------------------


def _consume_escape_sequence_state(text: str, start: int) -> tuple[int, bool]:
    if start >= len(text) or text[start] != ESC:
        return (start + 1, True)

    if start + 1 >= len(text):
        return (start + 1, False)

    marker = text[start + 1]
    if marker == "[":
        index = start + 2
        while index < len(text):
            if "@" <= text[index] <= "~":
                return (index + 1, True)
            index += 1
        return (len(text), False)
    if marker in {"]", "P", "X", "^", "_"}:
        index = start + 2
        while index < len(text):
            char = text[index]
            if char == "\a":
                return (index + 1, True)
            if char == ESC and index + 1 < len(text) and text[index + 1] == "\\":
                return (index + 2, True)
            index += 1
        return (len(text), False)
    return (min(start + 2, len(text)), True)


def consume_escape_sequence(text: str, start: int) -> int:
    end, _ = _consume_escape_sequence_state(text, start)
    return end


# ---------------------------------------------------------------------------
# Incremental tokenizer with 9-state FSM
# ---------------------------------------------------------------------------


class TerminalTokenizer:
    """Incremental ANSI tokenizer backed by a 9-state finite state machine.

    Feeding chunks via :meth:`feed` processes as many complete tokens as
    possible and buffers the remainder for the next call.  Use :meth:`flush`
    to force-emit any buffered data as plain text.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._state = TokenizerState.GROUND
        self._seq = ""  # accumulates the current escape sequence bytes

    @property
    def buffered_text(self) -> str:
        """Unprocessed text remaining in the internal buffer."""
        if self._state == TokenizerState.GROUND:
            return self._buffer
        return self._seq + self._buffer

    # -- public API --------------------------------------------------------

    def feed(self, chunk: str) -> tuple[TerminalToken, ...]:
        if not chunk:
            return ()

        self._buffer += chunk
        tokens: list[TerminalToken] = []

        while self._buffer:
            if self._state == TokenizerState.GROUND:
                self._process_ground(tokens)
            elif self._state == TokenizerState.ESCAPE:
                if not self._process_escape(tokens):
                    break
            elif self._state in (
                TokenizerState.CSI_ENTRY,
                TokenizerState.CSI_PARAM,
                TokenizerState.CSI_INTERMEDIATE,
            ):
                if not self._process_csi(tokens):
                    break
            elif self._state == TokenizerState.OSC_STRING:
                if not self._process_osc(tokens):
                    break
            elif self._state == TokenizerState.DCS_ENTRY:
                if not self._process_dcs_entry(tokens):
                    break
            elif self._state == TokenizerState.DCS_STRING:
                if not self._process_dcs_string(tokens):
                    break
            elif self._state == TokenizerState.SOS_PM_APC:
                if not self._process_sos_pm_apc(tokens):
                    break
            else:
                break

        return tuple(tokens)

    def flush(self) -> tuple[TerminalToken, ...]:
        text = self._seq + self._buffer
        if not text:
            return ()
        self._seq = ""
        self._buffer = ""
        self._state = TokenizerState.GROUND
        return (TerminalToken(kind="text", raw=text, value=text),)

    def reset(self) -> None:
        self._buffer = ""
        self._seq = ""
        self._state = TokenizerState.GROUND

    # -- state handlers ----------------------------------------------------

    def _emit_seq(self, tokens: list[TerminalToken]) -> None:
        tokens.append(parse_control_sequence(self._seq))
        self._seq = ""
        self._state = TokenizerState.GROUND

    def _process_ground(self, tokens: list[TerminalToken]) -> None:
        idx = self._buffer.find(ESC)
        if idx == -1:
            # No escape – everything is plain text.
            if self._buffer:
                tokens.append(
                    TerminalToken(kind="text", raw=self._buffer, value=self._buffer)
                )
                self._buffer = ""
            return

        # Emit text preceding the ESC.
        if idx > 0:
            text = self._buffer[:idx]
            tokens.append(TerminalToken(kind="text", raw=text, value=text))

        self._seq = ESC
        self._buffer = self._buffer[idx + 1 :]
        self._state = TokenizerState.ESCAPE

    def _process_escape(self, tokens: list[TerminalToken]) -> bool:
        if not self._buffer:
            return False

        char = self._buffer[0]
        code = ord(char)
        self._seq += char
        self._buffer = self._buffer[1:]

        if char == "[":
            self._state = TokenizerState.CSI_ENTRY
        elif char == "]":
            self._state = TokenizerState.OSC_STRING
        elif char == "P":
            self._state = TokenizerState.DCS_ENTRY
        elif char in ("X", "^", "_"):
            self._state = TokenizerState.SOS_PM_APC
        elif 0x20 <= code <= 0x2F:
            # Intermediate byte – stay in ESCAPE and accumulate.
            pass
        elif 0x30 <= code <= 0x7E:
            # Final byte – complete two/three-character escape sequence.
            self._emit_seq(tokens)
        else:
            # C0 control (includes double ESC) or high byte – emit as-is.
            self._emit_seq(tokens)
        return True

    def _process_csi(self, tokens: list[TerminalToken]) -> bool:
        if not self._buffer:
            return False

        char = self._buffer[0]
        code = ord(char)
        self._seq += char
        self._buffer = self._buffer[1:]

        if 0x40 <= code <= 0x7E:
            # Final byte – complete CSI sequence.
            self._emit_seq(tokens)
        elif 0x30 <= code <= 0x3F:
            self._state = TokenizerState.CSI_PARAM
        elif 0x20 <= code <= 0x2F:
            self._state = TokenizerState.CSI_INTERMEDIATE
        # else: C0 / high byte – consume silently and keep current state.
        return True

    def _process_osc(self, tokens: list[TerminalToken]) -> bool:
        bel_idx = self._buffer.find(BEL)
        st_idx = _find_st(self._buffer)

        if bel_idx != -1 and (st_idx == -1 or bel_idx <= st_idx):
            # BEL terminates the OSC.
            self._seq += self._buffer[: bel_idx + 1]
            self._buffer = self._buffer[bel_idx + 1 :]
            self._emit_seq(tokens)
            return True

        if st_idx != -1:
            # ST (ESC \\) terminates the OSC.
            self._seq += self._buffer[: st_idx + 2]
            self._buffer = self._buffer[st_idx + 2 :]
            self._emit_seq(tokens)
            return True

        # No terminator yet – buffer everything.
        self._seq += self._buffer
        self._buffer = ""
        return False

    def _process_dcs_entry(self, tokens: list[TerminalToken]) -> bool:
        if not self._buffer:
            return False

        char = self._buffer[0]
        code = ord(char)
        self._seq += char
        self._buffer = self._buffer[1:]

        if 0x40 <= code <= 0x7E:
            # Final byte marks end of DCS parameter area – data follows.
            self._state = TokenizerState.DCS_STRING
        # 0x30-0x3F params and 0x20-0x2F intermediates stay in DCS_ENTRY.
        return True

    def _process_dcs_string(self, tokens: list[TerminalToken]) -> bool:
        st_idx = _find_st(self._buffer)
        if st_idx >= 0:
            self._seq += self._buffer[: st_idx + 2]
            self._buffer = self._buffer[st_idx + 2 :]
            self._emit_seq(tokens)
            return True

        self._seq += self._buffer
        self._buffer = ""
        return False

    def _process_sos_pm_apc(self, tokens: list[TerminalToken]) -> bool:
        st_idx = _find_st(self._buffer)
        if st_idx >= 0:
            self._seq += self._buffer[: st_idx + 2]
            self._buffer = self._buffer[st_idx + 2 :]
            self._emit_seq(tokens)
            return True

        self._seq += self._buffer
        self._buffer = ""
        return False


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def terminal_tokenize(text: str) -> Iterator[TerminalToken]:
    """One-shot tokenize using the 9-state FSM.

    Token output uses :class:`TerminalToken` objects:
      ``[TerminalToken("text", ..., "hello"), TerminalToken("csi", ..., "m"), ...]``
    """
    tokenizer = TerminalTokenizer()
    yield from tokenizer.feed(text)
    yield from tokenizer.flush()


def tokenize_ansi(text: str) -> Iterator[TerminalToken]:
    """One-shot tokenize – equivalent to :func:`terminal_tokenize`."""
    return terminal_tokenize(text)


def strip_control_sequences(text: str) -> str:
    return "".join(
        token.value
        for token in tokenize_ansi(text)
        if token.kind == "text"
    )
