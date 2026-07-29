from __future__ import annotations

import inspect
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ...memdir.team_mem_paths import get_auto_mem_path
from .config import AutoDreamConfig, load_auto_dream_config
from .consolidation_lock import ConsolidationLock
from .consolidation_prompt import build_consolidation_prompt


DreamRunner = Callable[["DreamTask"], Awaitable[Mapping[str, Any]] | Mapping[str, Any]]
DreamProgressWatcher = Callable[[Sequence[str]], None]
_LAST_SCAN_AT = 0.0
_AUTO_DREAM_WATCHER: "DreamProgressWatcher | None" = None


@dataclass(frozen=True)
class DreamTask:
    id: str
    memory_dir: str
    transcript_dir: str
    sessions: tuple[str, ...]
    prompt: str
    status: str = "pending"
    touched_paths: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class AutoDreamResult:
    status: str
    reason: str | None = None
    task: DreamTask | None = None


async def maybe_run_auto_dream(
    *,
    settings: Mapping[str, Any] | None = None,
    project_root: str | None = None,
    config_home: str | None = None,
    current_session_id: str | None = None,
    app_state: Any = None,
    runner: DreamRunner | None = None,
    now: float | None = None,
) -> AutoDreamResult:
    current_time = time.time() if now is None else now
    config = load_auto_dream_config(settings)
    if not config.enabled:
        return AutoDreamResult(status="skipped", reason="disabled")
    if not _scan_allowed(config, current_time):
        return AutoDreamResult(status="skipped", reason="scan_throttled")

    memory_dir = get_auto_mem_path(project_root=project_root, config_home=config_home)
    transcript_dir = _transcript_dir_for_memory_dir(memory_dir)
    lock = ConsolidationLock(memory_dir)
    last_consolidated = lock.last_consolidated_mtime()
    if current_time - last_consolidated < config.min_hours * 3600:
        return AutoDreamResult(status="skipped", reason="too_recent")

    sessions = list_sessions_touched_since(
        last_consolidated,
        project_root=project_root,
        config_home=config_home,
        current_session_id=current_session_id,
    )
    if len(sessions) < config.min_sessions:
        return AutoDreamResult(status="skipped", reason="insufficient_sessions")
    if not lock.acquire():
        return AutoDreamResult(status="skipped", reason="lock_busy")

    prompt = build_consolidation_prompt(
        memory_dir=memory_dir,
        transcript_dir=transcript_dir,
        sessions=sessions,
    )
    task = DreamTask(
        id=f"autodream-{uuid.uuid4().hex}",
        memory_dir=memory_dir,
        transcript_dir=transcript_dir,
        sessions=tuple(sessions),
        prompt=prompt,
        status="running",
    )
    _record_task(app_state, task)

    try:
        if runner is None:
            pending = _replace_task(task, status="pending")
            _record_task(app_state, pending)
            lock.rollback()
            return AutoDreamResult(
                status="pending",
                reason="runner_required",
                task=pending,
            )

        raw_result = runner(task)
        if inspect.isawaitable(raw_result):
            raw_result = await raw_result
        touched_paths = _extract_touched_paths(raw_result)
        if _AUTO_DREAM_WATCHER is not None and touched_paths:
            _AUTO_DREAM_WATCHER(touched_paths)
        _inject_completion_message(touched_paths, app_state)
        completed = _replace_task(task, status="completed", touched_paths=touched_paths)
        _record_task(app_state, completed)
        lock.complete()
        return AutoDreamResult(status="completed", task=completed)
    except Exception as exc:
        failed = _replace_task(task, status="failed", error=str(exc))
        _record_task(app_state, failed)
        lock.rollback()
        return AutoDreamResult(status="failed", reason=str(exc), task=failed)


def make_dream_progress_watcher(
    on_progress: Callable[[Sequence[str]], None],
) -> None:
    global _AUTO_DREAM_WATCHER
    _AUTO_DREAM_WATCHER = on_progress


def _inject_completion_message(
    touched_paths: Sequence[str],
    app_state: Any,
) -> None:
    if not touched_paths or app_state is None:
        return
    completion_msg = {
        "type": "system",
        "content": f"Memory consolidation completed. Touched {len(touched_paths)} paths.",
    }
    messages = getattr(app_state, "messages", None)
    if isinstance(messages, list):
        messages.append(completion_msg)


def list_sessions_touched_since(
    since_mtime: float,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
    current_session_id: str | None = None,
) -> list[str]:
    memory_dir = get_auto_mem_path(project_root=project_root, config_home=config_home)
    transcript_dir = Path(_transcript_dir_for_memory_dir(memory_dir))
    if not transcript_dir.exists():
        return []

    current_session = (current_session_id or "").strip()
    sessions: list[str] = []
    for path in transcript_dir.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime <= since_mtime:
            continue
        if current_session and current_session in path.stem:
            continue
        sessions.append(str(path))
    return sorted(sessions)


def _scan_allowed(config: AutoDreamConfig, now: float) -> bool:
    global _LAST_SCAN_AT
    if now - _LAST_SCAN_AT < config.scan_interval_seconds:
        return False
    _LAST_SCAN_AT = now
    return True


def _transcript_dir_for_memory_dir(memory_dir: str) -> str:
    return str(Path(memory_dir).parent)


def _record_task(app_state: Any, task: DreamTask) -> None:
    if app_state is None:
        return
    tasks = getattr(app_state, "tasks", None)
    if isinstance(tasks, dict):
        tasks[task.id] = asdict(task)


def _replace_task(
    task: DreamTask,
    *,
    status: str,
    touched_paths: Sequence[str] | None = None,
    error: str | None = None,
) -> DreamTask:
    return DreamTask(
        id=task.id,
        memory_dir=task.memory_dir,
        transcript_dir=task.transcript_dir,
        sessions=task.sessions,
        prompt=task.prompt,
        status=status,
        touched_paths=tuple(touched_paths or task.touched_paths),
        error=error,
    )


def _extract_touched_paths(result: Mapping[str, Any] | Any) -> tuple[str, ...]:
    if not isinstance(result, Mapping):
        return ()
    value = result.get("touched_paths") or result.get("touchedPaths")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    paths = [str(item) for item in value if isinstance(item, str) and item.strip()]
    return tuple(paths)


def reset_auto_dream_scan_for_testing() -> None:
    global _LAST_SCAN_AT
    _LAST_SCAN_AT = 0.0
