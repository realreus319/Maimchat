from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .config import get_claude_config_home

TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_COMPLETED = "completed"
TASK_DELETED = "deleted"
TASK_STATUSES = {TASK_PENDING, TASK_IN_PROGRESS, TASK_COMPLETED}


@dataclass(frozen=True)
class TaskRecord:
    id: str
    subject: str
    description: str = ""
    activeForm: str | None = None
    owner: str | None = None
    status: str = TASK_PENDING
    blocks: tuple[str, ...] = field(default_factory=tuple)
    blockedBy: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)


def is_todo_v2_enabled() -> bool:
    raw = os.environ.get("CLAUDE_CODE_ENABLE_TASKS")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes"}


def get_task_list_id() -> str:
    override = os.environ.get("CLAUDE_CODE_TASK_LIST_ID")
    if isinstance(override, str) and override.strip():
        return override.strip()
    team_name = os.environ.get("CLAUDE_CODE_TEAM_NAME")
    if isinstance(team_name, str) and team_name.strip():
        return team_name.strip()
    return "session"


def sanitize_path_component(value: str) -> str:
    return (
        "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)
        or "session"
    )


def get_tasks_dir() -> str:
    return os.path.join(
        get_claude_config_home(),
        "tasks",
        sanitize_path_component(get_task_list_id()),
    )


def create_task(
    *,
    subject: str,
    description: str = "",
    active_form: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TaskRecord:
    tasks_dir = get_tasks_dir()
    os.makedirs(tasks_dir, exist_ok=True)
    next_id = str(_next_task_number(tasks_dir))
    task = TaskRecord(
        id=next_id,
        subject=subject,
        description=description,
        activeForm=active_form,
        metadata=dict(metadata or {}),
    )
    _write_task(tasks_dir, task)
    _write_high_water_mark(tasks_dir, int(next_id))
    return task


def get_task(task_id: str) -> TaskRecord | None:
    task_path = _task_path(get_tasks_dir(), task_id)
    if not os.path.exists(task_path):
        return None
    return _read_task(task_path)


def list_tasks() -> list[TaskRecord]:
    tasks_dir = get_tasks_dir()
    if not os.path.isdir(tasks_dir):
        return []
    task_paths = [
        os.path.join(tasks_dir, name)
        for name in os.listdir(tasks_dir)
        if name.endswith(".json") and not name.startswith(".")
    ]
    task_paths.sort(key=lambda path: int(os.path.splitext(os.path.basename(path))[0]))
    return [_read_task(path) for path in task_paths]


def update_task(
    *,
    task_id: str,
    subject: str | None = None,
    description: str | None = None,
    active_form: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    add_blocks: list[str] | None = None,
    add_blocked_by: list[str] | None = None,
) -> tuple[TaskRecord | None, list[str], str | None]:
    tasks_dir = get_tasks_dir()
    normalized_task_id = _normalize_task_id(task_id)
    current = get_task(normalized_task_id)
    if current is None:
        return None, [], "Task not found"
    if status == TASK_DELETED:
        delete_task(normalized_task_id)
        return None, ["deleted"], None
    if status is not None and status not in TASK_STATUSES:
        return None, [], f"Invalid status: {status}"

    updated_fields: list[str] = []
    task = current
    if subject is not None and subject != task.subject:
        task = _replace_task(task, subject=subject)
        updated_fields.append("subject")
    if description is not None and description != task.description:
        task = _replace_task(task, description=description)
        updated_fields.append("description")
    if active_form is not None and active_form != task.activeForm:
        task = _replace_task(task, activeForm=active_form)
        updated_fields.append("activeForm")
    if owner is not None and owner != task.owner:
        task = _replace_task(task, owner=owner)
        updated_fields.append("owner")
    if status is not None and status != task.status:
        task = _replace_task(task, status=status)
        updated_fields.append("status")
    if metadata is not None:
        merged_metadata = dict(task.metadata)
        for key, value in metadata.items():
            if value is None:
                merged_metadata.pop(key, None)
            else:
                merged_metadata[key] = value
        if merged_metadata != task.metadata:
            task = _replace_task(task, metadata=merged_metadata)
            updated_fields.append("metadata")

    related_updates: dict[str, TaskRecord] = {}
    blocks_changed = False
    blocked_by_changed = False
    if add_blocks:
        normalized_block_ids = [_normalize_task_id(value) for value in add_blocks]
        related_blocked_tasks: dict[str, TaskRecord] = {}
        for blocked_task_id in normalized_block_ids:
            blocked_task = get_task(blocked_task_id)
            if blocked_task is None:
                return None, [], f"Task not found: {blocked_task_id}"
            related_blocked_tasks[blocked_task_id] = blocked_task
        for blocked_task_id in normalized_block_ids:
            next_task = _append_unique(task, "blocks", blocked_task_id)
            if next_task != task:
                task = next_task
                blocks_changed = True
            blocked_task = related_blocked_tasks[blocked_task_id]
            related_updates[blocked_task_id] = _append_unique(
                related_updates.get(blocked_task_id, blocked_task),
                "blockedBy",
                normalized_task_id,
            )
        if blocks_changed:
            updated_fields.append("blocks")
    if add_blocked_by:
        normalized_blocker_ids = [_normalize_task_id(value) for value in add_blocked_by]
        related_blocker_tasks: dict[str, TaskRecord] = {}
        for blocker_task_id in normalized_blocker_ids:
            blocker_task = get_task(blocker_task_id)
            if blocker_task is None:
                return None, [], f"Task not found: {blocker_task_id}"
            related_blocker_tasks[blocker_task_id] = blocker_task
        for blocker_task_id in normalized_blocker_ids:
            next_task = _append_unique(task, "blockedBy", blocker_task_id)
            if next_task != task:
                task = next_task
                blocked_by_changed = True
            blocker_task = related_blocker_tasks[blocker_task_id]
            related_updates[blocker_task_id] = _append_unique(
                related_updates.get(blocker_task_id, blocker_task),
                "blocks",
                normalized_task_id,
            )
        if blocked_by_changed:
            updated_fields.append("blockedBy")

    for related_task in related_updates.values():
        _write_task(tasks_dir, related_task)
    _write_task(tasks_dir, task)
    return task, _dedupe(updated_fields), None


def delete_task(task_id: str) -> None:
    tasks_dir = get_tasks_dir()
    normalized_task_id = _normalize_task_id(task_id)
    current = get_task(normalized_task_id)
    if current is None:
        return
    highest = max(_read_high_water_mark(tasks_dir), int(normalized_task_id))
    _write_high_water_mark(tasks_dir, highest)
    for task in list_tasks():
        if task.id == normalized_task_id:
            continue
        updated = task
        if normalized_task_id in updated.blocks:
            updated = _replace_task(
                updated,
                blocks=tuple(
                    value for value in updated.blocks if value != normalized_task_id
                ),
            )
        if normalized_task_id in updated.blockedBy:
            updated = _replace_task(
                updated,
                blockedBy=tuple(
                    value for value in updated.blockedBy if value != normalized_task_id
                ),
            )
        if updated != task:
            _write_task(tasks_dir, updated)
    os.unlink(_task_path(tasks_dir, normalized_task_id))


def to_public_task(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.id,
        "subject": task.subject,
        "description": task.description,
        "activeForm": task.activeForm,
        "owner": task.owner,
        "status": task.status,
        "blocks": list(task.blocks),
        "blockedBy": list(task.blockedBy),
        "metadata": dict(task.metadata),
    }


def to_list_task(task: TaskRecord, completed_ids: set[str]) -> dict[str, Any] | None:
    if task.metadata.get("_internal") is True:
        return None
    filtered_blocked_by = [
        task_id for task_id in task.blockedBy if task_id not in completed_ids
    ]
    return {
        "id": task.id,
        "subject": task.subject,
        "status": task.status,
        "owner": task.owner,
        "blockedBy": filtered_blocked_by,
    }


def _replace_task(task: TaskRecord, **updates: Any) -> TaskRecord:
    payload = asdict(task)
    payload.update(updates)
    return TaskRecord(**payload)


def _append_unique(task: TaskRecord, field_name: str, task_id: str) -> TaskRecord:
    current = tuple(getattr(task, field_name))
    if task_id in current:
        return task
    return _replace_task(task, **{field_name: current + (task_id,)})


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _ensure_task_exists(task_id: str) -> None:
    if get_task(task_id) is None:
        raise ValueError(f"Task not found: {task_id}")


def _task_path(tasks_dir: str, task_id: str) -> str:
    normalized = _normalize_task_id(task_id)
    return os.path.join(tasks_dir, f"{normalized}.json")


def _normalize_task_id(task_id: str) -> str:
    value = task_id.strip()
    if not value or not value.isdigit():
        raise ValueError(f"Invalid task id: {task_id}")
    return value


def _next_task_number(tasks_dir: str) -> int:
    current_ids = [int(task.id) for task in list_tasks()]
    highest_existing = max(current_ids, default=0)
    return max(highest_existing, _read_high_water_mark(tasks_dir)) + 1


def _read_task(path: str) -> TaskRecord:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return TaskRecord(
        id=str(payload["id"]),
        subject=str(payload["subject"]),
        description=str(payload.get("description", "")),
        activeForm=payload.get("activeForm"),
        owner=payload.get("owner"),
        status=str(payload.get("status", TASK_PENDING)),
        blocks=tuple(str(value) for value in payload.get("blocks", [])),
        blockedBy=tuple(str(value) for value in payload.get("blockedBy", [])),
        metadata=dict(payload.get("metadata", {})),
    )


def _write_task(tasks_dir: str, task: TaskRecord) -> None:
    os.makedirs(tasks_dir, exist_ok=True)
    payload = to_public_task(task)
    path = _task_path(tasks_dir, task.id)
    fd, tmp_path = tempfile.mkstemp(
        dir=tasks_dir,
        prefix=f".{task.id}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _high_water_mark_path(tasks_dir: str) -> str:
    return os.path.join(tasks_dir, ".highwatermark")


def _read_high_water_mark(tasks_dir: str) -> int:
    path = _high_water_mark_path(tasks_dir)
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        content = handle.read().strip()
    if not content:
        return 0
    return int(content)


def _write_high_water_mark(tasks_dir: str, value: int) -> None:
    os.makedirs(tasks_dir, exist_ok=True)
    path = _high_water_mark_path(tasks_dir)
    fd, tmp_path = tempfile.mkstemp(
        dir=tasks_dir,
        prefix=".highwatermark.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = [
    "TASK_COMPLETED",
    "TASK_DELETED",
    "TASK_IN_PROGRESS",
    "TASK_PENDING",
    "TASK_STATUSES",
    "TaskRecord",
    "create_task",
    "delete_task",
    "get_task",
    "get_task_list_id",
    "get_tasks_dir",
    "is_todo_v2_enabled",
    "list_tasks",
    "sanitize_path_component",
    "to_list_task",
    "to_public_task",
    "update_task",
]
