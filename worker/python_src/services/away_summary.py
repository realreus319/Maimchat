from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
import re
from typing import Any, Mapping, Sequence


_AGENT_EVENT_TYPES = {
    "agent_spawned",
    "agent_backgrounded",
    "agent_progress",
    "agent_completed",
    "agent_failed",
    "agent_killed",
}
_AGENT_TERMINAL_EVENT_TYPES = {"agent_completed", "agent_failed", "agent_killed"}
_TERMINAL_STATUSES = {"completed", "failed", "killed"}
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class AwaySummary:
    markdown: str
    latest_timestamp_ms: int


def parse_iso_timestamp_ms(value: str | None) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def generate_away_summary(
    *,
    task_events: Sequence[Mapping[str, Any]],
    agent_events: Sequence[Mapping[str, Any]],
    tasks: Mapping[str, Any],
    since_timestamp_ms: int | None,
) -> AwaySummary | None:
    recent_task_ids: set[str] = set()
    recent_agent_task_ids: set[str] = set()
    latest_timestamp_ms = since_timestamp_ms or 0

    completed_items: list[str] = []
    failed_items: list[str] = []
    running_items: list[str] = []

    for event in task_events:
        event_type = event.get("type")
        if event_type != "task_notification":
            continue
        timestamp_ms = _event_timestamp_ms(event)
        if timestamp_ms is None or not _is_newer_than(timestamp_ms, since_timestamp_ms):
            continue
        latest_timestamp_ms = max(latest_timestamp_ms, timestamp_ms)
        task_id = _string_field(event, "task_id")
        if task_id:
            recent_task_ids.add(task_id)
        summary = _task_notification_summary(event)
        if summary is None:
            continue
        status = _string_field(event, "status") or "completed"
        if status == "completed":
            completed_items.append(summary)
        else:
            failed_items.append(summary)

    for event in agent_events:
        event_type = event.get("type")
        if event_type not in _AGENT_EVENT_TYPES:
            continue
        timestamp_ms = _event_timestamp_ms(event)
        if timestamp_ms is None or not _is_newer_than(timestamp_ms, since_timestamp_ms):
            continue
        latest_timestamp_ms = max(latest_timestamp_ms, timestamp_ms)
        task_id = _string_field(event, "task_id")
        if task_id:
            recent_agent_task_ids.add(task_id)
        if event_type not in _AGENT_TERMINAL_EVENT_TYPES:
            continue
        detail = _agent_event_summary(event)
        if detail is None:
            continue
        if event_type == "agent_completed":
            completed_items.append(detail)
        else:
            failed_items.append(detail)

    for task_id in sorted(recent_task_ids | recent_agent_task_ids):
        task = tasks.get(task_id)
        detail = _running_task_summary(task)
        if detail is not None:
            running_items.append(detail)

    if not completed_items and not failed_items and not running_items:
        return None

    lines = ["### While You Were Away"]
    if completed_items:
        lines.append(f"- Completed: {len(completed_items)}")
    if failed_items:
        lines.append(f"- Failed: {len(failed_items)}")
    if running_items:
        lines.append(f"- Still running: {len(running_items)}")

    for item in completed_items + failed_items + running_items:
        lines.append(f"- {item}")

    return AwaySummary(
        markdown="\n".join(lines),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def _event_timestamp_ms(event: Mapping[str, Any]) -> int | None:
    value = event.get("timestamp_ms")
    if isinstance(value, int):
        return value
    return None


def _is_newer_than(timestamp_ms: int, since_timestamp_ms: int | None) -> bool:
    if since_timestamp_ms is None:
        return True
    return timestamp_ms > since_timestamp_ms


def _string_field(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _task_notification_summary(event: Mapping[str, Any]) -> str | None:
    xml = event.get("xml")
    if not isinstance(xml, str) or not xml.strip():
        return None
    match = _SUMMARY_RE.search(xml)
    if match is None:
        return None
    summary = " ".join(unescape(match.group(1)).split())
    return summary or None


def _agent_event_summary(event: Mapping[str, Any]) -> str | None:
    agent_id = _string_field(event, "agent_id") or "unknown-agent"
    summary_payload = event.get("summary")
    if isinstance(summary_payload, Mapping):
        headline = _string_field(summary_payload, "headline")
        output_preview = _string_field(summary_payload, "outputPreview")
        detail = f"Background agent `{agent_id}`"
        if headline:
            detail += f": {headline}"
        if output_preview:
            detail += f" — {output_preview}"
        return detail
    event_type = _string_field(event, "type") or "agent_failed"
    if event_type == "agent_killed":
        return f"Background agent `{agent_id}` killed"
    error = _string_field(event, "error")
    if error:
        return f"Background agent `{agent_id}` failed — {error}"
    return f"Background agent `{agent_id}` failed"


def _running_task_summary(task: Any) -> str | None:
    if task is None:
        return None
    is_backgrounded = getattr(task, "is_backgrounded", False) is True
    status = getattr(task, "status", None)
    if not is_backgrounded or status in _TERMINAL_STATUSES:
        return None
    description = _normalize_inline_text(getattr(task, "description", None))
    if hasattr(task, "agent_id"):
        agent_id = _normalize_inline_text(getattr(task, "agent_id", None)) or "unknown-agent"
        summary = f"Background agent `{agent_id}` still running"
        if description:
            summary += f" — {description}"
        return summary
    if description:
        return f'Background task "{description}" still running'
    return "Background task still running"


def _normalize_inline_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


__all__ = ["AwaySummary", "generate_away_summary", "parse_iso_timestamp_ms"]
