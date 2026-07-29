"""Real-time memory extraction service.

The implementation is deterministic and local: it extracts explicit
"remember" statements from recent conversation text, queues them, and can write
them to a configured memory file.  It complements AutoDream, which performs
periodic cross-session consolidation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ...query import AssistantMessage, Message, TextBlock, ToolResultBlock, UserMessage


_EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"\bremember(?:\s+that)?\s+(?P<fact>[^.!?\n]{8,240})", re.IGNORECASE),
    re.compile(r"\bplease\s+remember\s+(?P<fact>[^.!?\n]{8,240})", re.IGNORECASE),
    re.compile(r"(?:请|帮我)?记住[:：]?\s*(?P<fact>[^。！？\n]{4,240})"),
    re.compile(r"我的(?P<fact>[^。！？\n]{2,80}(?:是|为)[^。！？\n]{1,160})"),
)
_MAX_FACTS_PER_RUN = 12


@dataclass(frozen=True)
class ExtractMemoriesConfig:
    enabled: bool = True
    memory_file_path: str | None = None
    max_pending: int = 50
    append_to_file: bool = True


@dataclass
class ExtractMemoriesState:
    config: ExtractMemoriesConfig = field(default_factory=ExtractMemoriesConfig)
    pending: list[str] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)


def init_extract_memories(config: ExtractMemoriesConfig | Mapping[str, Any] | None = None) -> ExtractMemoriesState:
    return ExtractMemoriesState(config=_coerce_config(config))


def initExtractMemories(config: ExtractMemoriesConfig | Mapping[str, Any] | None = None) -> ExtractMemoriesState:
    return init_extract_memories(config)


def execute_extract_memories(
    state: ExtractMemoriesState,
    messages: Sequence[Message],
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    if not state.config.enabled:
        return ()
    extracted: list[str] = []
    for message in messages:
        if not isinstance(message, UserMessage):
            continue
        for fact in _extract_facts_from_text(_message_text(message)):
            key = fact.casefold()
            if key in state.seen:
                continue
            state.seen.add(key)
            state.pending.append(fact)
            extracted.append(fact)
            if len(extracted) >= _MAX_FACTS_PER_RUN:
                break
        if len(extracted) >= _MAX_FACTS_PER_RUN:
            break
    if state.config.max_pending >= 0 and len(state.pending) > state.config.max_pending:
        del state.pending[: len(state.pending) - state.config.max_pending]
    if extracted and state.config.append_to_file and state.config.memory_file_path:
        _append_memory_file(state.config.memory_file_path, extracted, cwd=cwd)
    return tuple(extracted)


def executeExtractMemories(
    state: ExtractMemoriesState,
    messages: Sequence[Message],
    cwd: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    return execute_extract_memories(state, messages, cwd=cwd)


def drain_pending_extraction(state: ExtractMemoriesState) -> tuple[str, ...]:
    drained = tuple(state.pending)
    state.pending.clear()
    return drained


def drainPendingExtraction(state: ExtractMemoriesState) -> tuple[str, ...]:
    return drain_pending_extraction(state)


def create_auto_mem_can_use_tool(
    *,
    allowed_tools: Sequence[str] = ("Read", "Write", "Edit", "MultiEdit"),
) -> Callable[[str, Mapping[str, Any] | None], bool]:
    allowed = frozenset(allowed_tools)

    def can_use_tool(tool_name: str, tool_input: Mapping[str, Any] | None = None) -> bool:
        if tool_name not in allowed:
            return False
        if tool_name in {"Write", "Edit", "MultiEdit"} and isinstance(tool_input, Mapping):
            path = tool_input.get("file_path") or tool_input.get("path")
            if isinstance(path, str) and _looks_sensitive_path(path):
                return False
        return True

    return can_use_tool


def createAutoMemCanUseTool(
    allowed_tools: Sequence[str] = ("Read", "Write", "Edit", "MultiEdit"),
) -> Callable[[str, Mapping[str, Any] | None], bool]:
    return create_auto_mem_can_use_tool(allowed_tools=allowed_tools)


def _coerce_config(config: ExtractMemoriesConfig | Mapping[str, Any] | None) -> ExtractMemoriesConfig:
    if config is None:
        return ExtractMemoriesConfig()
    if isinstance(config, ExtractMemoriesConfig):
        return config
    return ExtractMemoriesConfig(
        enabled=bool(config.get("enabled", True)),
        memory_file_path=config.get("memory_file_path") or config.get("memoryFilePath"),
        max_pending=int(config.get("max_pending") or config.get("maxPending") or 50),
        append_to_file=bool(config.get("append_to_file", config.get("appendToFile", True))),
    )


def _extract_facts_from_text(text: str) -> tuple[str, ...]:
    facts: list[str] = []
    seen: set[str] = set()
    for pattern in _EXPLICIT_MEMORY_PATTERNS:
        for match in pattern.finditer(text):
            fact = " ".join(match.group("fact").strip(" ：:，,;；").split())
            fact = re.sub(r"^that\s+", "", fact, flags=re.IGNORECASE)
            if len(fact) < 4:
                continue
            key = fact.casefold()
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    return tuple(facts)


def _message_text(message: Message) -> str:
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(str(block.content))
        return "\n".join(parts)
    if isinstance(message, AssistantMessage):
        return "\n".join(
            block.text for block in message.message.content if isinstance(block, TextBlock)
        )
    return ""


def _append_memory_file(
    memory_file_path: str,
    facts: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
) -> None:
    path = Path(os.path.expanduser(memory_file_path))
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
    with path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for fact in facts:
            line = f"- {fact}"
            if line not in existing:
                handle.write(line + "\n")


def _looks_sensitive_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    return any(
        marker in normalized
        for marker in (
            "/.git/",
            "/.ssh/",
            ".env",
            "id_rsa",
            "id_ed25519",
        )
    )
