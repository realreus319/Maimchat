from __future__ import annotations

import asyncio
import codecs
import datetime
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, TextIO

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX fallback
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

from .bootstrap import getInitialEffortValue, getInitialMainLoopModel, getSdkBetas
from .commands.registry import find_command
from .components.permission_prompt import PermissionOption, PermissionPromptData
from .components.message_row import MessageRowData, MessageRowRenderer
from .components.status_line import StatusLineData
from .history import HistoryStore
from .local_tool_executor import LocalToolExecutor
from .query import (
    AssistantMessage,
    AttachmentMessage,
    CompactBoundaryMessage,
    Message,
    ProgressMessage,
    QuerySession,
    QueryStateTransitionError,
    SystemInformationalMessage,
    TerminalReason,
    TerminalTransition,
    ThinkingBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseSummaryMessage,
    UserMessage,
    createAssistantAPIErrorMessage,
    createSystemMessage,
    createUserMessage,
)
from .query_streaming import (
    AnthropicStreamingModelAdapter,
    LocalEchoModelAdapter,
    ModelAdapter,
    OpenAIChatStreamingModelAdapter,
    QueryStreamConfig,
    ToolExecutor,
    _compact_session_messages,
    create_model_adapter_from_env,
    stream_query_session,
)
from .services.away_summary import generate_away_summary, parse_iso_timestamp_ms
from .services.compact import clear_compact_warning_suppression, run_post_compact_cleanup
from .screens.repl import REPLRenderer, REPLScreenData
from .ink.parse_keypress import ParsedTerminalInput, parse_terminal_input
from .ink.terminal import TerminalCapabilities, string_width, wrap_synchronized_output
from .state.app_state_store import get_default_app_state
from .state.persistence import (
    SessionSnapshotValidationError,
    _load_session_index,
    generate_session_id,
    load_latest_session_snapshot,
    load_session_snapshot,
    save_session_snapshot,
)
from .state.validate_persistence import validate_persistence_contract
from .termio.csi import (
    clear_screen,
    cursor_home,
    cursor_position,
    erase_in_line,
    reset_scroll_region,
    scroll_down,
    scroll_up,
    set_scroll_region,
)
from .termio.dec import (
    disable_bracketed_paste,
    enable_bracketed_paste,
)
from .termio.osc import (
    ProgressState,
    clear_progress_bar,
    set_clipboard,
    set_current_directory,
    set_progress_bar,
    set_title,
    set_window_title,
)
from .termio.tokenize import consume_escape_sequence
from .tui_interaction import TUIInteractionFlow, TUIInteractionState
from .utils.sandbox.sandbox_adapter import validate_sandbox_core_contract
from .utils.config import get_claude_config_home
from .utils.error_display import (
    build_structured_error_display,
    format_compact_structured_error_markdown,
    format_structured_error_markdown,
)
from .utils.error_log_sink import DiagnosticContext
from .utils.hooks import HookEventResult, dispatch_hook_event
from .utils.model_selection import (
    default_thinking_enabled,
    is_valid_effort_level,
    resolve_fallback_model,
    resolve_main_loop_model,
)
from .utils.agents import (
    AgentDefinition,
    build_agent_registry,
    delete_custom_agent,
    format_agents_listing,
    get_custom_agent,
    upsert_custom_agent,
)
from .utils.mcp_cli import (
    format_project_mcp_listing,
    get_project_mcp_server_details,
    list_project_mcp_servers,
    remove_project_mcp_server,
    reset_project_mcp_choices,
    upsert_project_mcp_server,
)
from .utils.plugin_registry import (
    format_plugin_listing,
    install_plugin_from_directory,
    list_installed_plugins,
    set_installed_plugin_enabled,
    sync_managed_plugins_runtime_state,
    uninstall_installed_plugin,
    update_installed_plugin,
    validate_plugin_directory,
)
from .utils.permissions.permission_rule_parser import (
    permission_rule_value_from_string,
    permission_rule_value_to_string,
)
from .utils.permissions.permission_setup import transition_permission_mode
from .utils.settings import (
    get_initial_settings,
    get_setting_file_path,
    get_settings_for_source,
    sync_app_state_settings_from_disk,
    update_settings_for_source,
)
from .types.permissions import (
    PermissionAskDecision,
    PermissionAllowDecision,
    PermissionDenyDecision,
    RuleDecisionReason,
)

_TOOL_RESULT_COLLAPSE_CHAR_LIMIT = 4_000
_TOOL_RESULT_COLLAPSE_HEAD_CHARS = 1_400
_TOOL_RESULT_COLLAPSE_TAIL_CHARS = 1_200
_CONSOLE_CAPTURE_ENTRY_LIMIT = 20
_CONSOLE_CAPTURE_CHAR_LIMIT = 6_000
_REPL_FRAME_INTERVAL_MS = 16
_ACTIVE_PROBE_TIMEOUT_SECONDS = 0.06
_CODE_LANGUAGE_BY_EXTENSION = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".md": "markdown",
    ".mjs": "javascript",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".swift": "swift",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
_NATIVE_CSIU_TERMINALS = {
    "ghostty": "Ghostty",
    "kitty": "Kitty",
    "iterm.app": "iTerm2",
    "wezterm": "WezTerm",
    "warpterminal": "Warp",
}
_VSCODE_FAMILY_TERMINALS = {
    "vscode": "VSCode",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
}


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _kebab_to_camel(value: str) -> str:
    parts = [part for part in value.split("-") if part]
    if not parts:
        return value
    head, *tail = parts
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _camel_to_kebab(value: str) -> str:
    if not value:
        return value
    characters = [value[0].lower()]
    for char in value[1:]:
        if char.isupper():
            characters.append("-")
            characters.append(char.lower())
        else:
            characters.append(char)
    return "".join(characters)


def _format_index_ranges(indices: Iterable[int]) -> str:
    ordered = sorted({int(index) for index in indices})
    if not ordered:
        return ""
    ranges: list[str] = []
    start = ordered[0]
    end = start
    for value in ordered[1:]:
        if value == end + 1:
            end = value
            continue
        ranges.append(f"{start}-{end}" if start != end else str(start))
        start = end = value
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(ranges)


def _resolve_builtin_slash_command(name: str):
    resolved = find_command(name)
    if resolved is not None:
        return resolved
    if "-" in name:
        return find_command(_kebab_to_camel(name))
    return None


def _split_slash_args(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _is_vscode_remote_session() -> bool:
    askpass_main = os.environ.get("VSCODE_GIT_ASKPASS_MAIN", "")
    path_value = os.environ.get("PATH", "")
    combined = f"{askpass_main}\n{path_value}".lower()
    return any(
        marker in combined
        for marker in (
            ".vscode-server",
            ".cursor-server",
            ".windsurf-server",
        )
    )


def _detect_terminal_setup_target() -> tuple[str | None, str]:
    term_program = os.environ.get("TERM_PROGRAM", "").strip()
    normalized_program = term_program.lower()
    term = os.environ.get("TERM", "").strip().lower()
    if normalized_program == "apple_terminal":
        return ("apple_terminal", "Apple Terminal")
    if normalized_program in _NATIVE_CSIU_TERMINALS:
        return (normalized_program, _NATIVE_CSIU_TERMINALS[normalized_program])
    if normalized_program == "vscode":
        askpass_main = os.environ.get("VSCODE_GIT_ASKPASS_MAIN", "").lower()
        if ".cursor" in askpass_main:
            return ("cursor", "Cursor")
        if ".windsurf" in askpass_main:
            return ("windsurf", "Windsurf")
        return ("vscode", "VSCode")
    if normalized_program in _VSCODE_FAMILY_TERMINALS:
        return (normalized_program, _VSCODE_FAMILY_TERMINALS[normalized_program])
    if os.environ.get("KITTY_WINDOW_ID") or "kitty" in term:
        return ("kitty", "Kitty")
    if os.environ.get("WEZTERM_EXECUTABLE") or "wezterm" in term:
        return ("wezterm", "WezTerm")
    if "ghostty" in term:
        return ("ghostty", "Ghostty")
    if os.environ.get("WT_SESSION"):
        return ("windows_terminal", "Windows Terminal")
    if "alacritty" in term or os.environ.get("ALACRITTY_SOCKET"):
        return ("alacritty", "Alacritty")
    if os.environ.get("ZED_TERM") or os.environ.get("ZED_ENVIRONMENT"):
        return ("zed", "Zed")
    if term_program:
        return (normalized_program, term_program)
    if term:
        return (term, term)
    return (None, "your current terminal")


class _FrameRenderScheduler:
    """Coalesce rapid render requests into a lightweight frame loop."""

    def __init__(
        self,
        render: Callable[[], None],
        *,
        frame_interval_ms: int = _REPL_FRAME_INTERVAL_MS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._render = render
        self._frame_interval_seconds = max(frame_interval_ms, 0) / 1000.0
        self._monotonic = monotonic or time.monotonic
        self._last_render_at: float | None = None
        self._pending = False

    def request_render(self, *, force: bool = False) -> bool:
        now = self._monotonic()
        if (
            force
            or self._last_render_at is None
            or now - self._last_render_at >= self._frame_interval_seconds
        ):
            self._pending = False
            self._last_render_at = now
            self._render()
            return True
        self._pending = True
        return False

    def flush(self) -> bool:
        if not self._pending:
            return False
        self._pending = False
        self._last_render_at = self._monotonic()
        self._render()
        return True


@dataclass(frozen=True)
class _EphemeralConsoleEntry:
    stream_name: str
    text: str


@dataclass(frozen=True)
class _EphemeralSystemEntry:
    content: str
    level: str
    message_id: str


@dataclass(frozen=True)
class _SelectableTranscriptRow:
    ordinal: int
    message_id: str
    content: str


class _ConsoleCaptureStream:
    """Capture console writes and route them back into the REPL."""

    def __init__(
        self,
        *,
        stream_name: str,
        original: TextIO,
        emit: Callable[[str, str], None],
    ) -> None:
        self._stream_name = stream_name
        self._original = original
        self._emit = emit
        self._buffer = ""
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "strict")

    def isatty(self) -> bool:
        probe = getattr(self._original, "isatty", None)
        return bool(callable(probe) and probe())

    def fileno(self) -> int:
        fileno = getattr(self._original, "fileno", None)
        if not callable(fileno):
            raise OSError("captured stream does not expose fileno()")
        return fileno()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
        self._buffer += normalized
        self._drain_complete_lines()
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._emit(self._stream_name, self._buffer.strip())
        self._buffer = ""

    def _drain_complete_lines(self) -> None:
        while True:
            line, separator, remainder = self._buffer.partition("\n")
            if not separator:
                break
            if line.strip():
                self._emit(self._stream_name, line.strip())
            self._buffer = remainder


@contextmanager
def _patch_console_streams(
    *,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    captured_stdout = _ConsoleCaptureStream(
        stream_name="stdout",
        original=original_stdout,
        emit=lambda _stream, text: on_stdout(text),
    )
    captured_stderr = _ConsoleCaptureStream(
        stream_name="stderr",
        original=original_stderr,
        emit=lambda _stream, text: on_stderr(text),
    )
    sys.stdout = captured_stdout
    sys.stderr = captured_stderr
    try:
        yield
    finally:
        try:
            captured_stdout.flush()
            captured_stderr.flush()
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


@dataclass(frozen=True)
class _RenderedScreenFrame:
    lines: tuple[str, ...]
    cursor_row: int
    cursor_column: int


@dataclass(frozen=True)
class _ScrollRegionPatch:
    top: int
    bottom: int
    offset: int
    rewrites: tuple[tuple[int, str], ...]


def _build_rendered_screen_frame(screen_text: str) -> _RenderedScreenFrame:
    lines = tuple(screen_text.split("\n")) if screen_text else ("",)
    cursor_row = max(len(lines) - 1, 0)
    cursor_column = string_width(lines[cursor_row]) + 1 if lines else 1
    for row_index in range(len(lines) - 1, -1, -1):
        marker_index = _find_prompt_cursor_marker(lines, row_index)
        if marker_index < 0:
            continue
        line = lines[row_index]
        cursor_row = row_index
        cursor_column = string_width(line[:marker_index]) + 1
        break
    return _RenderedScreenFrame(
        lines=lines,
        cursor_row=cursor_row,
        cursor_column=max(cursor_column, 1),
    )


def _find_prompt_cursor_marker(lines: tuple[str, ...], row_index: int) -> int:
    line = lines[row_index]
    if line.startswith("> "):
        return line.find("_", 2)
    if line.startswith("  ") and _has_prompt_start_above(lines, row_index):
        return line.find("_", 2)
    return -1


def _has_prompt_start_above(lines: tuple[str, ...], row_index: int) -> bool:
    probe = row_index - 1
    while probe >= 0:
        line = lines[probe]
        if line.startswith("> "):
            return True
        if not line.startswith("  "):
            return False
        probe -= 1
    return False


def _build_fullscreen_render_payload(frame: _RenderedScreenFrame) -> str:
    return (
        clear_screen()
        + cursor_home()
        + "\n".join(frame.lines)
        + cursor_position(frame.cursor_row + 1, frame.cursor_column)
    )


def _build_incremental_render_payload(
    previous: _RenderedScreenFrame,
    current: _RenderedScreenFrame,
) -> str:
    payload_parts: list[str] = []
    scroll_patch = _detect_scroll_region_patch(previous, current)
    if scroll_patch is not None:
        payload_parts.extend(_build_scroll_region_payload(scroll_patch))
    else:
        max_line_count = max(len(previous.lines), len(current.lines))
        for line_index in range(max_line_count):
            previous_line = (
                previous.lines[line_index] if line_index < len(previous.lines) else ""
            )
            current_line = (
                current.lines[line_index] if line_index < len(current.lines) else ""
            )
            if previous_line == current_line:
                continue
            payload_parts.append(cursor_position(line_index + 1, 1))
            payload_parts.append(erase_in_line(2))
            if current_line:
                payload_parts.append(current_line)

    payload_parts.append(cursor_position(current.cursor_row + 1, current.cursor_column))
    return "".join(payload_parts)


def _cursor_up(lines: int) -> str:
    return f"\x1b[{max(lines, 1)}A" if lines > 0 else ""


def _cursor_down(lines: int) -> str:
    return f"\x1b[{max(lines, 1)}B" if lines > 0 else ""


def _cursor_right(columns: int) -> str:
    return f"\x1b[{max(columns, 1)}C" if columns > 0 else ""


def _truncate_status_segment(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if string_width(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    result = ""
    for char in text:
        if string_width(result + char) > width - 3:
            break
        result += char
    return f"{result.rstrip()}..."


def _is_volatile_terminal_row(row: MessageRowData) -> bool:
    return row.message_id.startswith("stream-preview")


def _detect_scroll_region_patch(
    previous: _RenderedScreenFrame,
    current: _RenderedScreenFrame,
) -> _ScrollRegionPatch | None:
    if len(previous.lines) != len(current.lines):
        return None
    if previous.lines == current.lines:
        return None

    line_count = len(previous.lines)
    try:
        start = next(
            index
            for index, (before, after) in enumerate(zip(previous.lines, current.lines))
            if before != after
        )
    except StopIteration:
        return None

    suffix_len = 0
    while (
        suffix_len < line_count - start
        and previous.lines[line_count - 1 - suffix_len]
        == current.lines[line_count - 1 - suffix_len]
    ):
        suffix_len += 1
    end = line_count - suffix_len - 1
    region_len = end - start + 1
    if region_len <= 1:
        return None

    for delta in range(1, region_len):
        if previous.lines[start + delta : end + 1] == current.lines[start : end + 1 - delta]:
            rewrites = tuple(
                (index, current.lines[index])
                for index in range(end + 1 - delta, end + 1)
            )
            return _ScrollRegionPatch(
                top=start,
                bottom=end,
                offset=delta,
                rewrites=rewrites,
            )
        if previous.lines[start : end + 1 - delta] == current.lines[start + delta : end + 1]:
            rewrites = tuple(
                (index, current.lines[index]) for index in range(start, start + delta)
            )
            return _ScrollRegionPatch(
                top=start,
                bottom=end,
                offset=-delta,
                rewrites=rewrites,
            )
    return None


def _build_scroll_region_payload(patch: _ScrollRegionPatch) -> list[str]:
    payload_parts = [set_scroll_region(patch.top + 1, patch.bottom + 1)]
    if patch.offset > 0:
        payload_parts.append(cursor_position(patch.bottom + 1, 1))
        payload_parts.append(scroll_up(patch.offset))
    else:
        payload_parts.append(cursor_position(patch.top + 1, 1))
        payload_parts.append(scroll_down(abs(patch.offset)))
    payload_parts.append(reset_scroll_region())
    for line_index, line_text in patch.rewrites:
        payload_parts.append(cursor_position(line_index + 1, 1))
        payload_parts.append(erase_in_line(2))
        if line_text:
            payload_parts.append(line_text)
    return payload_parts


class _PrefixedTextInput:
    """Text input wrapper that replays bytes already consumed during startup."""

    def __init__(self, stream: TextIO, *, prefix: str = "") -> None:
        self._stream = stream
        self._prefix = prefix

    def readline(self) -> str:
        if not self._prefix:
            return self._stream.readline()

        newline_index = self._prefix.find("\n")
        if newline_index >= 0:
            line = self._prefix[: newline_index + 1]
            self._prefix = self._prefix[newline_index + 1 :]
            return line

        line = self._stream.readline()
        if line:
            buffered = self._prefix
            self._prefix = ""
            return f"{buffered}{line}"

        buffered = self._prefix
        self._prefix = ""
        return buffered

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()

    def has_buffered_input(self) -> bool:
        return bool(self._prefix)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@dataclass(frozen=True)
class _PromptSlashCommandInvocation:
    prompt: str


def _consume_active_probe_chunk(
    chunk: str,
    *,
    terminal: TerminalCapabilities,
    known_responses: set[str],
) -> tuple[set[str], str]:
    applied: set[str] = set()
    index = 0
    while index < len(chunk):
        if chunk[index] != "\x1b":
            return applied, chunk[index:]
        end = consume_escape_sequence(chunk, index)
        if end <= index:
            return applied, chunk[index:]
        sequence = chunk[index:end]
        parsed = parse_terminal_input(sequence)
        if (
            parsed is None
            or parsed.kind != "response"
            or parsed.response not in known_responses
        ):
            return applied, chunk[index:]
        terminal.apply_active_probe_response(parsed)
        applied.add(parsed.response)
        index = end
    return applied, ""


def _negotiate_startup_terminal_capabilities(
    in_stream: TextIO,
    out_stream: TextIO,
    terminal: TerminalCapabilities,
    *,
    timeout_seconds: float = _ACTIVE_PROBE_TIMEOUT_SECONDS,
) -> TextIO:
    """Actively probe terminal capabilities without discarding queued user input."""
    if bool(getattr(terminal, "active_probe_applied", False)):
        return in_stream

    build_payload = getattr(terminal, "build_active_probe_payload", None)
    build_requests = getattr(terminal, "build_active_probe_requests", None)
    if not callable(build_payload) or not callable(build_requests):
        return in_stream

    payload = build_payload()
    if not payload:
        return in_stream

    if not bool(getattr(in_stream, "isatty", lambda: False)()):
        return in_stream
    if not bool(getattr(out_stream, "isatty", lambda: False)()):
        return in_stream

    fileno = getattr(in_stream, "fileno", None)
    if not callable(fileno) or termios is None or tty is None:
        return in_stream

    try:
        fd = fileno()
    except (OSError, ValueError):
        return in_stream

    known_responses = {request.expected_response for request in build_requests()}
    if not known_responses:
        return in_stream

    buffered_input = ""
    previous_attributes = None
    try:
        previous_attributes = termios.tcgetattr(fd)
        tty.setcbreak(fd, termios.TCSANOW)
        out_stream.write(payload)
        out_stream.flush()

        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            text = chunk.decode(getattr(in_stream, "encoding", None) or "utf-8", errors="ignore")
            applied, remainder = _consume_active_probe_chunk(
                text,
                terminal=terminal,
                known_responses=known_responses,
            )
            if not applied and not remainder:
                break
            if remainder:
                buffered_input += remainder
                break
    except (OSError, ValueError):
        return in_stream
    finally:
        if previous_attributes is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, previous_attributes)
            except OSError:
                pass

    if buffered_input:
        return _PrefixedTextInput(in_stream, prefix=buffered_input)
    return in_stream


def _collapse_tool_result_text(text: str, *, label: str) -> str:
    normalized = text.strip()
    if len(normalized) <= _TOOL_RESULT_COLLAPSE_CHAR_LIMIT:
        return normalized
    omitted_chars = len(normalized) - (
        _TOOL_RESULT_COLLAPSE_HEAD_CHARS + _TOOL_RESULT_COLLAPSE_TAIL_CHARS
    )
    return (
        f"[{label} collapsed: total_chars={len(normalized)} omitted_chars={max(omitted_chars, 0)}]\n"
        f"{normalized[:_TOOL_RESULT_COLLAPSE_HEAD_CHARS].rstrip()}\n"
        "[...]\n"
        f"{normalized[-_TOOL_RESULT_COLLAPSE_TAIL_CHARS:].lstrip()}"
    )


def _tool_result_values(
    message: UserMessage,
    blocks: Sequence[ToolResultBlock],
) -> tuple[Any, ...]:
    raw = message.toolUseResult
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw[index] if index < len(raw) else block.content for index, block in enumerate(blocks))
    if raw is not None:
        return tuple(raw for _ in blocks)
    return tuple(block.content for block in blocks)


def _parse_tool_result_payload(raw: Any) -> Any:
    if isinstance(raw, (Mapping, list, tuple, int, float, bool)) or raw is None:
        return raw
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return raw
    return raw


def _parse_task_output_legacy_payload(raw: str) -> Mapping[str, Any] | None:
    """Parse TaskOutput's XML-like context text for TUI formatting.

    The tool result stored for model context is intentionally text, not JSON. Rendering
    can still recover the main metadata without treating the task output itself as XML.
    """
    if "<retrieval_status>" not in raw:
        return None

    def tag_value(tag: str, *, multiline: bool = False) -> str | None:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", raw, flags=re.DOTALL)
        if match is None:
            return None
        value = match.group(1)
        if multiline:
            if value.startswith("\n"):
                value = value[1:]
            if value.endswith("\n"):
                value = value[:-1]
            return value
        return value.strip()

    retrieval_status = tag_value("retrieval_status")
    if not retrieval_status:
        return None

    def int_tag(tag: str) -> int | None:
        value = tag_value(tag)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    parsed: dict[str, Any] = {"retrieval_status": retrieval_status}
    task: dict[str, Any] = {}
    for tag, key in (
        ("task_id", "task_id"),
        ("task_type", "task_type"),
        ("status", "status"),
        ("output_file", "output_file"),
    ):
        value = tag_value(tag)
        if value:
            task[key] = value

    persisted_size = int_tag("persisted_output_size")
    if persisted_size is not None:
        task["persistedOutputSize"] = persisted_size
    exit_code = int_tag("exit_code")
    if exit_code is not None:
        task["exit_code"] = exit_code
    is_backgrounded = tag_value("is_backgrounded")
    if is_backgrounded is not None:
        task["is_backgrounded"] = is_backgrounded.lower() == "true"

    output = tag_value("output", multiline=True)
    if output is not None and output.strip():
        task["output"] = output
    error = tag_value("error", multiline=True)
    if error is not None and error.strip():
        task["error"] = error
    if task:
        parsed["task"] = task
    return parsed


def _inline_code(value: object) -> str:
    text = str(value).strip()
    return f"`{text}`" if text else "``"


def build_doctor_report(
    *,
    config_home: str | None = None,
    cwd: str | None = None,
    persist_sessions: bool = True,
    tool_count: int = 0,
) -> str:
    persistence_passed, persistence_errors = validate_persistence_contract()
    sandbox_passed, sandbox_errors = validate_sandbox_core_contract()
    resolved_config_home = config_home or get_claude_config_home()
    resolved_cwd = cwd or os.getcwd()
    lines = [
        "### Doctor",
        f"- Config Home: {_inline_code(resolved_config_home)}",
        f"- Working Directory Exists: {_inline_code(str(os.path.isdir(resolved_cwd)).lower())}",
        f"- Session Persistence: {_inline_code(str(persist_sessions).lower())}",
        f"- Registered Tools: {tool_count}",
        f"- Persistence Contract: {_inline_code('pass' if persistence_passed else 'fail')}",
        f"- Sandbox Contract: {_inline_code('pass' if sandbox_passed else 'fail')}",
    ]
    if persistence_errors:
        lines.append("- Persistence Errors: " + " | ".join(persistence_errors))
    if sandbox_errors:
        lines.append("- Sandbox Errors: " + " | ".join(sandbox_errors))
    return "\n".join(lines)


def _tool_result_code_language(
    tool_name: str,
    *,
    file_path: str | None = None,
    content: str | None = None,
) -> str | None:
    if file_path:
        extension = os.path.splitext(file_path)[1].lower()
        language = _CODE_LANGUAGE_BY_EXTENSION.get(extension)
        if language:
            return language
    if tool_name in {"Bash", "TaskOutput"}:
        return "bash"
    if tool_name in {"WebFetch", "WebSearch"}:
        return "markdown"
    normalized = (content or "").strip()
    if normalized.startswith("{") or normalized.startswith("["):
        return "json"
    if normalized.startswith("<") and normalized.endswith(">"):
        return "xml"
    return None


def _append_code_block(
    lines: list[str],
    text: str,
    *,
    label: str,
    language: str | None = None,
) -> None:
    if not text.strip():
        return
    rendered = _collapse_tool_result_text(text, label=label)
    lines.append(f"```{language or ''}")
    lines.append(rendered)
    lines.append("```")


def _append_bullet(lines: list[str], label: str, value: object) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text:
        return
    lines.append(f"- {label}: {text}")


def _lookup_mapping_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def _format_bytes_label(size: object) -> str | None:
    if isinstance(size, bool) or not isinstance(size, (int, float)):
        return None
    value = float(size)
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def _format_image_dimensions(dimensions: object) -> str | None:
    if not isinstance(dimensions, Mapping):
        return None
    width = dimensions.get("display_width") or dimensions.get("original_width")
    height = dimensions.get("display_height") or dimensions.get("original_height")
    if isinstance(width, int) and isinstance(height, int):
        return f"{width}x{height}"
    return None


def _build_notebook_preview(cells: object, *, limit: int = 3) -> str | None:
    if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes, bytearray)):
        return None
    preview_lines: list[str] = []
    for index, cell in enumerate(cells[:limit], start=1):
        if not isinstance(cell, Mapping):
            continue
        cell_type = str(cell.get("cell_type", "unknown")).strip() or "unknown"
        source = str(cell.get("source", "")).strip()
        snippet = source if len(source) <= 160 else f"{source[:157].rstrip()}..."
        if snippet:
            preview_lines.append(f"[{index}] {cell_type}: {snippet}")
        else:
            preview_lines.append(f"[{index}] {cell_type}")
    return "\n".join(preview_lines) if preview_lines else None


def _format_generic_tool_result(
    *,
    tool_name: str,
    tool_use_id: str,
    parsed_payload: Any,
    tool_input: Mapping[str, Any] | None,
    is_error: bool,
) -> str:
    lines = [f"### {tool_name} Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
    if is_error:
        lines.append("- Status: error")
    if tool_input:
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            lines.append("- Command:")
            _append_code_block(
                lines,
                command,
                label=f"{tool_name.lower()} command {tool_use_id}",
                language="bash",
            )
    if isinstance(parsed_payload, Mapping):
        pretty = json.dumps(dict(parsed_payload), ensure_ascii=False, indent=2)
        lines.append("- Payload:")
        _append_code_block(
            lines,
            pretty,
            label=f"tool result {tool_use_id}",
            language="json",
        )
    else:
        text = str(parsed_payload or "")
        if text.strip():
            _append_code_block(
                lines,
                text,
                label=f"tool result {tool_use_id}",
                language=_tool_result_code_language(tool_name, content=text),
            )
    return "\n".join(lines)


def _format_task_output_result(
    *,
    tool_use_id: str,
    parsed_payload: Any,
    is_error: bool,
) -> str:
    lines = ["### TaskOutput Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
    if is_error:
        lines.append("- Status: error")
    if isinstance(parsed_payload, Mapping):
        _append_bullet(lines, "Retrieval", parsed_payload.get("retrieval_status"))
        task = parsed_payload.get("task")
        if isinstance(task, Mapping):
            _append_bullet(lines, "Task ID", task.get("task_id"))
            _append_bullet(lines, "Task Type", task.get("task_type"))
            _append_bullet(lines, "Task Status", task.get("status"))
            _append_bullet(
                lines,
                "Output File",
                _inline_code(task.get("output_file")) if task.get("output_file") else None,
            )
            _append_bullet(
                lines,
                "Persisted Output Size",
                _format_bytes_label(task.get("persistedOutputSize")),
            )
            _append_bullet(lines, "Exit Code", task.get("exit_code"))
            output = task.get("output")
            if isinstance(output, str) and output.strip():
                lines.append("- Output:")
                _append_code_block(
                    lines,
                    output,
                    label=f"tool result {tool_use_id}",
                    language="text",
                )
            error = task.get("error")
            if isinstance(error, str) and error.strip():
                lines.append("- Error Output:")
                _append_code_block(
                    lines,
                    error,
                    label=f"tool result {tool_use_id}",
                    language="text",
                )
        return "\n".join(lines)
    return _format_generic_tool_result(
        tool_name="TaskOutput",
        tool_use_id=tool_use_id,
        parsed_payload=parsed_payload,
        tool_input=None,
        is_error=is_error,
    )


def _format_web_fetch_result(
    *,
    tool_use_id: str,
    parsed_payload: Any,
    is_error: bool,
) -> str:
    lines = ["### WebFetch Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
    if is_error:
        lines.append("- Status: error")
    if isinstance(parsed_payload, Mapping):
        _append_bullet(lines, "URL", parsed_payload.get("url"))
        _append_bullet(lines, "HTTP Status", parsed_payload.get("code"))
        _append_bullet(lines, "Status Text", parsed_payload.get("codeText"))
        _append_bullet(
            lines,
            "Content Type",
            _inline_code(parsed_payload.get("contentType"))
            if parsed_payload.get("contentType")
            else None,
        )
        _append_bullet(lines, "Bytes", _format_bytes_label(parsed_payload.get("bytes")))
        _append_bullet(lines, "Duration (ms)", parsed_payload.get("durationMs"))
        _append_bullet(
            lines,
            "Persisted Path",
            _inline_code(parsed_payload.get("persistedPath"))
            if parsed_payload.get("persistedPath")
            else None,
        )
        _append_bullet(
            lines,
            "Persisted Size",
            _format_bytes_label(parsed_payload.get("persistedSize")),
        )
        result = parsed_payload.get("result")
        if isinstance(result, str) and result.strip():
            lines.append("- Result:")
            _append_code_block(
                lines,
                result,
                label=f"tool result {tool_use_id}",
                language="markdown",
            )
        return "\n".join(lines)
    return _format_generic_tool_result(
        tool_name="WebFetch",
        tool_use_id=tool_use_id,
        parsed_payload=parsed_payload,
        tool_input=None,
        is_error=is_error,
    )


def _format_agent_result(
    *,
    tool_use_id: str,
    parsed_payload: Any,
    is_error: bool,
) -> str:
    lines = ["### Agent Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
    if isinstance(parsed_payload, Mapping):
        _append_bullet(lines, "Status", parsed_payload.get("status"))
        _append_bullet(lines, "Agent ID", parsed_payload.get("agentId"))
        _append_bullet(lines, "Agent Type", parsed_payload.get("agentType"))
        summary = parsed_payload.get("summary")
        if isinstance(summary, Mapping):
            _append_bullet(lines, "Headline", summary.get("headline"))
            _append_bullet(lines, "Status Text", summary.get("statusText"))
            _append_bullet(lines, "Last Activity", summary.get("lastActivity"))
        progress = parsed_payload.get("progress")
        if isinstance(progress, Mapping):
            recent = progress.get("recentActivities")
            if isinstance(recent, Sequence) and not isinstance(
                recent,
                (str, bytes, bytearray),
            ):
                normalized = [str(item).strip() for item in recent if str(item).strip()]
                if normalized:
                    lines.append("- Recent Activities: " + " -> ".join(normalized))
        content = parsed_payload.get("content")
        if isinstance(content, Sequence) and not isinstance(
            content,
            (str, bytes, bytearray),
        ):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(text)
            if text_parts:
                lines.append("- Output:")
                _append_code_block(
                    lines,
                    "\n\n".join(text_parts),
                    label=f"tool result {tool_use_id}",
                    language="markdown",
                )
        return "\n".join(lines)
    if is_error:
        lines.append("- Status: error")
    if str(parsed_payload or "").strip():
        lines.append("- Output:")
        _append_code_block(
            lines,
            str(parsed_payload),
            label=f"tool result {tool_use_id}",
            language="markdown",
        )
    return "\n".join(lines)


def _format_tool_result_markdown(
    *,
    tool_name: str,
    tool_use_id: str,
    raw_payload: Any,
    tool_input: Mapping[str, Any] | None,
    is_error: bool,
) -> str:
    parsed_payload = _parse_tool_result_payload(raw_payload)
    if tool_name == "TaskOutput" and isinstance(parsed_payload, str):
        parsed_payload = _parse_task_output_legacy_payload(parsed_payload) or parsed_payload
    if tool_name == "Read" and isinstance(parsed_payload, Mapping):
        result_type = parsed_payload.get("result_type")
        if not isinstance(result_type, str) or not result_type.strip():
            result_type = "text"
        file_path = parsed_payload.get("file_path")
        content = parsed_payload.get("content")
        total_lines = parsed_payload.get("total_lines")
        offset = parsed_payload.get("offset")
        limit = parsed_payload.get("limit")
        lines = ["### Read Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
        if is_error:
            lines.append("- Status: error")
        lines.append(f"- Result Type: {_inline_code(result_type)}")
        _append_bullet(lines, "File", _inline_code(file_path) if file_path else None)
        if result_type == "image":
            _append_bullet(
                lines,
                "MIME Type",
                _inline_code(parsed_payload.get("media_type"))
                if parsed_payload.get("media_type")
                else None,
            )
            _append_bullet(
                lines,
                "Size",
                _format_bytes_label(parsed_payload.get("original_size")),
            )
            _append_bullet(
                lines,
                "Dimensions",
                _format_image_dimensions(parsed_payload.get("dimensions")),
            )
            return "\n".join(lines)
        if result_type == "pdf":
            _append_bullet(
                lines,
                "Size",
                _format_bytes_label(parsed_payload.get("original_size")),
            )
            _append_bullet(lines, "Pages", parsed_payload.get("page_count"))
            return "\n".join(lines)
        if result_type == "parts":
            _append_bullet(lines, "Requested Pages", parsed_payload.get("pages"))
            _append_bullet(lines, "Pages Extracted", parsed_payload.get("count"))
            _append_bullet(lines, "PDF Pages", parsed_payload.get("page_count"))
            _append_bullet(
                lines,
                "Output Directory",
                _inline_code(parsed_payload.get("output_dir"))
                if parsed_payload.get("output_dir")
                else None,
            )
            _append_bullet(
                lines,
                "Original Size",
                _format_bytes_label(parsed_payload.get("original_size")),
            )
            return "\n".join(lines)
        if result_type == "notebook":
            cell_count = parsed_payload.get("cell_count")
            if not isinstance(cell_count, int):
                raw_cells = parsed_payload.get("cells")
                if isinstance(raw_cells, Sequence) and not isinstance(
                    raw_cells, (str, bytes, bytearray)
                ):
                    cell_count = len(raw_cells)
            _append_bullet(lines, "Cells", cell_count)
            preview = _build_notebook_preview(parsed_payload.get("cells"))
            if preview:
                lines.append("- Preview:")
                _append_code_block(
                    lines,
                    preview,
                    label=f"tool result {tool_use_id}",
                    language="text",
                )
            return "\n".join(lines)
        if isinstance(offset, int):
            displayed_lines = len(str(content or "").splitlines()) or 1
            line_end = offset + max(displayed_lines - 1, 0)
            if isinstance(total_lines, int):
                line_end = min(line_end, total_lines)
                lines.append(f"- Range: {offset}-{line_end} of {total_lines}")
            else:
                lines.append(f"- Range: {offset}-{line_end}")
        elif isinstance(total_lines, int):
            lines.append(f"- Total Lines: {total_lines}")
        if isinstance(limit, int):
            lines.append(f"- Limit: {limit}")
        if isinstance(content, str) and content.strip():
            lines.append("- Content:")
            _append_code_block(
                lines,
                content,
                label=f"tool result {tool_use_id}",
                language=_tool_result_code_language(
                    tool_name,
                    file_path=str(file_path) if isinstance(file_path, str) else None,
                    content=content,
                ),
            )
        return "\n".join(lines)
    if tool_name == "Bash" and isinstance(parsed_payload, Mapping):
        lines = ["### Bash Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
        if is_error:
            lines.append("- Status: error")
        command = None
        if tool_input is not None:
            raw_command = tool_input.get("command")
            if isinstance(raw_command, str) and raw_command.strip():
                command = raw_command
        if command:
            lines.append("- Command:")
            _append_code_block(
                lines,
                command,
                label=f"bash command {tool_use_id}",
                language="bash",
            )
        if "task_id" in parsed_payload:
            _append_bullet(lines, "Task ID", parsed_payload.get("task_id"))
            _append_bullet(lines, "Task Type", parsed_payload.get("task_type"))
            _append_bullet(lines, "Task Status", parsed_payload.get("status"))
            _append_bullet(lines, "Description", parsed_payload.get("description"))
            output_file = parsed_payload.get("output_file")
            if isinstance(output_file, str) and output_file.strip():
                lines.append(f"- Output File: {_inline_code(output_file)}")
            _append_bullet(
                lines,
                "Persisted Output Size",
                _format_bytes_label(parsed_payload.get("persistedOutputSize")),
            )
            if parsed_payload.get("backgroundedByUser") is True:
                lines.append("- Backgrounded By User: true")
            if parsed_payload.get("assistantAutoBackgrounded") is True:
                lines.append("- Auto Backgrounded: true")
            return "\n".join(lines)
        if parsed_payload.get("result_type") == "image":
            _append_bullet(lines, "Result Type", _inline_code("image"))
            _append_bullet(
                lines,
                "File",
                _inline_code(parsed_payload.get("file_path"))
                if parsed_payload.get("file_path")
                else None,
            )
            _append_bullet(
                lines,
                "MIME Type",
                _inline_code(parsed_payload.get("media_type"))
                if parsed_payload.get("media_type")
                else None,
            )
            _append_bullet(
                lines,
                "Size",
                _format_bytes_label(parsed_payload.get("original_size")),
            )
            _append_bullet(
                lines,
                "Dimensions",
                _format_image_dimensions(parsed_payload.get("dimensions")),
            )
        _append_bullet(lines, "Exit Code", parsed_payload.get("exit_code"))
        _append_bullet(lines, "Duration (ms)", parsed_payload.get("duration_ms"))
        if parsed_payload.get("semantic_status"):
            _append_bullet(
                lines,
                "Semantic Status",
                parsed_payload.get("semantic_status"),
            )
        if parsed_payload.get("semantic_message"):
            _append_bullet(
                lines,
                "Semantic Message",
                parsed_payload.get("semantic_message"),
            )
        if parsed_payload.get("timed_out"):
            lines.append("- Timed Out: true")
        if parsed_payload.get("interrupted"):
            lines.append("- Interrupted: true")
        if parsed_payload.get("git_operation"):
            _append_bullet(lines, "Git Operation", parsed_payload.get("git_operation"))
            _append_bullet(lines, "Git Branch", parsed_payload.get("git_branch"))
            git_commit_shas = parsed_payload.get("git_commit_shas")
            if isinstance(git_commit_shas, Sequence) and not isinstance(
                git_commit_shas, (str, bytes, bytearray)
            ):
                lines.append(
                    "- Git Commits: "
                    + ", ".join(_inline_code(str(value)) for value in git_commit_shas)
                )
            git_pr_urls = parsed_payload.get("git_pr_urls")
            if isinstance(git_pr_urls, Sequence) and not isinstance(
                git_pr_urls, (str, bytes, bytearray)
            ):
                lines.append(
                    "- Pull Requests: " + ", ".join(str(value) for value in git_pr_urls)
                )
        if parsed_payload.get("git_index_lock_error"):
            lines.append("- Git Index Lock Error: true")
        if parsed_payload.get("code_indexing_tool"):
            _append_bullet(
                lines,
                "Code Indexing Tool",
                parsed_payload.get("code_indexing_tool"),
            )
        if parsed_payload.get("sleep_pattern_detected"):
            lines.append("- Sleep Pattern Detected: true")
        if parsed_payload.get("backgrounding_suggestion"):
            _append_bullet(
                lines,
                "Backgrounding Suggestion",
                parsed_payload.get("backgrounding_suggestion"),
            )
        claude_code_hints = parsed_payload.get("claude_code_hints")
        if isinstance(claude_code_hints, Sequence) and not isinstance(
            claude_code_hints, (str, bytes, bytearray)
        ):
            filtered_hints = [
                str(value) for value in claude_code_hints if str(value).strip()
            ]
            if filtered_hints:
                lines.append("- Claude Code Hints:")
                for hint in filtered_hints:
                    lines.append(f"  - {hint}")
        stdout = parsed_payload.get("stdout")
        if (
            parsed_payload.get("result_type") != "image"
            and isinstance(stdout, str)
            and stdout.strip()
        ):
            lines.append("- Stdout:")
            _append_code_block(
                lines,
                stdout,
                label=f"tool result {tool_use_id}",
                language="text",
            )
        stderr = parsed_payload.get("stderr")
        if isinstance(stderr, str) and stderr.strip():
            lines.append("- Stderr:")
            _append_code_block(
                lines,
                stderr,
                label=f"tool result {tool_use_id}",
                language="text",
            )
        return "\n".join(lines)
    if tool_name in {"Write", "Edit"} and isinstance(parsed_payload, Mapping):
        file_path = parsed_payload.get("file_path")
        lines = [f"### {tool_name} Result", f"- Tool Use ID: {_inline_code(tool_use_id)}"]
        if is_error:
            lines.append("- Status: error")
        _append_bullet(lines, "Result Type", parsed_payload.get("result_type"))
        if isinstance(file_path, str) and file_path.strip():
            lines.append(f"- File: {_inline_code(file_path)}")
        if tool_name == "Edit":
            _append_bullet(lines, "Replace All", parsed_payload.get("replace_all"))
        git_diff = _lookup_mapping_value(parsed_payload, "gitDiff", "git_diff")
        if isinstance(git_diff, Mapping):
            _append_bullet(
                lines,
                "Git Diff",
                _inline_code(git_diff.get("status")) if git_diff.get("status") else None,
            )
            _append_bullet(lines, "Git File", _inline_code(git_diff.get("filename")) if git_diff.get("filename") else None)
            _append_bullet(lines, "Additions", git_diff.get("additions"))
            _append_bullet(lines, "Deletions", git_diff.get("deletions"))
            patch = git_diff.get("patch")
            if isinstance(patch, str) and patch.strip():
                lines.append("- Git Patch:")
                _append_code_block(
                    lines,
                    patch,
                    label=f"tool result {tool_use_id}",
                    language="diff",
                )
        content = parsed_payload.get("content")
        if isinstance(content, str) and content.strip():
            lines.append("- Updated Content:")
            _append_code_block(
                lines,
                content,
                label=f"tool result {tool_use_id}",
                language=_tool_result_code_language(
                    tool_name,
                    file_path=file_path if isinstance(file_path, str) else None,
                    content=content,
                ),
            )
        return "\n".join(lines)
    if tool_name == "TaskOutput":
        return _format_task_output_result(
            tool_use_id=tool_use_id,
            parsed_payload=parsed_payload,
            is_error=is_error,
        )
    if tool_name == "WebFetch":
        return _format_web_fetch_result(
            tool_use_id=tool_use_id,
            parsed_payload=parsed_payload,
            is_error=is_error,
        )
    if tool_name == "Agent":
        return _format_agent_result(
            tool_use_id=tool_use_id,
            parsed_payload=parsed_payload,
            is_error=is_error,
        )
    return _format_generic_tool_result(
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        parsed_payload=parsed_payload,
        tool_input=tool_input,
        is_error=is_error,
    )


def _format_user_message_tool_results(
    message: UserMessage,
    *,
    tool_lookup: Mapping[str, ToolUseBlock],
) -> str | None:
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return None
    rendered_blocks: list[str] = []
    for block, raw_payload in zip(content, _tool_result_values(message, content)):
        tool_use = tool_lookup.get(block.tool_use_id)
        tool_name = tool_use.name if tool_use is not None else "Tool"
        tool_input = tool_use.input if tool_use is not None else None
        rendered_blocks.append(
            _format_tool_result_markdown(
                tool_name=tool_name,
                tool_use_id=block.tool_use_id,
                raw_payload=raw_payload,
                tool_input=tool_input,
                is_error=block.is_error,
            )
        )
    return "\n\n".join(part for part in rendered_blocks if part.strip()) or None


def _build_tool_use_lookup(messages: Sequence[Message]) -> dict[str, ToolUseBlock]:
    lookup: dict[str, ToolUseBlock] = {}
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.message.content:
            if isinstance(block, ToolUseBlock):
                lookup[block.id] = block
    return lookup


def _build_tool_status_lookup(messages: Sequence[Message]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for message in messages:
        if isinstance(message, ProgressMessage):
            tool_id = message.parentToolUseID or message.toolUseID
            if tool_id:
                lookup[tool_id] = str(message.data.get("step") or "running")
            continue
        if not isinstance(message, UserMessage):
            continue
        content = message.message.content
        if not isinstance(content, tuple):
            continue
        for block in content:
            if isinstance(block, ToolResultBlock):
                lookup[block.tool_use_id] = "error" if block.is_error else "completed"
    return lookup


def can_start_interactive_repl() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _flatten_message_content(message: Message) -> str:
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, str):
            return content
        rendered_blocks: list[str] = []
        for block in content:
            if isinstance(block, TextBlock):
                rendered_blocks.append(block.text)
            elif isinstance(block, ToolResultBlock):
                rendered_blocks.append(
                    _collapse_tool_result_text(
                        str(block.content),
                        label=f"tool result {block.tool_use_id}",
                    )
                )
        return " ".join(rendered_blocks).strip()

    if isinstance(message, AssistantMessage):
        return _flatten_assistant_message_content(message)

    if isinstance(message, SystemInformationalMessage):
        return message.content

    if isinstance(message, ToolUseSummaryMessage):
        return message.summary

    if isinstance(message, ProgressMessage):
        return str(message.data)

    if isinstance(message, AttachmentMessage):
        return str(message.attachment)

    if isinstance(message, CompactBoundaryMessage):
        return (
            "Conversation compacted "
            f"({message.originalTokenCount} -> {message.newTokenCount} tokens)"
        )

        return str(message)


def _flatten_assistant_message_content(
    message: AssistantMessage,
    *,
    include_thinking: bool = False,
) -> str:
    rendered_blocks: list[str] = []
    for block in message.message.content:
        if isinstance(block, TextBlock):
            rendered_blocks.append(block.text)
        elif isinstance(block, ToolUseBlock):
            rendered_blocks.append(f"[{block.name}]")
        elif isinstance(block, ThinkingBlock) and include_thinking:
            thinking = block.thinking.strip()
            if thinking:
                rendered_blocks.append(f"Thinking: {thinking}")
    return " ".join(rendered_blocks).strip()


def _assistant_message_has_displayable_output(
    message: AssistantMessage,
    *,
    include_thinking: bool = False,
) -> bool:
    return any(
        isinstance(block, (TextBlock, ToolUseBlock))
        or (
            include_thinking
            and isinstance(block, ThinkingBlock)
            and bool(block.thinking.strip())
        )
        for block in message.message.content
    )


def _message_type_label(message: Message) -> str:
    if isinstance(message, UserMessage):
        return "user"
    if isinstance(message, AssistantMessage):
        return "assistant"
    return getattr(message, "type", "system")


def _coerce_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    return None


def _progress_fraction(data: Mapping[str, Any]) -> float | None:
    raw_fraction = data.get("progress")
    if isinstance(raw_fraction, (int, float)) and not isinstance(raw_fraction, bool):
        fraction = float(raw_fraction)
        if fraction > 1.0:
            fraction /= 100.0
        return max(0.0, min(fraction, 1.0))
    completed = data.get("completed")
    total = data.get("total")
    if (
        isinstance(completed, (int, float))
        and isinstance(total, (int, float))
        and not isinstance(completed, bool)
        and not isinstance(total, bool)
        and total > 0
    ):
        return max(0.0, min(float(completed) / float(total), 1.0))
    return None


def _summarize_tool_input(tool_name: str, tool_input: Mapping[str, Any] | None) -> str | None:
    if not tool_input:
        return None
    for key in ("command", "file_path", "pattern", "url", "description", "task_id", "taskId"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            normalized = " ".join(value.strip().split())
            if len(normalized) > 88:
                normalized = normalized[:85].rstrip() + "..."
            label = {
                "command": "command",
                "file_path": "file",
                "pattern": "pattern",
                "url": "url",
                "description": "description",
                "task_id": "task",
                "taskId": "task",
            }.get(key, key)
            return f"{label}: {normalized}"
    return tool_name


def _progress_detail_lines(
    data: Mapping[str, Any],
    *,
    tool_name: str,
    tool_input: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    details: list[str] = []
    if summary := _summarize_tool_input(tool_name, tool_input):
        details.append(summary)
    stdout = data.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        details.append(f"stdout: {' '.join(stdout.strip().splitlines())}")
    stderr = data.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        details.append(f"stderr: {' '.join(stderr.strip().splitlines())}")
    note = data.get("message")
    if isinstance(note, str) and note.strip():
        details.append(note.strip())
    return tuple(details)


def _stream_preview_detail_lines(
    tool_name: str,
    raw_tool_input: str,
) -> tuple[str, ...]:
    normalized = " ".join(str(raw_tool_input).split())
    if not normalized:
        return ()
    parsed = _parse_tool_result_payload(normalized)
    if isinstance(parsed, Mapping):
        summary = _summarize_tool_input(tool_name, parsed)
        if summary:
            return (summary,)
        normalized = json.dumps(dict(parsed), ensure_ascii=False, separators=(", ", ": "))
    if len(normalized) > 120:
        normalized = normalized[:117].rstrip() + "..."
    return (f"input: {normalized}",)


def _tool_result_block_ids(message: UserMessage) -> tuple[str, ...]:
    raw_content = message.message.content
    if not isinstance(raw_content, tuple):
        return ()
    return tuple(
        block.tool_use_id
        for block in raw_content
        if isinstance(block, ToolResultBlock)
    )


def _native_progress_bar_state(message: ProgressMessage) -> tuple[ProgressState, int | None]:
    step = str(message.data.get("step") or "").strip().lower()
    progress_fraction = _progress_fraction(message.data)
    percent = None if progress_fraction is None else int(round(progress_fraction * 100.0))
    if any(token in step for token in ("error", "fail", "abort", "cancel")):
        return (ProgressState.ERROR, None)
    if "warn" in step and percent is not None:
        return (ProgressState.WARNING, percent)
    if percent is None:
        return (ProgressState.INDETERMINATE, None)
    return (ProgressState.NORMAL, percent)


@dataclass
class InteractiveReplRuntime:
    session: QuerySession = field(default_factory=QuerySession)
    interaction: TUIInteractionFlow = field(
        default_factory=lambda: TUIInteractionFlow(TUIInteractionState(mode="prompt"))
    )
    cwd: str = field(default_factory=os.getcwd)
    version: str = field(
        default_factory=lambda: os.environ.get(
            "CLAUDE_CODE_VERSION", "0.0.0-python-port"
        )
    )
    model_adapter: ModelAdapter | None = None
    tool_executor: ToolExecutor | None = None
    stream_config: QueryStreamConfig = field(default_factory=QueryStreamConfig)
    config_home: str | None = None
    prompt_history_store: HistoryStore | None = None
    persist_sessions: bool = True
    session_id: str | None = None
    continue_most_recent: bool = False
    resume_session_id: str | None = None
    resume_latest_session: bool = False
    display_name: str | None = None
    show_streaming_thinking: bool = field(
        default_factory=lambda: _env_truthy("CLAUDE_PY_SHOW_STREAMING_THINKING")
    )
    permission_mode: str = "default"
    inline_agents: tuple[AgentDefinition, ...] = ()
    approval_input_reader: Callable[[], str] | None = None
    clipboard_copy_handler: Callable[[str], bool] | None = None
    _runner: asyncio.Runner | None = field(default=None, init=False, repr=False)
    _session_created_at: str | None = field(default=None, init=False, repr=False)
    _search_query: str = field(default="", init=False, repr=False)
    _search_current_match_index: int = field(default=0, init=False, repr=False)
    _search_total_matches: int = field(default=0, init=False, repr=False)
    _selected_message_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _selection_cursor_index: int = field(default=-1, init=False, repr=False)
    _last_screen_width: int = field(default=80, init=False, repr=False)
    _last_screen_height: int = field(default=24, init=False, repr=False)
    _message_scroll_offset: int = field(default=0, init=False, repr=False)
    _message_follow_output: bool = field(default=True, init=False, repr=False)
    _stream_preview_text: str = field(default="", init=False, repr=False)
    _stream_preview_tool_name: str | None = field(default=None, init=False, repr=False)
    _stream_preview_tool_input: str = field(default="", init=False, repr=False)
    _stream_preview_mode: str | None = field(default=None, init=False, repr=False)
    _turn_running: bool = field(default=False, init=False, repr=False)
    _console_entries: list[_EphemeralConsoleEntry] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _ephemeral_system_entries: list[_EphemeralSystemEntry] = field(
        default_factory=list,
        init=False,
        repr=False,
    )
    _active_render: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _setup_emitted: bool = field(default=False, init=False, repr=False)
    _session_start_emitted: bool = field(default=False, init=False, repr=False)
    _session_end_emitted: bool = field(default=False, init=False, repr=False)
    _away_summary_cutoff_ms: int | None = field(default=None, init=False, repr=False)
    _away_summary_session_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.inline_agents = tuple(self.inline_agents or ())
        if self.interaction.get_mode() != "prompt":
            self.interaction.transition_to_prompt("Type a message or /exit")
        if self.config_home is None:
            self.config_home = get_claude_config_home()
        initial_settings = get_initial_settings(
            config_home=self.config_home,
            project_root=self.cwd,
        )
        if self.model_adapter is None:
            selected_model = resolve_main_loop_model(settings=initial_settings)
            self.model_adapter = (
                create_model_adapter_from_env(model=selected_model)
                or LocalEchoModelAdapter()
            )
        if self.tool_executor is None:
            self.tool_executor = LocalToolExecutor(
                app_state=get_default_app_state(
                    settings=dict(initial_settings),
                    initial_mode=self.permission_mode,
                    settings_runtime_context={
                        "config_home": self.config_home,
                        "project_root": self.cwd,
                    },
                ),
                inline_agents=tuple(self.inline_agents),
            )
        elif isinstance(self.tool_executor, LocalToolExecutor) and self.inline_agents:
            self.tool_executor.inline_agents = tuple(self.inline_agents)
        self._bind_executor_interaction()
        self._apply_permission_mode_to_executor()
        executor_obj: Any = self.tool_executor
        get_tool_schemas = getattr(executor_obj, "get_tool_schemas", None)
        if self.stream_config.tools is None and callable(get_tool_schemas):
            self.stream_config = replace(
                self.stream_config,
                tools=get_tool_schemas(),
            )
        self._restore_persisted_session_if_requested()
        if self.session_id is None:
            self.session_id = generate_session_id()
        if self.prompt_history_store is None and self.persist_sessions:
            self.prompt_history_store = HistoryStore(
                config_home=self.config_home,
                project_root=self.cwd,
                session_id=self.session_id,
            )
        self._apply_permission_mode_to_executor()
        self._emit_setup_hook()
        self._emit_session_start_hook()
        self._persist_session()

    def get_prompt_history_entries(self) -> list[str]:
        if self.prompt_history_store is None:
            return []

        try:
            entries = [
                display
                for entry in self.prompt_history_store.get_history()
                if isinstance(display := entry.get("display"), str) and display.strip()
            ]
        except Exception:
            return []

        entries.reverse()
        return entries

    def record_prompt_history_entry(self, line: str) -> None:
        if self.prompt_history_store is None:
            return

        try:
            self.prompt_history_store.add_to_history(line)
            self.prompt_history_store.flush()
        except Exception:
            return

    def build_screen_text(self, width: int = 80, height: int = 24) -> str:
        self._last_screen_width = max(width, 1)
        self._last_screen_height = max(height, 1)
        prompt_output = self.interaction.render(
            width,
            8 if self.interaction.get_mode() == "approval" else 3,
        )
        try:
            message_rows = self._visible_message_rows()
            search_query, current_match_index, total_matches = self._refresh_search_state(
                message_rows=message_rows,
                width=self._last_screen_width,
            )
            selected_model = self._selected_model()
            usage = self._session_usage_totals()
            status = StatusLineData(
                model_id=selected_model,
                model_display_name=selected_model,
                current_dir=self.cwd,
                version=self.version,
                permission_mode=self.permission_mode,
                total_input_tokens=(
                    usage["input_tokens"] + usage["cache_creation_input_tokens"]
                ),
                total_output_tokens=(
                    usage["output_tokens"] + usage["cache_read_input_tokens"]
                ),
                search_query=search_query,
                current_match_index=current_match_index,
                total_matches=total_matches,
                selection_summary=self._selection_status_summary(),
                status_line_text=None,
            )
            repl = REPLRenderer(
                REPLScreenData(
                    width=width,
                    height=height,
                    messages=message_rows,
                    status_line=status,
                    fullscreen=True,
                    search_query=search_query,
                    current_match_index=current_match_index,
                    total_matches=total_matches,
                    scroll_offset=self._message_scroll_offset,
                    follow_output=self._message_follow_output,
                )
            )
            screen = repl.render()
        except Exception as exc:
            display = self._build_structured_error_display(
                exc,
                title="Render Error",
                code="render_error",
                stage="repl_screen",
                log_error=True,
            )
            screen = MessageRowRenderer(
                MessageRowData(
                    message_id="repl-render-error",
                    message_type="system",
                    content=f"System:\n{format_structured_error_markdown(display)}",
                    theme=self._current_theme(),
                )
            ).render(self._last_screen_width)
        if screen.strip():
            return f"{screen}\n\n{prompt_output}"
        return prompt_output

    def is_turn_running(self) -> bool:
        return self._turn_running

    def build_terminal_status_text(self, width: int = 80) -> str:
        usage = self._session_usage_totals()
        total_tokens = (
            usage["input_tokens"]
            + usage["cache_creation_input_tokens"]
            + usage["output_tokens"]
            + usage["cache_read_input_tokens"]
        )
        state = "running" if self._turn_running else "idle"
        parts = [
            f"[{state}]",
            f"[{self._selected_model()}]",
            self.cwd,
            f"{total_tokens} tokens",
        ]
        if self.permission_mode != "default":
            parts.insert(2, f"permission:{self.permission_mode}")
        return _truncate_status_segment(" | ".join(parts), max(width, 1))

    def build_terminal_footer_text(self, width: int = 80) -> str:
        prompt_output = self.interaction.render(
            width,
            8 if self.interaction.get_mode() == "approval" else 1,
        )
        return f"{prompt_output}\n{self.build_terminal_status_text(width)}"

    def build_native_progress_bar_payload(self) -> str:
        completed_tool_use_ids: set[str] = set()
        for message in reversed(self.session.messages):
            if isinstance(message, UserMessage):
                completed_tool_use_ids.update(_tool_result_block_ids(message))
                continue
            if not isinstance(message, ProgressMessage):
                continue
            tool_use_id = message.parentToolUseID or message.toolUseID
            if tool_use_id in completed_tool_use_ids:
                continue
            state, percent = _native_progress_bar_state(message)
            if percent is None:
                return set_progress_bar(state)
            return set_progress_bar(state, percent)
        return clear_progress_bar()

    def reset_transcript_scroll(self) -> None:
        self._message_scroll_offset = 0
        self._message_follow_output = True

    def scroll_transcript_by(self, delta: int) -> bool:
        rows = self._visible_message_rows()
        if not rows or delta == 0:
            return False
        max_index = max(len(rows) - 1, 0)
        if delta < 0:
            anchor = max_index if self._message_follow_output else self._message_scroll_offset
            self._message_scroll_offset = max(0, min(max_index, anchor + delta))
            self._message_follow_output = False
            return True
        if self._message_follow_output:
            return False
        self._message_scroll_offset = max(0, min(max_index, self._message_scroll_offset + delta))
        if self._message_scroll_offset >= max_index:
            self.reset_transcript_scroll()
        return True

    def handle_line(
        self,
        raw_line: str,
        *,
        render: Callable[[], None] | None = None,
        stream_render: Callable[[], None] | None = None,
    ) -> bool:
        line = raw_line.strip()
        if not line:
            return True
        self.reset_transcript_scroll()

        if line.startswith("/"):
            handled = self._handle_slash_command(line)
            if isinstance(handled, _PromptSlashCommandInvocation):
                line = handled.prompt
            elif handled is not None:
                self._persist_session()
                return handled

        try:
            self._turn_running = True
            self.session = self.session.startTurn()
            line = self._emit_user_prompt_submit_hook(line)
            self.session = self.session.appendMessage(createUserMessage(content=line))
            turn_start_message_count = len(self.session.messages)
            self._persist_session()
            if render is not None:
                render()
            self._active_render = render
            try:
                try:
                    self.session = self._get_runner().run(
                        self._run_single_turn_stream(render=stream_render or render)
                    )
                    self._ensure_terminal_error_message_for_turn(
                        previous_message_count=turn_start_message_count,
                        title="Model Error",
                        code="model_request_error",
                        stage="handle_line",
                    )
                finally:
                    self._active_render = None
            except QueryStateTransitionError as exc:
                display = self._build_structured_error_display(
                    exc,
                    title="Query Error",
                    code="query_state_transition_error",
                    stage="handle_line",
                    log_error=True,
                )
                self.session = self.session.appendMessage(
                    createAssistantAPIErrorMessage(
                        content=format_structured_error_markdown(display),
                        apiError="query_state_transition_error",
                        error=exc,
                        errorDetails=display.detail,
                    )
                )
                self.session = self.session.finishTurn(
                    TerminalTransition(reason=TerminalReason.MODEL_ERROR)
                )
                self._clear_stream_preview()
            except Exception as exc:
                display = self._build_structured_error_display(
                    exc,
                    title="Model Error",
                    code="model_request_error",
                    stage="handle_line",
                    log_error=True,
                )
                self.session = self.session.appendMessage(
                    createAssistantAPIErrorMessage(
                        content=format_structured_error_markdown(display),
                        apiError="model_request_error",
                        error=exc,
                        errorDetails=display.detail,
                    )
                )
                self.session = self.session.finishTurn(
                    TerminalTransition(reason=TerminalReason.MODEL_ERROR)
                )
                self._clear_stream_preview()
        finally:
            self._turn_running = False
        self._persist_session()
        self.interaction.transition_to_prompt("Type a message or /exit")
        if render is not None:
            render()
        return True

    def _handle_slash_command(
        self,
        line: str,
    ) -> bool | _PromptSlashCommandInvocation | None:
        command, _, raw_args = line.partition(" ")
        args = raw_args.strip()
        resolved_command = (
            _resolve_builtin_slash_command(command[1:]) if command.startswith("/") else None
        )
        command_variants = {command}
        if resolved_command is not None:
            command_variants.add(f"/{resolved_command.name}")
            command_variants.add(f"/{_camel_to_kebab(resolved_command.name)}")

        def command_is(*names: str) -> bool:
            return any(name in command_variants for name in names)

        if "/exit" in command_variants:
            self.close()
            return False

        if "/help" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(
                    self._build_help_message(),
                    "info",
                )
            )
            return True

        if "/clear" in command_variants:
            self.session = QuerySession()
            self._clear_search_state()
            self._clear_selection_state()
            self._clear_stream_preview()
            clear_beta_latches = getattr(self.model_adapter, "clear_beta_header_latches", None)
            if callable(clear_beta_latches):
                clear_beta_latches()
            self.interaction.transition_to_prompt("Type a message or /exit")
            return True

        if "/status" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_status_message(), "info")
            )
            return True

        if "/memory" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_memory_message(), "info")
            )
            return True

        if "/model" in command_variants:
            response = self._handle_model_command(args)
            self.session = self.session.appendMessage(
                createSystemMessage(response, "info")
            )
            return True

        if "/resume" in command_variants:
            response = self._handle_resume_command(args)
            self.session = self.session.appendMessage(
                createSystemMessage(response, "info")
            )
            self.interaction.transition_to_prompt("Type a message or /exit")
            return True

        if "/compact" in command_variants:
            response = self._handle_compact_command()
            self.session = self.session.appendMessage(
                createSystemMessage(response, "info")
            )
            return True

        if "/config" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_config_message(args), "info")
            )
            return True

        if "/cost" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_cost_message(), "info")
            )
            return True

        if "/doctor" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_doctor_message(), "info")
            )
            return True

        if "/init" in command_variants:
            return _PromptSlashCommandInvocation(prompt=self._build_init_prompt())

        if "/terminal-setup" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_terminal_setup_message(), "info")
            )
            return True

        if "/search" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_search_command(args), "info")
            )
            return True

        if "/select" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_select_command(args), "info")
            )
            return True

        if "/copy" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_copy_command(args), "info")
            )
            return True

        if "/diff" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_diff_command(args), "info")
            )
            return True

        if "/hooks" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_hooks_message(), "info")
            )
            return True

        if "/permissions" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_permissions_command(args), "info")
            )
            return True

        if "/plan" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_plan_command(args), "info")
            )
            return True

        if "/session" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_session_command(args), "info")
            )
            return True

        if "/skills" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_skills_message(), "info")
            )
            return True

        if "/stats" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_stats_message(), "info")
            )
            return True

        if "/usage" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_usage_message(), "info")
            )
            return True

        if "/effort" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_effort_command(args), "info")
            )
            return True

        if "/files" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_files_message(), "info")
            )
            return True

        if "/feedback" in command_variants:
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_feedback_message(), "info")
            )
            return True

        if "/review" in command_variants:
            return _PromptSlashCommandInvocation(prompt=self._build_review_prompt(args))

        if command_is("/addDir", "/add-dir"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_add_dir_command(args), "info")
            )
            return True

        if command_is("/advisor"):
            return _PromptSlashCommandInvocation(prompt=self._build_advisor_prompt(args))

        if command_is("/agents"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_agents_command(args), "info")
            )
            return True

        if command_is("/branch"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_branch_command(args), "info")
            )
            return True

        if command_is("/btw"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_btw_command(args), "info")
            )
            return True

        if command_is("/color"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_color_command(args), "info")
            )
            return True

        if command_is("/context", "/contextNonInteractive", "/context-non-interactive"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_context_message(args), "info")
            )
            return True

        if command_is("/export", "/exportCommand", "/export-command"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_export_command(args), "info")
            )
            return True

        if command_is("/fast"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_fast_command(args), "info")
            )
            return True

        if command_is("/mcp"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_mcp_command(args), "info")
            )
            return True

        if command_is("/outputStyle", "/output-style"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_output_style_command(args), "info")
            )
            return True

        if command_is("/passes"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_passes_message(), "info")
            )
            return True

        if command_is("/plugin"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_plugin_command(args), "info")
            )
            return True

        if command_is("/releaseNotes", "/release-notes"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_release_notes_message(), "info")
            )
            return True

        if command_is("/reloadPlugins", "/reload-plugins"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_reload_plugins_command(), "info")
            )
            return True

        if command_is("/rename"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_rename_command(args), "info")
            )
            return True

        if command_is("/rewind"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_rewind_command(args), "info")
            )
            return True

        if command_is("/sandboxToggle", "/sandbox-toggle"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_sandbox_toggle_command(args), "info")
            )
            return True

        if command_is("/securityReview", "/security-review"):
            return _PromptSlashCommandInvocation(prompt=self._build_security_review_prompt(args))

        if command_is("/statusline"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_statusline_command(args), "info")
            )
            return True

        if command_is("/tag"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_tag_command(args), "info")
            )
            return True

        if command_is("/tasks"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_tasks_command(args), "info")
            )
            return True

        if command_is("/theme"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._handle_theme_command(args), "info")
            )
            return True

        if command_is("/ultrareview"):
            return _PromptSlashCommandInvocation(prompt=self._build_ultrareview_prompt(args))

        if command_is("/usageReport", "/usage-report"):
            return _PromptSlashCommandInvocation(prompt=self._build_usage_report_prompt())

        if command_is("/extraUsage", "/extra-usage", "/extraUsageNonInteractive", "/extra-usage-non-interactive"):
            self.session = self.session.appendMessage(
                createSystemMessage(self._build_usage_message(), "info")
            )
            return True

        scope_excluded_command = self._scope_excluded_command(command_variants)
        if scope_excluded_command is not None:
            self.session = self.session.appendMessage(
                createSystemMessage(
                    self._build_scope_excluded_message(scope_excluded_command),
                    "info",
                )
            )
            return True

        skill_invocation = self._resolve_skill_slash_command(command[1:], args)
        if skill_invocation is not None:
            return skill_invocation

        self.session = self.session.appendMessage(
            createSystemMessage(
                f"Unknown command: {command}. Run /help for available commands.",
                "error",
            )
        )
        return True

    def close(self) -> None:
        self._emit_session_end_hook()
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    async def _run_single_turn_stream(
        self,
        *,
        render: Callable[[], None] | None = None,
    ) -> QuerySession:
        current_session = self.session
        model_adapter = self.model_adapter
        if model_adapter is None:
            raise RuntimeError("Model adapter was not initialized")
        assert model_adapter is not None
        self._clear_ephemeral_system_messages()
        effective_config = self._effective_stream_config()
        async for event in stream_query_session(
            current_session,
            model_adapter=model_adapter,
            tool_executor=self.tool_executor,
            config=effective_config,
        ):
            current_session = event.session
            self.session = current_session
            self._update_stream_preview(event.output)
            self._persist_session()
            if render is not None:
                render()
        self._clear_stream_preview()
        self._clear_ephemeral_system_messages()
        return current_session

    def _message_to_row(
        self,
        message: Message,
        *,
        tool_lookup: Mapping[str, ToolUseBlock] | None = None,
        tool_status_lookup: Mapping[str, str] | None = None,
    ) -> MessageRowData | None:
        if isinstance(message, AssistantMessage) and not _assistant_message_has_displayable_output(
            message,
            include_thinking=self.show_streaming_thinking,
        ):
            return None
        if isinstance(message, AssistantMessage):
            tool_row = self._assistant_tool_use_row(
                message,
                tool_status_lookup=tool_status_lookup
                or _build_tool_status_lookup(self.session.messages),
            )
            if tool_row is not None:
                return tool_row
        if isinstance(message, ProgressMessage):
            resolved_lookup = tool_lookup or {}
            tool_use = resolved_lookup.get(message.parentToolUseID) or resolved_lookup.get(
                message.toolUseID
            )
            tool_name = tool_use.name if tool_use is not None else "Tool"
            tool_input = tool_use.input if tool_use is not None else None
            elapsed_ms = _coerce_non_negative_int(message.data.get("elapsedMs"))
            progress_fraction = _progress_fraction(message.data)
            return MessageRowData(
                message_id=getattr(message, "uuid", ""),
                message_type="progress",
                content="",
                title=tool_name,
                status_text=str(message.data.get("step") or "running"),
                detail_lines=_progress_detail_lines(
                    message.data,
                    tool_name=tool_name,
                    tool_input=tool_input,
                ),
                progress_fraction=progress_fraction,
                elapsed_ms=elapsed_ms,
                progress_frame_index=(elapsed_ms or 0) // 125,
                timestamp=getattr(message, "timestamp", None),
                is_selected=getattr(message, "uuid", "") in self._selected_message_ids,
                theme=self._current_theme(),
            )
        label = _message_type_label(message)
        content = (
            _format_user_message_tool_results(
                message,
                tool_lookup=tool_lookup or {},
            )
            if isinstance(message, UserMessage)
            else None
        ) or (
            _flatten_assistant_message_content(
                message,
                include_thinking=self.show_streaming_thinking,
            )
            if isinstance(message, AssistantMessage)
            else _flatten_message_content(message)
        )
        prefix = {
            "user": "You",
            "assistant": "Claude",
            "system": "System",
        }.get(label, label.title())
        row_content = f"{prefix}: {content}" if "\n" not in content else f"{prefix}:\n{content}"
        return MessageRowData(
            message_id=getattr(message, "uuid", ""),
            message_type=label,
            content=row_content,
            timestamp=getattr(message, "timestamp", None),
            is_selected=getattr(message, "uuid", "") in self._selected_message_ids
            and not isinstance(message, SystemInformationalMessage),
            theme=self._current_theme(),
        )

    def _assistant_tool_use_row(
        self,
        message: AssistantMessage,
        *,
        tool_status_lookup: Mapping[str, str],
    ) -> MessageRowData | None:
        content = message.message.content
        tool_blocks = tuple(block for block in content if isinstance(block, ToolUseBlock))
        if not tool_blocks:
            return None
        visible_text = tuple(
            block.text.strip()
            for block in content
            if isinstance(block, TextBlock) and block.text.strip()
        )
        if visible_text:
            return None

        first_block = tool_blocks[0]
        status_text = tool_status_lookup.get(first_block.id, "queued")
        title = first_block.name
        if len(tool_blocks) > 1:
            names = tuple(block.name for block in tool_blocks[:3])
            title = ", ".join(names)
            if len(tool_blocks) > 3:
                title = f"{title}, +{len(tool_blocks) - 3}"
        details = tuple(
            summary
            for block in tool_blocks
            if (summary := _summarize_tool_input(block.name, block.input))
        )
        progress_fraction = 1.0 if status_text in {"completed", "error"} else None
        return MessageRowData(
            message_id=getattr(message, "uuid", ""),
            message_type="progress",
            content="",
            title=title,
            status_text=status_text,
            detail_lines=details,
            progress_fraction=progress_fraction,
            progress_frame_index=int(time.monotonic() * 8.0),
            timestamp=getattr(message, "timestamp", None),
            is_selected=getattr(message, "uuid", "") in self._selected_message_ids,
            theme=self._current_theme(),
        )

    def _current_theme(self) -> str:
        app_state = self._current_app_state()
        settings = getattr(app_state, "settings", None)
        if isinstance(settings, Mapping):
            theme = settings.get("theme")
            if isinstance(theme, str) and theme.strip():
                return theme
        return "dark"

    def _current_app_state(self) -> Any:
        executor_obj: Any = self.tool_executor
        return getattr(executor_obj, "app_state", None)

    def _runtime_hook_sources(self) -> tuple[Mapping[str, object], ...]:
        sources: list[Mapping[str, object]] = []
        app_state = self._current_app_state()
        sync_app_state_settings_from_disk(app_state, project_root=self.cwd)
        sync_managed_plugins_runtime_state(app_state)
        settings = getattr(app_state, "settings", None)
        if isinstance(settings, Mapping):
            settings_hooks = settings.get("hooks")
            if isinstance(settings_hooks, Mapping):
                sources.append(settings_hooks)
        session_hooks = getattr(app_state, "session_hooks", None)
        if isinstance(session_hooks, Mapping):
            sources.append(session_hooks)
        plugins = getattr(app_state, "plugins", None)
        enabled_plugins = getattr(plugins, "get", None)
        plugin_entries = (
            enabled_plugins("enabled")
            if callable(enabled_plugins)
            else plugins.get("enabled")
            if isinstance(plugins, Mapping)
            else None
        )
        if isinstance(plugin_entries, Sequence) and not isinstance(
            plugin_entries,
            (str, bytes, bytearray),
        ):
            for plugin in plugin_entries:
                hooks = getattr(plugin, "hooks_config", None)
                if not isinstance(hooks, Mapping) and isinstance(plugin, Mapping):
                    for key in ("hooks_config", "hooks"):
                        candidate = plugin.get(key)
                        if isinstance(candidate, Mapping):
                            hooks = candidate
                            break
                if isinstance(hooks, Mapping):
                    sources.append(hooks)
        scoped_hook_configs = getattr(self.tool_executor, "scoped_hook_configs", ())
        if isinstance(scoped_hook_configs, Sequence) and not isinstance(
            scoped_hook_configs,
            (str, bytes, bytearray),
        ):
            for hooks in scoped_hook_configs:
                if isinstance(hooks, Mapping):
                    sources.append(hooks)
        return tuple(sources)

    def _has_runtime_hook(self, event_name: str) -> bool:
        for hooks in self._runtime_hook_sources():
            if event_name in hooks:
                return True
        return False

    async def _emit_runtime_hook(
        self,
        event_name: str,
        payload: Mapping[str, Any],
    ) -> HookEventResult:
        executor_obj: Any = self.tool_executor
        emit_hook = getattr(executor_obj, "emit_hook_event", None)
        if callable(emit_hook):
            result = emit_hook(event_name, payload)
            if asyncio.iscoroutine(result):
                return await result
            if isinstance(result, HookEventResult):
                return result
        app_state = self._current_app_state()
        if app_state is None:
            return HookEventResult()
        scoped_hook_configs = getattr(executor_obj, "scoped_hook_configs", ())
        if not isinstance(scoped_hook_configs, Sequence):
            scoped_hook_configs = ()
        return await dispatch_hook_event(
            event_name,
            payload,
            app_state=app_state,
            cwd=self.cwd,
            scoped_hook_configs=scoped_hook_configs,
            runtime_context={
                "tool_executor": executor_obj,
                "session_messages": self.session.messages,
            },
        )

    def _apply_hook_messages(
        self,
        messages: Sequence[Message],
    ) -> None:
        for message in messages:
            self.session = self.session.appendMessage(message)

    def _runtime_hook_payload(
        self,
        *,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "displayName": self.display_name,
            "messageCount": len(self.session.messages),
            "createdAt": self._session_created_at,
            "continued": self.continue_most_recent,
            "resumeLatestSession": self.resume_latest_session,
            "resumeSessionId": self.resume_session_id,
        }
        if prompt is not None:
            payload["prompt"] = prompt
            payload["content"] = prompt
        return payload

    def _emit_session_start_hook(self) -> None:
        if self._session_start_emitted:
            return
        if not self._has_runtime_hook("SessionStart"):
            self._session_start_emitted = True
            return
        payload = self._runtime_hook_payload()
        payload["resumed"] = bool(
            self.resume_session_id
            or self.continue_most_recent
            or self.resume_latest_session
        )
        hook_result = self._get_runner().run(
            self._emit_runtime_hook("SessionStart", payload)
        )
        self._apply_hook_messages(hook_result.messages)
        self._session_start_emitted = True

    def _emit_setup_hook(self) -> None:
        if self._setup_emitted:
            return
        if not self._has_runtime_hook("Setup"):
            self._setup_emitted = True
            return
        app_state = self._current_app_state()
        payload = self._runtime_hook_payload()
        payload.update(
            {
                "version": self.version,
                "configHome": self.config_home,
                "persistSessions": self.persist_sessions,
                "permissionMode": self.permission_mode,
                "toolCount": len(self.stream_config.tools or ()),
                "selectedModel": getattr(self.model_adapter, "model", None)
                or getattr(app_state, "main_loop_model_for_session", None)
                or getattr(app_state, "main_loop_model", None),
            }
        )
        hook_result = self._get_runner().run(
            self._emit_runtime_hook("Setup", payload)
        )
        self._apply_hook_messages(hook_result.messages)
        self._setup_emitted = True

    def _emit_session_end_hook(self) -> None:
        if self._session_end_emitted:
            return
        if not self._has_runtime_hook("SessionEnd"):
            self._session_end_emitted = True
            return
        payload = self._runtime_hook_payload()
        if self.session.messages:
            payload["lastMessageType"] = _message_type_label(self.session.messages[-1])
        hook_result = self._get_runner().run(
            self._emit_runtime_hook("SessionEnd", payload)
        )
        self._apply_hook_messages(hook_result.messages)
        self._persist_session()
        self._session_end_emitted = True

    def _emit_user_prompt_submit_hook(self, line: str) -> str:
        if not self._has_runtime_hook("UserPromptSubmit"):
            return line
        hook_result = self._get_runner().run(
            self._emit_runtime_hook(
                "UserPromptSubmit",
                self._runtime_hook_payload(prompt=line),
            )
        )
        self._apply_hook_messages(hook_result.messages)
        updated_input = hook_result.updated_input
        if isinstance(updated_input, Mapping):
            for key in ("prompt", "text", "content"):
                value = updated_input.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return line

    def _effective_stream_config(self) -> QueryStreamConfig:
        app_state = self._current_app_state()
        settings = getattr(app_state, "settings", None)
        normalized_settings = settings if isinstance(settings, Mapping) else {}
        selected_model = self._selected_model(settings=normalized_settings)
        fallback_adapter = self.stream_config.fallback_model_adapter
        if isinstance(
            self.model_adapter,
            (AnthropicStreamingModelAdapter, OpenAIChatStreamingModelAdapter),
        ):
            self.model_adapter.model = selected_model

        if isinstance(self.model_adapter, AnthropicStreamingModelAdapter):
            fallback_model = resolve_fallback_model(
                app_state=app_state,
                settings=normalized_settings,
                primary_model=selected_model,
            )
            self.model_adapter.fallback_model = fallback_model
            fallback_adapter = (
                AnthropicStreamingModelAdapter.from_env(model=fallback_model)
                if fallback_model is not None
                else None
            )

        merged_options: dict[str, Any] = dict(self.stream_config.options or {})
        merged_beta_headers = tuple(
            dict.fromkeys(
                beta.strip()
                for beta in (
                    *(merged_options.get("beta_headers") or ()),
                    *getSdkBetas(),
                )
                if isinstance(beta, str) and beta.strip()
            )
        )
        if merged_beta_headers:
            merged_options["beta_headers"] = merged_beta_headers

        effort_value = getInitialEffortValue()
        if not is_valid_effort_level(effort_value):
            candidate_effort = getattr(app_state, "effort_value", None)
            if is_valid_effort_level(candidate_effort):
                effort_value = candidate_effort
            else:
                setting_effort = normalized_settings.get("effortValue")
                effort_value = setting_effort if is_valid_effort_level(setting_effort) else None
        if "effort" not in merged_options and is_valid_effort_level(effort_value):
            merged_options["effort"] = effort_value

        if "thinking_enabled" not in merged_options:
            thinking_enabled = getattr(app_state, "thinking_enabled", None)
            if isinstance(thinking_enabled, bool):
                merged_options["thinking_enabled"] = thinking_enabled
            else:
                merged_options["thinking_enabled"] = default_thinking_enabled(
                    settings=normalized_settings
                )

        return replace(
            self.stream_config,
            options=merged_options or None,
            fallback_model_adapter=fallback_adapter,
        )

    def _selected_model(self, *, settings: Mapping[str, Any] | None = None) -> str:
        app_state = self._current_app_state()
        return resolve_main_loop_model(
            app_state=app_state,
            settings=settings,
            fallback=getattr(self.model_adapter, "model", None)
            or getInitialMainLoopModel()
            or "claude-sonnet-4-5",
        )

    def _build_status_message(self) -> str:
        memory = self.session.externalMetadata.session_memory
        lines = [
            "### Session Status",
            f"- Session ID: {_inline_code(self.session_id or 'unknown')}",
            f"- Session State: {_inline_code(self.session.sessionState.value)}",
            f"- Messages: {len(self.session.messages)}",
            f"- Model: {_inline_code(self._selected_model())}",
            f"- Permission Mode: {_inline_code(self.permission_mode)}",
            f"- Persist Sessions: {_inline_code(str(self.persist_sessions).lower())}",
            f"- Working Directory: {_inline_code(self.cwd)}",
        ]
        if self._session_created_at:
            lines.append(f"- Created At: {_inline_code(self._session_created_at)}")
        if memory is not None:
            lines.append(f"- Compactions: {memory.compaction_count}")
            if memory.summary_source:
                lines.append(f"- Memory Source: {_inline_code(memory.summary_source)}")
        return "\n".join(lines)

    def _build_memory_message(self) -> str:
        memory = self.session.externalMetadata.session_memory
        if memory is None:
            return "No session memory extracted yet."
        lines = ["### Session Memory"]
        if memory.summary:
            lines.append(f"- Summary: {memory.summary}")
        if memory.user_requests:
            lines.append("- User Requests: " + "; ".join(memory.user_requests))
        if memory.decisions:
            lines.append("- Decisions: " + "; ".join(memory.decisions))
        if memory.tool_activity:
            lines.append("- Tool Activity: " + "; ".join(memory.tool_activity))
        if memory.files:
            lines.append("- Files: " + "; ".join(memory.files))
        if memory.open_questions:
            lines.append("- Open Questions: " + "; ".join(memory.open_questions))
        lines.append(f"- Compactions: {memory.compaction_count}")
        if memory.summary_source:
            lines.append(f"- Source: {_inline_code(memory.summary_source)}")
        if memory.summary_origin:
            lines.append(f"- Origin: {_inline_code(memory.summary_origin)}")
        return "\n".join(lines)

    def _build_help_message(self) -> str:
        lines = [
            (
                "Commands: /help, /exit (/quit), /clear (/reset, /new), /status, "
                "/memory, /model [name|reset], /resume [latest|session-id] (/continue), "
                "/compact, /config [show|get|set|unset] (/settings), /cost, /doctor, "
                "/init, /terminal-setup"
            ),
            (
                "Workspace: /diff, /files, /hooks, /permissions, /plan, /review, "
                "/session, /skills, /stats, /usage, /effort, /feedback"
            ),
            (
                "Runtime: /agents, /mcp, /plugin, /tasks, /theme, /fast, "
                "/context, /export, /rename, /rewind, /search [query|next|prev|clear], "
                "/select [index|range|next|prev|all|clear], /copy [selection|last|all]"
            )
        ]
        skill_labels = self._visible_skill_help_labels()
        if skill_labels:
            preview = ", ".join(skill_labels[:8])
            remaining = len(skill_labels) - 8
            if remaining > 0:
                preview += f", +{remaining} more"
            lines.append(f"Custom skills: {preview}")
        return "\n".join(lines)

    def _build_init_prompt(self) -> str:
        project_name = os.path.basename(os.path.abspath(self.cwd.rstrip(os.sep))) or self.cwd
        return (
            "Set up a minimal CLAUDE.md for this repository.\n\n"
            f"Working directory: {self.cwd}\n"
            f"Project name: {project_name}\n\n"
            "First inspect the codebase and any existing guidance files before writing anything. "
            "Read the key manifest/build/test files, README, Makefile or task runner files, CI config, "
            "existing CLAUDE.md or CLAUDE.local.md, AGENTS.md, .claude_py/rules/, .cursor/rules or "
            ".cursorrules, .github/copilot-instructions.md, .windsurfrules, .clinerules, and .mcp.json "
            "if they exist.\n\n"
            "Figure out the non-obvious build, test, lint, and verification commands; the main project "
            "structure; architecture decisions that require reading multiple files; code style rules that "
            "differ from language defaults; required env/setup steps; and workflow gotchas future Claude "
            "Code sessions would otherwise miss.\n\n"
            "Ask focused follow-up questions only for information that cannot be inferred from the repo. "
            "If the user wants personal instructions, offer CLAUDE.local.md as a private companion file.\n\n"
            "Then create or improve CLAUDE.md. Keep it concise: include only information whose absence "
            "would cause future agent runs to make mistakes. Do not list obvious files, do not repeat "
            "generic best practices, and do not invent commands or project conventions.\n\n"
            "Prefix CLAUDE.md with exactly:\n"
            "# CLAUDE.md\n\n"
            "This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository."
        )

    def _build_terminal_setup_message(self) -> str:
        terminal_id, display_name = _detect_terminal_setup_target()
        if terminal_id in _NATIVE_CSIU_TERMINALS:
            return (
                f"Shift+Enter is natively supported in {display_name}.\n\n"
                "No configuration is needed. Use Shift+Enter to add newlines in the REPL."
            )

        if terminal_id in _VSCODE_FAMILY_TERMINALS:
            remote_note = (
                "\n\nThis looks like a remote VSCode-family session. Install the keybinding on "
                "the local machine, not on the remote host."
                if _is_vscode_remote_session()
                else ""
            )
            editor_name = _VSCODE_FAMILY_TERMINALS[terminal_id]
            return (
                f"Detected {editor_name} terminal.\n\n"
                "Add this keybinding to your Keyboard Shortcuts JSON:\n\n"
                "```json\n"
                "[\n"
                "  {\n"
                "    \"key\": \"shift+enter\",\n"
                "    \"command\": \"workbench.action.terminal.sendSequence\",\n"
                "    \"args\": { \"text\": \"\\u001b\\r\" },\n"
                "    \"when\": \"terminalFocus\"\n"
                "  }\n"
                "]\n"
                "```\n\n"
                "This enables Shift+Enter to insert a newline without sending the prompt. "
                "The existing fallback `\\\\ + Enter` continues to work without any setup."
                f"{remote_note}"
            )

        if terminal_id == "apple_terminal":
            return (
                "Detected Apple Terminal.\n\n"
                "The Python port does not yet edit Terminal.app preferences automatically. "
                "For multiline input, either keep using the existing `\\\\ + Enter` fallback, "
                "or configure Terminal.app so Option is sent as Meta and use Option+Enter for "
                "newline input."
            )

        if terminal_id == "alacritty":
            return (
                "Detected Alacritty.\n\n"
                "Configure Shift+Enter to send the sequence `ESC CR` (`\\u001b\\r`) in your "
                "Alacritty keybindings if you want native multiline entry. The fallback "
                "`\\\\ + Enter` already works without terminal changes."
            )

        if terminal_id == "zed":
            return (
                "Detected Zed terminal.\n\n"
                "Configure a terminal keybinding that sends `ESC CR` (`\\u001b\\r`) for "
                "Shift+Enter if you want native multiline entry. The fallback `\\\\ + Enter` "
                "already works without setup."
            )

        return (
            f"Terminal setup cannot be run automatically from {display_name}.\n\n"
            "This command is used to configure a convenient Shift+Enter shortcut for multiline "
            "input. If your terminal supports custom keybindings, map Shift+Enter to send "
            "`ESC CR` (`\\u001b\\r`). Otherwise keep using the built-in `\\\\ + Enter` fallback.\n\n"
            "Native Shift+Enter works out of the box in iTerm2, WezTerm, Ghostty, Kitty, and Warp."
        )

    def _last_copyable_message_content(self) -> str | None:
        tool_lookup = _build_tool_use_lookup(self.session.messages)
        fallback_content: str | None = None
        for message in reversed(self.session.messages):
            if isinstance(message, SystemInformationalMessage):
                continue
            row = self._message_to_row(message, tool_lookup=tool_lookup)
            if row is None:
                continue
            content = row.content.strip()
            if not content:
                continue
            if isinstance(message, AssistantMessage):
                return content
            if fallback_content is None:
                fallback_content = content
        return fallback_content

    def _handle_copy_command(self, args: str = "") -> str:
        payload = self._selection_copy_payload(args)
        if payload is None:
            payload = self._last_copyable_message_content()
        if not payload:
            return "Nothing to copy yet."
        handler = self.clipboard_copy_handler
        if not callable(handler):
            return "Clipboard copy is unavailable in this REPL session."
        try:
            copied = bool(handler(payload))
        except Exception as exc:
            return f"Clipboard copy failed: {exc}"
        if copied:
            if payload == self._last_copyable_message_content() and not args.strip():
                return f"Copied last message to clipboard ({len(payload)} chars)."
            return f"Copied transcript selection to clipboard ({len(payload)} chars)."
        return "Clipboard copy is unavailable in this REPL session."

    def _selection_copy_payload(self, args: str) -> str | None:
        normalized = args.strip().lower()
        selected_rows = self._selected_transcript_rows()
        if not normalized:
            if not selected_rows:
                return None
            return "\n\n".join(row.content for row in selected_rows)
        if normalized in {"selection", "selected"}:
            if not selected_rows:
                return ""
            return "\n\n".join(row.content for row in selected_rows)
        if normalized == "last":
            return None
        if normalized == "all":
            rows = self._selectable_transcript_rows()
            if not rows:
                return ""
            return "\n\n".join(row.content for row in rows)
        return None

    def _visible_message_rows(self) -> list[MessageRowData]:
        tool_lookup = _build_tool_use_lookup(self.session.messages)
        rows: list[MessageRowData] = []
        for index, message in enumerate(self.session.messages):
            try:
                row = self._message_to_row(message, tool_lookup=tool_lookup)
            except Exception as exc:
                rows.append(
                    self._render_error_row(
                        error=exc,
                        message_id=f"message-render-error-{index}",
                        stage=f"message:{message.__class__.__name__}",
                    )
                )
                continue
            if row is not None:
                rows.append(row)
        rows.extend(self._console_message_rows())
        rows.extend(self._ephemeral_system_message_rows())
        preview_row = self._stream_preview_row()
        if preview_row is not None:
            rows.append(preview_row)
        return rows

    def append_console_output(self, stream_name: str, text: str) -> None:
        normalized_text = " ".join(str(text).split())
        if not normalized_text:
            return
        if self._console_entries and self._console_entries[-1].stream_name == stream_name:
            merged = f"{self._console_entries[-1].text} {normalized_text}".strip()
            self._console_entries[-1] = _EphemeralConsoleEntry(
                stream_name=stream_name,
                text=merged[-_CONSOLE_CAPTURE_CHAR_LIMIT:],
            )
        else:
            self._console_entries.append(
                _EphemeralConsoleEntry(stream_name=stream_name, text=normalized_text)
            )
        if len(self._console_entries) > _CONSOLE_CAPTURE_ENTRY_LIMIT:
            self._console_entries = self._console_entries[-_CONSOLE_CAPTURE_ENTRY_LIMIT:]

    def _console_message_rows(self) -> list[MessageRowData]:
        rows: list[MessageRowData] = []
        for index, entry in enumerate(self._console_entries):
            label = "stderr" if entry.stream_name == "stderr" else "stdout"
            rows.append(
                MessageRowData(
                    message_id=f"console-{label}-{index}",
                    message_type="system",
                    content=f"System: [{label}] {entry.text}",
                    timestamp=None,
                    theme=self._current_theme(),
                )
            )
        return rows

    def _count_search_matches(
        self,
        message_rows: Sequence[MessageRowData],
        *,
        width: int,
        query: str,
    ) -> int:
        normalized_query = query.strip()
        if not normalized_query:
            return 0
        return sum(
            MessageRowRenderer(
                replace(
                    row,
                    search_query="",
                    search_match_offset=0,
                    active_search_match_index=None,
                )
            ).count_search_matches(width, query=normalized_query)
            for row in message_rows
        )

    def _refresh_search_state(
        self,
        *,
        message_rows: Sequence[MessageRowData] | None = None,
        width: int | None = None,
    ) -> tuple[str, int, int]:
        query = self._search_query.strip()
        if not query:
            self._search_query = ""
            self._search_current_match_index = 0
            self._search_total_matches = 0
            return ("", 0, 0)
        rows = list(message_rows) if message_rows is not None else self._visible_message_rows()
        render_width = max(width or self._last_screen_width, 1)
        total_matches = self._count_search_matches(rows, width=render_width, query=query)
        self._search_total_matches = total_matches
        if total_matches <= 0:
            self._search_current_match_index = 0
        else:
            self._search_current_match_index %= total_matches
        return (query, self._search_current_match_index, self._search_total_matches)

    def _clear_search_state(self) -> None:
        self._search_query = ""
        self._search_current_match_index = 0
        self._search_total_matches = 0

    def _clear_selection_state(self) -> None:
        self._selected_message_ids.clear()
        self._selection_cursor_index = -1
        self._sync_selection_app_state(())

    def _selectable_transcript_rows(self) -> tuple[_SelectableTranscriptRow, ...]:
        tool_lookup = _build_tool_use_lookup(self.session.messages)
        rows: list[_SelectableTranscriptRow] = []
        for message in self.session.messages:
            if isinstance(message, SystemInformationalMessage):
                continue
            try:
                row = self._message_to_row(message, tool_lookup=tool_lookup)
            except Exception:
                continue
            if row is None:
                continue
            content = row.content.strip()
            if not content:
                continue
            rows.append(
                _SelectableTranscriptRow(
                    ordinal=len(rows) + 1,
                    message_id=row.message_id or getattr(message, "uuid", ""),
                    content=content,
                )
            )
        self._reconcile_selection_state(rows)
        return tuple(rows)

    def _reconcile_selection_state(
        self,
        rows: Sequence[_SelectableTranscriptRow],
    ) -> None:
        available_ids = {row.message_id for row in rows if row.message_id}
        self._selected_message_ids.intersection_update(available_ids)
        if not self._selected_message_ids:
            self._selection_cursor_index = -1
            self._sync_selection_app_state(rows)
            return
        if not (0 <= self._selection_cursor_index < len(rows)):
            self._selection_cursor_index = -1
        elif rows[self._selection_cursor_index].message_id not in self._selected_message_ids:
            self._selection_cursor_index = -1
        if self._selection_cursor_index < 0:
            for index, row in enumerate(rows):
                if row.message_id in self._selected_message_ids:
                    self._selection_cursor_index = index
                    break
        self._sync_selection_app_state(rows)

    def _selected_transcript_rows(
        self,
        rows: Sequence[_SelectableTranscriptRow] | None = None,
    ) -> tuple[_SelectableTranscriptRow, ...]:
        if not self._selected_message_ids:
            return ()
        if rows is None:
            rows = self._selectable_transcript_rows()
        return tuple(row for row in rows if row.message_id in self._selected_message_ids)

    def _selection_status_summary(
        self,
        rows: Sequence[_SelectableTranscriptRow] | None = None,
    ) -> str:
        if not self._selected_message_ids:
            return ""
        selected_rows = self._selected_transcript_rows(rows)
        if not selected_rows:
            return ""
        selection_ranges = _format_index_ranges(row.ordinal for row in selected_rows)
        if len(selection_ranges) <= 18:
            return f"sel {selection_ranges}"
        return f"sel {len(selected_rows)}"

    def _selection_detail_message(
        self,
        rows: Sequence[_SelectableTranscriptRow] | None = None,
    ) -> str:
        selected_rows = self._selected_transcript_rows(rows)
        if not selected_rows:
            return "No active selection."
        selection_ranges = _format_index_ranges(row.ordinal for row in selected_rows)
        row_word = "row" if len(selected_rows) == 1 else "rows"
        return f"Selected transcript {row_word} #{selection_ranges}."

    def _sync_selection_app_state(
        self,
        rows: Sequence[_SelectableTranscriptRow] | None = None,
    ) -> None:
        app_state = self._current_app_state()
        if app_state is None:
            return
        setattr(
            app_state,
            "view_selection_mode",
            "transcript" if self._selected_message_ids else "none",
        )
        setattr(app_state, "footer_selection", self._selection_status_summary(rows) or None)

    def _resolve_selection_indices(
        self,
        spec: str,
        *,
        rows: Sequence[_SelectableTranscriptRow],
    ) -> tuple[tuple[int, ...], str | None]:
        normalized = spec.strip().lower()
        if not rows:
            return ((), "Nothing selectable in the transcript yet.")
        if not normalized:
            return (
                (),
                "Usage: /select [index|range|next|prev|all|clear] or /select add <spec>.",
            )
        if normalized == "all":
            return (tuple(range(len(rows))), None)
        if normalized == "last":
            return ((len(rows) - 1,), None)
        if normalized in {"next", "prev"}:
            if 0 <= self._selection_cursor_index < len(rows):
                base_index = self._selection_cursor_index
            else:
                base_index = -1 if normalized == "next" else len(rows)
            step = 1 if normalized == "next" else -1
            return (((base_index + step) % len(rows),), None)

        resolved: set[int] = set()
        for raw_token in normalized.split(","):
            token = raw_token.strip()
            if not token:
                continue
            if "-" in token:
                start_text, _, end_text = token.partition("-")
                if not (start_text.isdigit() and end_text.isdigit()):
                    return ((), f"Invalid selection range: {raw_token.strip()!r}.")
                start_value = int(start_text)
                end_value = int(end_text)
                lower = min(start_value, end_value)
                upper = max(start_value, end_value)
                if lower < 1 or upper > len(rows):
                    return (
                        (),
                        f"Selection range {raw_token.strip()!r} is out of bounds (1-{len(rows)}).",
                    )
                resolved.update(range(lower - 1, upper))
                continue
            if not token.isdigit():
                return ((), f"Invalid selection token: {raw_token.strip()!r}.")
            index_value = int(token)
            if index_value < 1 or index_value > len(rows):
                return ((), f"Selection index {index_value} is out of bounds (1-{len(rows)}).")
            resolved.add(index_value - 1)

        if not resolved:
            return ((), "No transcript rows matched the requested selection.")
        return (tuple(index for index in range(len(rows)) if index in resolved), None)

    def _handle_select_command(self, args: str) -> str:
        normalized = args.strip()
        if not normalized or normalized.lower() == "show":
            return self._selection_detail_message()
        if normalized.lower() == "clear":
            if not self._selected_message_ids:
                return "No active selection."
            self._clear_selection_state()
            return "Selection cleared."

        additive = False
        spec = normalized
        if normalized.lower().startswith("add "):
            additive = True
            spec = normalized[4:].strip()

        rows = self._selectable_transcript_rows()
        resolved_indices, error = self._resolve_selection_indices(spec, rows=rows)
        if error is not None:
            return error

        selected_ids = {rows[index].message_id for index in resolved_indices if rows[index].message_id}
        if additive:
            selected_ids |= self._selected_message_ids
        self._selected_message_ids = selected_ids
        self._selection_cursor_index = resolved_indices[-1] if resolved_indices else -1
        self._sync_selection_app_state(rows)
        return self._selection_detail_message(rows)

    def _handle_search_command(self, args: str) -> str:
        normalized = args.strip()
        if not normalized:
            query, current_match_index, total_matches = self._refresh_search_state()
            if not query:
                return "No active search query."
            if total_matches <= 0:
                return "No matches for the current search query."
            return f"Search match {current_match_index + 1}/{total_matches}."

        lowered = normalized.lower()
        if lowered == "clear":
            if not self._search_query:
                return "No active search query."
            self._clear_search_state()
            return "Search cleared."

        if lowered in {"next", "prev"}:
            query, current_match_index, total_matches = self._refresh_search_state()
            if not query:
                return "No active search query."
            if total_matches <= 0:
                return "No matches for the current search query."
            step = 1 if lowered == "next" else -1
            self._search_current_match_index = (
                current_match_index + step
            ) % total_matches
            return f"Search match {self._search_current_match_index + 1}/{total_matches}."

        self._search_query = normalized
        self._search_current_match_index = 0
        query, current_match_index, total_matches = self._refresh_search_state()
        if total_matches <= 0:
            return "Search set (0 matches)."
        return f"Search set ({current_match_index + 1}/{total_matches} matches)."

    def _load_repl_skill_commands(self) -> tuple[Any, ...]:
        executor = self.tool_executor
        if executor is None:
            return ()
        loader = getattr(executor, "_load_skill_commands", None)
        if not callable(loader):
            return ()
        commands = loader()
        if not isinstance(commands, Sequence):
            return ()
        available: list[Any] = []
        for command in commands:
            name = getattr(command, "name", None)
            if not isinstance(name, str) or not name.strip():
                continue
            if getattr(command, "disable_model_invocation", False):
                continue
            is_enabled = getattr(command, "is_enabled", None)
            if callable(is_enabled):
                try:
                    if not is_enabled():
                        continue
                except Exception:
                    continue
            available.append(command)
        return tuple(available)

    def _find_repl_skill_command(self, name: str) -> Any | None:
        normalized = name.strip().lstrip("/")
        if not normalized:
            return None
        for command in self._load_repl_skill_commands():
            command_name = getattr(command, "name", None)
            if not isinstance(command_name, str):
                continue
            if command_name == normalized:
                return command
            aliases = getattr(command, "aliases", ())
            if (
                isinstance(aliases, Sequence)
                and not isinstance(aliases, (str, bytes, bytearray))
                and normalized in aliases
            ):
                return command
        return None

    def _visible_skill_help_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        for command in sorted(
            self._load_repl_skill_commands(),
            key=lambda item: str(getattr(item, "name", "")).lower(),
        ):
            if not getattr(command, "user_invocable", True) or getattr(command, "is_hidden", False):
                continue
            name = getattr(command, "name", None)
            if not isinstance(name, str) or not name.strip():
                continue
            label = f"/{name}"
            aliases = tuple(
                alias
                for alias in getattr(command, "aliases", ())
                if isinstance(alias, str) and alias.strip()
            )
            if aliases:
                label += " (/" + ", /".join(aliases) + ")"
            argument_hint = getattr(command, "argument_hint", None)
            if isinstance(argument_hint, str) and argument_hint.strip():
                label += f" {argument_hint.strip()}"
            labels.append(label)
        return tuple(labels)

    def _resolve_skill_slash_command(
        self,
        command_name: str,
        args: str,
    ) -> bool | _PromptSlashCommandInvocation | None:
        normalized = command_name.strip().lstrip("/")
        if not normalized:
            return None
        command = self._find_repl_skill_command(normalized)
        if command is None:
            return None
        if not getattr(command, "user_invocable", True):
            self.session = self.session.appendMessage(
                createSystemMessage(
                    f"Command /{normalized} is not user-invocable.",
                    "error",
                )
            )
            return True
        prompt_factory = getattr(command, "get_prompt_for_command", None)
        if not callable(prompt_factory):
            self.session = self.session.appendMessage(
                createSystemMessage(
                    f"Command /{normalized} does not expose prompt content.",
                    "error",
                )
            )
            return True
        try:
            prompt_blocks = prompt_factory(args, {"session_id": self.session_id or ""})
        except Exception as exc:
            self.session = self.session.appendMessage(
                createSystemMessage(f"Failed to expand /{normalized}: {exc}", "error")
            )
            return True
        text_parts: list[str] = []
        if isinstance(prompt_blocks, Sequence):
            for block in prompt_blocks:
                if not isinstance(block, Mapping) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        prompt = "\n".join(text_parts).strip()
        if not prompt:
            self.session = self.session.appendMessage(
                createSystemMessage(
                    f"Command /{normalized} did not produce prompt content.",
                    "error",
                )
            )
            return True
        return _PromptSlashCommandInvocation(prompt=prompt)

    def _build_config_message(self, args: str) -> str:
        normalized_args = args.strip()
        if normalized_args:
            tokens = _split_slash_args(normalized_args)
            if not tokens:
                return self._build_config_message("")
            action = tokens[0].lower()
            if action in {"show", "list", "status"}:
                return self._build_config_message("")
            if action == "get" and len(tokens) == 2:
                merged_settings = get_initial_settings(
                    config_home=self.config_home,
                    project_root=self.cwd,
                )
                app_state = self._current_app_state()
                settings = getattr(app_state, "settings", None)
                if isinstance(settings, Mapping):
                    merged_settings = dict(merged_settings)
                    merged_settings.update(settings)
                value = self._setting_value(merged_settings, tokens[1])
                if value is None:
                    return f"Setting `{tokens[1]}` is not set."
                return f"{tokens[1]} = `{json.dumps(value, sort_keys=True)}`"
            if action in {"set", "update"} and len(tokens) >= 3:
                key = tokens[1]
                value = self._coerce_setting_value(" ".join(tokens[2:]))
                error = self._save_local_setting(key, value)
                if error:
                    return f"Failed to save setting `{key}`: {error}"
                return f"Saved local setting `{key}` = `{json.dumps(value, sort_keys=True)}`."
            if action in {"unset", "remove", "reset", "clear"} and len(tokens) == 2:
                key = tokens[1]
                error = self._save_local_setting(key, None)
                if error:
                    return f"Failed to clear setting `{key}`: {error}"
                return f"Cleared local setting `{key}`."
            return "Usage: /config [show|get <key>|set <key> <value>|unset <key>]"

        merged_settings = get_initial_settings(
            config_home=self.config_home,
            project_root=self.cwd,
        )
        app_state = self._current_app_state()
        settings = getattr(app_state, "settings", None)
        normalized_settings = settings if isinstance(settings, Mapping) else merged_settings
        effort_value = getattr(app_state, "effort_value", None)
        if not is_valid_effort_level(effort_value):
            candidate_effort = normalized_settings.get("effortValue")
            effort_value = candidate_effort if is_valid_effort_level(candidate_effort) else None
        lines = [
            "### Config",
            f"- Config Home: {_inline_code(self.config_home or get_claude_config_home())}",
            f"- Working Directory: {_inline_code(self.cwd)}",
            f"- Selected Model: {_inline_code(self._selected_model(settings=normalized_settings))}",
            f"- Permission Mode: {_inline_code(self.permission_mode)}",
            f"- Persist Sessions: {_inline_code(str(self.persist_sessions).lower())}",
            f"- Thinking Enabled: {_inline_code(str(default_thinking_enabled(settings=normalized_settings)).lower())}",
        ]
        if isinstance(effort_value, str):
            lines.append(f"- Effort: {_inline_code(effort_value)}")
        if isinstance(normalized_settings.get("theme"), str):
            lines.append(f"- Theme: {_inline_code(normalized_settings['theme'])}")

        lines.append("")
        lines.append("### Setting Sources")
        for source in ("userSettings", "projectSettings", "localSettings", "policySettings"):
            path = get_setting_file_path(
                source,
                config_home=self.config_home,
                project_root=self.cwd,
            )
            source_settings = get_settings_for_source(
                source,
                config_home=self.config_home,
                project_root=self.cwd,
            )
            if path is None:
                status = "n/a"
            elif isinstance(source_settings, Mapping) and source_settings:
                status = "present"
            elif os.path.exists(path):
                status = "empty"
            else:
                status = "missing"
            lines.append(f"- {source}: {_inline_code(status)} {_inline_code(path or '(none)')}")
            if isinstance(source_settings, Mapping) and source_settings:
                keys = sorted(str(key) for key in source_settings.keys())
                preview = ", ".join(keys[:6])
                if len(keys) > 6:
                    preview += f", +{len(keys) - 6} keys"
                lines.append(f"  Keys: {preview}")
        return "\n".join(lines)

    def _build_cost_message(self) -> str:
        usage = self._session_usage_totals()
        total_tokens = (
            usage["input_tokens"]
            + usage["output_tokens"]
            + usage["cache_creation_input_tokens"]
            + usage["cache_read_input_tokens"]
        )
        lines = [
            "### Session Cost",
            f"- Assistant Turns: {usage['assistant_messages']}",
            f"- Input Tokens: {usage['input_tokens']}",
            f"- Output Tokens: {usage['output_tokens']}",
            f"- Cache Creation Tokens: {usage['cache_creation_input_tokens']}",
            f"- Cache Read Tokens: {usage['cache_read_input_tokens']}",
            f"- Server Web Search Requests: {usage['web_search_requests']}",
            f"- Server Web Fetch Requests: {usage['web_fetch_requests']}",
            f"- Total Recorded Tokens: {total_tokens}",
            "- USD Estimate: unavailable (no pricing table is configured in the Python port)",
        ]
        return "\n".join(lines)

    def _run_git_command(self, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", self.cwd, *args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _handle_diff_command(self, args: str) -> str:
        normalized = args.strip().lower()
        try:
            inside = self._run_git_command("rev-parse", "--is-inside-work-tree")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Unable to inspect git diff: {exc}"
        if inside.returncode != 0:
            return "Current working directory is not inside a git repository."

        show_full = normalized in {"full", "--full", "patch", "--patch"}
        try:
            stat = self._run_git_command("diff", "--stat")
            name_status = self._run_git_command("diff", "--name-status")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Unable to inspect git diff: {exc}"

        stat_text = (stat.stdout or stat.stderr or "").strip()
        name_status_text = (name_status.stdout or name_status.stderr or "").strip()
        if not stat_text and not name_status_text:
            return "No unstaged git diff in the current repository."

        lines = ["### Git Diff"]
        if name_status_text:
            lines.append("- Changed Files:")
            for row in name_status_text.splitlines()[:80]:
                lines.append(f"  {row}")
            if len(name_status_text.splitlines()) > 80:
                lines.append("  ...")
        if stat_text:
            lines.append("")
            lines.append("```text")
            lines.append(stat_text)
            lines.append("```")

        if show_full:
            try:
                full = self._run_git_command("diff", "--", timeout=8.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                lines.append(f"\nFull diff unavailable: {exc}")
            else:
                full_text = (full.stdout or full.stderr or "").strip()
                if full_text:
                    if len(full_text) > 12_000:
                        full_text = full_text[:12_000].rstrip() + "\n...[truncated]"
                    lines.append("")
                    lines.append("```diff")
                    lines.append(full_text)
                    lines.append("```")
        return "\n".join(lines)

    def _build_hooks_message(self) -> str:
        sources = self._runtime_hook_sources()
        if not sources:
            return "No runtime hooks are configured."
        event_counts: dict[str, int] = {}
        for source in sources:
            for event_name, hooks in source.items():
                count = (
                    len(hooks)
                    if isinstance(hooks, Sequence)
                    and not isinstance(hooks, (str, bytes, bytearray))
                    else 1
                )
                event_counts[str(event_name)] = event_counts.get(str(event_name), 0) + count
        lines = ["### Hooks", f"- Sources: {len(sources)}"]
        for event_name in sorted(event_counts):
            lines.append(f"- {event_name}: {event_counts[event_name]}")
        return "\n".join(lines)

    def _context_rule_buckets(self, context: Mapping[str, Any], behavior: str) -> dict[str, list[str]]:
        key = f"always_{behavior}_rules"
        camel_key = "always" + behavior[:1].upper() + behavior[1:] + "Rules"
        raw = context.get(key)
        if not isinstance(raw, Mapping):
            raw = context.get(camel_key)
        buckets: dict[str, list[str]] = {}
        if isinstance(raw, Mapping):
            for source, values in raw.items():
                if not isinstance(source, str):
                    continue
                if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
                    continue
                buckets[source] = [
                    value for value in values if isinstance(value, str) and value.strip()
                ]
        return buckets

    def _store_context_rule_buckets(
        self,
        context: dict[str, Any],
        behavior: str,
        buckets: Mapping[str, Sequence[str]],
    ) -> None:
        normalized = {
            source: [value for value in values if isinstance(value, str) and value.strip()]
            for source, values in buckets.items()
            if isinstance(source, str)
        }
        key = f"always_{behavior}_rules"
        camel_key = "always" + behavior[:1].upper() + behavior[1:] + "Rules"
        context[key] = normalized
        context[camel_key] = {source: list(values) for source, values in normalized.items()}

    def _set_permission_mode(self, target_mode: str) -> str:
        valid_modes = {
            "acceptEdits",
            "auto",
            "bypassPermissions",
            "default",
            "dontAsk",
            "plan",
            "bubble",
        }
        if target_mode not in valid_modes:
            return (
                "Unknown permission mode. Expected one of: "
                + ", ".join(_inline_code(mode) for mode in sorted(valid_modes))
            )
        self.permission_mode = target_mode
        self._apply_permission_mode_to_executor()
        return f"Permission mode set to {_inline_code(target_mode)}."

    def _build_permissions_message(self) -> str:
        app_state = self._current_app_state()
        context = getattr(app_state, "tool_permission_context", None)
        normalized_context = context if isinstance(context, Mapping) else {}
        lines = [
            "### Permissions",
            f"- Mode: {_inline_code(self.permission_mode)}",
        ]
        for behavior in ("allow", "deny", "ask"):
            buckets = self._context_rule_buckets(normalized_context, behavior)
            rules = [
                f"{source}:{rule}"
                for source in sorted(buckets)
                for rule in buckets[source]
            ]
            preview = ", ".join(_inline_code(rule) for rule in rules[:12])
            if len(rules) > 12:
                preview += f", +{len(rules) - 12} more"
            lines.append(f"- {behavior.title()} Rules: {preview or 'none'}")
        stripped = self._context_rule_buckets(
            {"always_allow_rules": normalized_context.get("stripped_dangerous_rules", {})},
            "allow",
        )
        stripped_count = sum(len(values) for values in stripped.values())
        if stripped_count:
            lines.append(f"- Stripped Dangerous Rules: {stripped_count}")
        return "\n".join(lines)

    def _handle_permissions_command(self, args: str) -> str:
        normalized = args.strip()
        if not normalized or normalized in {"show", "list"}:
            return self._build_permissions_message()

        parts = normalized.split(maxsplit=1)
        action = parts[0]
        remainder = parts[1].strip() if len(parts) > 1 else ""
        mode_aliases = {
            "accept-edits": "acceptEdits",
            "acceptEdits": "acceptEdits",
            "auto": "auto",
            "bypass": "bypassPermissions",
            "bypass-permissions": "bypassPermissions",
            "bypassPermissions": "bypassPermissions",
            "default": "default",
            "dont-ask": "dontAsk",
            "dontAsk": "dontAsk",
            "plan": "plan",
            "bubble": "bubble",
        }
        if action == "mode" and remainder:
            action = remainder
        if action in mode_aliases:
            return self._set_permission_mode(mode_aliases[action])

        app_state = self._current_app_state()
        if app_state is None:
            return "No app state is attached; permissions cannot be updated."
        raw_context = getattr(app_state, "tool_permission_context", None)
        context = dict(raw_context) if isinstance(raw_context, Mapping) else {}

        if action == "clear":
            for behavior in ("allow", "deny", "ask"):
                buckets = self._context_rule_buckets(context, behavior)
                if "session" in buckets:
                    buckets["session"] = []
                self._store_context_rule_buckets(context, behavior, buckets)
            app_state.tool_permission_context = context
            self._apply_permission_mode_to_executor()
            return "Session permission rules cleared."

        if action not in {"allow", "deny", "ask"} or not remainder:
            return (
                "Usage: /permissions [show|mode <mode>|allow <rule>|deny <rule>|ask <rule>|clear]"
            )

        parsed = permission_rule_value_from_string(remainder)
        if not isinstance(parsed.get("tool_name"), str) or not parsed["tool_name"].strip():
            return f"Invalid permission rule: {_inline_code(remainder)}"
        buckets = self._context_rule_buckets(context, action)
        session_rules = buckets.setdefault("session", [])
        if remainder not in session_rules:
            session_rules.append(remainder)
        self._store_context_rule_buckets(context, action, buckets)
        app_state.tool_permission_context = context
        self._apply_permission_mode_to_executor()
        return f"Added session {action} rule: {_inline_code(remainder)}"

    def _handle_plan_command(self, args: str) -> str:
        normalized = args.strip().lower()
        app_state = self._current_app_state()
        raw_context = getattr(app_state, "tool_permission_context", None) if app_state is not None else {}
        context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
        if normalized in {"off", "exit", "disable", "default"}:
            target = context.get("pre_plan_mode")
            if not isinstance(target, str) or not target.strip() or target == "plan":
                target = "default"
            context["pre_plan_mode"] = None
            if app_state is not None:
                app_state.tool_permission_context = context
            return self._set_permission_mode(target)

        if normalized not in {"", "on", "enable"}:
            return "Usage: /plan [on|off]"
        if self.permission_mode != "plan":
            context["pre_plan_mode"] = self.permission_mode
            if app_state is not None:
                app_state.tool_permission_context = context
        return self._set_permission_mode("plan")

    def _handle_session_command(self, args: str) -> str:
        normalized = args.strip()
        lowered = normalized.lower()
        if not normalized or lowered in {"show", "status"}:
            return self._build_status_message()
        if lowered == "memory":
            return self._build_memory_message()
        if lowered == "id":
            return f"Session ID: {_inline_code(self.session_id or 'unknown')}"
        if lowered.startswith("resume"):
            _, _, resume_args = normalized.partition(" ")
            return self._handle_resume_command(resume_args)
        return "Usage: /session [show|id|memory|resume <latest|session-id>]"

    def _build_skills_message(self) -> str:
        labels = self._visible_skill_help_labels()
        if not labels:
            return "No user-invocable skills are available in this runtime."
        lines = ["### Skills"]
        lines.extend(f"- {label}" for label in labels)
        return "\n".join(lines)

    def _build_stats_message(self) -> str:
        counts: dict[str, int] = {}
        for message in self.session.messages:
            label = _message_type_label(message)
            counts[label] = counts.get(label, 0) + 1
        usage = self._session_usage_totals()
        lines = [
            "### Session Stats",
            f"- Messages: {len(self.session.messages)}",
            f"- User Messages: {counts.get('user', 0)}",
            f"- Assistant Messages: {counts.get('assistant', 0)}",
            f"- System Messages: {counts.get('system', 0)}",
            f"- Attachments: {counts.get('attachment', 0)}",
            f"- Recorded Input Tokens: {usage['input_tokens']}",
            f"- Recorded Output Tokens: {usage['output_tokens']}",
        ]
        return "\n".join(lines)

    def _build_usage_message(self) -> str:
        usage = self._session_usage_totals()
        total_tokens = (
            usage["input_tokens"]
            + usage["output_tokens"]
            + usage["cache_creation_input_tokens"]
            + usage["cache_read_input_tokens"]
        )
        return "\n".join(
            [
                "### Usage",
                f"- Assistant Turns: {usage['assistant_messages']}",
                f"- Input Tokens: {usage['input_tokens']}",
                f"- Output Tokens: {usage['output_tokens']}",
                f"- Cache Creation Tokens: {usage['cache_creation_input_tokens']}",
                f"- Cache Read Tokens: {usage['cache_read_input_tokens']}",
                f"- Total Tokens: {total_tokens}",
                f"- Web Search Requests: {usage['web_search_requests']}",
                f"- Web Fetch Requests: {usage['web_fetch_requests']}",
            ]
        )

    def _handle_effort_command(self, args: str) -> str:
        normalized = args.strip()
        app_state = self._current_app_state()
        current = getattr(app_state, "effort_value", None)
        if not normalized:
            if is_valid_effort_level(current):
                return f"Current effort: {_inline_code(str(current))}"
            return "No session effort override is set."
        lowered = normalized.lower()
        if lowered in {"reset", "default", "clear", "off"}:
            if app_state is not None:
                app_state.effort_value = None
            return "Session effort override cleared."
        candidate = lowered
        if candidate == "max":
            candidate = "max"
        if not is_valid_effort_level(candidate):
            return "Invalid effort. Expected one of: `low`, `medium`, `high`, `max`."
        if app_state is not None:
            app_state.effort_value = candidate
        return f"Session effort set to {_inline_code(candidate)}."

    def _message_file_references(self) -> tuple[str, ...]:
        paths: list[str] = []

        def add_path(value: object) -> None:
            if not isinstance(value, str):
                return
            normalized = value.strip()
            if not normalized or normalized in paths:
                return
            paths.append(normalized)

        memory = self.session.externalMetadata.session_memory
        if memory is not None:
            for file_path in memory.files:
                add_path(file_path)

        at_mention_pattern = re.compile(r"(?<!\S)@([^\s`'\"<>]+)")
        for message in self.session.messages:
            text = _flatten_message_content(message)
            for match in at_mention_pattern.finditer(text):
                add_path(match.group(1))
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.message.content:
                if not isinstance(block, ToolUseBlock) or not isinstance(block.input, Mapping):
                    continue
                for key in (
                    "file_path",
                    "filePath",
                    "path",
                    "notebook_path",
                    "notebookPath",
                    "output_file",
                    "outputFile",
                ):
                    add_path(block.input.get(key))
        return tuple(paths)

    def _build_files_message(self) -> str:
        paths = self._message_file_references()
        if not paths:
            return "No file references have been captured in this session."
        lines = ["### Session Files"]
        for path in paths[:100]:
            lines.append(f"- {_inline_code(path)}")
        if len(paths) > 100:
            lines.append(f"- ... +{len(paths) - 100} more")
        return "\n".join(lines)

    def _build_feedback_message(self) -> str:
        return (
            "Feedback capture is available through the conversation: "
            "send the feedback text as your next message and include relevant logs or session IDs."
        )

    def _settings_update_payload(self, key_path: str, value: Any) -> dict[str, Any]:
        parts = [part for part in key_path.replace("/", ".").split(".") if part]
        if not parts:
            raise ValueError("Setting key must be non-empty")
        payload: Any = value
        for part in reversed(parts):
            payload = {part: payload}
        return payload

    def _setting_value(self, settings: Mapping[str, Any], key_path: str) -> Any:
        current: Any = settings
        for part in [part for part in key_path.replace("/", ".").split(".") if part]:
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current

    def _coerce_setting_value(self, raw_value: str) -> Any:
        value = raw_value.strip()
        lowered = value.lower()
        if lowered in {"true", "yes", "on"}:
            return True
        if lowered in {"false", "no", "off"}:
            return False
        if lowered in {"null", "none"}:
            return None
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value

    def _apply_setting_to_app_state(self, key_path: str, value: Any) -> None:
        app_state = self._current_app_state()
        if app_state is None:
            return
        current_settings = getattr(app_state, "settings", None)
        settings = dict(current_settings) if isinstance(current_settings, Mapping) else {}
        parts = [part for part in key_path.replace("/", ".").split(".") if part]
        if not parts:
            return
        target = settings
        for part in parts[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                nested = {}
            target[part] = nested
            target = nested
        if value is None:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = value
        app_state.settings = settings

        root_key = parts[0]
        if root_key == "model":
            app_state.main_loop_model = value if isinstance(value, str) else None
            app_state.main_loop_model_for_session = value if isinstance(value, str) else None
            self._sync_selected_model_to_adapter(value if isinstance(value, str) else None)
        elif root_key == "fastMode":
            app_state.fast_mode = bool(value)
        elif root_key == "effortValue":
            app_state.effort_value = value if is_valid_effort_level(value) else None
        elif root_key == "statusLine":
            app_state.status_line_text = value if isinstance(value, str) and value.strip() else None

    def _save_local_setting(self, key_path: str, value: Any) -> str | None:
        try:
            payload = self._settings_update_payload(key_path, value)
            update_settings_for_source(
                "localSettings",
                payload,
                config_home=self.config_home,
                project_root=self.cwd,
            )
        except ValueError as exc:
            return str(exc)
        self._apply_setting_to_app_state(key_path, value)
        app_state = self._current_app_state()
        if app_state is not None:
            try:
                sync_app_state_settings_from_disk(app_state, project_root=self.cwd)
            except Exception:
                pass
        return None

    def _handle_add_dir_command(self, args: str) -> str:
        tokens = _split_slash_args(args)
        remember = False
        paths: list[str] = []
        for token in tokens:
            if token in {"--remember", "--persist"}:
                remember = True
                continue
            paths.append(token)
        if not paths:
            return "Usage: /add-dir <directory> [--remember]"

        app_state = self._current_app_state()
        if app_state is None:
            return "No app state is attached; additional directories cannot be updated."
        context = dict(getattr(app_state, "tool_permission_context", {}) or {})
        directories = dict(context.get("additional_working_directories") or {})
        added: list[str] = []
        for raw_path in paths:
            resolved = os.path.abspath(os.path.join(self.cwd, raw_path))
            if not os.path.isdir(resolved):
                return f"Directory not found: {_inline_code(resolved)}"
            if any(
                isinstance(item, Mapping)
                and os.path.abspath(str(item.get("path", ""))) == resolved
                for item in directories.values()
            ):
                continue
            base_key = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(resolved) or "directory")
            key = base_key
            index = 2
            while key in directories:
                key = f"{base_key}-{index}"
                index += 1
            directories[key] = {
                "path": resolved,
                "source": "settings" if remember else "session",
            }
            added.append(resolved)
        context["additional_working_directories"] = directories
        context.pop("additionalWorkingDirectories", None)
        app_state.tool_permission_context = context
        self._apply_permission_mode_to_executor()
        if not added:
            return "All requested directories were already available to tools."
        source = "persistent" if remember else "session"
        return "Added {} tool director{} ({})".format(
            len(added),
            "y" if len(added) == 1 else "ies",
            source,
        ) + ":\n" + "\n".join(f"- {_inline_code(path)}" for path in added)

    def _build_advisor_prompt(self, args: str) -> str:
        topic = args.strip() or "the current task and repository state"
        return (
            f"Act as an advisor for {topic}. Identify the main engineering risks, "
            "compare viable approaches, and recommend the next concrete step. "
            "Keep the response concise and evidence-based."
        )

    def _handle_agents_command(self, args: str) -> str:
        tokens = _split_slash_args(args)
        command = tokens[0].lower() if tokens else "list"
        if command in {"list", "ls", "show"}:
            return format_agents_listing(
                build_agent_registry(
                    config_home=self.config_home,
                    inline_agents=self.inline_agents,
                )
            ).rstrip()
        if command == "get" and len(tokens) >= 2:
            agent = get_custom_agent(tokens[1], config_home=self.config_home)
            if agent is None:
                return f"Agent not found: {_inline_code(tokens[1])}"
            return json.dumps(agent.to_public_payload(), indent=2, sort_keys=True)
        if command == "set" and len(tokens) >= 2:
            name = tokens[1]
            description = None
            prompt = None
            model = None
            color = None
            agent_type = None
            tools: tuple[str, ...] | None = None
            index = 2
            while index < len(tokens):
                token = tokens[index]
                if token in {"--description", "--prompt", "--model", "--color", "--type", "--tools"}:
                    if index + 1 >= len(tokens):
                        return f"Missing value for {token}."
                    value = tokens[index + 1]
                    if token == "--description":
                        description = value
                    elif token == "--prompt":
                        prompt = value
                    elif token == "--model":
                        model = value
                    elif token == "--color":
                        color = value
                    elif token == "--type":
                        agent_type = value
                    else:
                        tools = tuple(item.strip() for item in value.split(",") if item.strip())
                    index += 2
                    continue
                return f"Unknown agents option: {_inline_code(token)}"
            if all(value is None for value in (description, prompt, model, color, agent_type)) and tools is None:
                return (
                    "Usage: /agents set <name> "
                    "[--description <text>] [--prompt <text>] [--model <model>] "
                    "[--color <color>] [--type <type>] [--tools a,b]"
                )
            agent = upsert_custom_agent(
                name,
                config_home=self.config_home,
                description=description,
                prompt=prompt,
                model=model,
                color=color,
                tools=tools,
                agent_type=agent_type,
            )
            return f"Saved agent {_inline_code(agent.name)}."
        if command in {"delete", "remove"} and len(tokens) == 2:
            removed = delete_custom_agent(tokens[1], config_home=self.config_home)
            if not removed:
                return f"Agent not found: {_inline_code(tokens[1])}"
            return f"Deleted agent {_inline_code(tokens[1])}."
        return "Usage: /agents [list|get <name>|set <name> ...|delete <name>]"

    def _handle_branch_command(self, args: str) -> str:
        if not self.persist_sessions:
            return "Session branching requires session persistence for this runtime."
        branch_id = generate_session_id()
        name = args.strip() or f"{self.display_name or 'session'} branch"
        try:
            save_session_snapshot(
                self.session,
                session_id=branch_id,
                cwd=self.cwd,
                config_home=self.config_home,
                display_name=name,
            )
        except Exception as exc:
            return f"Failed to create session branch: {exc}"
        return (
            f"Created session branch {_inline_code(branch_id)} "
            f"with {len(self.session.messages)} messages."
        )

    def _handle_btw_command(self, args: str) -> str:
        note = args.strip()
        if not note:
            return "Usage: /btw <note>"
        notes = getattr(self, "_btw_notes", None)
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        setattr(self, "_btw_notes", notes[-20:])
        return f"Captured note: {note}"

    def _handle_color_command(self, args: str) -> str:
        app_state = self._current_app_state()
        current = getattr(app_state, "agent_color", None) if app_state is not None else None
        normalized = args.strip().lower()
        if not normalized:
            return f"Current agent color: {_inline_code(str(current or 'default'))}"
        if normalized in {"reset", "default", "clear"}:
            if app_state is not None:
                setattr(app_state, "agent_color", None)
            error = self._save_local_setting("agentColor", None)
            return f"Failed to clear color: {error}" if error else "Agent color reset."
        valid = {
            "red",
            "orange",
            "yellow",
            "green",
            "cyan",
            "blue",
            "purple",
            "pink",
            "gray",
        }
        if normalized not in valid:
            return "Invalid color. Expected one of: " + ", ".join(_inline_code(value) for value in sorted(valid))
        if app_state is not None:
            setattr(app_state, "agent_color", normalized)
        error = self._save_local_setting("agentColor", normalized)
        if error:
            return f"Failed to save color: {error}"
        return f"Agent color set to {_inline_code(normalized)}."

    def _build_context_message(self, args: str) -> str:
        text_chars = sum(len(_flatten_message_content(message)) for message in self.session.messages)
        estimated_tokens = max(text_chars // 4, len(self.session.messages))
        usage = self._session_usage_totals()
        recorded_tokens = (
            usage["input_tokens"]
            + usage["output_tokens"]
            + usage["cache_creation_input_tokens"]
            + usage["cache_read_input_tokens"]
        )
        if recorded_tokens:
            estimated_tokens = recorded_tokens
        config = self._effective_stream_config()
        max_tokens = max(config.max_context_tokens, 1)
        percent = min(100.0, estimated_tokens * 100.0 / max_tokens)
        memory = self.session.externalMetadata.session_memory
        lines = [
            "### Context",
            f"- Messages: {len(self.session.messages)}",
            f"- Text Chars: {text_chars}",
            f"- Estimated Tokens: {estimated_tokens}/{max_tokens} ({percent:.1f}%)",
            f"- Compact Boundaries: {sum(1 for message in self.session.messages if isinstance(message, CompactBoundaryMessage))}",
            f"- Session Memory: {'present' if memory is not None else 'absent'}",
        ]
        if args.strip().lower() in {"files", "file"}:
            files = self._message_file_references()
            lines.append("- Files: " + (", ".join(_inline_code(path) for path in files[:20]) if files else "none"))
        return "\n".join(lines)

    def _render_session_export_text(self) -> str:
        lines = [
            "# Session Export",
            f"- Session ID: {self.session_id or 'unknown'}",
            f"- Working Directory: {self.cwd}",
            f"- Exported At: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
            "",
        ]
        for index, message in enumerate(self.session.messages, start=1):
            label = _message_type_label(message)
            content = _flatten_message_content(message).strip()
            if not content:
                continue
            lines.append(f"## {index}. {label}")
            lines.append(content)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _handle_export_command(self, args: str) -> str:
        target = args.strip()
        if not target:
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            target = f"claude-session-{self.session_id or timestamp}.md"
        output_path = os.path.abspath(os.path.join(self.cwd, target))
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(output_path, "w", encoding="utf-8") as handle:
                handle.write(self._render_session_export_text())
        except OSError as exc:
            return f"Failed to export session: {exc}"
        return f"Exported session to {_inline_code(output_path)}."

    def _handle_fast_command(self, args: str) -> str:
        app_state = self._current_app_state()
        current = bool(getattr(app_state, "fast_mode", False)) if app_state is not None else False
        normalized = args.strip().lower()
        if normalized in {"", "toggle"}:
            target = not current
        elif normalized in {"on", "true", "enable", "enabled"}:
            target = True
        elif normalized in {"off", "false", "disable", "disabled"}:
            target = False
        elif normalized in {"show", "status"}:
            return f"Fast mode is {_inline_code('on' if current else 'off')}."
        else:
            return "Usage: /fast [on|off|toggle|status]"
        if app_state is not None:
            app_state.fast_mode = target
        error = self._save_local_setting("fastMode", target)
        if error:
            return f"Failed to save fast mode: {error}"
        return f"Fast mode {'enabled' if target else 'disabled'}."

    def _handle_mcp_command(self, args: str) -> str:
        tokens = _split_slash_args(args)
        command = tokens[0].lower() if tokens else "list"
        settings = getattr(self._current_app_state(), "settings", None)
        normalized_settings = settings if isinstance(settings, Mapping) else None
        try:
            if command in {"list", "ls", "show"}:
                return format_project_mcp_listing(
                    list_project_mcp_servers(cwd=self.cwd, settings=normalized_settings)
                )
            if command == "get" and len(tokens) == 2:
                server = get_project_mcp_server_details(
                    tokens[1],
                    cwd=self.cwd,
                    settings=normalized_settings,
                )
                if server is None:
                    return f"MCP server not found: {_inline_code(tokens[1])}"
                return json.dumps({"server": server}, indent=2, sort_keys=True)
            if command == "add-json" and len(tokens) >= 3:
                payload = self._coerce_setting_value(" ".join(tokens[2:]))
                if not isinstance(payload, Mapping):
                    return "Usage: /mcp add-json <name> <json-object>"
                server = upsert_project_mcp_server(
                    tokens[1],
                    payload,
                    cwd=self.cwd,
                )
                return f"Saved MCP server {_inline_code(str(server.get('name', tokens[1])))}."
            if command in {"remove", "delete"} and len(tokens) == 2:
                removed = remove_project_mcp_server(tokens[1], cwd=self.cwd)
                if removed is None:
                    return f"MCP server not found: {_inline_code(tokens[1])}"
                return f"Removed MCP server {_inline_code(tokens[1])}."
            if command == "reset-project-choices":
                reset_project_mcp_choices(cwd=self.cwd)
                return "Reset project MCP allow/deny choices."
            if command == "serve":
                return "Use the CLI command `mcp serve` to start the stdio MCP server."
        except Exception as exc:
            return f"MCP command failed: {exc}"
        return "Usage: /mcp [list|get <name>|add-json <name> <json>|remove <name>|reset-project-choices|serve]"

    def _handle_output_style_command(self, args: str) -> str:
        app_state = self._current_app_state()
        settings = getattr(app_state, "settings", None) if app_state is not None else {}
        current = settings.get("outputStyle") if isinstance(settings, Mapping) else None
        normalized = args.strip()
        if not normalized:
            return f"Current output style: {_inline_code(str(current or 'default'))}"
        lowered = normalized.lower()
        if lowered in {"reset", "default", "clear"}:
            error = self._save_local_setting("outputStyle", None)
            return f"Failed to clear output style: {error}" if error else "Output style reset."
        error = self._save_local_setting("outputStyle", normalized)
        if error:
            return f"Failed to save output style: {error}"
        return f"Output style set to {_inline_code(normalized)}."

    def _build_passes_message(self) -> str:
        app_state = self._current_app_state()
        passes = getattr(app_state, "passes", None) if app_state is not None else None
        if not isinstance(passes, Sequence) or isinstance(passes, (str, bytes, bytearray)):
            return "No runtime passes are active."
        return "### Passes\n" + "\n".join(f"- {item}" for item in passes)

    def _handle_plugin_command(self, args: str) -> str:
        tokens = _split_slash_args(args)
        command = tokens[0].lower() if tokens else "list"
        try:
            if command in {"list", "ls", "show"}:
                settings = getattr(self._current_app_state(), "settings", None)
                normalized_settings = settings if isinstance(settings, Mapping) else None
                return format_plugin_listing(
                    list_installed_plugins(
                        settings=normalized_settings,
                        config_home=self.config_home,
                    )
                ).rstrip()
            if command == "validate" and len(tokens) == 2:
                plugin = validate_plugin_directory(tokens[1])
                return json.dumps(
                    {
                        "valid": True,
                        "plugin": {
                            "name": plugin.name,
                            "path": os.path.abspath(plugin.path),
                            "description": plugin.manifest.get("description", ""),
                            "version": plugin.manifest.get("version"),
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            if command == "install" and len(tokens) == 2:
                record = install_plugin_from_directory(tokens[1], config_home=self.config_home)
                sync_managed_plugins_runtime_state(
                    self._current_app_state(),
                    force=True,
                    config_home=self.config_home,
                )
                return f"Installed plugin {_inline_code(record.name)}."
            if command == "uninstall" and len(tokens) == 2:
                record = uninstall_installed_plugin(tokens[1], config_home=self.config_home)
                sync_managed_plugins_runtime_state(
                    self._current_app_state(),
                    force=True,
                    config_home=self.config_home,
                )
                return f"Uninstalled plugin {_inline_code(record.name)}."
            if command in {"enable", "disable"} and len(tokens) == 2:
                record = set_installed_plugin_enabled(
                    tokens[1],
                    command == "enable",
                    config_home=self.config_home,
                )
                sync_managed_plugins_runtime_state(
                    self._current_app_state(),
                    force=True,
                    config_home=self.config_home,
                )
                return f"{'Enabled' if command == 'enable' else 'Disabled'} plugin {_inline_code(record.name)}."
            if command == "update" and len(tokens) == 2:
                record = update_installed_plugin(tokens[1], config_home=self.config_home)
                sync_managed_plugins_runtime_state(
                    self._current_app_state(),
                    force=True,
                    config_home=self.config_home,
                )
                return f"Updated plugin {_inline_code(record.name)}."
        except Exception as exc:
            return f"Plugin command failed: {exc}"
        return "Usage: /plugin [list|validate <path>|install <path>|uninstall <name>|enable <name>|disable <name>|update <name>]"

    def _build_release_notes_message(self) -> str:
        return (
            f"Release notes for this Python port are tracked in repository docs. "
            f"Runtime version: {_inline_code(self.version)}."
        )

    def _handle_reload_plugins_command(self) -> str:
        app_state = self._current_app_state()
        if app_state is None:
            return "No app state is attached; plugins cannot be reloaded."
        changed = sync_managed_plugins_runtime_state(
            app_state,
            force=True,
            config_home=self.config_home,
        )
        status = "reloaded" if changed else "already current"
        return f"Managed plugins {status}.\n" + self._handle_plugin_command("list")

    def _handle_rename_command(self, args: str) -> str:
        name = args.strip()
        if not name:
            first_user = next(
                (
                    _flatten_message_content(message).strip()
                    for message in self.session.messages
                    if isinstance(message, UserMessage)
                ),
                "",
            )
            name = first_user[:60].strip() or f"Session {self.session_id or 'current'}"
        self.display_name = name
        self._persist_session()
        return f"Session renamed to {_inline_code(name)}."

    def _handle_rewind_command(self, args: str) -> str:
        if not self.session.messages:
            return "No conversation messages to rewind."
        normalized = args.strip().lower()
        visible_indices = [
            index
            for index, message in enumerate(self.session.messages)
            if not isinstance(message, SystemInformationalMessage)
        ]
        if not visible_indices:
            return "No conversation messages to rewind."
        if normalized.isdigit():
            keep_count = max(0, min(int(normalized), len(self.session.messages)))
        else:
            keep_count = visible_indices[-1]
        removed = len(self.session.messages) - keep_count
        self.session = QuerySession(
            sessionState=self.session.sessionState,
            externalMetadata=self.session.externalMetadata,
            messageState=self.session.messageState,
            messages=self.session.messages[:keep_count],
            lastTerminal=self.session.lastTerminal,
        )._refresh_session_memory()
        self._clear_search_state()
        self._clear_selection_state()
        self._clear_stream_preview()
        self._persist_session()
        return f"Rewound conversation; removed {removed} message{'s' if removed != 1 else ''}."

    def _handle_sandbox_toggle_command(self, args: str) -> str:
        app_state = self._current_app_state()
        settings = getattr(app_state, "settings", None) if app_state is not None else {}
        sandbox = settings.get("sandbox") if isinstance(settings, Mapping) else None
        current = bool(sandbox.get("enabled")) if isinstance(sandbox, Mapping) else False
        normalized = args.strip().lower()
        if normalized in {"", "toggle"}:
            target = not current
        elif normalized in {"on", "true", "enable", "enabled"}:
            target = True
        elif normalized in {"off", "false", "disable", "disabled"}:
            target = False
        elif normalized in {"show", "status"}:
            return f"Sandbox is {_inline_code('on' if current else 'off')}."
        else:
            return "Usage: /sandbox-toggle [on|off|toggle|status]"
        error = self._save_local_setting("sandbox.enabled", target)
        if error:
            return f"Failed to save sandbox setting: {error}"
        return f"Sandbox {'enabled' if target else 'disabled'}."

    def _build_security_review_prompt(self, args: str) -> str:
        target = args.strip() or "the current working tree"
        return (
            f"Perform a security-focused review of {target}. Prioritize exploitable "
            "bugs, permission bypasses, injection paths, unsafe file/network access, "
            "and missing tests. Report findings first with severity and file references."
        )

    def _handle_statusline_command(self, args: str) -> str:
        app_state = self._current_app_state()
        current = getattr(app_state, "status_line_text", None) if app_state is not None else None
        normalized = args.strip()
        lowered = normalized.lower()
        if not normalized or lowered in {"show", "status"}:
            return f"Status line: {_inline_code(current or 'default')}"
        if lowered in {"off", "disable", "clear", "reset"}:
            if app_state is not None:
                app_state.status_line_text = None
            error = self._save_local_setting("statusLine", None)
            return f"Failed to clear status line: {error}" if error else "Status line reset."
        if lowered in {"on", "default"}:
            text = "Claude Code"
        else:
            text = normalized
        if app_state is not None:
            app_state.status_line_text = text
        error = self._save_local_setting("statusLine", text)
        if error:
            return f"Failed to save status line: {error}"
        return f"Status line set to {_inline_code(text)}."

    def _handle_tag_command(self, args: str) -> str:
        tokens = _split_slash_args(args)
        app_state = self._current_app_state()
        tags = getattr(app_state, "session_tags", None) if app_state is not None else None
        if not isinstance(tags, list):
            tags = []
        command = tokens[0].lower() if tokens else "list"
        if command in {"list", "ls", "show"}:
            return "Tags: " + (", ".join(_inline_code(str(tag)) for tag in tags) if tags else "none")
        if command in {"add", "set"}:
            values = tokens[1:]
        elif tokens:
            values = tokens
        else:
            values = []
        if command in {"clear", "reset"}:
            tags = []
        elif command in {"remove", "delete"} and len(tokens) >= 2:
            remove = set(tokens[1:])
            tags = [tag for tag in tags if tag not in remove]
        elif values:
            for value in values:
                if value not in tags:
                    tags.append(value)
        else:
            return "Usage: /tag [list|add <tag>|remove <tag>|clear]"
        if app_state is not None:
            setattr(app_state, "session_tags", tags)
        return "Tags: " + (", ".join(_inline_code(str(tag)) for tag in tags) if tags else "none")

    def _handle_tasks_command(self, args: str) -> str:
        from .utils.tasks import (
            TASK_DELETED,
            create_task,
            get_task,
            is_todo_v2_enabled,
            list_tasks,
            to_list_task,
            to_public_task,
            update_task,
        )

        if not is_todo_v2_enabled():
            return "Task V2 tools are disabled."
        tokens = _split_slash_args(args)
        command = tokens[0].lower() if tokens else "list"
        if command in {"list", "ls", "show"}:
            tasks = list_tasks()
            completed_ids = {task.id for task in tasks if task.status == "completed"}
            public_tasks = [
                public for task in tasks
                for public in (to_list_task(task, completed_ids),)
                if public is not None
            ]
            if not public_tasks:
                return "No tasks."
            return json.dumps({"tasks": public_tasks}, indent=2, sort_keys=True)
        if command == "get" and len(tokens) == 2:
            task = get_task(tokens[1])
            if task is None:
                return f"Task not found: {_inline_code(tokens[1])}"
            return json.dumps({"task": to_public_task(task)}, indent=2, sort_keys=True)
        if command == "create" and len(tokens) >= 2:
            task = create_task(subject=" ".join(tokens[1:]))
            return f"Created task {_inline_code(task.id)}."
        if command in {"delete", "remove"} and len(tokens) == 2:
            _, _, error = update_task(task_id=tokens[1], status=TASK_DELETED)
            if error:
                return f"Task delete failed: {error}"
            return f"Deleted task {_inline_code(tokens[1])}."
        return "Usage: /tasks [list|get <id>|create <subject>|delete <id>]"

    def _handle_theme_command(self, args: str) -> str:
        normalized = args.strip().lower()
        if not normalized:
            return f"Current theme: {_inline_code(self._current_theme())}"
        if normalized in {"dark", "light"}:
            error = self._save_local_setting("theme", normalized)
            if error:
                return f"Failed to save theme: {error}"
            return f"Theme set to {_inline_code(normalized)}."
        return "Invalid theme. Expected `dark` or `light`."

    def _build_ultrareview_prompt(self, args: str) -> str:
        base = self._build_review_prompt(args)
        return (
            base
            + "\n\nRun an especially deep review: include concurrency, data-loss, security, "
            "API compatibility, migration, and missing-test risks. Avoid stylistic nits."
        )

    def _build_usage_report_prompt(self) -> str:
        return (
            "Generate a usage report for this session from the available transcript, "
            "token accounting, tool activity, and context state. Highlight cost drivers, "
            "compaction opportunities, and concrete ways to reduce waste."
        )

    def _scope_excluded_command(self, command_variants: set[str]) -> str | None:
        excluded = {
            "/chrome": "Chrome/browser integration",
            "/desktop": "desktop app integration",
            "/app": "desktop app integration",
            "/heapDump": "heap diagnostics",
            "/heap-dump": "heap diagnostics",
            "/ide": "IDE integration",
            "/installGitHubApp": "GitHub App authentication flow",
            "/install-git-hub-app": "GitHub App authentication flow",
            "/installSlackApp": "Slack App authentication flow",
            "/install-slack-app": "Slack App authentication flow",
            "/keybindings": "terminal/editor keybinding UI",
            "/mobile": "mobile pairing flow",
            "/ios": "mobile pairing flow",
            "/android": "mobile pairing flow",
            "/pr_comments": "hosted PR comments integration",
            "/privacySettings": "hosted privacy settings",
            "/privacy-settings": "hosted privacy settings",
            "/rateLimitOptions": "hosted rate-limit options",
            "/rate-limit-options": "hosted rate-limit options",
            "/remoteEnv": "remote environment setup",
            "/remote-env": "remote environment setup",
            "/stickers": "stickers UX",
            "/thinkback": "thinking replay UX",
            "/thinkbackPlay": "thinking replay playback",
            "/thinkback-play": "thinking replay playback",
            "/upgrade": "package self-upgrade flow",
            "/vim": "interactive Vim-mode editor integration",
        }
        for variant in command_variants:
            if variant in excluded:
                return excluded[variant]
        return None

    def _build_scope_excluded_message(self, feature: str) -> str:
        return (
            f"{feature} is outside the Python migration scope documented for this audit. "
            "The command is recognized so it does not fall through to Unknown, but no "
            "local migration work is required for this excluded integration."
        )

    def _build_review_prompt(self, args: str) -> str:
        target = args.strip() or "the current working tree"
        diff_summary = ""
        try:
            stat = self._run_git_command("diff", "--stat")
        except (OSError, subprocess.TimeoutExpired):
            stat = None
        if stat is not None and stat.returncode == 0 and stat.stdout.strip():
            diff_summary = "\n\nCurrent diff summary:\n```text\n" + stat.stdout.strip() + "\n```"
        return (
            f"Review {target}. Focus on bugs, behavioral regressions, missing tests, "
            "and security or permission risks. Report findings first with file and line references."
            f"{diff_summary}"
        )

    def _build_doctor_message(self) -> str:
        return build_doctor_report(
            config_home=self.config_home,
            cwd=self.cwd,
            persist_sessions=self.persist_sessions,
            tool_count=len(self.stream_config.tools or ()),
        )

    def _handle_model_command(self, args: str) -> str:
        normalized = args.strip()
        if not normalized:
            return f"Current model: {_inline_code(self._selected_model())}"

        app_state = self._current_app_state()
        if normalized in {"reset", "default"}:
            if app_state is not None:
                app_state.main_loop_model_for_session = None
            if self.session.externalMetadata.model is not None:
                self.session = QuerySession(
                    sessionState=self.session.sessionState,
                    externalMetadata=replace(self.session.externalMetadata, model=None),
                    messageState=self.session.messageState,
                    messages=self.session.messages,
                    lastTerminal=self.session.lastTerminal,
                )
            self._sync_selected_model_to_adapter()
            return f"Model override cleared. Current model: {_inline_code(self._selected_model())}"

        if app_state is not None:
            app_state.main_loop_model_for_session = normalized
        self._sync_selected_model_to_adapter(normalized)
        self.session = QuerySession(
            sessionState=self.session.sessionState,
            externalMetadata=replace(self.session.externalMetadata, model=normalized),
            messageState=self.session.messageState,
            messages=self.session.messages,
            lastTerminal=self.session.lastTerminal,
        )
        return f"Model set to {_inline_code(normalized)}"

    def _handle_resume_command(self, args: str) -> str:
        if not self.persist_sessions:
            return "Session persistence is disabled for this runtime."

        normalized = args.strip()
        try:
            if not normalized or normalized == "latest":
                snapshot = self._load_latest_resumable_snapshot()
            else:
                snapshot = load_session_snapshot(
                    normalized,
                    config_home=self.config_home,
                    recover=True,
                )
        except SessionSnapshotValidationError as exc:
            return f"Failed to resume session: {exc}"

        if snapshot is None:
            target = normalized or "latest"
            return f"No persisted session found for {_inline_code(target)}."

        self._restore_snapshot(snapshot)
        return (
            f"Resumed session {_inline_code(snapshot.session_id)} "
            f"with {len(snapshot.session.messages)} messages."
        )

    def _handle_compact_command(self) -> str:
        compacted = self._get_runner().run(self._compact_session_for_repl())
        if compacted is None:
            return "Not enough conversation history to compact."

        self.session = QuerySession(
            sessionState=self.session.sessionState,
            externalMetadata=self.session.externalMetadata,
            messageState=self.session.messageState,
            messages=compacted,
            lastTerminal=self.session.lastTerminal,
        )._refresh_session_memory()
        self._clear_stream_preview()
        boundary = next(
            (message for message in compacted if isinstance(message, CompactBoundaryMessage)),
            None,
        )
        if boundary is None:
            return "Conversation compacted."
        return (
            "Conversation compacted manually "
            f"({_inline_code(boundary.trigger)}: "
            f"{boundary.originalTokenCount} -> {boundary.newTokenCount} tokens)."
        )

    async def _compact_session_for_repl(self) -> tuple[Message, ...] | None:
        config = self._effective_stream_config()
        clear_compact_warning_suppression()
        compacted = await _compact_session_messages(
            self.session.messages,
            model_adapter=self.model_adapter,
            runtime_config=config,
            trigger="manual_compact",
            keep_tail_messages=max(config.reactive_compact_tail_messages, 1),
            max_context_tokens=max(config.max_context_tokens, 1),
            threshold_ratio=None,
        )
        if compacted is not None:
            run_post_compact_cleanup(
                "repl_main_thread",
                tool_executor=self.tool_executor,
                model_adapter=self.model_adapter,
            )
        return compacted

    def _load_latest_resumable_snapshot(self):
        normalized_cwd = os.path.abspath(self.cwd)
        seen_ids: set[str] = set()
        for entry in sorted(
            _load_session_index(self.config_home),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        ):
            session_id = entry.get("session_id")
            if not isinstance(session_id, str) or session_id in seen_ids:
                continue
            seen_ids.add(session_id)
            if session_id == self.session_id:
                continue
            entry_cwd = entry.get("cwd")
            if not isinstance(entry_cwd, str) or os.path.abspath(entry_cwd) != normalized_cwd:
                continue
            snapshot = load_session_snapshot(
                session_id,
                config_home=self.config_home,
                recover=True,
            )
            if snapshot is not None:
                return snapshot

        fallback = load_latest_session_snapshot(
            config_home=self.config_home,
            cwd=self.cwd,
            recover=True,
        )
        if fallback is None or fallback.session_id == self.session_id:
            return None
        return fallback

    def _sync_model_override_from_session(self) -> None:
        model_override = self.session.externalMetadata.model
        app_state = self._current_app_state()
        if app_state is not None:
            app_state.main_loop_model_for_session = model_override
        self._sync_selected_model_to_adapter(model_override)

    def _restore_snapshot(self, snapshot: Any) -> None:
        self.session = snapshot.session
        self.session_id = snapshot.session_id
        self._session_created_at = snapshot.created_at
        if snapshot.display_name:
            self.display_name = snapshot.display_name
        self._sync_model_override_from_session()
        self._clear_stream_preview()
        clear_beta_latches = getattr(self.model_adapter, "clear_beta_header_latches", None)
        if callable(clear_beta_latches):
            clear_beta_latches()
        self._append_away_summary(snapshot)

    def _append_away_summary(self, snapshot: Any) -> None:
        if not isinstance(self.tool_executor, LocalToolExecutor):
            return
        since_timestamp_ms = parse_iso_timestamp_ms(getattr(snapshot, "updated_at", None))
        if self._away_summary_session_id == getattr(snapshot, "session_id", None):
            if since_timestamp_ms is None:
                since_timestamp_ms = self._away_summary_cutoff_ms
            elif self._away_summary_cutoff_ms is not None:
                since_timestamp_ms = max(since_timestamp_ms, self._away_summary_cutoff_ms)
        task_events = self.tool_executor.task_manager.list_events()
        agent_events = tuple(
            event
            for event in self.tool_executor.agent_manager.list_events()
            if isinstance(event.get("type"), str)
            and str(event.get("type")).startswith("agent_")
        )
        away_summary = generate_away_summary(
            task_events=task_events,
            agent_events=agent_events,
            tasks=self.tool_executor.app_state.tasks,
            since_timestamp_ms=since_timestamp_ms,
        )
        if away_summary is None:
            self._away_summary_session_id = getattr(snapshot, "session_id", None)
            self._away_summary_cutoff_ms = since_timestamp_ms
            return
        self.session = self.session.appendMessage(
            createSystemMessage(away_summary.markdown, "info")
        )
        self._away_summary_session_id = getattr(snapshot, "session_id", None)
        self._away_summary_cutoff_ms = away_summary.latest_timestamp_ms

    def _sync_selected_model_to_adapter(self, model: str | None = None) -> None:
        if not hasattr(self.model_adapter, "model"):
            return
        selected_model = model.strip() if isinstance(model, str) else ""
        if not selected_model:
            app_state = self._current_app_state()
            settings = getattr(app_state, "settings", None)
            normalized_settings = settings if isinstance(settings, Mapping) else {}
            selected_model = resolve_main_loop_model(
                app_state=app_state,
                settings=normalized_settings,
                fallback=getInitialMainLoopModel() or "claude-sonnet-4-5",
            )
        setattr(self.model_adapter, "model", selected_model)

    def _session_usage_totals(self) -> dict[str, int]:
        totals = {
            "assistant_messages": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "web_search_requests": 0,
            "web_fetch_requests": 0,
        }
        for message in self.session.messages:
            if not isinstance(message, AssistantMessage):
                continue
            totals["assistant_messages"] += 1
            usage = message.message.usage
            if not isinstance(usage, Mapping):
                continue
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int) and value > 0:
                    totals[key] += value
            server_tool_use = usage.get("server_tool_use")
            if isinstance(server_tool_use, Mapping):
                web_search = server_tool_use.get("web_search_requests")
                if isinstance(web_search, int) and web_search > 0:
                    totals["web_search_requests"] += web_search
                web_fetch = server_tool_use.get("web_fetch_requests")
                if isinstance(web_fetch, int) and web_fetch > 0:
                    totals["web_fetch_requests"] += web_fetch
        return totals

    def _get_runner(self) -> asyncio.Runner:
        if self._runner is None:
            self._runner = asyncio.Runner()
        return self._runner

    def _apply_permission_mode_to_executor(self) -> None:
        executor = self.tool_executor
        if executor is None:
            return
        app_state = getattr(executor, "app_state", None)
        if app_state is None:
            return
        raw_context = getattr(app_state, "tool_permission_context", None)
        if isinstance(raw_context, dict):
            context = dict(raw_context)
        else:
            context = {}
        transition = transition_permission_mode(context, self.permission_mode)
        context = dict(transition.context)
        context["cwd"] = self.cwd
        if self.config_home is not None:
            context["configHome"] = self.config_home
            context["config_home"] = self.config_home
        if self.session_id is not None:
            context["sessionId"] = self.session_id
            context["session_id"] = self.session_id
            app_state.repl_bridge_session_id = self.session_id
        app_state.tool_permission_context = context

    def _bind_executor_interaction(self) -> None:
        if isinstance(self.tool_executor, LocalToolExecutor):
            self.tool_executor.approval_prompt_handler = self._handle_permission_request

    def _handle_permission_request(
        self,
        tool_name: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        decision: PermissionAskDecision,
    ) -> PermissionAllowDecision | PermissionDenyDecision | None:
        if self.approval_input_reader is None:
            return None

        prompt_data = self._build_permission_prompt_data(
            tool_name=tool_name,
            permission_tool_name=permission_tool_name,
            decision=decision,
        )
        self.interaction.transition_to_approval(prompt_data)
        self._render_active_screen()

        selected_value = "deny"
        while True:
            user_input = self.approval_input_reader()
            if user_input == "":
                selected_value = "deny"
                break
            parsed = self._parse_permission_response(user_input)
            if parsed is not None:
                selected_value = parsed
                break
            prompt_data = replace(
                prompt_data,
                question=(
                    f"{decision.message or f'Allow {tool_name}?'}\n"
                    "Enter 1, 2, 3, or 4."
                ),
            )
            self.interaction.transition_to_approval(prompt_data)
            self._render_active_screen()

        self.interaction.handle_permission_decision(selected_value)
        try:
            if selected_value == "allow_all":
                self._persist_permission_rule(
                    behavior="allow",
                    permission_tool_name=permission_tool_name,
                    raw_input=raw_input,
                    decision=decision,
                )
            elif selected_value == "deny_all":
                self._persist_permission_rule(
                    behavior="deny",
                    permission_tool_name=permission_tool_name,
                    raw_input=raw_input,
                    decision=decision,
                )

            if selected_value in {"allow", "allow_this_once", "allow_all"}:
                return PermissionAllowDecision(
                    updated_input=dict(decision.updated_input or raw_input),
                    decision_reason=decision.decision_reason,
                )
            return PermissionDenyDecision(
                message=decision.message or f"Permission to use {tool_name} has been denied.",
                decision_reason=decision.decision_reason,
            )
        finally:
            self.interaction.transition_to_prompt("Type a message or /exit")
            self._render_active_screen()

    def _build_permission_prompt_data(
        self,
        *,
        tool_name: str,
        permission_tool_name: str,
        decision: PermissionAskDecision,
    ) -> PermissionPromptData:
        question = decision.message or f"Allow {tool_name}?"
        return PermissionPromptData(
            question=f"{question}\nChoose an action for {permission_tool_name}.",
            tool_name=tool_name,
            allow_feedback=False,
            options=[
                PermissionOption(value="allow_this_once", label="1. Allow once"),
                PermissionOption(value="allow_all", label="2. Always allow"),
                PermissionOption(value="deny", label="3. Deny once"),
                PermissionOption(value="deny_all", label="4. Always deny"),
            ],
        )

    def _parse_permission_response(self, raw_value: str) -> str | None:
        normalized = raw_value.strip().lower()
        if not normalized:
            return None
        aliases = {
            "1": "allow_this_once",
            "allow": "allow_this_once",
            "allow once": "allow_this_once",
            "once": "allow_this_once",
            "2": "allow_all",
            "always allow": "allow_all",
            "allow all": "allow_all",
            "always": "allow_all",
            "yes": "allow_all",
            "y": "allow_all",
            "3": "deny",
            "deny": "deny",
            "deny once": "deny",
            "no": "deny",
            "n": "deny",
            "4": "deny_all",
            "always deny": "deny_all",
            "deny all": "deny_all",
            "never": "deny_all",
            "d": "deny_all",
        }
        return aliases.get(normalized)

    def _persist_permission_rule(
        self,
        *,
        behavior: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        decision: PermissionAskDecision,
    ) -> None:
        executor = self.tool_executor
        if executor is None:
            return
        app_state = getattr(executor, "app_state", None)
        if app_state is None:
            return
        raw_context = getattr(app_state, "tool_permission_context", None)
        context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
        rule_string = self._permission_rule_string(permission_tool_name, raw_input)
        target_key = (
            "always_allow_rules" if behavior == "allow" else "always_deny_rules"
        )
        current_rules = context.get(target_key)
        normalized_rules = dict(current_rules) if isinstance(current_rules, Mapping) else {}
        session_rules = list(normalized_rules.get("session") or ())
        if rule_string not in session_rules:
            session_rules.append(rule_string)
        normalized_rules["session"] = session_rules
        context[target_key] = normalized_rules
        self._remove_session_ask_rule(context, decision, rule_string=rule_string)
        app_state.tool_permission_context = context

    def _remove_session_ask_rule(
        self,
        context: dict[str, Any],
        decision: PermissionAskDecision,
        *,
        rule_string: str,
    ) -> None:
        current_rules = context.get("always_ask_rules")
        if not isinstance(current_rules, Mapping):
            return
        session_rules = list(current_rules.get("session") or ())
        if not session_rules:
            return
        removal_candidates = {rule_string}
        if isinstance(decision.decision_reason, RuleDecisionReason):
            rule = decision.decision_reason.rule
            if rule.source.value == "session":
                removal_candidates.add(
                    permission_rule_value_to_string(
                        rule.rule_value.tool_name,
                        rule.rule_value.rule_content,
                    )
                )
        remaining = [rule for rule in session_rules if rule not in removal_candidates]
        updated = dict(current_rules)
        if remaining:
            updated["session"] = remaining
        else:
            updated.pop("session", None)
        context["always_ask_rules"] = updated

    def _permission_rule_string(
        self,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
    ) -> str:
        rule_content: str | None = None
        if permission_tool_name == "Bash":
            command = raw_input.get("command")
            if isinstance(command, str) and command.strip():
                rule_content = command.strip()
        elif permission_tool_name == "WebFetch":
            url = raw_input.get("url")
            if isinstance(url, str) and url.strip():
                from urllib.parse import urlparse

                rule_content = urlparse(url).hostname or url.strip()
        elif permission_tool_name == "Skill":
            skill_name = raw_input.get("resolved_skill_name") or raw_input.get("skill")
            if isinstance(skill_name, str) and skill_name.strip():
                rule_content = skill_name.strip()
        elif permission_tool_name in {
            "Read",
            "Write",
            "Edit",
            "Glob",
            "NotebookEdit",
        }:
            for key in ("file_path", "filePath", "path", "notebook_path", "notebookPath"):
                value = raw_input.get(key)
                if isinstance(value, str) and value.strip():
                    rule_content = value.strip()
                    break
        return permission_rule_value_to_string(permission_tool_name, rule_content)

    def _render_active_screen(self) -> None:
        if self._active_render is not None:
            self._active_render()

    def _build_structured_error_display(
        self,
        error: BaseException,
        *,
        title: str,
        code: str,
        stage: str,
        log_error: bool,
    ):
        return build_structured_error_display(
            error,
            title=title,
            code=code,
            stage=stage,
            config_home=self.config_home,
            diagnostic_context=DiagnosticContext(
                session_id=self.session_id or "repl",
                cwd=self.cwd,
                version=self.version,
            ),
            log_error=log_error,
        )

    def _render_error_row(
        self,
        *,
        error: BaseException,
        message_id: str,
        stage: str,
    ) -> MessageRowData:
        display = self._build_structured_error_display(
            error,
            title="Render Error",
            code="render_error",
            stage=stage,
            log_error=False,
        )
        return MessageRowData(
            message_id=message_id,
            message_type="system",
            content=f"System:\n{format_compact_structured_error_markdown(display)}",
            theme=self._current_theme(),
        )

    def _ensure_terminal_error_message_for_turn(
        self,
        *,
        previous_message_count: int,
        title: str,
        code: str,
        stage: str,
    ) -> AssistantMessage | None:
        terminal = self.session.lastTerminal
        if terminal is None or terminal.reason is not TerminalReason.MODEL_ERROR:
            return None
        if any(
            isinstance(message, AssistantMessage) and message.isApiErrorMessage
            for message in self.session.messages[previous_message_count:]
        ):
            return None
        detail = terminal.error if isinstance(terminal.error, str) and terminal.error.strip() else code
        display = self._build_structured_error_display(
            RuntimeError(detail),
            title=title,
            code=code,
            stage=stage,
            log_error=True,
        )
        error_message = createAssistantAPIErrorMessage(
            content=format_structured_error_markdown(display),
            apiError=code,
            error=RuntimeError(detail),
            errorDetails=display.detail,
        )
        self.session = self.session.appendMessage(error_message)
        return error_message

    def _restore_persisted_session_if_requested(self) -> None:
        if not self.persist_sessions:
            return
        try:
            snapshot = None
            if self.resume_session_id:
                snapshot = load_session_snapshot(
                    self.resume_session_id,
                    config_home=self.config_home,
                    recover=True,
                )
            elif self.continue_most_recent:
                snapshot = load_latest_session_snapshot(
                    config_home=self.config_home,
                    cwd=self.cwd,
                    recover=True,
                )
            elif self.resume_latest_session:
                snapshot = load_latest_session_snapshot(
                    config_home=self.config_home,
                    recover=True,
                )
        except SessionSnapshotValidationError:
            snapshot = None
        if snapshot is None:
            return
        self._restore_snapshot(snapshot)

    def _persist_session(self) -> None:
        if not self.persist_sessions or self.session_id is None:
            return
        snapshot = save_session_snapshot(
            self.session,
            session_id=self.session_id,
            cwd=self.cwd,
            config_home=self.config_home,
            created_at=self._session_created_at,
            display_name=self.display_name,
        )
        self._session_created_at = snapshot.created_at

    def _clear_stream_preview(self) -> None:
        self._stream_preview_text = ""
        self._stream_preview_tool_name = None
        self._stream_preview_tool_input = ""
        self._stream_preview_mode = None

    def _clear_ephemeral_system_messages(self) -> None:
        self._ephemeral_system_entries.clear()

    def _remember_ephemeral_system_message(
        self,
        message: SystemInformationalMessage,
    ) -> None:
        content = str(message.content).strip()
        if not content:
            return
        self._ephemeral_system_entries.append(
            _EphemeralSystemEntry(
                content=content,
                level=str(message.level),
                message_id=str(
                    getattr(message, "uuid", "")
                    or f"system-{len(self._ephemeral_system_entries)}"
                ),
            )
        )
        if len(self._ephemeral_system_entries) > 4:
            self._ephemeral_system_entries = self._ephemeral_system_entries[-4:]

    def _ephemeral_system_message_rows(self) -> list[MessageRowData]:
        rows: list[MessageRowData] = []
        for index, entry in enumerate(self._ephemeral_system_entries):
            content = (
                f"System: {entry.content}"
                if "\n" not in entry.content
                else f"System:\n{entry.content}"
            )
            rows.append(
                MessageRowData(
                    message_id=f"{entry.message_id}-{index}",
                    message_type="system",
                    content=content,
                    theme=self._current_theme(),
                )
            )
        return rows

    def _update_stream_preview(self, output: object) -> None:
        if output is None:
            return
        if isinstance(output, AssistantMessage):
            self._clear_stream_preview()
            return
        if isinstance(output, SystemInformationalMessage):
            if not any(
                isinstance(message, SystemInformationalMessage)
                and message.uuid == output.uuid
                for message in self.session.messages
            ):
                self._remember_ephemeral_system_message(output)
            return
        if isinstance(output, (UserMessage, AttachmentMessage)):
            return
        raw_event = getattr(output, "event", None)
        if not isinstance(raw_event, dict):
            return
        part_type = raw_event.get("type")
        if part_type == "message_start":
            self._clear_stream_preview()
            return
        if part_type == "content_block_start":
            block = raw_event.get("content_block")
            if isinstance(block, dict):
                if block.get("type") == "text":
                    self._stream_preview_mode = "text"
                    self._stream_preview_text = str(block.get("text", ""))
                elif block.get("type") == "thinking" and self.show_streaming_thinking:
                    self._stream_preview_mode = "thinking"
                    self._stream_preview_text = str(block.get("thinking", ""))
                elif block.get("type") == "tool_use":
                    self._stream_preview_mode = "tool_use"
                    self._stream_preview_tool_name = str(block.get("name", "tool"))
                    self._stream_preview_tool_input = ""
            return
        if part_type == "content_block_delta":
            delta = raw_event.get("delta")
            if not isinstance(delta, dict):
                return
            if delta.get("type") == "text_delta":
                self._stream_preview_mode = "text"
                self._stream_preview_text = (
                    f"{self._stream_preview_text}{str(delta.get('text', ''))}"
                )
            elif (
                delta.get("type") == "thinking_delta"
                and self.show_streaming_thinking
            ):
                if self._stream_preview_mode != "thinking":
                    self._stream_preview_text = ""
                self._stream_preview_mode = "thinking"
                self._stream_preview_text = (
                    f"{self._stream_preview_text}{str(delta.get('thinking', ''))}"
                )
            elif delta.get("type") == "input_json_delta":
                self._stream_preview_mode = "tool_use"
                self._stream_preview_tool_input = (
                    f"{self._stream_preview_tool_input}{str(delta.get('partial_json', ''))}"
                )
            return

    def _stream_preview_row(self) -> MessageRowData | None:
        if self._stream_preview_mode == "text" and self._stream_preview_text.strip():
            return MessageRowData(
                message_id="stream-preview",
                message_type="assistant",
                content=f"Claude: {self._stream_preview_text}",
                timestamp=None,
                theme=self._current_theme(),
            )
        if (
            self._stream_preview_mode == "thinking"
            and self.show_streaming_thinking
            and self._stream_preview_text.strip()
        ):
            return MessageRowData(
                message_id="stream-preview-thinking",
                message_type="assistant",
                content=f"Thinking: {self._stream_preview_text}",
                timestamp=None,
                theme=self._current_theme(),
            )
        if self._stream_preview_mode == "tool_use" and self._stream_preview_tool_name:
            return MessageRowData(
                message_id="stream-preview-tool",
                message_type="progress",
                content="",
                title=self._stream_preview_tool_name,
                status_text="streaming",
                detail_lines=_stream_preview_detail_lines(
                    self._stream_preview_tool_name,
                    self._stream_preview_tool_input,
                ),
                progress_frame_index=int(time.monotonic() * 8.0),
                timestamp=None,
                theme=self._current_theme(),
            )
        return None


class _AppendTerminalRenderer:
    """Scrollback-friendly renderer for real terminal sessions."""

    def __init__(
        self,
        *,
        runtime: InteractiveReplRuntime,
        out_stream: TextIO,
        width: int,
        terminal: TerminalCapabilities,
        out_is_tty: bool,
    ) -> None:
        self._runtime = runtime
        self._out_stream = out_stream
        self._width = max(width, 1)
        self._terminal = terminal
        self._out_is_tty = out_is_tty
        self._setup_emitted = False
        self._rendered_row_count = 0
        self._footer_line_count = 0
        self._footer_cursor_row = 0

    def render(self, *, native_progress_payload: str = "") -> None:
        payload = self._build_payload(native_progress_payload=native_progress_payload)
        self._out_stream.write(
            wrap_synchronized_output(
                payload,
                enabled=bool(
                    self._out_is_tty and self._terminal.supports_synchronized_output
                ),
            )
        )
        self._out_stream.flush()

    def _build_payload(self, *, native_progress_payload: str = "") -> str:
        payload_parts: list[str] = []
        if self._out_is_tty and not self._setup_emitted:
            title = (self._runtime.display_name or "Claude Code").strip() or "Claude Code"
            payload_parts.append(
                set_title(title)
                + set_window_title(title)
                + set_current_directory(self._runtime.cwd)
            )
            self._setup_emitted = True
        payload_parts.append(native_progress_payload)
        payload_parts.append(self._clear_footer_payload())

        new_message_lines = self._new_message_lines()
        for line in new_message_lines:
            payload_parts.append(line)
            payload_parts.append("\n")

        footer_text = self._runtime.build_terminal_footer_text(self._width)
        footer_frame = _build_rendered_screen_frame(footer_text)
        should_reserve_footer_lines = (
            self._footer_line_count <= 0 or bool(new_message_lines)
        )
        payload_parts.append(
            self._render_footer_payload(
                footer_frame,
                reserve_lines=should_reserve_footer_lines,
            )
        )
        self._footer_line_count = len(footer_frame.lines)
        self._footer_cursor_row = footer_frame.cursor_row
        payload_parts.append(self._move_to_footer_cursor_payload(footer_frame))
        return "".join(payload_parts)

    def _clear_footer_payload(self) -> str:
        if self._footer_line_count <= 0:
            return ""
        payload_parts = ["\r"]
        payload_parts.append(_cursor_up(self._footer_cursor_row))
        for index in range(self._footer_line_count):
            payload_parts.append(erase_in_line(2))
            if index < self._footer_line_count - 1:
                payload_parts.append("\r")
                payload_parts.append(_cursor_down(1))
        payload_parts.append("\r")
        payload_parts.append(_cursor_up(self._footer_line_count - 1))
        return "".join(payload_parts)

    def _render_footer_payload(
        self,
        frame: _RenderedScreenFrame,
        *,
        reserve_lines: bool,
    ) -> str:
        if reserve_lines:
            return "\n".join(frame.lines)
        payload_parts: list[str] = []
        for index, line in enumerate(frame.lines):
            payload_parts.append(line)
            if index < len(frame.lines) - 1:
                payload_parts.append("\r")
                payload_parts.append(_cursor_down(1))
        return "".join(payload_parts)

    def _move_to_footer_cursor_payload(self, frame: _RenderedScreenFrame) -> str:
        rows_below_cursor = max(len(frame.lines) - 1 - frame.cursor_row, 0)
        return "\r" + _cursor_up(rows_below_cursor) + _cursor_right(frame.cursor_column - 1)

    def _new_message_lines(self) -> list[str]:
        rows = self._appendable_rows()
        if len(rows) < self._rendered_row_count:
            self._rendered_row_count = 0
        new_rows = rows[self._rendered_row_count :]
        self._rendered_row_count = len(rows)
        lines: list[str] = []
        for row in new_rows:
            lines.extend(MessageRowRenderer(row).render_lines(self._width))
        return lines

    def _appendable_rows(self) -> list[MessageRowData]:
        rows = [
            row
            for row in self._runtime._visible_message_rows()
            if not _is_volatile_terminal_row(row)
        ]
        if len(rows) < self._rendered_row_count:
            self._rendered_row_count = 0
        if not self._runtime.is_turn_running():
            return rows

        allowed_count = self._rendered_row_count
        if allowed_count < len(rows) and rows[allowed_count].message_type == "user":
            allowed_count += 1
        return rows[:allowed_count]


@dataclass(frozen=True)
class _PromptInputAction:
    submitted_line: str | None = None
    render_requested: bool = False


_CTRL_C_EXIT_WINDOW_SECONDS = 2.0


class _InteractivePromptEditor:
    def __init__(self, runtime: InteractiveReplRuntime) -> None:
        self._runtime = runtime
        self._history: list[str] = list(runtime.get_prompt_history_entries())
        self._history_index: int | None = None
        self._draft_text = ""
        self._last_ctrl_c_monotonic: float | None = None
        text, cursor = self._runtime.interaction.get_prompt_input()
        self._text = text
        self._cursor = max(0, min(cursor, len(text)))
        self._sync_prompt()

    def apply(self, event: ParsedTerminalInput | None) -> _PromptInputAction:
        if event is None:
            return _PromptInputAction()
        if event.kind == "paste":
            return self._insert_text(event.text)
        if event.kind != "key":
            return _PromptInputAction()

        key = event.key
        if key in {"wheelup", "wheeldown", "pageup", "pagedown"}:
            return _PromptInputAction()
        if event.ctrl and key == "d":
            if self._text:
                return _PromptInputAction()
            return _PromptInputAction(submitted_line="/exit", render_requested=True)
        if event.ctrl and key == "c":
            return self._handle_ctrl_c()
        if key in {"enter", "return"}:
            return self._submit()
        if key == "backspace":
            return self._backspace()
        if key == "delete":
            return self._delete()
        if key == "left":
            return self._move_cursor(-1)
        if key == "right":
            return self._move_cursor(1)
        if key == "home":
            return self._replace_text(self._text, 0)
        if key == "end":
            return self._replace_text(self._text, len(self._text))
        if key == "up":
            return self._navigate_history(-1)
        if key == "down":
            return self._navigate_history(1)
        if key == "space":
            return self._insert_text(" ")
        if not event.ctrl and not event.alt:
            if len(event.raw) == 1 and event.raw.isprintable():
                return self._insert_text(event.raw)
            if len(key) == 1 and key.isprintable():
                return self._insert_text(key)
        return _PromptInputAction()

    def apply_raw(self, raw: str) -> _PromptInputAction:
        parsed = parse_terminal_input(raw)
        if parsed is not None:
            return self.apply(parsed)
        combined = _PromptInputAction()
        for char in raw:
            action = self.apply(parse_terminal_input(char))
            combined = _PromptInputAction(
                submitted_line=action.submitted_line or combined.submitted_line,
                render_requested=combined.render_requested or action.render_requested,
            )
            if action.submitted_line is not None:
                break
        return combined

    def _sync_prompt(self) -> None:
        self._runtime.interaction.set_prompt_input(self._text, self._cursor)

    def _reset_ctrl_c_sequence(self) -> None:
        self._last_ctrl_c_monotonic = None

    def _handle_ctrl_c(self) -> _PromptInputAction:
        now = time.monotonic()
        should_exit = (
            not self._text
            and self._last_ctrl_c_monotonic is not None
            and now - self._last_ctrl_c_monotonic <= _CTRL_C_EXIT_WINDOW_SECONDS
        )
        self._last_ctrl_c_monotonic = now
        if should_exit:
            self._reset_ctrl_c_sequence()
            return _PromptInputAction(submitted_line="/exit", render_requested=True)
        if self._text:
            return self._replace_text("", 0, reset_ctrl_c_sequence=False)
        return _PromptInputAction(render_requested=True)

    def _replace_text(
        self,
        text: str,
        cursor: int | None = None,
        *,
        reset_ctrl_c_sequence: bool = True,
    ) -> _PromptInputAction:
        if reset_ctrl_c_sequence:
            self._reset_ctrl_c_sequence()
        self._text = text
        if cursor is None:
            cursor = len(text)
        self._cursor = max(0, min(cursor, len(text)))
        self._sync_prompt()
        return _PromptInputAction(render_requested=True)

    def _insert_text(self, text: str) -> _PromptInputAction:
        if not text:
            return _PromptInputAction()
        self._reset_ctrl_c_sequence()
        self._history_index = None
        self._text = self._text[: self._cursor] + text + self._text[self._cursor :]
        self._cursor += len(text)
        self._sync_prompt()
        return _PromptInputAction(render_requested=True)

    def _backspace(self) -> _PromptInputAction:
        if self._cursor <= 0:
            return _PromptInputAction()
        self._reset_ctrl_c_sequence()
        self._history_index = None
        self._text = self._text[: self._cursor - 1] + self._text[self._cursor :]
        self._cursor -= 1
        self._sync_prompt()
        return _PromptInputAction(render_requested=True)

    def _delete(self) -> _PromptInputAction:
        if self._cursor >= len(self._text):
            return _PromptInputAction()
        self._reset_ctrl_c_sequence()
        self._history_index = None
        self._text = self._text[: self._cursor] + self._text[self._cursor + 1 :]
        self._sync_prompt()
        return _PromptInputAction(render_requested=True)

    def _move_cursor(self, delta: int) -> _PromptInputAction:
        next_cursor = max(0, min(len(self._text), self._cursor + delta))
        if next_cursor == self._cursor:
            return _PromptInputAction()
        self._reset_ctrl_c_sequence()
        self._cursor = next_cursor
        self._sync_prompt()
        return _PromptInputAction(render_requested=True)

    def _navigate_history(self, delta: int) -> _PromptInputAction:
        if not self._history:
            return _PromptInputAction()
        if self._history_index is None:
            if delta > 0:
                return _PromptInputAction()
            self._draft_text = self._text
            next_index = len(self._history) - 1
        else:
            next_index = self._history_index + delta
        if next_index < 0:
            next_index = 0
        if next_index >= len(self._history):
            self._history_index = None
            return self._replace_text(self._draft_text)
        self._history_index = next_index
        return self._replace_text(self._history[next_index])

    def _submit(self) -> _PromptInputAction:
        line = self._text.strip()
        if not line:
            return _PromptInputAction()
        self._reset_ctrl_c_sequence()
        should_add_history = not self._history or self._history[-1] != line
        if should_add_history:
            self._history.append(line)
            self._runtime.record_prompt_history_entry(line)
        self._history_index = None
        self._draft_text = ""
        self._replace_text("", 0)
        return _PromptInputAction(submitted_line=line, render_requested=True)


def _stream_has_buffered_input(stream: TextIO) -> bool:
    checker = getattr(stream, "has_buffered_input", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def _should_use_terminal_event_input(
    *,
    stdin: TextIO | None,
    stdout: TextIO | None,
    in_stream: TextIO,
    out_stream: TextIO,
) -> bool:
    if stdin is not None or stdout is not None:
        return False
    if termios is None or tty is None:
        return False
    if _stream_has_buffered_input(in_stream):
        return False
    if not bool(getattr(in_stream, "isatty", lambda: False)()):
        return False
    if not bool(getattr(out_stream, "isatty", lambda: False)()):
        return False
    fileno = getattr(in_stream, "fileno", None)
    if not callable(fileno):
        return False
    try:
        fileno()
    except (OSError, ValueError):
        return False
    return True


@contextmanager
def _terminal_event_input_mode(
    in_stream: TextIO,
    out_stream: TextIO,
) -> Iterator[int]:
    fd = in_stream.fileno()
    setup = enable_bracketed_paste()
    teardown = disable_bracketed_paste()
    try:
        out_stream.write(setup)
        out_stream.flush()
        yield fd
    finally:
        out_stream.write(teardown)
        out_stream.flush()


@contextmanager
def _raw_terminal_input_mode(fd: int) -> Iterator[None]:
    previous_attributes = termios.tcgetattr(fd)  # type: ignore[union-attr]
    try:
        tty.setraw(fd, termios.TCSANOW)  # type: ignore[union-attr]
        yield
    finally:
        termios.tcsetattr(  # type: ignore[union-attr]
            fd,
            termios.TCSANOW,  # type: ignore[union-attr]
            previous_attributes,
        )


def _read_decoded_terminal_char(fd: int, decoder: Any) -> str:
    while True:
        chunk = os.read(fd, 1)
        if not chunk:
            return ""
        text = decoder.decode(chunk, final=False)
        if text:
            return text


def _read_terminal_input_chunk(fd: int, *, encoding: str) -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors="ignore")
    first = _read_decoded_terminal_char(fd, decoder)
    if not first or first != "\x1b":
        return first

    raw = first
    deadline = time.monotonic() + 0.03
    while True:
        timeout = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return raw
        chunk = os.read(fd, 4096)
        if not chunk:
            return raw
        raw += decoder.decode(chunk, final=False)
        parsed = parse_terminal_input(raw)
        if parsed is not None and parsed.kind != "paste_start":
            return raw
        if raw == "\x1b[200~":
            deadline = time.monotonic() + 0.5
        if raw.endswith("\x1b[201~") or len(raw) >= 16384:
            return raw


def _run_terminal_event_repl_loop(
    *,
    runtime: InteractiveReplRuntime,
    in_stream: TextIO,
    out_stream: TextIO,
    render: Callable[[], None],
    stream_render: Callable[[], None],
) -> int:
    editor = _InteractivePromptEditor(runtime)
    encoding = getattr(in_stream, "encoding", None) or "utf-8"
    with _terminal_event_input_mode(in_stream, out_stream) as fd:
        render()
        while True:
            with _raw_terminal_input_mode(fd):
                raw = _read_terminal_input_chunk(fd, encoding=encoding)
            if raw == "":
                return 0

            parsed = parse_terminal_input(raw)
            action = editor.apply(parsed) if parsed is not None else editor.apply_raw(raw)
            if action.render_requested:
                render()
            if action.submitted_line is None:
                continue
            if not runtime.handle_line(
                action.submitted_line,
                render=render,
                stream_render=stream_render,
            ):
                return 0


def run_interactive_repl(
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    width: int = 80,
    height: int = 24,
    session_id: str | None = None,
    continue_most_recent: bool = False,
    resume_session_id: str | None = None,
    resume_latest_session: bool = False,
    persist_sessions: bool = True,
    config_home: str | None = None,
    display_name: str | None = None,
    permission_mode: str = "default",
    stream_config: QueryStreamConfig | None = None,
    inline_agents: Sequence[AgentDefinition] | None = None,
) -> int:
    runtime = InteractiveReplRuntime(
        session_id=session_id,
        continue_most_recent=continue_most_recent,
        resume_session_id=resume_session_id,
        resume_latest_session=resume_latest_session,
        persist_sessions=persist_sessions,
        config_home=config_home,
        display_name=display_name,
        permission_mode=permission_mode,
        stream_config=stream_config or QueryStreamConfig(),
        inline_agents=tuple(inline_agents or ()),
    )
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    terminal = TerminalCapabilities()
    out_is_tty = getattr(out_stream, "isatty", lambda: False)()
    in_stream = _negotiate_startup_terminal_capabilities(in_stream, out_stream, terminal)
    runtime.clipboard_copy_handler = (
        (
            lambda text: (
                out_stream.write(set_clipboard(text)),
                out_stream.flush(),
                True,
            )[-1]
        )
        if out_is_tty
        else None
    )
    previous_rendered_frame: _RenderedScreenFrame | None = None
    append_renderer: _AppendTerminalRenderer | None = None
    is_rendering = False
    console_render_pending = False

    def _render() -> None:
        nonlocal previous_rendered_frame, is_rendering, console_render_pending
        if is_rendering:
            console_render_pending = True
            return
        is_rendering = True
        setup_payload = ""
        native_progress_payload = (
            runtime.build_native_progress_bar_payload()
            if bool(out_is_tty and terminal.supports_native_progress_bar)
            else ""
        )
        try:
            if append_renderer is not None:
                append_renderer.render(native_progress_payload=native_progress_payload)
                return
            current_frame = _build_rendered_screen_frame(
                runtime.build_screen_text(width=width, height=height)
            )
            if not out_is_tty or previous_rendered_frame is None:
                screen_payload = _build_fullscreen_render_payload(current_frame)
            else:
                screen_payload = _build_incremental_render_payload(
                    previous_rendered_frame,
                    current_frame,
                )
            if bool(out_is_tty and previous_rendered_frame is None):
                title = (runtime.display_name or "Claude Code").strip() or "Claude Code"
                setup_payload = (
                    set_title(title)
                    + set_window_title(title)
                    + set_current_directory(runtime.cwd)
                )
            payload = setup_payload + native_progress_payload + screen_payload
            out_stream.write(
                wrap_synchronized_output(
                    payload,
                    enabled=bool(out_is_tty and terminal.supports_synchronized_output),
                )
            )
            out_stream.flush()
            previous_rendered_frame = current_frame
        finally:
            is_rendering = False
        if console_render_pending:
            console_render_pending = False
            render_scheduler.request_render(force=True)

    render_scheduler = _FrameRenderScheduler(_render)

    def _render_now() -> None:
        render_scheduler.request_render(force=True)

    def _schedule_stream_render() -> None:
        render_scheduler.request_render()

    def _capture_console_text(stream_name: str, text: str) -> None:
        nonlocal console_render_pending
        runtime.append_console_output(stream_name, text)
        if is_rendering:
            console_render_pending = True
            return
        render_scheduler.request_render(force=True)

    try:
        with _patch_console_streams(
            on_stdout=lambda text: _capture_console_text("stdout", text),
            on_stderr=lambda text: _capture_console_text("stderr", text),
        ):
            if _should_use_terminal_event_input(
                stdin=stdin,
                stdout=stdout,
                in_stream=in_stream,
                out_stream=out_stream,
            ):
                append_renderer = _AppendTerminalRenderer(
                    runtime=runtime,
                    out_stream=out_stream,
                    width=width,
                    terminal=terminal,
                    out_is_tty=bool(out_is_tty),
                )
                return _run_terminal_event_repl_loop(
                    runtime=runtime,
                    in_stream=in_stream,
                    out_stream=out_stream,
                    render=_render_now,
                    stream_render=_schedule_stream_render,
                )
            while True:
                _render_now()

                line = in_stream.readline()
                if line == "":
                    return 0

                if not runtime.handle_line(
                    line,
                    render=_render_now,
                    stream_render=_schedule_stream_render,
                ):
                    return 0
    finally:
        if bool(out_is_tty and terminal.supports_native_progress_bar):
            out_stream.write(clear_progress_bar())
            out_stream.flush()
        runtime.close()


__all__ = [
    "InteractiveReplRuntime",
    "can_start_interactive_repl",
    "run_interactive_repl",
]
