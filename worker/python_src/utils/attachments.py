"""Attachment facade helpers.

The core attachment message type lives in :mod:`python_src.query`; this module
collects the higher-level helpers used by the TS runtime into one Python entry
point so prompt assembly, memory surfacing, and UI deltas can share behavior.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..query import AttachmentMessage, createAttachmentMessage


TODO_REMINDER_CONFIG = {
    "id": "todo_reminder",
    "priority": "low",
    "title": "Todo reminder",
}
PLAN_MODE_ATTACHMENT_CONFIG = {
    "id": "plan_mode",
    "priority": "high",
    "title": "Plan mode reminder",
}
_MEMORY_FILENAMES = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    ".claude_py/memory.md",
    ".claude_py/CLAUDE.md",
)
_AT_MENTION_RE = re.compile(r"(?<!\S)@([^\s`'\"<>]+)")
_PREFETCH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-prefetch")


@dataclass(frozen=True)
class FileAttachment:
    path: str
    content: str
    exists: bool
    attachment_type: str = "file"

    def as_attachment(self) -> AttachmentMessage:
        return createAttachmentMessage(
            {
                "type": self.attachment_type,
                "path": self.path,
                "content": self.content,
                "exists": self.exists,
            }
        )


@dataclass(frozen=True)
class MemoryPrefetchHandle:
    future: Future[tuple[AttachmentMessage, ...]]

    def result(self, timeout: float | None = None) -> tuple[AttachmentMessage, ...]:
        return self.future.result(timeout=timeout)


def extract_at_mentioned_files(text: str, *, cwd: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    if not isinstance(text, str) or not text:
        return ()
    base = Path(cwd or os.getcwd())
    paths: list[str] = []
    for match in _AT_MENTION_RE.finditer(text):
        raw = match.group(1).rstrip(".,;:)")
        if not raw:
            continue
        path = Path(os.path.expanduser(raw))
        normalized = str(path if path.is_absolute() else base / path)
        if normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


def extractAtMentionedFiles(text: str, cwd: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    return extract_at_mentioned_files(text, cwd=cwd)


def collect_surfaced_memories(
    *,
    cwd: str | os.PathLike[str] | None = None,
    query: str | None = None,
    limit: int = 5,
) -> tuple[AttachmentMessage, ...]:
    base = Path(cwd or os.getcwd())
    query_terms = {
        term.casefold()
        for term in re.findall(r"[A-Za-z0-9_\-/]{3,}", query or "")
        if len(term) >= 3
    }
    memories: list[AttachmentMessage] = []
    for filename in _MEMORY_FILENAMES:
        path = base / filename
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = _select_memory_excerpt(content, query_terms=query_terms)
        if not excerpt:
            continue
        memories.append(
            createAttachmentMessage(
                {
                    "type": "memory",
                    "source": str(path),
                    "content": excerpt,
                    "query": query or "",
                }
            )
        )
        if len(memories) >= max(limit, 0):
            break
    return tuple(memories)


def collectSurfacedMemories(
    cwd: str | os.PathLike[str] | None = None,
    query: str | None = None,
    limit: int = 5,
) -> tuple[AttachmentMessage, ...]:
    return collect_surfaced_memories(cwd=cwd, query=query, limit=limit)


def start_relevant_memory_prefetch(
    prompt: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    limit: int = 5,
) -> MemoryPrefetchHandle:
    future = _PREFETCH_EXECUTOR.submit(
        collect_surfaced_memories,
        cwd=cwd,
        query=prompt,
        limit=limit,
    )
    return MemoryPrefetchHandle(future=future)


def startRelevantMemoryPrefetch(
    prompt: str,
    cwd: str | os.PathLike[str] | None = None,
    limit: int = 5,
) -> MemoryPrefetchHandle:
    return start_relevant_memory_prefetch(prompt, cwd=cwd, limit=limit)


def get_context_efficiency_attachment(
    *,
    input_tokens: int,
    max_context_tokens: int,
    message_count: int | None = None,
) -> AttachmentMessage | None:
    if max_context_tokens <= 0:
        return None
    ratio = max(0.0, input_tokens / max_context_tokens)
    if ratio < 0.72:
        return None
    return createAttachmentMessage(
        {
            "type": "context_efficiency",
            "input_tokens": input_tokens,
            "max_context_tokens": max_context_tokens,
            "usage_ratio": round(ratio, 4),
            "message_count": message_count,
            "severity": "high" if ratio >= 0.9 else "medium",
        }
    )


def getContextEfficiencyAttachment(
    input_tokens: int,
    max_context_tokens: int,
    message_count: int | None = None,
) -> AttachmentMessage | None:
    return get_context_efficiency_attachment(
        input_tokens=input_tokens,
        max_context_tokens=max_context_tokens,
        message_count=message_count,
    )


def get_compaction_reminder_attachment(
    *,
    compaction_count: int,
    original_tokens: int | None = None,
    new_tokens: int | None = None,
) -> AttachmentMessage:
    return createAttachmentMessage(
        {
            "type": "compaction_reminder",
            "compaction_count": max(compaction_count, 0),
            "original_tokens": original_tokens,
            "new_tokens": new_tokens,
        }
    )


def getCompactionReminderAttachment(
    compaction_count: int,
    original_tokens: int | None = None,
    new_tokens: int | None = None,
) -> AttachmentMessage:
    return get_compaction_reminder_attachment(
        compaction_count=compaction_count,
        original_tokens=original_tokens,
        new_tokens=new_tokens,
    )


def get_deferred_tools_delta_attachment(
    previous_tools: Sequence[str],
    current_tools: Sequence[str],
) -> AttachmentMessage | None:
    previous = set(previous_tools)
    current = set(current_tools)
    added = sorted(current - previous)
    removed = sorted(previous - current)
    if not added and not removed:
        return None
    return createAttachmentMessage(
        {"type": "deferred_tools_delta", "added": added, "removed": removed}
    )


def getDeferredToolsDeltaAttachment(
    previous_tools: Sequence[str],
    current_tools: Sequence[str],
) -> AttachmentMessage | None:
    return get_deferred_tools_delta_attachment(previous_tools, current_tools)


def get_agent_listing_delta_attachment(
    previous_agents: Sequence[str],
    current_agents: Sequence[str],
) -> AttachmentMessage | None:
    previous = set(previous_agents)
    current = set(current_agents)
    added = sorted(current - previous)
    removed = sorted(previous - current)
    if not added and not removed:
        return None
    return createAttachmentMessage(
        {"type": "agent_listing_delta", "added": added, "removed": removed}
    )


def getAgentListingDeltaAttachment(
    previous_agents: Sequence[str],
    current_agents: Sequence[str],
) -> AttachmentMessage | None:
    return get_agent_listing_delta_attachment(previous_agents, current_agents)


def get_mcp_instructions_delta_attachment(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> AttachmentMessage | None:
    changed: dict[str, dict[str, str | None]] = {}
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            changed[key] = {"previous": before, "current": after}
    if not changed:
        return None
    return createAttachmentMessage({"type": "mcp_instructions_delta", "changed": changed})


def getMcpInstructionsDeltaAttachment(
    previous: Mapping[str, str],
    current: Mapping[str, str],
) -> AttachmentMessage | None:
    return get_mcp_instructions_delta_attachment(previous, current)


def try_get_pdf_reference(path: str | os.PathLike[str]) -> AttachmentMessage | None:
    resolved = Path(path)
    if resolved.suffix.casefold() != ".pdf" or not resolved.exists() or not resolved.is_file():
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        size = None
    return createAttachmentMessage(
        {"type": "pdf_reference", "path": str(resolved), "size": size}
    )


def tryGetPDFReference(path: str | os.PathLike[str]) -> AttachmentMessage | None:
    return try_get_pdf_reference(path)


def get_attachments(
    prompt: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    include_memory: bool = True,
) -> tuple[AttachmentMessage, ...]:
    attachments: list[AttachmentMessage] = []
    for path in extract_at_mentioned_files(prompt, cwd=cwd):
        attachments.append(_file_reference_attachment(path))
    if include_memory:
        attachments.extend(collect_surfaced_memories(cwd=cwd, query=prompt))
    return tuple(attachments)


def getAttachments(
    prompt: str,
    cwd: str | os.PathLike[str] | None = None,
    include_memory: bool = True,
) -> tuple[AttachmentMessage, ...]:
    return get_attachments(prompt, cwd=cwd, include_memory=include_memory)


def _select_memory_excerpt(content: str, *, query_terms: set[str], max_chars: int = 2_400) -> str:
    normalized = content.strip()
    if not normalized:
        return ""
    if not query_terms:
        return normalized[:max_chars].rstrip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    scored: list[tuple[int, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        lowered = paragraph.casefold()
        score = sum(1 for term in query_terms if term in lowered)
        if score:
            scored.append((score, -index, paragraph))
    if not scored:
        return normalized[:max_chars].rstrip()
    selected = "\n\n".join(item[2] for item in sorted(scored, reverse=True)[:3])
    return selected[:max_chars].rstrip()


def _file_reference_attachment(path: str) -> AttachmentMessage:
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        return createAttachmentMessage({"type": "file_reference", "path": path, "exists": False})
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return createAttachmentMessage(
            {"type": "file_reference", "path": path, "exists": True, "error": str(exc)}
        )
    if len(content) > 24_000:
        content = content[:24_000].rstrip() + "\n...[truncated]"
    return createAttachmentMessage(
        {"type": "file_reference", "path": path, "exists": True, "content": content}
    )
