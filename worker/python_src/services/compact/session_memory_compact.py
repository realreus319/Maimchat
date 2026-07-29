"""Session-memory aware partial compaction.

This module keeps the public shape of the TS ``sessionMemoryCompact`` helper
while using the Python port's existing ``SessionMemory`` metadata as the
summary source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ...query import (
    AssistantMessage,
    CompactBoundaryMessage,
    Message,
    SessionMemory,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    createCompactBoundaryMessage,
    createUserMessage,
    initSessionMemory,
)


EstimateTokens = Callable[[Sequence[Message]], int]


@dataclass(frozen=True)
class SessionMemoryCompactConfig:
    # Aligned with TypeScript DEFAULT_SM_COMPACT_CONFIG in sessionMemoryCompact.ts
    min_tokens: int = 10_000
    min_text_block_messages: int = 5
    max_tokens: int = 40_000
    keep_tail_messages: int = 8
    trigger: str = "session_memory_compact"


def calculate_messages_to_keep_index(
    messages: Sequence[Message],
    config: SessionMemoryCompactConfig | None = None,
    *,
    estimate_tokens: EstimateTokens | None = None,
) -> int | None:
    cfg = config or SessionMemoryCompactConfig()
    if not messages:
        return None
    total_tokens = (estimate_tokens or estimate_message_tokens)(messages)
    text_message_count = sum(1 for message in messages if isinstance(message, (UserMessage, AssistantMessage)))
    if total_tokens < cfg.min_tokens or text_message_count < cfg.min_text_block_messages:
        return None

    target_index = max(0, len(messages) - max(cfg.keep_tail_messages, 1))
    while target_index > 0:
        kept_tokens = (estimate_tokens or estimate_message_tokens)(messages[target_index:])
        if kept_tokens <= cfg.max_tokens:
            break
        target_index += 1
        if target_index >= len(messages):
            return None
    return adjust_index_to_preserve_api_invariants(messages, target_index)


def adjust_index_to_preserve_api_invariants(
    messages: Sequence[Message],
    index: int,
) -> int:
    if index <= 0:
        return 0
    if index >= len(messages):
        return len(messages)

    adjusted = index
    first_kept = messages[adjusted]
    if isinstance(first_kept, UserMessage):
        result_ids = _tool_result_ids(first_kept)
        if result_ids:
            assistant_index = _find_assistant_with_tool_use(messages, result_ids, before=adjusted)
            if assistant_index is not None:
                adjusted = assistant_index

    deleted_tool_ids = {
        block.id
        for message in messages[:adjusted]
        if isinstance(message, AssistantMessage)
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    }
    if deleted_tool_ids:
        for kept_offset, message in enumerate(messages[adjusted:], start=adjusted):
            if isinstance(message, UserMessage) and deleted_tool_ids.intersection(_tool_result_ids(message)):
                assistant_index = _find_assistant_with_tool_use(
                    messages,
                    deleted_tool_ids.intersection(_tool_result_ids(message)),
                    before=kept_offset,
                )
                if assistant_index is not None:
                    adjusted = min(adjusted, assistant_index)
                break
    return adjusted


def try_session_memory_compaction(
    messages: Sequence[Message],
    *,
    session_memory: SessionMemory | None = None,
    config: SessionMemoryCompactConfig | None = None,
    estimate_tokens: EstimateTokens | None = None,
) -> tuple[Message, ...] | None:
    cfg = config or SessionMemoryCompactConfig()
    keep_index = calculate_messages_to_keep_index(
        messages,
        cfg,
        estimate_tokens=estimate_tokens,
    )
    if keep_index is None or keep_index <= 0 or keep_index >= len(messages):
        return None

    removed = tuple(messages[:keep_index])
    preserved_tail = tuple(messages[keep_index:])
    memory = session_memory or initSessionMemory(messages)
    if memory is None:
        return None

    preserved_uuids = tuple(
        message.uuid
        for message in preserved_tail
        if isinstance(message, (AssistantMessage, UserMessage, CompactBoundaryMessage))
    )
    deleted_tool_ids = tuple(
        block.id
        for message in removed
        if isinstance(message, AssistantMessage)
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    )
    original_token_count = (estimate_tokens or estimate_message_tokens)(messages)
    summary_message = _build_session_memory_summary_message(
        memory,
        removed_count=len(removed),
        preserved_message_uuids=preserved_uuids,
    )
    provisional = (
        createCompactBoundaryMessage(
            trigger=cfg.trigger,
            originalTokenCount=original_token_count,
            newTokenCount=0,
            deletedToolUseIds=deleted_tool_ids,
            preservedMessageUuids=preserved_uuids,
        ),
        summary_message,
        *preserved_tail,
    )
    new_token_count = (estimate_tokens or estimate_message_tokens)(provisional)
    return (
        createCompactBoundaryMessage(
            trigger=cfg.trigger,
            originalTokenCount=original_token_count,
            newTokenCount=new_token_count,
            deletedToolUseIds=deleted_tool_ids,
            preservedMessageUuids=preserved_uuids,
        ),
        summary_message,
        *preserved_tail,
    )


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    characters = 0
    for message in messages:
        characters += len(_message_text(message))
    return max(1, characters // 4)


def _build_session_memory_summary_message(
    memory: SessionMemory,
    *,
    removed_count: int,
    preserved_message_uuids: Sequence[str],
) -> UserMessage:
    sections = ["# Session memory summary"]
    if memory.summary:
        sections.append(memory.summary)
    _append_section(sections, "User requests", memory.user_requests)
    _append_section(sections, "Decisions", memory.decisions)
    _append_section(sections, "Tool activity", memory.tool_activity)
    _append_section(sections, "Files", memory.files)
    _append_section(sections, "Open questions", memory.open_questions)
    content = "\n\n".join(sections)
    metadata = {
        "summary": memory.summary or content,
        "summarySource": memory.summary_source or "session_memory",
        "summaryOrigin": memory.summary_origin or "session_memory_compact",
        "snippedMessages": removed_count,
        "preservedMessageUuids": tuple(preserved_message_uuids),
        "userRequests": tuple(memory.user_requests),
        "decisions": tuple(memory.decisions),
        "toolActivity": tuple(memory.tool_activity),
        "files": tuple(memory.files),
        "openQuestions": tuple(memory.open_questions),
        "compactionCount": memory.compaction_count + 1,
    }
    return createUserMessage(
        content=content,
        isMeta=True,
        isCompactSummary=True,
        summarizeMetadata=metadata,
        origin="session_memory_compact",
    )


def _append_section(sections: list[str], title: str, values: Sequence[str]) -> None:
    if not values:
        return
    rendered = "\n".join(f"- {value}" for value in values)
    sections.append(f"## {title}\n{rendered}")


def _tool_result_ids(message: UserMessage) -> set[str]:
    content = message.message.content
    if isinstance(content, str):
        return set()
    return {
        block.tool_use_id
        for block in content
        if isinstance(block, ToolResultBlock) and isinstance(block.tool_use_id, str)
    }


def _find_assistant_with_tool_use(
    messages: Sequence[Message],
    tool_use_ids: set[str],
    *,
    before: int,
) -> int | None:
    for index in range(min(before - 1, len(messages) - 1), -1, -1):
        message = messages[index]
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolUseBlock) and block.id in tool_use_ids
            for block in message.message.content
        ):
            return index
    return None


def _message_text(message: Message) -> str:
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, str):
            return content
        return " ".join(
            str(block.content if isinstance(block, ToolResultBlock) else getattr(block, "text", ""))
            for block in content
        )
    if isinstance(message, AssistantMessage):
        return " ".join(
            getattr(block, "text", "")
            if not isinstance(block, ToolUseBlock)
            else f"{block.name} {block.input}"
            for block in message.message.content
        )
    return str(message)


# TS-compatible aliases.
calculateMessagesToKeepIndex = calculate_messages_to_keep_index
adjustIndexToPreserveAPIInvariants = adjust_index_to_preserve_api_invariants
trySessionMemoryCompaction = try_session_memory_compaction
