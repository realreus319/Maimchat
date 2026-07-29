from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple, Union
from uuid import uuid4

from python_src.utils.config import (  # noqa: E402
    get_timebased_microcompact_gap_minutes,
)


NO_CONTENT_MESSAGE = "(no content)"
SYNTHETIC_MODEL = "<synthetic>"
INTERRUPT_MESSAGE = "[Request interrupted by user]"
INTERRUPT_MESSAGE_FOR_TOOL_USE = "[Request interrupted by user for tool use]"
MAX_HISTORY_ITEMS = 100
_SESSION_MEMORY_LIST_LIMIT = 12
_MICROCOMPACT_CHAR_LIMIT = 1_200
_MICROCOMPACT_LINE_LIMIT = 18
_MICROCOMPACT_EXCERPT_LIMIT = 140
_MICROCOMPACT_EXCERPT_LINES = 3
_MICROCOMPACT_JSON_KEY_LIMIT = 6
_MICROCOMPACT_PATH_LIMIT = 4

# In-memory cache for microcompact results keyed by message structure hash.
# Mirrors the TS cache_edits optimisation: when the same compactable
# message structure is seen again the previously computed compacted messages
# are reused, avoiding redundant heuristic work.
_MICROCOMPACT_CACHE: dict[str, tuple[Message, ...]] = {}
_MICROCOMPACT_CACHE_MAX_SIZE = 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid4())


def _default_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0,
        },
        "service_tier": None,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": None,
        "iterations": None,
        "speed": None,
    }


class QueryStateTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: Mapping[str, Any]
    signature: str = ""
    type: str = "tool_use"


@dataclass(frozen=True)
class ThinkingBlock:
    thinking: str
    signature: str = ""
    type: str = "thinking"


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False
    type: str = "tool_result"


AssistantContentBlock = Union[TextBlock, ToolUseBlock, ThinkingBlock]
UserContentBlock = Union[TextBlock, ToolResultBlock]


def _derive_assistant_signature(
    content: Sequence[AssistantContentBlock],
) -> Optional[str]:
    for block in reversed(tuple(content)):
        if isinstance(block, ThinkingBlock) and block.signature:
            return block.signature
    return None


@dataclass
class AssistantPayload:
    content: Tuple[AssistantContentBlock, ...]
    id: str = field(default_factory=_uuid)
    container: None = None
    model: str = SYNTHETIC_MODEL
    role: str = "assistant"
    stop_reason: str = "stop_sequence"
    stop_sequence: str = ""
    type: str = "message"
    usage: Mapping[str, Any] = field(default_factory=_default_usage)
    context_management: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class UserPayload:
    content: Union[str, Tuple[UserContentBlock, ...]]
    role: str = "user"


@dataclass
class AssistantMessage:
    message: AssistantPayload
    type: str = "assistant"
    uuid: str = field(default_factory=_uuid)
    timestamp: str = field(default_factory=_now_iso)
    signature: Optional[str] = None
    requestId: Optional[str] = None
    apiError: Optional[str] = None
    error: Any = None
    errorDetails: Optional[str] = None
    isApiErrorMessage: bool = False
    isVirtual: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.signature is None:
            self.signature = _derive_assistant_signature(self.message.content)


@dataclass(frozen=True)
class UserMessage:
    message: UserPayload
    type: str = "user"
    uuid: str = field(default_factory=_uuid)
    timestamp: str = field(default_factory=_now_iso)
    isMeta: Optional[bool] = None
    isVisibleInTranscriptOnly: Optional[bool] = None
    isVirtual: Optional[bool] = None
    isCompactSummary: Optional[bool] = None
    summarizeMetadata: Optional[Mapping[str, Any]] = None
    toolUseResult: Any = None
    toolUsePayload: Optional[Mapping[str, Any]] = None
    mcpMeta: Optional[Mapping[str, Any]] = None
    imagePasteIds: Optional[Tuple[int, ...]] = None
    sourceToolAssistantUUID: Optional[str] = None
    permissionMode: Optional[str] = None
    origin: Optional[str] = None


@dataclass(frozen=True)
class SystemInformationalMessage:
    content: str
    level: str
    type: str = "system"
    subtype: str = "informational"
    isMeta: bool = False
    timestamp: str = field(default_factory=_now_iso)
    uuid: str = field(default_factory=_uuid)
    toolUseID: Optional[str] = None
    preventContinuation: Optional[bool] = None


@dataclass(frozen=True)
class CompactBoundaryMessage:
    trigger: str
    originalTokenCount: int
    newTokenCount: int
    compact_type: str = "full"
    deletedToolUseIds: Tuple[str, ...] = ()
    preservedMessageUuids: Tuple[str, ...] = ()
    type: str = "system"
    subtype: str = "compact_boundary"
    isMeta: bool = False
    timestamp: str = field(default_factory=_now_iso)
    uuid: str = field(default_factory=_uuid)


@dataclass(frozen=True)
class ToolUseSummaryMessage:
    summary: str
    precedingToolUseIds: Tuple[str, ...]
    type: str = "tool_use_summary"
    uuid: str = field(default_factory=_uuid)
    timestamp: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class ProgressMessage:
    data: Mapping[str, Any]
    toolUseID: str
    parentToolUseID: str
    type: str = "progress"
    uuid: str = field(default_factory=_uuid)
    timestamp: str = field(default_factory=_now_iso)


@dataclass(frozen=True)
class AttachmentMessage:
    attachment: Mapping[str, Any]
    type: str = "attachment"
    uuid: str = field(default_factory=_uuid)
    timestamp: str = field(default_factory=_now_iso)


Message = Union[
    AssistantMessage,
    UserMessage,
    SystemInformationalMessage,
    CompactBoundaryMessage,
    ToolUseSummaryMessage,
    ProgressMessage,
    AttachmentMessage,
]


@dataclass(frozen=True)
class RequestStartEvent:
    type: str = "stream_request_start"


@dataclass(frozen=True)
class StreamEvent:
    event: Mapping[str, Any]
    ttftMs: Optional[int] = None
    type: str = "stream_event"


@dataclass(frozen=True)
class TombstoneMessage:
    message: AssistantMessage
    type: str = "tombstone"


@dataclass(frozen=True)
class ToolExecutionUpdate:
    message: Optional[Message] = None
    newContext: Optional[Mapping[str, Any]] = None


QueryOutput = Union[RequestStartEvent, StreamEvent, Message, TombstoneMessage]


@dataclass(frozen=True)
class QueryTurnReplay:
    outputs: Tuple[QueryOutput, ...]
    assistantMessages: Tuple[AssistantMessage, ...]
    toolResults: Tuple[Union[UserMessage, AttachmentMessage], ...]
    needsFollowUp: bool
    terminal: Optional[TerminalTransition] = None
    continuation: Optional[ContinueTransition] = None
    updatedToolUseContext: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class CompactionResult:
    boundaryMarker: CompactBoundaryMessage
    summaryMessages: Tuple[UserMessage, ...]
    messagesToKeep: Tuple[Message, ...] = ()
    attachments: Tuple[AttachmentMessage, ...] = ()
    hookResults: Tuple[Message, ...] = ()


@dataclass(frozen=True)
class CompactionOutcome:
    emittedMessages: Tuple[Message, ...]
    messagesForQuery: Tuple[Message, ...]
    tracking: Optional[Mapping[str, Any]] = None
    nextState: Optional[QueryLoopState] = None


def createAssistantMessage(
    *,
    content: Union[str, Sequence[AssistantContentBlock]],
    usage: Optional[Mapping[str, Any]] = None,
    isVirtual: Optional[bool] = None,
) -> AssistantMessage:
    if isinstance(content, str):
        content_blocks: Tuple[AssistantContentBlock, ...] = (
            TextBlock(content or NO_CONTENT_MESSAGE),
        )
    else:
        content_blocks = tuple(content)
    return AssistantMessage(
        message=AssistantPayload(
            content=content_blocks, usage=usage or _default_usage()
        ),
        isVirtual=isVirtual,
    )


def createAssistantAPIErrorMessage(
    *,
    content: str,
    apiError: Optional[str] = None,
    error: Any = None,
    errorDetails: Optional[str] = None,
) -> AssistantMessage:
    return AssistantMessage(
        message=AssistantPayload(content=(TextBlock(content or NO_CONTENT_MESSAGE),)),
        apiError=apiError,
        error=error,
        errorDetails=errorDetails,
        isApiErrorMessage=True,
    )


def createUserMessage(
    *,
    content: Union[str, Sequence[UserContentBlock]],
    isMeta: Optional[bool] = None,
    isVisibleInTranscriptOnly: Optional[bool] = None,
    isVirtual: Optional[bool] = None,
    isCompactSummary: Optional[bool] = None,
    summarizeMetadata: Optional[Mapping[str, Any]] = None,
    toolUseResult: Any = None,
    toolUsePayload: Optional[Mapping[str, Any]] = None,
    mcpMeta: Optional[Mapping[str, Any]] = None,
    uuid: Optional[str] = None,
    timestamp: Optional[str] = None,
    imagePasteIds: Optional[Sequence[int]] = None,
    sourceToolAssistantUUID: Optional[str] = None,
    permissionMode: Optional[str] = None,
    origin: Optional[str] = None,
) -> UserMessage:
    if isinstance(content, str):
        payload_content: Union[str, Tuple[UserContentBlock, ...]] = (
            content if content else NO_CONTENT_MESSAGE
        )
    else:
        payload_content = tuple(content)
    return UserMessage(
        message=UserPayload(content=payload_content),
        uuid=uuid or _uuid(),
        timestamp=timestamp or _now_iso(),
        isMeta=isMeta,
        isVisibleInTranscriptOnly=isVisibleInTranscriptOnly,
        isVirtual=isVirtual,
        isCompactSummary=isCompactSummary,
        summarizeMetadata=summarizeMetadata,
        toolUseResult=toolUseResult,
        toolUsePayload=toolUsePayload,
        mcpMeta=mcpMeta,
        imagePasteIds=tuple(imagePasteIds) if imagePasteIds is not None else None,
        sourceToolAssistantUUID=sourceToolAssistantUUID,
        permissionMode=permissionMode,
        origin=origin,
    )


def createUserInterruptionMessage(*, toolUse: bool = False) -> UserMessage:
    content = INTERRUPT_MESSAGE_FOR_TOOL_USE if toolUse else INTERRUPT_MESSAGE
    return createUserMessage(content=(TextBlock(content),))


def createSystemMessage(
    content: str,
    level: str,
    toolUseID: Optional[str] = None,
    preventContinuation: Optional[bool] = None,
) -> SystemInformationalMessage:
    return SystemInformationalMessage(
        content=content,
        level=level,
        toolUseID=toolUseID,
        preventContinuation=preventContinuation,
    )


def _compact_type_from_trigger(trigger: str) -> str:
    """Map a compact trigger string to a compact_type category.

    Categories:
      "reactive" – PTL / media-size triggered compaction
      "full"     – proactive auto-compaction or snip/session-memory compaction
    """
    if trigger == "reactive_compact":
        return "reactive"
    return "full"


def createCompactBoundaryMessage(
    *,
    trigger: str,
    originalTokenCount: int,
    newTokenCount: int,
    compact_type: Optional[str] = None,
    deletedToolUseIds: Optional[Sequence[str]] = None,
    preservedMessageUuids: Optional[Sequence[str]] = None,
) -> CompactBoundaryMessage:
    resolved_type = compact_type or _compact_type_from_trigger(trigger)
    return CompactBoundaryMessage(
        trigger=trigger,
        originalTokenCount=originalTokenCount,
        newTokenCount=newTokenCount,
        compact_type=resolved_type,
        deletedToolUseIds=tuple(deletedToolUseIds or ()),
        preservedMessageUuids=tuple(preservedMessageUuids or ()),
    )


def createToolUseSummaryMessage(
    summary: str,
    precedingToolUseIds: Sequence[str],
) -> ToolUseSummaryMessage:
    return ToolUseSummaryMessage(
        summary=summary,
        precedingToolUseIds=tuple(precedingToolUseIds),
    )


class ContinueReason(str, Enum):
    COLLAPSE_DRAIN_RETRY = "collapse_drain_retry"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    STOP_HOOK_BLOCKING = "stop_hook_blocking"
    TOKEN_BUDGET_CONTINUATION = "token_budget_continuation"
    NEXT_TURN = "next_turn"


class TerminalReason(str, Enum):
    BLOCKING_LIMIT = "blocking_limit"
    IMAGE_ERROR = "image_error"
    MODEL_ERROR = "model_error"
    ABORTED_STREAMING = "aborted_streaming"
    PROMPT_TOO_LONG = "prompt_too_long"
    COMPLETED = "completed"
    STOP_HOOK_PREVENTED = "stop_hook_prevented"
    ABORTED_TOOLS = "aborted_tools"
    HOOK_STOPPED = "hook_stopped"
    MAX_TURNS = "max_turns"


@dataclass(frozen=True)
class ContinueTransition:
    reason: ContinueReason
    committed: Optional[int] = None
    attempt: Optional[int] = None

    def __post_init__(self) -> None:
        if self.reason == ContinueReason.COLLAPSE_DRAIN_RETRY:
            if self.committed is None or self.committed <= 0:
                raise QueryStateTransitionError(
                    "collapse_drain_retry requires committed > 0"
                )
            if self.attempt is not None:
                raise QueryStateTransitionError(
                    "collapse_drain_retry does not carry attempt"
                )
            return
        if self.reason == ContinueReason.MAX_OUTPUT_TOKENS_RECOVERY:
            if self.attempt is None or self.attempt <= 0:
                raise QueryStateTransitionError(
                    "max_output_tokens_recovery requires attempt > 0"
                )
            if self.committed is not None:
                raise QueryStateTransitionError(
                    "max_output_tokens_recovery does not carry committed"
                )
            return
        if self.committed is not None:
            raise QueryStateTransitionError(
                f"{self.reason.value} does not carry committed"
            )
        if self.attempt is not None:
            raise QueryStateTransitionError(
                f"{self.reason.value} does not carry attempt"
            )


@dataclass(frozen=True)
class TerminalTransition:
    reason: TerminalReason
    error: Any = None
    turnCount: Optional[int] = None

    def __post_init__(self) -> None:
        if self.reason == TerminalReason.MODEL_ERROR:
            if self.turnCount is not None:
                raise QueryStateTransitionError("model_error does not carry turnCount")
            return
        if self.reason == TerminalReason.MAX_TURNS:
            if self.turnCount is None or self.turnCount <= 0:
                raise QueryStateTransitionError("max_turns requires turnCount > 0")
            if self.error is not None:
                raise QueryStateTransitionError("max_turns does not carry error")
            return
        if self.error is not None:
            raise QueryStateTransitionError(f"{self.reason.value} does not carry error")
        if self.turnCount is not None:
            raise QueryStateTransitionError(
                f"{self.reason.value} does not carry turnCount"
            )


@dataclass(frozen=True)
class QueryLoopState:
    messages: Tuple[Message, ...]
    toolUseContext: Mapping[str, Any]
    autoCompactTracking: Optional[Mapping[str, Any]] = None
    maxOutputTokensRecoveryCount: int = 0
    hasAttemptedReactiveCompact: bool = False
    maxOutputTokensOverride: Optional[int] = None
    pendingToolUseSummary: Any = None
    stopHookActive: Optional[bool] = None
    turnCount: int = 1
    transition: Optional[ContinueTransition] = None

    def __post_init__(self) -> None:
        if self.turnCount < 1:
            raise QueryStateTransitionError("turnCount must be >= 1")
        if self.maxOutputTokensRecoveryCount < 0:
            raise QueryStateTransitionError("maxOutputTokensRecoveryCount must be >= 0")


def createInitialQueryState(
    *,
    messages: Sequence[Message],
    toolUseContext: Mapping[str, Any],
    maxOutputTokensOverride: Optional[int] = None,
) -> QueryLoopState:
    return QueryLoopState(
        messages=tuple(messages),
        toolUseContext=toolUseContext,
        maxOutputTokensOverride=maxOutputTokensOverride,
    )


class SessionState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    REQUIRES_ACTION = "requires_action"


@dataclass(frozen=True)
class RequiresActionDetails:
    tool_name: str
    action_description: str
    tool_use_id: str
    request_id: str
    input: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class SessionExternalMetadata:
    permission_mode: Optional[str] = None
    is_ultraplan_mode: Optional[bool] = None
    model: Optional[str] = None
    pending_action: Optional[RequiresActionDetails] = None
    post_turn_summary: Any = None
    task_summary: Optional[str] = None
    session_memory: Optional["SessionMemory"] = None
    last_microcompact_time: Optional[float] = None


@dataclass(frozen=True)
class SessionMemory:
    summary: Optional[str] = None
    user_requests: Tuple[str, ...] = ()
    decisions: Tuple[str, ...] = ()
    tool_activity: Tuple[str, ...] = ()
    files: Tuple[str, ...] = ()
    open_questions: Tuple[str, ...] = ()
    compaction_count: int = 0
    summary_source: Optional[str] = None
    summary_origin: Optional[str] = None
    last_updated_at: Optional[str] = None
    summary_message_uuids: Tuple[str, ...] = ()


def _normalize_session_memory_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _normalize_session_memory_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _normalize_session_memory_string(value)
        if item is None:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
        if len(normalized) >= _SESSION_MEMORY_LIST_LIMIT:
            break
    return tuple(normalized)


def _merge_session_memory_strings(
    existing: Sequence[str],
    incoming: Sequence[str],
) -> tuple[str, ...]:
    return _normalize_session_memory_strings([*existing, *incoming])


def _compact_summary_text(message: "UserMessage") -> str | None:
    metadata = message.summarizeMetadata
    if isinstance(metadata, Mapping):
        summary = _normalize_session_memory_string(metadata.get("summary"))
        if summary is not None:
            return summary
    content = message.message.content
    if isinstance(content, str):
        return _normalize_session_memory_string(content)
    return None


def initSessionMemory(
    messages: Sequence["Message"],
    existing: Optional[SessionMemory] = None,
) -> Optional[SessionMemory]:
    current = existing or SessionMemory()
    processed = list(current.summary_message_uuids)
    seen_processed = set(processed)
    summary = current.summary
    user_requests = current.user_requests
    decisions = current.decisions
    tool_activity = current.tool_activity
    files = current.files
    open_questions = current.open_questions
    compaction_count = current.compaction_count
    summary_source = current.summary_source
    summary_origin = current.summary_origin
    last_updated_at = current.last_updated_at
    found_summary_message = False

    for message in messages:
        if not (isinstance(message, UserMessage) and message.isCompactSummary):
            continue
        found_summary_message = True
        if message.uuid in seen_processed:
            continue

        seen_processed.add(message.uuid)
        processed.append(message.uuid)
        metadata = (
            message.summarizeMetadata
            if isinstance(message.summarizeMetadata, Mapping)
            else {}
        )

        compact_summary = _compact_summary_text(message)
        if compact_summary is not None:
            summary = compact_summary

        user_requests = _merge_session_memory_strings(
            user_requests,
            _normalize_session_memory_strings(metadata.get("userRequests")),
        )
        decisions = _merge_session_memory_strings(
            decisions,
            _normalize_session_memory_strings(metadata.get("decisions")),
        )
        tool_activity = _merge_session_memory_strings(
            tool_activity,
            _normalize_session_memory_strings(metadata.get("toolActivity")),
        )
        files = _merge_session_memory_strings(
            files,
            _normalize_session_memory_strings(metadata.get("files")),
        )
        open_questions = _merge_session_memory_strings(
            open_questions,
            _normalize_session_memory_strings(metadata.get("openQuestions")),
        )

        counted_compactions = metadata.get("compactionCount")
        if isinstance(counted_compactions, int) and counted_compactions > 0:
            compaction_count = max(compaction_count, counted_compactions)
        else:
            compaction_count += 1

        normalized_source = _normalize_session_memory_string(
            metadata.get("summarySource")
        )
        if normalized_source is not None:
            summary_source = normalized_source

        normalized_origin = _normalize_session_memory_string(message.origin)
        if normalized_origin is not None:
            summary_origin = normalized_origin

        last_updated_at = message.timestamp or last_updated_at

    if not found_summary_message:
        return current if existing is not None else None

    return SessionMemory(
        summary=summary,
        user_requests=user_requests,
        decisions=decisions,
        tool_activity=tool_activity,
        files=files,
        open_questions=open_questions,
        compaction_count=compaction_count,
        summary_source=summary_source,
        summary_origin=summary_origin,
        last_updated_at=last_updated_at,
        summary_message_uuids=tuple(processed[-MAX_HISTORY_ITEMS:]),
    )


def _stringify_microcompact_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return json.dumps(list(value), ensure_ascii=False)
    return str(value)


def _truncate_microcompact_text(text: str, *, limit: int = _MICROCOMPACT_EXCERPT_LIMIT) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _extract_microcompact_paths(text: str) -> tuple[str, ...]:
    candidates = [
        candidate
        for candidate in re.findall(
            r"(?:[\w./-]+/)?[\w.-]+\.[A-Za-z0-9]{1,8}",
            text,
        )
        if "/" in candidate or "." in candidate
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
        if len(deduped) >= _MICROCOMPACT_PATH_LIMIT:
            break
    return tuple(deduped)


def _summarize_microcompact_text(
    text: str,
    *,
    label: str,
) -> tuple[str, Mapping[str, Any] | None]:
    normalized = text.strip()
    if not normalized:
        return normalized, None

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if (
        len(normalized) <= _MICROCOMPACT_CHAR_LIMIT
        and len(lines) <= _MICROCOMPACT_LINE_LIMIT
    ):
        return normalized, None

    highlights: list[str] = []
    parsed_json: Any = None
    if normalized[:1] in {"{", "["}:
        try:
            parsed_json = json.loads(normalized)
        except Exception:
            parsed_json = None
    if isinstance(parsed_json, Mapping):
        keys = [str(key) for key in list(parsed_json.keys())[:_MICROCOMPACT_JSON_KEY_LIMIT]]
        if keys:
            highlights.append("json keys=" + ", ".join(keys))
    elif isinstance(parsed_json, list):
        highlights.append(f"json array items={len(parsed_json)}")

    paths = _extract_microcompact_paths(normalized)
    if paths:
        highlights.append("paths=" + ", ".join(paths))

    excerpt_lines: list[str] = []
    for line in lines:
        if line in excerpt_lines:
            continue
        excerpt_lines.append(line)
        if len(excerpt_lines) >= _MICROCOMPACT_EXCERPT_LINES:
            break
    if lines:
        tail_line = lines[-1]
        if tail_line not in excerpt_lines and len(excerpt_lines) < _MICROCOMPACT_EXCERPT_LINES:
            excerpt_lines.append(tail_line)
    excerpt = " | ".join(
        _truncate_microcompact_text(line) for line in excerpt_lines if line
    )
    if excerpt:
        highlights.append("excerpt=" + excerpt)
    if not highlights:
        highlights.append("excerpt=" + _truncate_microcompact_text(normalized))

    summary = (
        f"[{label} microcompacted: total_chars={len(normalized)} total_lines={max(len(lines), 1)}]\n"
        + "; ".join(highlights)
    )
    metadata = {
        "label": label,
        "totalChars": len(normalized),
        "totalLines": max(len(lines), 1),
        "summaryChars": len(summary),
    }
    return summary, metadata


def _message_tool_result_sources(message: "UserMessage") -> tuple[tuple[str, str], ...]:
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return ()
    raw = message.toolUseResult
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        sources: list[tuple[str, str]] = []
        for index, block in enumerate(content):
            if index < len(raw):
                sources.append((_stringify_microcompact_value(raw[index]), "toolUseResult"))
            else:
                sources.append(
                    (_stringify_microcompact_value(block.content), "block.content")
                )
        return tuple(sources)
    if raw is not None:
        return tuple(
            (_stringify_microcompact_value(raw), "toolUseResult") for _ in content
        )
    return tuple(
        (_stringify_microcompact_value(block.content), "block.content") for block in content
    )


def _microcompact_user_message(message: "UserMessage") -> "UserMessage":
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return message
    if isinstance(message.mcpMeta, Mapping) and "microCompact" in message.mcpMeta:
        return message

    sources = _message_tool_result_sources(message)
    rewritten_blocks: list[ToolResultBlock] = []
    metadata_entries: list[Mapping[str, Any]] = []
    changed = False
    for block, (source_text, source_kind) in zip(content, sources):
        summary, metadata = _summarize_microcompact_text(
            source_text,
            label=f"tool result {block.tool_use_id}",
        )
        if metadata is None:
            rewritten_blocks.append(block)
            continue
        changed = True
        rewritten_blocks.append(
            ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=summary,
                is_error=block.is_error,
            )
        )
        metadata_entries.append(
            {
                **metadata,
                "toolUseId": block.tool_use_id,
                "source": source_kind,
            }
        )

    if not changed:
        return message

    mcp_meta = dict(message.mcpMeta or {})
    mcp_meta["microCompact"] = tuple(metadata_entries)
    return createUserMessage(
        content=tuple(rewritten_blocks),
        isMeta=message.isMeta,
        isVisibleInTranscriptOnly=message.isVisibleInTranscriptOnly,
        isVirtual=message.isVirtual,
        isCompactSummary=message.isCompactSummary,
        summarizeMetadata=message.summarizeMetadata,
        toolUseResult=message.toolUseResult,
        toolUsePayload=message.toolUsePayload,
        mcpMeta=mcp_meta,
        uuid=message.uuid,
        timestamp=message.timestamp,
        imagePasteIds=message.imagePasteIds,
        sourceToolAssistantUUID=message.sourceToolAssistantUUID,
        permissionMode=message.permissionMode,
        origin=message.origin,
    )


def _should_trigger_time_based_microcompact(
    last_compact_time: Optional[float],
    *,
    gap_minutes: int = 60,
) -> bool:
    if last_compact_time is None:
        return True
    elapsed_minutes = (time.monotonic() - last_compact_time) / 60.0
    return elapsed_minutes >= gap_minutes


def _microcompact_session_messages(messages: Sequence["Message"]) -> tuple["Message", ...]:
    changed = False
    rewritten: list[Message] = []
    for message in messages:
        if isinstance(message, UserMessage):
            updated = _microcompact_user_message(message)
            if updated != message:
                changed = True
            rewritten.append(updated)
            continue
        rewritten.append(message)
    if not changed:
        return tuple(messages)
    return tuple(rewritten)


def _compute_microcompact_cache_key(messages: Sequence["Message"]) -> Optional[str]:
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, UserMessage):
            continue
        content = msg.message.content
        if not (
            isinstance(content, tuple)
            and content
            and all(isinstance(block, ToolResultBlock) for block in content)
        ):
            continue
        if isinstance(msg.mcpMeta, Mapping) and "microCompact" in msg.mcpMeta:
            continue
        for block in content:
            parts.append(block.tool_use_id)
            source_text = _stringify_microcompact_value(
                msg.toolUseResult
                if msg.toolUseResult is not None
                else block.content
            )
            parts.append(str(len(source_text)))
            parts.append(source_text[:256])
    if not parts:
        return None
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return digest


def _cached_microcompact_session_messages(
    messages: Sequence["Message"],
) -> tuple["Message", ...]:
    cache_key = _compute_microcompact_cache_key(messages)
    if cache_key is not None and cache_key in _MICROCOMPACT_CACHE:
        return _MICROCOMPACT_CACHE[cache_key]
    result = _microcompact_session_messages(messages)
    if cache_key is not None and result != tuple(messages):
        if len(_MICROCOMPACT_CACHE) >= _MICROCOMPACT_CACHE_MAX_SIZE:
            oldest_key = next(iter(_MICROCOMPACT_CACHE))
            del _MICROCOMPACT_CACHE[oldest_key]
        _MICROCOMPACT_CACHE[cache_key] = result
    return result


def clear_microcompact_cache() -> None:
    _MICROCOMPACT_CACHE.clear()


def invalidate_microcompact_cache_for(
    messages: Sequence["Message"],
) -> None:
    cache_key = _compute_microcompact_cache_key(messages)
    if cache_key is not None and cache_key in _MICROCOMPACT_CACHE:
        del _MICROCOMPACT_CACHE[cache_key]


class MessagePhase(str, Enum):
    IDLE = "idle"
    AWAITING_ASSISTANT = "awaiting_assistant"
    AWAITING_TOOL_RESULTS = "awaiting_tool_results"
    AWAITING_ASSISTANT_AFTER_TOOL_RESULTS = "awaiting_assistant_after_tool_results"
    AWAITING_USER = "awaiting_user"


@dataclass(frozen=True)
class MessageState:
    phase: MessagePhase = MessagePhase.IDLE
    pendingToolUseIds: Tuple[str, ...] = ()
    seenToolUseIds: frozenset[str] = field(default_factory=frozenset)

    def apply(self, message: Message) -> "MessageState":
        if isinstance(
            message,
            (
                SystemInformationalMessage,
                CompactBoundaryMessage,
                ToolUseSummaryMessage,
                ProgressMessage,
                AttachmentMessage,
            ),
        ):
            return self
        if isinstance(message, UserMessage):
            return self._apply_user(message)
        if isinstance(message, AssistantMessage):
            return self._apply_assistant(message)
        raise QueryStateTransitionError(f"Unsupported message type: {type(message)!r}")

    def _apply_user(self, message: UserMessage) -> "MessageState":
        content = message.message.content
        if isinstance(content, str):
            if self.phase not in (MessagePhase.IDLE, MessagePhase.AWAITING_USER):
                raise QueryStateTransitionError(
                    f"User message is not allowed during {self.phase.value}"
                )
            return MessageState(
                phase=MessagePhase.AWAITING_ASSISTANT,
                pendingToolUseIds=self.pendingToolUseIds,
                seenToolUseIds=self.seenToolUseIds,
            )

        tool_results = [
            block for block in content if isinstance(block, ToolResultBlock)
        ]
        if not tool_results:
            if self.phase not in (MessagePhase.IDLE, MessagePhase.AWAITING_USER):
                raise QueryStateTransitionError(
                    f"User message is not allowed during {self.phase.value}"
                )
            return MessageState(
                phase=MessagePhase.AWAITING_ASSISTANT,
                pendingToolUseIds=self.pendingToolUseIds,
                seenToolUseIds=self.seenToolUseIds,
            )

        if len(tool_results) != len(content):
            raise QueryStateTransitionError(
                "tool_result messages must not mix tool_result and non-tool_result blocks"
            )
        if self.phase != MessagePhase.AWAITING_TOOL_RESULTS:
            raise QueryStateTransitionError(
                "tool_result message requires pending tool_use blocks"
            )

        remaining = list(self.pendingToolUseIds)
        seen_in_message: set[str] = set()
        for block in tool_results:
            if block.tool_use_id in seen_in_message:
                raise QueryStateTransitionError(
                    f"Duplicate tool_result for tool_use_id '{block.tool_use_id}'"
                )
            if block.tool_use_id not in remaining:
                raise QueryStateTransitionError(
                    f"tool_result references unknown tool_use_id '{block.tool_use_id}'"
                )
            seen_in_message.add(block.tool_use_id)
            remaining.remove(block.tool_use_id)

        next_phase = (
            MessagePhase.AWAITING_TOOL_RESULTS
            if remaining
            else MessagePhase.AWAITING_ASSISTANT_AFTER_TOOL_RESULTS
        )
        return MessageState(
            phase=next_phase,
            pendingToolUseIds=tuple(remaining),
            seenToolUseIds=self.seenToolUseIds,
        )

    def _apply_assistant(self, message: AssistantMessage) -> "MessageState":
        if self.phase not in (
            MessagePhase.AWAITING_ASSISTANT,
            MessagePhase.AWAITING_ASSISTANT_AFTER_TOOL_RESULTS,
        ):
            raise QueryStateTransitionError(
                f"Assistant message is not allowed during {self.phase.value}"
            )

        tool_use_blocks = [
            block
            for block in message.message.content
            if isinstance(block, ToolUseBlock)
        ]
        if not tool_use_blocks:
            return MessageState(
                phase=MessagePhase.AWAITING_USER,
                pendingToolUseIds=(),
                seenToolUseIds=self.seenToolUseIds,
            )

        seen_now: set[str] = set()
        for block in tool_use_blocks:
            if block.id in seen_now:
                raise QueryStateTransitionError(
                    f"Duplicate tool_use id '{block.id}' in assistant message"
                )
            if block.id in self.seenToolUseIds:
                raise QueryStateTransitionError(
                    f"tool_use id '{block.id}' was already used earlier in the session"
                )
            seen_now.add(block.id)

        return MessageState(
            phase=MessagePhase.AWAITING_TOOL_RESULTS,
            pendingToolUseIds=tuple(block.id for block in tool_use_blocks),
            seenToolUseIds=self.seenToolUseIds.union(seen_now),
        )

    def apply_streaming_tool_use_fragment(
        self,
        message: AssistantMessage,
    ) -> "MessageState":
        if self.phase != MessagePhase.AWAITING_TOOL_RESULTS:
            raise QueryStateTransitionError(
                f"Assistant message is not allowed during {self.phase.value}"
            )

        tool_use_blocks = [
            block
            for block in message.message.content
            if isinstance(block, ToolUseBlock)
        ]
        if not tool_use_blocks:
            raise QueryStateTransitionError(
                f"Assistant message is not allowed during {self.phase.value}"
            )

        seen_now: set[str] = set()
        for block in tool_use_blocks:
            if block.id in seen_now:
                raise QueryStateTransitionError(
                    f"Duplicate tool_use id '{block.id}' in assistant message"
                )
            if block.id in self.seenToolUseIds:
                raise QueryStateTransitionError(
                    f"tool_use id '{block.id}' was already used earlier in the session"
                )
            seen_now.add(block.id)

        return MessageState(
            phase=MessagePhase.AWAITING_TOOL_RESULTS,
            pendingToolUseIds=(
                *self.pendingToolUseIds,
                *(block.id for block in tool_use_blocks),
            ),
            seenToolUseIds=self.seenToolUseIds.union(seen_now),
        )


@dataclass(frozen=True)
class QuerySession:
    sessionState: SessionState = SessionState.IDLE
    externalMetadata: SessionExternalMetadata = field(
        default_factory=SessionExternalMetadata
    )
    messageState: MessageState = field(default_factory=MessageState)
    messages: Tuple[Message, ...] = ()
    lastTerminal: Optional[TerminalTransition] = None

    @classmethod
    def fromMessages(
        cls,
        messages: Sequence[Message],
        *,
        sessionState: SessionState = SessionState.IDLE,
        externalMetadata: Optional[SessionExternalMetadata] = None,
    ) -> "QuerySession":
        session = cls(
            sessionState=sessionState,
            externalMetadata=externalMetadata or SessionExternalMetadata(),
        )
        for message in messages:
            session = session.appendMessage(message)
        return session._refresh_session_memory()

    def startTurn(self) -> "QuerySession":
        return self.transitionSessionState(SessionState.RUNNING)

    def requireAction(self, details: RequiresActionDetails) -> "QuerySession":
        return self.transitionSessionState(
            SessionState.REQUIRES_ACTION, details=details
        )

    def resumeTurn(self) -> "QuerySession":
        return self.transitionSessionState(SessionState.RUNNING)

    def finishTurn(
        self,
        terminal: Optional[TerminalTransition] = None,
    ) -> "QuerySession":
        next_session = self._apply_microcompact()
        next_session = next_session.transitionSessionState(SessionState.IDLE)
        return QuerySession(
            sessionState=next_session.sessionState,
            externalMetadata=next_session.externalMetadata,
            messageState=next_session.messageState,
            messages=next_session.messages,
            lastTerminal=terminal,
        )

    def transitionSessionState(
        self,
        nextState: SessionState,
        *,
        details: Optional[RequiresActionDetails] = None,
    ) -> "QuerySession":
        allowed: dict[SessionState, set[SessionState]] = {
            SessionState.IDLE: {SessionState.RUNNING},
            SessionState.RUNNING: {SessionState.REQUIRES_ACTION, SessionState.IDLE},
            SessionState.REQUIRES_ACTION: {
                SessionState.REQUIRES_ACTION,
                SessionState.RUNNING,
                SessionState.IDLE,
            },
        }
        if nextState not in allowed[self.sessionState]:
            raise QueryStateTransitionError(
                f"Invalid session transition: {self.sessionState.value} -> {nextState.value}"
            )
        if nextState == SessionState.REQUIRES_ACTION and details is None:
            raise QueryStateTransitionError(
                "requires_action transitions require details"
            )
        if nextState != SessionState.REQUIRES_ACTION and details is not None:
            raise QueryStateTransitionError(
                f"{nextState.value} transitions do not accept requires_action details"
            )

        metadata = self.externalMetadata
        if nextState == SessionState.REQUIRES_ACTION:
            metadata = replace(metadata, pending_action=details)
        elif metadata.pending_action is not None:
            metadata = replace(metadata, pending_action=None)

        if nextState == SessionState.IDLE and metadata.task_summary is not None:
            metadata = replace(metadata, task_summary=None)

        return QuerySession(
            sessionState=nextState,
            externalMetadata=metadata,
            messageState=self.messageState,
            messages=self.messages,
            lastTerminal=self.lastTerminal,
        )

    def appendMessage(self, message: Message) -> "QuerySession":
        next_state = self._apply_message_state(message)
        return QuerySession(
            sessionState=self.sessionState,
            externalMetadata=self._metadata_with_message(message),
            messageState=next_state,
            messages=(*self.messages, message),
            lastTerminal=self.lastTerminal,
        )

    def _metadata_with_message(self, message: Message) -> SessionExternalMetadata:
        if not (isinstance(message, UserMessage) and message.isCompactSummary):
            return self.externalMetadata
        session_memory = initSessionMemory((message,), self.externalMetadata.session_memory)
        if session_memory == self.externalMetadata.session_memory:
            return self.externalMetadata
        return replace(self.externalMetadata, session_memory=session_memory)

    def _refresh_session_memory(self) -> "QuerySession":
        session_memory = initSessionMemory(
            self.messages,
            self.externalMetadata.session_memory,
        )
        if session_memory == self.externalMetadata.session_memory:
            return self
        return QuerySession(
            sessionState=self.sessionState,
            externalMetadata=replace(
                self.externalMetadata,
                session_memory=session_memory,
            ),
            messageState=self.messageState,
            messages=self.messages,
            lastTerminal=self.lastTerminal,
        )

    def _apply_microcompact(self) -> "QuerySession":
        if not _should_trigger_time_based_microcompact(
            self.externalMetadata.last_microcompact_time,
            gap_minutes=get_timebased_microcompact_gap_minutes(),
        ):
            return self
        messages = _cached_microcompact_session_messages(self.messages)
        if messages == self.messages:
            return self
        new_metadata = replace(
            self.externalMetadata,
            last_microcompact_time=time.monotonic(),
        )
        return QuerySession.fromMessages(
            messages,
            sessionState=self.sessionState,
            externalMetadata=new_metadata,
        )

    def _apply_message_state(self, message: Message) -> MessageState:
        try:
            return self.messageState.apply(message)
        except QueryStateTransitionError:
            if (
                self.sessionState == SessionState.RUNNING
                and isinstance(message, AssistantMessage)
                and self.messageState.phase == MessagePhase.AWAITING_USER
            ):
                # Streaming responses may flush several assistant blocks within one
                # in-flight turn before the model fully stops. Treat these blocks as
                # consecutive assistant output instead of forcing a user turn
                # boundary between them.
                streaming_state = MessageState(
                    phase=MessagePhase.AWAITING_ASSISTANT,
                    pendingToolUseIds=self.messageState.pendingToolUseIds,
                    seenToolUseIds=self.messageState.seenToolUseIds,
                )
                return streaming_state.apply(message)
            if (
                self.sessionState == SessionState.RUNNING
                and isinstance(message, AssistantMessage)
                and self.messageState.phase == MessagePhase.AWAITING_TOOL_RESULTS
            ):
                # The streaming layer currently appends assistant content blocks as
                # they close. A single provider message can contain multiple
                # tool_use blocks, so keep accumulating pending tool ids until the
                # corresponding tool_result user message arrives.
                return self.messageState.apply_streaming_tool_use_fragment(message)
            raise


@dataclass(frozen=True)
class PromptHistoryLogEntry:
    display: str
    timestamp: int
    project: str
    sessionId: Optional[str] = None


@dataclass(frozen=True)
class PromptHistoryState:
    pendingEntries: Tuple[PromptHistoryLogEntry, ...] = ()
    flushedEntries: Tuple[PromptHistoryLogEntry, ...] = ()
    skippedTimestamps: frozenset[int] = field(default_factory=frozenset)
    lastAddedEntry: Optional[PromptHistoryLogEntry] = None


def buildPostCompactMessages(result: CompactionResult) -> Tuple[Message, ...]:
    return (
        result.boundaryMarker,
        *result.summaryMessages,
        *result.messagesToKeep,
        *result.attachments,
        *result.hookResults,
    )


def applySuccessfulCompaction(
    *,
    compactionResult: CompactionResult,
    turnId: str,
) -> CompactionOutcome:
    postCompactMessages = buildPostCompactMessages(compactionResult)
    tracking = {
        "compacted": True,
        "turnId": turnId,
        "turnCounter": 0,
        "consecutiveFailures": 0,
    }
    return CompactionOutcome(
        emittedMessages=postCompactMessages,
        messagesForQuery=postCompactMessages,
        tracking=tracking,
    )


def continueAfterCollapseDrainRetry(
    *,
    state: QueryLoopState,
    drainedMessages: Sequence[Message],
    toolUseContext: Mapping[str, Any],
    tracking: Optional[Mapping[str, Any]],
    committed: int,
) -> CompactionOutcome:
    nextState = QueryLoopState(
        messages=tuple(drainedMessages),
        toolUseContext=toolUseContext,
        autoCompactTracking=tracking,
        maxOutputTokensRecoveryCount=state.maxOutputTokensRecoveryCount,
        hasAttemptedReactiveCompact=state.hasAttemptedReactiveCompact,
        maxOutputTokensOverride=None,
        pendingToolUseSummary=None,
        stopHookActive=None,
        turnCount=state.turnCount,
        transition=ContinueTransition(
            reason=ContinueReason.COLLAPSE_DRAIN_RETRY,
            committed=committed,
        ),
    )
    return CompactionOutcome(
        emittedMessages=(),
        messagesForQuery=tuple(drainedMessages),
        nextState=nextState,
    )


def continueAfterReactiveCompactRetry(
    *,
    state: QueryLoopState,
    compactionResult: CompactionResult,
    toolUseContext: Mapping[str, Any],
) -> CompactionOutcome:
    postCompactMessages = buildPostCompactMessages(compactionResult)
    nextState = QueryLoopState(
        messages=postCompactMessages,
        toolUseContext=toolUseContext,
        autoCompactTracking=None,
        maxOutputTokensRecoveryCount=state.maxOutputTokensRecoveryCount,
        hasAttemptedReactiveCompact=True,
        maxOutputTokensOverride=None,
        pendingToolUseSummary=None,
        stopHookActive=None,
        turnCount=state.turnCount,
        transition=ContinueTransition(reason=ContinueReason.REACTIVE_COMPACT_RETRY),
    )
    return CompactionOutcome(
        emittedMessages=postCompactMessages,
        messagesForQuery=postCompactMessages,
        nextState=nextState,
    )


def addPromptHistoryEntry(
    state: PromptHistoryState,
    *,
    display: str,
    timestamp: int,
    project: str,
    sessionId: Optional[str] = None,
) -> PromptHistoryState:
    entry = PromptHistoryLogEntry(
        display=display,
        timestamp=timestamp,
        project=project,
        sessionId=sessionId,
    )
    return PromptHistoryState(
        pendingEntries=(*state.pendingEntries, entry),
        flushedEntries=state.flushedEntries,
        skippedTimestamps=state.skippedTimestamps,
        lastAddedEntry=entry,
    )


def flushPromptHistoryEntries(state: PromptHistoryState) -> PromptHistoryState:
    if not state.pendingEntries:
        return state
    return PromptHistoryState(
        pendingEntries=(),
        flushedEntries=(*state.flushedEntries, *state.pendingEntries),
        skippedTimestamps=state.skippedTimestamps,
        lastAddedEntry=state.lastAddedEntry,
    )


def clearPendingHistoryEntries(state: PromptHistoryState) -> PromptHistoryState:
    return PromptHistoryState(flushedEntries=state.flushedEntries)


def removeLastFromHistory(state: PromptHistoryState) -> PromptHistoryState:
    if state.lastAddedEntry is None:
        return state
    entry = state.lastAddedEntry
    pendingEntries = list(state.pendingEntries)
    if entry in pendingEntries:
        idx = len(pendingEntries) - 1 - pendingEntries[::-1].index(entry)
        pendingEntries.pop(idx)
        skipped = state.skippedTimestamps
    else:
        skipped = frozenset((*state.skippedTimestamps, entry.timestamp))
    return PromptHistoryState(
        pendingEntries=tuple(pendingEntries),
        flushedEntries=state.flushedEntries,
        skippedTimestamps=skipped,
        lastAddedEntry=None,
    )


def getHistoryEntries(
    state: PromptHistoryState,
    *,
    currentProject: str,
    currentSession: Optional[str],
    maxItems: int = MAX_HISTORY_ITEMS,
) -> Tuple[PromptHistoryLogEntry, ...]:
    currentSessionEntries: list[PromptHistoryLogEntry] = []
    otherSessionEntries: list[PromptHistoryLogEntry] = []

    for entry in reversed(state.pendingEntries):
        if entry.project != currentProject:
            continue
        if entry.sessionId == currentSession:
            currentSessionEntries.append(entry)
        else:
            otherSessionEntries.append(entry)
        if len(currentSessionEntries) + len(otherSessionEntries) >= maxItems:
            return tuple(
                currentSessionEntries
                + otherSessionEntries[: maxItems - len(currentSessionEntries)]
            )

    for entry in reversed(state.flushedEntries):
        if entry.project != currentProject:
            continue
        if (
            entry.sessionId == currentSession
            and entry.timestamp in state.skippedTimestamps
        ):
            continue
        if entry.sessionId == currentSession:
            currentSessionEntries.append(entry)
        else:
            otherSessionEntries.append(entry)
        if len(currentSessionEntries) + len(otherSessionEntries) >= maxItems:
            break

    return tuple(
        currentSessionEntries
        + otherSessionEntries[: maxItems - len(currentSessionEntries)]
    )


STREAM_UPDATE_AFTER_ASSISTANT = "after_assistant"
STREAM_UPDATE_AFTER_STREAM_EVENT = "after_stream_event"


def createAttachmentMessage(attachment: Mapping[str, Any]) -> AttachmentMessage:
    return AttachmentMessage(attachment=dict(attachment))


def createStreamRequestStartEvent() -> RequestStartEvent:
    return RequestStartEvent()


def createStreamEvent(
    event: Mapping[str, Any], *, ttftMs: Optional[int] = None
) -> StreamEvent:
    return StreamEvent(event=dict(event), ttftMs=ttftMs)


def yieldMissingToolResultBlocks(
    assistantMessages: Sequence[AssistantMessage],
    errorMessage: str,
) -> Tuple[UserMessage, ...]:
    missing: list[UserMessage] = []
    for assistantMessage in assistantMessages:
        for block in assistantMessage.message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            missing.append(
                createUserMessage(
                    content=(
                        ToolResultBlock(
                            tool_use_id=block.id,
                            content=errorMessage,
                            is_error=True,
                        ),
                    ),
                    toolUseResult=errorMessage,
                    sourceToolAssistantUUID=assistantMessage.uuid,
                )
            )
    return tuple(missing)


def _merge_mapping(
    base: Optional[Mapping[str, Any]], delta: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base or {})
    for key, value in dict(delta or {}).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
            continue
        if value is not None:
            merged[key] = value
    return merged


def _normalize_tool_input(rawInput: Any) -> Mapping[str, Any]:
    if isinstance(rawInput, Mapping):
        return dict(rawInput)
    if rawInput == "":
        return {}
    if not isinstance(rawInput, str):
        raise QueryStateTransitionError(
            "tool_use input must be a mapping or JSON string"
        )
    try:
        parsed = json.loads(rawInput)
    except json.JSONDecodeError:
        # Attempt to repair truncated JSON from streaming (e.g. unterminated strings)
        repaired = _attempt_json_repair(rawInput)
        if repaired is not None:
            parsed = repaired
        else:
            raise QueryStateTransitionError(
                "tool_use input is not valid JSON and could not be repaired"
            )
    if not isinstance(parsed, dict):
        raise QueryStateTransitionError("tool_use input must decode to an object")
    return parsed


def _attempt_json_repair(raw: str) -> dict | None:
    """Try to repair common streaming JSON truncation issues."""
    # Try closing unterminated strings and braces
    candidates = [
        raw + '"}',
        raw + '"}}',
        raw + '}',
        raw + '"}]}',
    ]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _initialize_stream_block(contentBlock: Mapping[str, Any]) -> dict[str, Any]:
    block = dict(contentBlock)
    blockType = block.get("type")
    if blockType == "text":
        block["text"] = ""
        return block
    if blockType == "tool_use":
        block["input"] = ""
        return block
    if blockType == "thinking":
        block["thinking"] = ""
        return block
    raise QueryStateTransitionError(
        f"Unsupported content_block_start type: {blockType!r}"
    )


def _apply_stream_delta(
    contentBlocks: dict[int, dict[str, Any]],
    part: Mapping[str, Any],
) -> None:
    index = int(part["index"])
    if index not in contentBlocks:
        raise QueryStateTransitionError("Content block not found")
    block = contentBlocks[index]
    delta = part.get("delta")
    if not isinstance(delta, Mapping):
        raise QueryStateTransitionError("content_block_delta requires delta payload")
    deltaType = delta.get("type")
    if deltaType == "text_delta":
        if block.get("type") != "text":
            raise QueryStateTransitionError("Content block is not a text block")
        block["text"] = f"{block.get('text', '')}{delta.get('text', '')}"
        return
    if deltaType == "input_json_delta":
        if block.get("type") != "tool_use":
            raise QueryStateTransitionError("Content block is not a input_json block")
        block["input"] = f"{block.get('input', '')}{delta.get('partial_json', '')}"
        return
    if deltaType == "thinking_delta":
        if block.get("type") != "thinking":
            raise QueryStateTransitionError("Content block is not a thinking block")
        block["thinking"] = f"{block.get('thinking', '')}{delta.get('thinking', '')}"
        return
    if deltaType == "signature_delta":
        if block.get("type") != "thinking":
            raise QueryStateTransitionError("Content block is not a thinking block")
        block["signature"] = f"{block.get('signature', '')}{delta.get('signature', '')}"
        return
    raise QueryStateTransitionError(
        f"Unsupported content_block_delta type: {deltaType!r}"
    )


def _request_id_from_partial_message(
    partialMessage: Optional[Mapping[str, Any]],
) -> Optional[str]:
    if partialMessage is None:
        return None
    raw = partialMessage.get("requestId", partialMessage.get("request_id"))
    return str(raw) if raw is not None else None


def _build_assistant_message_from_stream(
    partialMessage: Optional[Mapping[str, Any]],
    contentBlock: Mapping[str, Any],
) -> Optional[AssistantMessage]:
    if partialMessage is None:
        raise QueryStateTransitionError("Message not found")
    blockType = contentBlock.get("type")
    if blockType == "text":
        normalizedBlock: AssistantContentBlock = TextBlock(
            str(contentBlock.get("text", ""))
        )
    elif blockType == "tool_use":
        normalizedBlock = ToolUseBlock(
            id=str(contentBlock.get("id", "")),
            name=str(contentBlock.get("name", "")),
            input=_normalize_tool_input(contentBlock.get("input", "")),
            signature=str(contentBlock.get("signature", "")),
        )
    elif blockType == "thinking":
        normalizedBlock = ThinkingBlock(
            thinking=str(contentBlock.get("thinking", "")),
            signature=str(contentBlock.get("signature", "")),
        )
    else:
        raise QueryStateTransitionError(
            f"Unsupported content block type at stop: {blockType!r}"
        )

    usage = _merge_mapping(_default_usage(), partialMessage.get("usage"))
    return AssistantMessage(
        message=AssistantPayload(
            content=(normalizedBlock,),
            id=str(partialMessage.get("id", _uuid())),
            container=partialMessage.get("container"),
            model=str(partialMessage.get("model", SYNTHETIC_MODEL)),
            role=str(partialMessage.get("role", "assistant")),
            stop_reason=str(partialMessage.get("stop_reason", "stop_sequence")),
            stop_sequence=str(partialMessage.get("stop_sequence", "")),
            type=str(partialMessage.get("type", "message")),
            usage=usage,
            context_management=partialMessage.get("context_management"),
        ),
        signature=(
            str(partialMessage.get("signature"))
            if isinstance(partialMessage.get("signature"), str)
            else None
        ),
        requestId=_request_id_from_partial_message(partialMessage),
    )


def _assistant_message_has_visible_output(message: AssistantMessage) -> bool:
    return any(
        isinstance(block, (TextBlock, ToolUseBlock))
        for block in message.message.content
    )


def _apply_message_delta(
    assistantMessages: Sequence[AssistantMessage],
    part: Mapping[str, Any],
) -> None:
    if not assistantMessages:
        return
    lastMessage = assistantMessages[-1]
    usageDelta = part.get("usage")
    if isinstance(usageDelta, Mapping):
        lastMessage.message.usage = _merge_mapping(
            lastMessage.message.usage, usageDelta
        )
    delta = part.get("delta")
    if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
        lastMessage.message.stop_reason = str(delta["stop_reason"])


def _record_tool_update(
    update: ToolExecutionUpdate,
    outputs: list[QueryOutput],
    toolResults: list[Union[UserMessage, AttachmentMessage]],
    currentContext: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    updatedContext = currentContext
    if update.newContext is not None:
        updatedContext = update.newContext
    if update.message is not None:
        outputs.append(update.message)
        if isinstance(update.message, (UserMessage, AttachmentMessage)):
            toolResults.append(update.message)
    return updatedContext


def replayQueryTurn(
    *,
    parts: Sequence[Mapping[str, Any]],
    toolUseContext: Optional[Mapping[str, Any]] = None,
    scheduledToolUpdates: Optional[
        Mapping[Tuple[int, str], Sequence[ToolExecutionUpdate]]
    ] = None,
    remainingToolUpdates: Sequence[ToolExecutionUpdate] = (),
    postStreamToolUpdates: Sequence[ToolExecutionUpdate] = (),
    abortAfter: Optional[Tuple[int, str]] = None,
    abortReason: Optional[str] = None,
    abortDuringToolExecution: Optional[str] = None,
    turnCount: int = 1,
    maxTurns: Optional[int] = None,
) -> QueryTurnReplay:
    if turnCount < 1:
        raise QueryStateTransitionError("turnCount must be >= 1")

    outputs: list[QueryOutput] = [createStreamRequestStartEvent()]
    assistantMessages: list[AssistantMessage] = []
    toolResults: list[Union[UserMessage, AttachmentMessage]] = []
    contentBlocks: dict[int, dict[str, Any]] = {}
    partialMessage: Optional[Mapping[str, Any]] = None
    scheduled = dict(scheduledToolUpdates or {})
    updatedToolUseContext: Optional[Mapping[str, Any]] = toolUseContext
    needsFollowUp = False
    streamAborted = False

    for partIndex, part in enumerate(parts):
        partType = part.get("type")
        if partType == "message_start":
            rawMessage = part.get("message")
            if not isinstance(rawMessage, Mapping):
                raise QueryStateTransitionError(
                    "message_start requires message payload"
                )
            partialMessage = dict(rawMessage)
        elif partType == "content_block_start":
            rawBlock = part.get("content_block")
            if not isinstance(rawBlock, Mapping):
                raise QueryStateTransitionError(
                    "content_block_start requires content_block payload"
                )
            contentBlocks[int(part["index"])] = _initialize_stream_block(rawBlock)
        elif partType == "content_block_delta":
            _apply_stream_delta(contentBlocks, part)
        elif partType == "content_block_stop":
            blockIndex = int(part["index"])
            if blockIndex not in contentBlocks:
                raise QueryStateTransitionError("Content block not found")
            assistant = _build_assistant_message_from_stream(
                partialMessage,
                contentBlocks[blockIndex],
            )
            assistantMessages.append(assistant)
            if _assistant_message_has_visible_output(assistant):
                outputs.append(assistant)
            if any(
                isinstance(block, ToolUseBlock) for block in assistant.message.content
            ):
                needsFollowUp = True
            for update in scheduled.get((partIndex, STREAM_UPDATE_AFTER_ASSISTANT), ()):
                updatedToolUseContext = _record_tool_update(
                    update,
                    outputs,
                    toolResults,
                    updatedToolUseContext,
                )
            if abortAfter == (partIndex, STREAM_UPDATE_AFTER_ASSISTANT):
                streamAborted = True
                break
        elif partType == "message_delta":
            _apply_message_delta(assistantMessages, part)
        elif partType == "message_stop":
            pass
        else:
            raise QueryStateTransitionError(
                f"Unsupported stream event type: {partType!r}"
            )

        outputs.append(
            createStreamEvent(
                part,
                ttftMs=int(part["ttftMs"])
                if partType == "message_start" and "ttftMs" in part
                else None,
            )
        )
        for update in scheduled.get((partIndex, STREAM_UPDATE_AFTER_STREAM_EVENT), ()):
            updatedToolUseContext = _record_tool_update(
                update,
                outputs,
                toolResults,
                updatedToolUseContext,
            )
        if abortAfter == (partIndex, STREAM_UPDATE_AFTER_STREAM_EVENT):
            streamAborted = True
            break

    if streamAborted:
        interruptedUpdates = list(remainingToolUpdates)
        if not interruptedUpdates:
            interruptedUpdates = [
                ToolExecutionUpdate(message=message)
                for message in yieldMissingToolResultBlocks(
                    assistantMessages,
                    "Interrupted by user",
                )
            ]
        for update in interruptedUpdates:
            updatedToolUseContext = _record_tool_update(
                update,
                outputs,
                toolResults,
                updatedToolUseContext,
            )
        if abortReason != "interrupt":
            outputs.append(createUserInterruptionMessage(toolUse=False))
        return QueryTurnReplay(
            outputs=tuple(outputs),
            assistantMessages=tuple(assistantMessages),
            toolResults=tuple(toolResults),
            needsFollowUp=needsFollowUp,
            terminal=TerminalTransition(reason=TerminalReason.ABORTED_STREAMING),
            updatedToolUseContext=updatedToolUseContext,
        )

    if not needsFollowUp:
        return QueryTurnReplay(
            outputs=tuple(outputs),
            assistantMessages=tuple(assistantMessages),
            toolResults=tuple(toolResults),
            needsFollowUp=False,
            terminal=TerminalTransition(reason=TerminalReason.COMPLETED),
            updatedToolUseContext=updatedToolUseContext,
        )

    for update in postStreamToolUpdates:
        updatedToolUseContext = _record_tool_update(
            update,
            outputs,
            toolResults,
            updatedToolUseContext,
        )

    if abortDuringToolExecution is not None:
        if abortDuringToolExecution != "interrupt":
            outputs.append(createUserInterruptionMessage(toolUse=True))
        nextTurnCount = turnCount + 1
        if maxTurns is not None and nextTurnCount > maxTurns:
            outputs.append(
                createAttachmentMessage(
                    {
                        "type": "max_turns_reached",
                        "maxTurns": maxTurns,
                        "turnCount": nextTurnCount,
                    }
                )
            )
        return QueryTurnReplay(
            outputs=tuple(outputs),
            assistantMessages=tuple(assistantMessages),
            toolResults=tuple(toolResults),
            needsFollowUp=True,
            terminal=TerminalTransition(reason=TerminalReason.ABORTED_TOOLS),
            updatedToolUseContext=updatedToolUseContext,
        )

    nextTurnCount = turnCount + 1
    if maxTurns is not None and nextTurnCount > maxTurns:
        outputs.append(
            createAttachmentMessage(
                {
                    "type": "max_turns_reached",
                    "maxTurns": maxTurns,
                    "turnCount": nextTurnCount,
                }
            )
        )
        return QueryTurnReplay(
            outputs=tuple(outputs),
            assistantMessages=tuple(assistantMessages),
            toolResults=tuple(toolResults),
            needsFollowUp=True,
            terminal=TerminalTransition(
                reason=TerminalReason.MAX_TURNS,
                turnCount=nextTurnCount,
            ),
            updatedToolUseContext=updatedToolUseContext,
        )

    return QueryTurnReplay(
        outputs=tuple(outputs),
        assistantMessages=tuple(assistantMessages),
        toolResults=tuple(toolResults),
        needsFollowUp=True,
        continuation=ContinueTransition(reason=ContinueReason.NEXT_TURN),
        updatedToolUseContext=updatedToolUseContext,
    )


def _output_signature(outputs: Sequence[QueryOutput]) -> Tuple[str, ...]:
    signature: list[str] = []
    for item in outputs:
        if isinstance(item, RequestStartEvent):
            signature.append(item.type)
        elif isinstance(item, StreamEvent):
            signature.append(f"stream_event:{item.event.get('type')}")
        elif isinstance(item, AssistantMessage):
            first = item.message.content[0]
            signature.append(f"assistant:{first.type}")
        elif isinstance(item, UserMessage):
            content = item.message.content
            if (
                isinstance(content, tuple)
                and content
                and isinstance(content[0], ToolResultBlock)
            ):
                signature.append("user:tool_result")
            elif (
                isinstance(content, tuple)
                and content
                and isinstance(content[0], TextBlock)
            ):
                signature.append("user:text")
            else:
                signature.append("user:string")
        elif isinstance(item, CompactBoundaryMessage):
            signature.append(f"system:{item.subtype}")
        elif isinstance(item, SystemInformationalMessage):
            signature.append(f"system:{item.subtype}")
        elif isinstance(item, ToolUseSummaryMessage):
            signature.append(item.type)
        elif isinstance(item, ProgressMessage):
            signature.append(item.type)
        elif isinstance(item, AttachmentMessage):
            signature.append(f"attachment:{item.attachment.get('type')}")
        elif isinstance(item, TombstoneMessage):
            signature.append(item.type)
        else:
            signature.append(type(item).__name__)
    return tuple(signature)


def validateQueryStreamingContract() -> Tuple[bool, Tuple[str, ...]]:
    errors: list[str] = []

    normal = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "ttftMs": 17,
                "message": {
                    "id": "msg-normal",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 11, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "prefill ignored"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": " there"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command": "pwd"',
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "}"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "usage": {"output_tokens": 7},
                "delta": {"stop_reason": "tool_use"},
            },
            {"type": "message_stop"},
        ),
        scheduledToolUpdates={
            (8, STREAM_UPDATE_AFTER_ASSISTANT): (
                ToolExecutionUpdate(
                    message=createUserMessage(
                        content=(
                            ToolResultBlock(
                                tool_use_id="tool-1",
                                content="/tmp/workspace",
                            ),
                        ),
                        toolUseResult="/tmp/workspace",
                    )
                ),
            )
        },
    )
    expectedNormal = (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "stream_event:content_block_delta",
        "assistant:text",
        "stream_event:content_block_stop",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "user:tool_result",
        "stream_event:content_block_stop",
        "stream_event:message_delta",
        "stream_event:message_stop",
    )
    if _output_signature(normal.outputs) != expectedNormal:
        errors.append(
            "normal sequence mismatch: "
            f"expected {expectedNormal}, got {_output_signature(normal.outputs)}"
        )
    if (
        normal.continuation is None
        or normal.continuation.reason != ContinueReason.NEXT_TURN
    ):
        errors.append("normal stream should continue with next_turn")
    firstNormalBlock = normal.assistantMessages[0].message.content[0]
    if (
        not isinstance(firstNormalBlock, TextBlock)
        or firstNormalBlock.text != "hello there"
    ):
        errors.append("text deltas must preserve their original chunk order")
    if normal.assistantMessages[-1].message.stop_reason != "tool_use":
        errors.append("message_delta must update the last assistant stop_reason")

    interrupted = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-interrupt",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-interrupt",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"file_path": "src/query.ts"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
        ),
        abortAfter=(3, STREAM_UPDATE_AFTER_ASSISTANT),
        abortReason="abort",
    )
    expectedInterrupted = (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "user:tool_result",
        "user:text",
    )
    if _output_signature(interrupted.outputs) != expectedInterrupted:
        errors.append(
            "interrupted sequence mismatch: "
            f"expected {expectedInterrupted}, got {_output_signature(interrupted.outputs)}"
        )
    if (
        interrupted.terminal is None
        or interrupted.terminal.reason != TerminalReason.ABORTED_STREAMING
    ):
        errors.append("interrupted stream should terminate with aborted_streaming")

    submitInterrupt = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-submit-interrupt",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-submit-interrupt",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"file_path": "python_src/query.py"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
        ),
        abortAfter=(3, STREAM_UPDATE_AFTER_ASSISTANT),
        abortReason="interrupt",
    )
    if _output_signature(submitInterrupt.outputs) != (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "user:tool_result",
    ):
        errors.append("submit interrupt should skip the trailing user interruption")

    return (not errors, tuple(errors))


def validateCancelTimeoutCompactionContract() -> Tuple[bool, Tuple[str, ...]]:
    errors: list[str] = []

    compactionResult = CompactionResult(
        boundaryMarker=createCompactBoundaryMessage(
            trigger="auto_compact",
            originalTokenCount=240_000,
            newTokenCount=120_000,
            deletedToolUseIds=("tool-old",),
            preservedMessageUuids=("keep-1",),
        ),
        summaryMessages=(
            createUserMessage(
                content="Conversation compacted",
                isCompactSummary=True,
            ),
        ),
        messagesToKeep=(createAssistantMessage(content="Preserved tail"),),
        attachments=(
            createAttachmentMessage(
                {"type": "edited_text_file", "filePath": "src/query.ts"}
            ),
        ),
        hookResults=(createSystemMessage("post-compact hook", "info"),),
    )
    expectedPostCompact = (
        "system:compact_boundary",
        "user:string",
        "assistant:text",
        "attachment:edited_text_file",
        "system:informational",
    )
    postCompactMessages = buildPostCompactMessages(compactionResult)
    if _output_signature(postCompactMessages) != expectedPostCompact:
        errors.append(
            "post-compact message ordering mismatch: "
            f"expected {expectedPostCompact}, got {_output_signature(postCompactMessages)}"
        )

    proactiveCompaction = applySuccessfulCompaction(
        compactionResult=compactionResult,
        turnId="turn-compact-1",
    )
    if proactiveCompaction.tracking != {
        "compacted": True,
        "turnId": "turn-compact-1",
        "turnCounter": 0,
        "consecutiveFailures": 0,
    }:
        errors.append(
            "successful compaction must reset tracking for the most recent compact"
        )
    if proactiveCompaction.messagesForQuery != postCompactMessages:
        errors.append("successful compaction must continue with post-compact messages")

    compactionState = QueryLoopState(
        messages=(createUserMessage(content="before compact"),),
        toolUseContext={"messages": []},
        autoCompactTracking={"compacted": False, "turnId": "old", "turnCounter": 7},
        maxOutputTokensRecoveryCount=2,
        hasAttemptedReactiveCompact=False,
        maxOutputTokensOverride=64_000,
        pendingToolUseSummary="pending-summary",
        stopHookActive=True,
        turnCount=3,
    )
    collapseDrain = continueAfterCollapseDrainRetry(
        state=compactionState,
        drainedMessages=postCompactMessages,
        toolUseContext={"messages": list(postCompactMessages)},
        tracking={"compacted": False, "turnId": "old", "turnCounter": 7},
        committed=2,
    )
    if collapseDrain.nextState is None:
        errors.append("collapse drain retry must return a next state")
    else:
        if collapseDrain.nextState.transition is None or (
            collapseDrain.nextState.transition.reason
            != ContinueReason.COLLAPSE_DRAIN_RETRY
        ):
            errors.append("collapse drain must continue with collapse_drain_retry")
        elif collapseDrain.nextState.transition.committed != 2:
            errors.append("collapse drain retry must preserve committed count")
        if collapseDrain.nextState.maxOutputTokensOverride is not None:
            errors.append("collapse drain retry must clear maxOutputTokensOverride")
        if collapseDrain.nextState.pendingToolUseSummary is not None:
            errors.append("collapse drain retry must clear pending tool-use summary")
        if collapseDrain.nextState.stopHookActive is not None:
            errors.append("collapse drain retry must clear stop-hook activity")

    reactiveCompact = continueAfterReactiveCompactRetry(
        state=compactionState,
        compactionResult=compactionResult,
        toolUseContext={"messages": list(postCompactMessages)},
    )
    if _output_signature(reactiveCompact.emittedMessages) != expectedPostCompact:
        errors.append(
            "reactive compact retry must emit the same post-compact message ordering"
        )
    if reactiveCompact.nextState is None:
        errors.append("reactive compact retry must return a next state")
    else:
        if reactiveCompact.nextState.transition is None or (
            reactiveCompact.nextState.transition.reason
            != ContinueReason.REACTIVE_COMPACT_RETRY
        ):
            errors.append("reactive compact must continue with reactive_compact_retry")
        if reactiveCompact.nextState.autoCompactTracking is not None:
            errors.append("reactive compact retry must clear autoCompactTracking")
        if reactiveCompact.nextState.hasAttemptedReactiveCompact is not True:
            errors.append("reactive compact retry must record the reactive attempt")

    timeoutDuringStreaming = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-timeout-stream",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 4, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-timeout-stream",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"file_path": "src/history.ts"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
        ),
        abortAfter=(3, STREAM_UPDATE_AFTER_ASSISTANT),
        abortReason="timeout",
    )
    expectedStreamingTimeout = (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "user:tool_result",
        "user:text",
    )
    if _output_signature(timeoutDuringStreaming.outputs) != expectedStreamingTimeout:
        errors.append(
            "timeout during streaming must synthesize tool_result before interruption"
        )
    if timeoutDuringStreaming.terminal is None or (
        timeoutDuringStreaming.terminal.reason != TerminalReason.ABORTED_STREAMING
    ):
        errors.append("timeout during streaming must terminate with aborted_streaming")

    timeoutDuringTools = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-timeout-tools",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 8, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-timeout-tools",
                    "name": "Bash",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command": "sleep 30"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "usage": {"output_tokens": 5},
                "delta": {"stop_reason": "tool_use"},
            },
            {"type": "message_stop"},
        ),
        postStreamToolUpdates=(
            ToolExecutionUpdate(
                message=createUserMessage(
                    content=(
                        ToolResultBlock(
                            tool_use_id="tool-timeout-tools",
                            content="partial tool output",
                            is_error=True,
                        ),
                    ),
                    toolUseResult="partial tool output",
                ),
                newContext={"mode": "timed_out"},
            ),
        ),
        abortDuringToolExecution="timeout",
        turnCount=1,
        maxTurns=1,
    )
    expectedToolTimeout = (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "stream_event:content_block_stop",
        "stream_event:message_delta",
        "stream_event:message_stop",
        "user:tool_result",
        "user:text",
        "attachment:max_turns_reached",
    )
    if _output_signature(timeoutDuringTools.outputs) != expectedToolTimeout:
        errors.append(
            "timeout during tool execution must preserve updates before interruption/max_turns"
        )
    if timeoutDuringTools.terminal is None or (
        timeoutDuringTools.terminal.reason != TerminalReason.ABORTED_TOOLS
    ):
        errors.append("timeout during tool execution must terminate with aborted_tools")
    if timeoutDuringTools.updatedToolUseContext != {"mode": "timed_out"}:
        errors.append("tool-execution timeout must preserve the last newContext update")

    cancelDuringTools = replayQueryTurn(
        parts=(
            {
                "type": "message_start",
                "message": {
                    "id": "msg-cancel-tools",
                    "model": "claude-test",
                    "role": "assistant",
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": {"input_tokens": 6, "output_tokens": 0},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-cancel-tools",
                    "name": "Bash",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command": "sleep 30"}',
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "usage": {"output_tokens": 4},
                "delta": {"stop_reason": "tool_use"},
            },
            {"type": "message_stop"},
        ),
        postStreamToolUpdates=(
            ToolExecutionUpdate(
                message=createUserMessage(
                    content=(
                        ToolResultBlock(
                            tool_use_id="tool-cancel-tools",
                            content="partial tool output",
                        ),
                    ),
                    toolUseResult="partial tool output",
                )
            ),
        ),
        abortDuringToolExecution="interrupt",
        turnCount=1,
        maxTurns=3,
    )
    expectedToolCancel = (
        "stream_request_start",
        "stream_event:message_start",
        "stream_event:content_block_start",
        "stream_event:content_block_delta",
        "assistant:tool_use",
        "stream_event:content_block_stop",
        "stream_event:message_delta",
        "stream_event:message_stop",
        "user:tool_result",
    )
    if _output_signature(cancelDuringTools.outputs) != expectedToolCancel:
        errors.append(
            "interrupt during tool execution must skip the trailing interruption message"
        )
    if cancelDuringTools.terminal is None or (
        cancelDuringTools.terminal.reason != TerminalReason.ABORTED_TOOLS
    ):
        errors.append(
            "interrupt during tool execution must still terminate with aborted_tools"
        )

    historyState = PromptHistoryState()
    historyState = addPromptHistoryEntry(
        historyState,
        display="first command",
        timestamp=100,
        project="/repo",
        sessionId="session-a",
    )
    historyState = addPromptHistoryEntry(
        historyState,
        display="second command",
        timestamp=200,
        project="/repo",
        sessionId="session-a",
    )
    historyState = removeLastFromHistory(historyState)
    pendingHistory = getHistoryEntries(
        historyState,
        currentProject="/repo",
        currentSession="session-a",
    )
    if tuple(entry.display for entry in pendingHistory) != ("first command",):
        errors.append("removeLastFromHistory must pop the pending entry before flush")

    flushedHistory = flushPromptHistoryEntries(
        addPromptHistoryEntry(
            PromptHistoryState(),
            display="flushed command",
            timestamp=300,
            project="/repo",
            sessionId="session-a",
        )
    )
    flushedHistory = removeLastFromHistory(flushedHistory)
    visibleFlushed = getHistoryEntries(
        flushedHistory,
        currentProject="/repo",
        currentSession="session-a",
    )
    if visibleFlushed:
        errors.append(
            "removeLastFromHistory must skip the flushed entry for the active session"
        )

    session = QuerySession(
        sessionState=SessionState.REQUIRES_ACTION,
        externalMetadata=SessionExternalMetadata(
            pending_action=RequiresActionDetails(
                tool_name="Bash",
                action_description="sleep 30",
                tool_use_id="tool-timeout-tools",
                request_id="req-1",
            ),
            task_summary="Running tool",
        ),
    ).finishTurn(TerminalTransition(reason=TerminalReason.ABORTED_TOOLS))
    if session.sessionState != SessionState.IDLE:
        errors.append("finishing a timed-out turn must transition the session to idle")
    if session.externalMetadata.pending_action is not None:
        errors.append("finishing a timed-out turn must clear pending_action")
    if session.externalMetadata.task_summary is not None:
        errors.append("finishing a timed-out turn must clear task_summary at idle")

    return (not errors, tuple(errors))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--validate-query-streaming"]:
        passed, errors = validateQueryStreamingContract()
        print("Query streaming contract")
        print(f"Passed: {passed}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            print("STATUS: FAILED")
            return 1
        print("STATUS: PASSED")
        return 0
    if args == ["--validate-cancel-timeout-compaction"]:
        passed, errors = validateCancelTimeoutCompactionContract()
        print("Query control-flow contract")
        print(f"Passed: {passed}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            print("STATUS: FAILED")
            return 1
        print("STATUS: PASSED")
        return 0
    print(
        "Usage: python python_src/query.py --validate-query-streaming | --validate-cancel-timeout-compaction",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
