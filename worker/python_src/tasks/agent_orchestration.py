from __future__ import annotations

import inspect
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

if __package__ in (None, ""):
    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.dirname(os.path.dirname(_here))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    _local_tasks = import_module("python_src.tasks.local_tasks")
else:
    from . import local_tasks as _local_tasks


TASK_STATUS_PENDING = _local_tasks.TASK_STATUS_PENDING
TASK_STATUS_RUNNING = _local_tasks.TASK_STATUS_RUNNING
TASK_STATUS_COMPLETED = _local_tasks.TASK_STATUS_COMPLETED
TASK_STATUS_FAILED = _local_tasks.TASK_STATUS_FAILED
TASK_STATUS_KILLED = _local_tasks.TASK_STATUS_KILLED
LocalTaskManager = _local_tasks.LocalTaskManager
LocalTaskState = _local_tasks.LocalTaskState

_TERMINAL_STATUSES = frozenset(
    (TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_KILLED)
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _truncate_summary_text(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _format_agent_status_text(
    *,
    status: str,
    last_activity: str = "",
    is_backgrounded: bool = False,
) -> str:
    if status == "completed":
        return "Completed"
    if status == "failed":
        return "Failed"
    if status == "async_launched":
        if last_activity:
            return f"Backgrounded while {last_activity}"
        return "Running in background"
    if last_activity:
        return f"Running {last_activity}"
    if is_backgrounded:
        return "Running in background"
    return "Running"


def agent_progress_payload(progress: "AgentProgress") -> Dict[str, object]:
    return {
        "toolUseCount": progress.tool_use_count,
        "tokenCount": progress.token_count,
        "lastActivity": progress.last_activity,
        "recentActivities": list(progress.recent_activities),
        "statusText": progress.status_text,
        "summary": progress.summary,
    }


def agent_summary_payload(
    *,
    status: str,
    agent_id: str,
    agent_type: str,
    progress: "AgentProgress",
    is_backgrounded: bool,
    total_tool_use_count: int | None = None,
    total_tokens: int | None = None,
    total_duration_ms: int | None = None,
    content: Tuple[str, ...] = (),
    error: str | None = None,
) -> Dict[str, object]:
    effective_tool_uses = (
        total_tool_use_count
        if total_tool_use_count is not None
        else progress.tool_use_count
    )
    effective_tokens = total_tokens if total_tokens is not None else progress.token_count
    status_text = _format_agent_status_text(
        status=status,
        last_activity=progress.last_activity,
        is_backgrounded=is_backgrounded,
    )
    headline_parts: list[str] = [status_text]
    if effective_tool_uses:
        headline_parts.append(f"{effective_tool_uses} tool uses")
    if effective_tokens:
        headline_parts.append(f"{effective_tokens} tokens")
    if total_duration_ms is not None and total_duration_ms > 0:
        headline_parts.append(f"{total_duration_ms} ms")
    headline = " | ".join(headline_parts)
    output_preview = ""
    for text in content:
        normalized = _truncate_summary_text(text)
        if normalized:
            output_preview = normalized
            break
    return {
        "status": status,
        "statusText": status_text,
        "headline": headline,
        "agentId": agent_id,
        "agentType": agent_type,
        "isBackgrounded": is_backgrounded,
        "toolUseCount": effective_tool_uses,
        "tokenCount": effective_tokens,
        "lastActivity": progress.last_activity,
        "recentActivities": list(progress.recent_activities),
        "progressSummary": progress.summary,
        "durationMs": total_duration_ms,
        "outputPreview": output_preview,
        "error": error or "",
    }


def _agent_progress_is_empty(progress: "AgentProgress") -> bool:
    return (
        progress.tool_use_count == 0
        and progress.token_count == 0
        and not progress.last_activity
        and not progress.recent_activities
        and not progress.summary
        and not progress.status_text
    )


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AgentProgress:
    tool_use_count: int = 0
    token_count: int = 0
    last_activity: str = ""
    recent_activities: Tuple[str, ...] = ()
    summary: str = ""
    status_text: str = ""


@dataclass(frozen=True)
class AgentToolResult:
    agent_id: str
    agent_type: str
    content: Tuple[str, ...]
    total_tool_use_count: int
    total_duration_ms: int
    total_tokens: int
    usage: AgentUsage = field(default_factory=AgentUsage)
    progress: AgentProgress = field(default_factory=AgentProgress)

    def as_tool_payload(self) -> Dict[str, object]:
        return {
            "status": "completed",
            "agentId": self.agent_id,
            "agentType": self.agent_type,
            "content": [{"type": "text", "text": text} for text in self.content],
            "totalToolUseCount": self.total_tool_use_count,
            "totalDurationMs": self.total_duration_ms,
            "totalTokens": self.total_tokens,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
            },
            "progress": agent_progress_payload(self.progress),
            "summary": agent_summary_payload(
                status="completed",
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                progress=self.progress,
                is_backgrounded=False,
                total_tool_use_count=self.total_tool_use_count,
                total_tokens=self.total_tokens,
                total_duration_ms=self.total_duration_ms,
                content=self.content,
            ),
        }


def _coerce_agent_tool_result(result: object) -> AgentToolResult | None:
    if isinstance(result, AgentToolResult):
        return result
    agent_id = getattr(result, "agent_id", None)
    agent_type = getattr(result, "agent_type", None)
    content = getattr(result, "content", None)
    total_tool_use_count = getattr(result, "total_tool_use_count", None)
    total_duration_ms = getattr(result, "total_duration_ms", None)
    total_tokens = getattr(result, "total_tokens", None)
    usage = getattr(result, "usage", None)
    if not (
        isinstance(agent_id, str)
        and isinstance(agent_type, str)
        and isinstance(content, tuple)
        and isinstance(total_tool_use_count, int)
        and isinstance(total_duration_ms, int)
        and isinstance(total_tokens, int)
    ):
        return None
    progress = getattr(result, "progress", AgentProgress())
    if not isinstance(progress, AgentProgress):
        progress = AgentProgress()
    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    normalized_usage = AgentUsage(
        input_tokens=input_tokens if isinstance(input_tokens, int) else 0,
        output_tokens=output_tokens if isinstance(output_tokens, int) else 0,
    )
    return AgentToolResult(
        agent_id=agent_id,
        agent_type=agent_type,
        content=content,
        total_tool_use_count=total_tool_use_count,
        total_duration_ms=total_duration_ms,
        total_tokens=total_tokens,
        usage=normalized_usage,
        progress=progress,
    )


@dataclass(frozen=True)
class AgentTaskState:
    task_id: str
    agent_id: str
    description: str
    prompt: str
    agent_type: str
    status: str
    is_backgrounded: bool
    output_file: str
    output_offset: int = 0
    logs: str = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None
    result: Optional[AgentToolResult] = None
    progress: AgentProgress = field(default_factory=AgentProgress)
    tool_use_id: Optional[str] = None
    cwd: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    notified: bool = False
    session_hooks: Optional[Mapping[str, object]] = None
    resume_state: Optional[Mapping[str, object]] = None
    skill_mcp_sources: Tuple[str, ...] = ()
    skill_permissions: Tuple[Mapping[str, object], ...] = ()

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def as_resume_payload(self) -> Mapping[str, object]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "description": self.description,
            "prompt": self.prompt,
            "agent_type": self.agent_type,
            "status": self.status,
            "is_backgrounded": self.is_backgrounded,
            "output_file": self.output_file,
            "output_offset": self.output_offset,
            "logs": self.logs,
            "progress": agent_progress_payload(self.progress),
            "session_hooks": dict(self.session_hooks) if self.session_hooks else None,
            "skill_mcp_sources": list(self.skill_mcp_sources),
            "skill_permissions": [dict(p) for p in self.skill_permissions],
        }


@dataclass(frozen=True)
class SessionHookSpec:
    event_name: str
    hook_type: str = "callback"
    callback: Optional[Callable[..., object]] = None
    enabled: bool = True
    source: str = "agent-session"


@dataclass(frozen=True)
class SkillMcpSource:
    server_name: str
    skill_names: Tuple[str, ...] = ()
    source_type: str = "mcp"


@dataclass(frozen=True)
class SkillPermissionRule:
    skill_name: str
    behavior: str
    rule_content: Optional[str] = None
    source: str = "session"

    def matches(self, tool_name: str, tool_input: Mapping[str, Any]) -> bool:
        if tool_name != "Skill":
            return False
        if not self.rule_content:
            return True
        candidates: list[str] = []
        for key in ("resolved_skill_name", "skill"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                normalized = value.strip()
                if normalized.startswith("/"):
                    normalized = normalized[1:]
                if normalized:
                    candidates.append(normalized)
        aliases = tool_input.get("skill_aliases")
        if isinstance(aliases, (list, tuple)):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    normalized = alias.strip()
                    if normalized.startswith("/"):
                        normalized = normalized[1:]
                    if normalized:
                        candidates.append(normalized)
        if not candidates:
            return False
        import fnmatch

        rule_pattern = self.rule_content.strip()
        if rule_pattern.startswith("/"):
            rule_pattern = rule_pattern[1:]
        if any(ch in rule_pattern for ch in "*?[]"):
            return any(fnmatch.fnmatchcase(c, rule_pattern) for c in candidates)
        return rule_pattern in candidates


@dataclass(frozen=True)
class ForegroundAgentHandle:
    task_id: str
    background_signal: threading.Event
    cancel_auto_background: Callable[[], None]


class ProgressTracker:
    def __init__(self) -> None:
        self.tool_use_count = 0
        self.latest_input_tokens = 0
        self.cumulative_output_tokens = 0
        self.recent_activities: List[str] = []

    def update(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        activity: str = "",
    ) -> AgentProgress:
        self.tool_use_count += 1
        self.latest_input_tokens = input_tokens
        self.cumulative_output_tokens += output_tokens
        if activity:
            self.recent_activities.append(activity)
            self.recent_activities = self.recent_activities[-5:]
        total_tokens = self.latest_input_tokens + self.cumulative_output_tokens
        status_text = _format_agent_status_text(
            status="running",
            last_activity=self.recent_activities[-1] if self.recent_activities else "",
        )
        return AgentProgress(
            tool_use_count=self.tool_use_count,
            token_count=total_tokens,
            last_activity=self.recent_activities[-1] if self.recent_activities else "",
            recent_activities=tuple(self.recent_activities),
            summary="{}; {} tool uses; {} total tokens".format(
                status_text,
                self.tool_use_count,
                total_tokens,
            ),
            status_text=status_text,
        )


class _AgentLocalTaskManager:
    def __init__(self) -> None:
        self._inner = LocalTaskManager()

    def next_task_id(self) -> str:
        return self._inner._next_task_id("a")

    def list_events(self) -> Tuple[dict, ...]:
        return self._inner.list_events()

    def get_task(self, task_id: str) -> LocalTaskState:
        return self._inner.get_task(task_id)

    def wait_for_task(self, task_id: str, timeout: float = 5.0) -> LocalTaskState:
        return self._inner.wait_for_task(task_id, timeout)

    def background_task(self, task_id: str) -> LocalTaskState:
        return self._inner.background_task(task_id)

    def stop_task(self, task_id: str) -> LocalTaskState:
        return self._inner.stop_task(task_id)

    def create_local_agent_task(
        self,
        *,
        task_id: str,
        description: str,
        runner: Callable[[Callable[[str], None]], None],
        output_file: str,
        tool_use_id: Optional[str] = None,
        backgrounded: bool,
        agent_type: str,
    ) -> LocalTaskState:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        state = LocalTaskState(
            id=task_id,
            description=description,
            task_type="local_agent",
            agent_type=agent_type,
            status=TASK_STATUS_RUNNING,
            is_backgrounded=backgrounded,
            tool_use_id=tool_use_id,
            output_file=str(output_path),
        )
        worker = threading.Thread(
            target=self._inner._run_main_session_task,
            args=(task_id, runner),
            daemon=True,
        )
        with self._inner._lock:
            self._inner._tasks[task_id] = state
            self._inner._workers[task_id] = worker
            self._inner._record_event(
                {
                    "type": "task_started",
                    "task_id": task_id,
                    "task_type": state.task_type,
                    "description": description,
                    "tool_use_id": tool_use_id,
                    "workflow_name": agent_type,
                }
            )
        worker.start()
        return self._inner.get_task(task_id)


AgentRunner = Callable[
    ...,
    AgentToolResult,
]


class AgentOrchestrationManager:
    def __init__(self) -> None:
        self._task_manager = _AgentLocalTaskManager()
        self._lock = threading.Lock()
        self._events: List[dict] = []
        self._agent_states: Dict[str, AgentTaskState] = {}
        self._background_signals: Dict[str, threading.Event] = {}
        self._auto_background_cancel: Dict[str, threading.Event] = {}
        self._session_hooks: Dict[str, List[SessionHookSpec]] = {}
        self._resume_snapshots: Dict[str, Mapping[str, object]] = {}
        self._skill_mcp_sources: Dict[str, List[SkillMcpSource]] = {}
        self._skill_permissions: Dict[str, List[SkillPermissionRule]] = {}

    def list_events(self) -> Tuple[dict, ...]:
        with self._lock:
            local_events = [dict(event) for event in self._task_manager.list_events()]
            own_events = [dict(event) for event in self._events]
        return tuple(local_events + own_events)

    def get_agent_task(self, task_id: str) -> AgentTaskState:
        with self._lock:
            return replace(self._agent_states[task_id])

    def wait_for_agent(self, task_id: str, timeout: float = 5.0) -> AgentTaskState:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._sync_state(task_id)
            if state.status == TASK_STATUS_COMPLETED and state.result is not None:
                self._cleanup_agent_resources(task_id)
                return state
            if state.status == TASK_STATUS_FAILED and self._has_event(
                task_id,
                "agent_failed",
            ):
                self._cleanup_agent_resources(task_id)
                return state
            if state.status == TASK_STATUS_KILLED and self._has_event(
                task_id,
                "agent_killed",
            ):
                self._cleanup_agent_resources(task_id)
                return state
            time.sleep(0.01)
        state = self._sync_state(task_id)
        if state.is_terminal:
            self._cleanup_agent_resources(task_id)
        return state

    def register_async_agent(
        self,
        *,
        agent_id: str,
        description: str,
        prompt: str,
        runner: AgentRunner,
        output_file: str,
        tool_use_id: Optional[str] = None,
        agent_type: str = "local-subagent",
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> AgentTaskState:
        task_id = self.run_async_agent_lifecycle(
            agent_id=agent_id,
            description=description,
            prompt=prompt,
            runner=runner,
            output_file=output_file,
            tool_use_id=tool_use_id,
            backgrounded=True,
            agent_type=agent_type,
            cwd=cwd,
            model=model,
            max_tokens=max_tokens,
        )
        return self.get_agent_task(task_id)

    def register_agent_foreground(
        self,
        *,
        agent_id: str,
        description: str,
        prompt: str,
        runner: AgentRunner,
        output_file: str,
        tool_use_id: Optional[str] = None,
        agent_type: str = "local-subagent",
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        auto_background_ms: int = 0,
    ) -> ForegroundAgentHandle:
        task_id = self.run_async_agent_lifecycle(
            agent_id=agent_id,
            description=description,
            prompt=prompt,
            runner=runner,
            output_file=output_file,
            tool_use_id=tool_use_id,
            backgrounded=False,
            agent_type=agent_type,
            cwd=cwd,
            model=model,
            max_tokens=max_tokens,
        )
        background_signal = self._background_signals[task_id]
        cancel_auto_background = self._install_auto_background(
            task_id,
            auto_background_ms,
        )
        return ForegroundAgentHandle(
            task_id=task_id,
            background_signal=background_signal,
            cancel_auto_background=cancel_auto_background,
        )

    def run_async_agent_lifecycle(
        self,
        *,
        agent_id: str,
        description: str,
        prompt: str,
        runner: AgentRunner,
        output_file: str,
        tool_use_id: Optional[str],
        backgrounded: bool,
        agent_type: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        tracker = ProgressTracker()
        outcome: Dict[str, object] = {}
        task_id = self._task_manager.next_task_id()

        def _wrapped_runner(record: Callable[[str], None]) -> None:
            started_at = _now_ms()

            def report_progress(
                input_tokens: int,
                output_tokens: int,
                activity: str,
            ) -> None:
                progress = tracker.update(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    activity=activity,
                )
                self._update_progress(task_id, progress)

            result = _invoke_agent_runner(
                runner,
                record=record,
                report_progress=report_progress,
                task_id=task_id,
            )
            duration_ms = max(0, _now_ms() - started_at)
            outcome["result"] = replace(
                result,
                total_duration_ms=duration_ms,
                total_tokens=result.usage.total_tokens,
            )

        local_task = self._task_manager.create_local_agent_task(
            task_id=task_id,
            description=description,
            runner=_wrapped_runner,
            output_file=output_file,
            tool_use_id=tool_use_id,
            backgrounded=backgrounded,
            agent_type=agent_type,
        )
        with self._lock:
            self._background_signals[task_id] = threading.Event()
            self._agent_states[task_id] = AgentTaskState(
                task_id=task_id,
                agent_id=agent_id,
                description=description,
                prompt=prompt,
                agent_type=agent_type,
                status=local_task.status,
                is_backgrounded=local_task.is_backgrounded,
                output_file=local_task.output_file or "",
                output_offset=local_task.output_offset,
                logs=local_task.logs,
                exit_code=local_task.exit_code,
                error=local_task.error,
                tool_use_id=tool_use_id,
                cwd=cwd,
                model=model,
                max_tokens=max_tokens,
                notified=local_task.notified,
            )
            self._record_event(
                {
                    "type": "agent_spawned",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": local_task.status,
                    "backgrounded": backgrounded,
                    "tool_use_id": tool_use_id,
                }
            )

        watcher = threading.Thread(
            target=self._watch_agent_completion,
            args=(task_id, outcome),
            daemon=True,
        )
        watcher.start()
        return task_id

    def background_agent_task(self, task_id: str) -> bool:
        current = self._sync_state(task_id)
        if current.is_backgrounded or current.is_terminal:
            return current.is_backgrounded
        updated_local = self._task_manager.background_task(task_id)
        signal = self._background_signals.get(task_id)
        if signal is not None:
            signal.set()
        with self._lock:
            current = self._agent_states[task_id]
            self._agent_states[task_id] = replace(
                current,
                is_backgrounded=updated_local.is_backgrounded,
                status=updated_local.status,
                notified=updated_local.notified,
            )
            self._record_event(
                {
                    "type": "agent_backgrounded",
                    "task_id": task_id,
                    "agent_id": current.agent_id,
                }
            )
        return True

    def kill_async_agent(self, task_id: str) -> AgentTaskState:
        self._cancel_auto_background(task_id)
        killed = self._task_manager.stop_task(task_id)
        with self._lock:
            current = self._agent_states[task_id]
            self._agent_states[task_id] = replace(
                current,
                status=killed.status,
                exit_code=killed.exit_code,
                error=killed.error,
                notified=killed.notified,
            )
            self._record_event(
                {
                    "type": "agent_killed",
                    "task_id": task_id,
                    "agent_id": current.agent_id,
                    "status": killed.status,
                }
            )
        return self.get_agent_task(task_id)

    def complete_agent_task(
        self,
        task_id: str,
        result: AgentToolResult,
    ) -> AgentTaskState:
        local_state = self._task_manager.get_task(task_id)
        with self._lock:
            current = self._agent_states[task_id]
            progress = (
                current.progress
                if not _agent_progress_is_empty(current.progress)
                else result.progress
            )
            result = replace(result, progress=progress)
            updated = replace(
                current,
                status=local_state.status,
                is_backgrounded=local_state.is_backgrounded,
                output_offset=local_state.output_offset,
                logs=local_state.logs,
                exit_code=local_state.exit_code,
                error=local_state.error,
                result=result,
                notified=local_state.notified,
            )
            self._agent_states[task_id] = updated
            self._record_event(
                {
                    "type": "agent_completed",
                    "task_id": task_id,
                    "agent_id": current.agent_id,
                    "total_tool_use_count": result.total_tool_use_count,
                    "total_tokens": result.total_tokens,
                    "summary": agent_summary_payload(
                        status="completed",
                        agent_id=current.agent_id,
                        agent_type=current.agent_type,
                        progress=progress,
                        is_backgrounded=updated.is_backgrounded,
                        total_tool_use_count=result.total_tool_use_count,
                        total_tokens=result.total_tokens,
                        total_duration_ms=result.total_duration_ms,
                        content=result.content,
                    ),
                }
            )
        return self.get_agent_task(task_id)

    def fail_agent_task(self, task_id: str, error: str) -> AgentTaskState:
        local_state = self._task_manager.get_task(task_id)
        with self._lock:
            current = self._agent_states[task_id]
            updated = replace(
                current,
                status=local_state.status,
                is_backgrounded=local_state.is_backgrounded,
                output_offset=local_state.output_offset,
                logs=local_state.logs,
                exit_code=local_state.exit_code,
                error=error,
                notified=local_state.notified,
            )
            self._agent_states[task_id] = updated
            self._record_event(
                {
                    "type": "agent_failed",
                    "task_id": task_id,
                    "agent_id": current.agent_id,
                    "error": error,
                    "summary": agent_summary_payload(
                        status="failed",
                        agent_id=current.agent_id,
                        agent_type=current.agent_type,
                        progress=updated.progress,
                        is_backgrounded=updated.is_backgrounded,
                        error=error,
                    ),
                }
            )
        return self.get_agent_task(task_id)

    # -- B5-M1: Session hooks --

    def register_session_hooks(
        self,
        task_id: str,
        hooks: Sequence[SessionHookSpec],
    ) -> None:
        with self._lock:
            self._session_hooks[task_id] = list(hooks)
            self._record_event(
                {
                    "type": "agent_session_hooks_registered",
                    "task_id": task_id,
                    "hook_count": len(hooks),
                    "hook_events": [h.event_name for h in hooks],
                }
            )

    def get_session_hooks(self, task_id: str) -> Tuple[SessionHookSpec, ...]:
        with self._lock:
            return tuple(self._session_hooks.get(task_id, ()))

    def clear_session_hooks(self, task_id: str) -> None:
        with self._lock:
            self._session_hooks.pop(task_id, None)
            self._record_event(
                {
                    "type": "agent_session_hooks_cleared",
                    "task_id": task_id,
                }
            )

    # -- B5-M2: Agent resume --

    def store_resume_snapshot(self, task_id: str) -> Mapping[str, object]:
        with self._lock:
            state = self._agent_states.get(task_id)
            if state is None:
                return {}
            snapshot = state.as_resume_payload()
            self._resume_snapshots[task_id] = snapshot
            self._record_event(
                {
                    "type": "agent_resume_snapshot_stored",
                    "task_id": task_id,
                    "agent_id": state.agent_id,
                    "status": state.status,
                }
            )
            return dict(snapshot)

    def get_resume_snapshot(self, task_id: str) -> Optional[Mapping[str, object]]:
        with self._lock:
            snapshot = self._resume_snapshots.get(task_id)
            return dict(snapshot) if snapshot is not None else None

    def resume_agent(self, task_id: str) -> AgentTaskState:
        with self._lock:
            state = self._agent_states.get(task_id)
            if state is None:
                raise KeyError(f"no agent state for task {task_id}")
            snapshot = self._resume_snapshots.get(task_id)
            if snapshot is None:
                snapshot = state.as_resume_payload()
                self._resume_snapshots[task_id] = snapshot
            resumed = replace(
                state,
                resume_state=snapshot,
            )
            self._agent_states[task_id] = resumed
            self._record_event(
                {
                    "type": "agent_resumed",
                    "task_id": task_id,
                    "agent_id": state.agent_id,
                    "status": state.status,
                    "has_snapshot": True,
                }
            )
            return replace(resumed)

    # -- B5-M3: Skill MCP source discovery --

    def register_skill_mcp_sources(
        self,
        task_id: str,
        sources: Sequence[SkillMcpSource],
    ) -> None:
        with self._lock:
            self._skill_mcp_sources[task_id] = list(sources)
            state = self._agent_states.get(task_id)
            if state is not None:
                self._agent_states[task_id] = replace(
                    state,
                    skill_mcp_sources=tuple(s.server_name for s in sources),
                )
            self._record_event(
                {
                    "type": "agent_skill_mcp_sources_registered",
                    "task_id": task_id,
                    "source_count": len(sources),
                    "server_names": [s.server_name for s in sources],
                }
            )

    def get_skill_mcp_sources(self, task_id: str) -> Tuple[SkillMcpSource, ...]:
        with self._lock:
            return tuple(self._skill_mcp_sources.get(task_id, ()))

    # -- B5-M4: Skill permission rules --

    def register_skill_permissions(
        self,
        task_id: str,
        rules: Sequence[SkillPermissionRule],
    ) -> None:
        with self._lock:
            self._skill_permissions[task_id] = list(rules)
            state = self._agent_states.get(task_id)
            if state is not None:
                self._agent_states[task_id] = replace(
                    state,
                    skill_permissions=tuple(rules),
                )
            self._record_event(
                {
                    "type": "agent_skill_permissions_registered",
                    "task_id": task_id,
                    "rule_count": len(rules),
                    "behaviors": [r.behavior for r in rules],
                }
            )

    def get_skill_permissions(self, task_id: str) -> Tuple[SkillPermissionRule, ...]:
        with self._lock:
            return tuple(self._skill_permissions.get(task_id, ()))

    def check_skill_permission(
        self,
        task_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
    ) -> Optional[str]:
        with self._lock:
            rules = self._skill_permissions.get(task_id, ())
        for rule in rules:
            if rule.matches(tool_name, tool_input):
                return rule.behavior
        return None

    def _record_event(self, payload: dict) -> None:
        payload = dict(payload)
        payload.setdefault("timestamp_ms", _now_ms())
        self._events.append(payload)

    def _has_event(self, task_id: str, event_type: str) -> bool:
        with self._lock:
            for event in self._events:
                if event.get("task_id") == task_id and event.get("type") == event_type:
                    return True
        return False

    def _sync_state(self, task_id: str) -> AgentTaskState:
        local_state = self._task_manager.get_task(task_id)
        with self._lock:
            current = self._agent_states[task_id]
            updated = replace(
                current,
                status=local_state.status,
                is_backgrounded=local_state.is_backgrounded,
                output_offset=local_state.output_offset,
                logs=local_state.logs,
                exit_code=local_state.exit_code,
                error=local_state.error,
                notified=local_state.notified,
            )
            self._agent_states[task_id] = updated
            return replace(updated)

    def _update_progress(self, task_id: str, progress: AgentProgress) -> None:
        with self._lock:
            current = self._agent_states.get(task_id)
            if current is None or current.is_terminal:
                return
            self._agent_states[task_id] = replace(current, progress=progress)
            self._record_event(
                {
                    "type": "agent_progress",
                    "task_id": task_id,
                    "agent_id": current.agent_id,
                    "tool_use_count": progress.tool_use_count,
                    "token_count": progress.token_count,
                    "last_activity": progress.last_activity,
                    "recent_activities": list(progress.recent_activities),
                    "status_text": progress.status_text,
                    "summary": progress.summary,
                }
            )

    def _watch_agent_completion(self, task_id: str, outcome: Dict[str, object]) -> None:
        local_state = self._task_manager.wait_for_task(task_id, timeout=30.0)
        self._cancel_auto_background(task_id)
        self._cleanup_agent_resources(task_id)
        if local_state.status == TASK_STATUS_COMPLETED:
            result = _coerce_agent_tool_result(outcome.get("result"))
            if result is not None:
                self.complete_agent_task(task_id, result)
                return
            self.fail_agent_task(task_id, "agent completed without result payload")
            return
        if local_state.status == TASK_STATUS_FAILED:
            self.fail_agent_task(
                task_id,
                local_state.error or "agent execution failed",
            )
            return
        if local_state.status == TASK_STATUS_KILLED:
            self.kill_async_agent(task_id)

    def _cleanup_agent_resources(self, task_id: str) -> None:
        with self._lock:
            self._session_hooks.pop(task_id, None)
            already_cleaned = any(
                event.get("task_id") == task_id
                and event.get("type") == "agent_resources_cleaned"
                for event in self._events
            )
            if not already_cleaned:
                self._record_event(
                    {
                        "type": "agent_resources_cleaned",
                        "task_id": task_id,
                    }
                )

    def _install_auto_background(
        self,
        task_id: str,
        auto_background_ms: int,
    ) -> Callable[[], None]:
        cancel_signal = threading.Event()
        self._auto_background_cancel[task_id] = cancel_signal
        if auto_background_ms > 0:
            worker = threading.Thread(
                target=self._auto_background_worker,
                args=(task_id, auto_background_ms, cancel_signal),
                daemon=True,
            )
            worker.start()

        def _cancel() -> None:
            cancel_signal.set()

        return _cancel

    def _cancel_auto_background(self, task_id: str) -> None:
        cancel_signal = self._auto_background_cancel.pop(task_id, None)
        if cancel_signal is not None:
            cancel_signal.set()

    def _auto_background_worker(
        self,
        task_id: str,
        auto_background_ms: int,
        cancel_signal: threading.Event,
    ) -> None:
        if cancel_signal.wait(auto_background_ms / 1000.0):
            return
        self.background_agent_task(task_id)


def _invoke_agent_runner(
    runner: AgentRunner,
    *,
    record: Callable[[str], None],
    report_progress: Callable[[int, int, str], None],
    task_id: str,
) -> AgentToolResult:
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return runner(record, report_progress)

    parameters = signature.parameters
    if "task_id" in parameters:
        return runner(record, report_progress, task_id=task_id)
    if any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    ):
        return runner(record, report_progress, task_id)
    positional_parameters = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(positional_parameters) >= 3:
        return runner(record, report_progress, task_id)
    return runner(record, report_progress)


def make_agent_result(
    *,
    agent_id: str,
    agent_type: str,
    text: str,
    input_tokens: int,
    output_tokens: int,
    tool_use_count: int,
    duration_ms: int = 0,
) -> AgentToolResult:
    usage = AgentUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    return AgentToolResult(
        agent_id=agent_id,
        agent_type=agent_type,
        content=(text,),
        total_tool_use_count=tool_use_count,
        total_duration_ms=duration_ms,
        total_tokens=usage.total_tokens,
        usage=usage,
    )


def validate_agent_orchestration_contract() -> Tuple[bool, Tuple[str, ...]]:
    manager = AgentOrchestrationManager()
    errors: List[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        async_output = os.path.join(tmpdir, "async.log")
        foreground_output = os.path.join(tmpdir, "foreground.log")
        failing_output = os.path.join(tmpdir, "failing.log")

        def _async_runner(record, progress):
            record("spawned\n")
            progress(3, 5, "tool-call")
            time.sleep(0.03)
            record("completed\n")
            return make_agent_result(
                agent_id="agent-async",
                agent_type="local-subagent",
                text="async result",
                input_tokens=3,
                output_tokens=5,
                tool_use_count=1,
            )

        async_task = manager.register_async_agent(
            agent_id="agent-async",
            description="async agent",
            prompt="do async work",
            runner=_async_runner,
            output_file=async_output,
            tool_use_id="tool-async",
        )
        completed_async = manager.wait_for_agent(async_task.task_id, timeout=5.0)
        if completed_async.status != TASK_STATUS_COMPLETED:
            errors.append("async agent did not complete successfully")
        if completed_async.result is None:
            errors.append("async agent completion did not preserve result payload")
        elif completed_async.result.total_tokens != 8:
            errors.append("async agent total token count drifted")
        if completed_async.progress.tool_use_count != 1:
            errors.append("async agent progress updates were not preserved")
        if "completed" not in completed_async.logs:
            errors.append("async agent transcript logs were not captured")

        def _foreground_runner(record, progress):
            record("foreground-start\n")
            progress(2, 4, "draft")
            time.sleep(0.08)
            record("foreground-end\n")
            return make_agent_result(
                agent_id="agent-foreground",
                agent_type="local-subagent",
                text="foreground result",
                input_tokens=2,
                output_tokens=4,
                tool_use_count=1,
            )

        handle = manager.register_agent_foreground(
            agent_id="agent-foreground",
            description="foreground agent",
            prompt="do foreground work",
            runner=_foreground_runner,
            output_file=foreground_output,
            auto_background_ms=10,
        )
        if not handle.background_signal.wait(2.0):
            errors.append("foreground agent did not transition into background mode")
        foreground_state = manager.wait_for_agent(handle.task_id, timeout=5.0)
        if not foreground_state.is_backgrounded:
            errors.append("foreground agent background state was not preserved")
        if foreground_state.status != TASK_STATUS_COMPLETED:
            errors.append("foreground agent did not complete after backgrounding")

        def _failing_runner(record, progress):
            record("before-failure\n")
            progress(1, 0, "starting")
            raise RuntimeError("subagent exploded")

        failing_task = manager.register_async_agent(
            agent_id="agent-failing",
            description="failing agent",
            prompt="fail now",
            runner=_failing_runner,
            output_file=failing_output,
        )
        failed_state = manager.wait_for_agent(failing_task.task_id, timeout=5.0)
        if failed_state.status != TASK_STATUS_FAILED:
            errors.append("failing subagent did not propagate failed status")
        if failed_state.error != "subagent exploded":
            errors.append("failing subagent error message drifted")
        if "subagent exploded" not in failed_state.logs:
            errors.append("failing subagent traceback/log output was not preserved")

        event_types = [event.get("type") for event in manager.list_events()]
        for required in (
            "agent_spawned",
            "agent_progress",
            "agent_backgrounded",
            "agent_completed",
            "agent_failed",
            "task_notification",
        ):
            if required not in event_types:
                errors.append("missing orchestration event '{}'".format(required))

        # B5-M1: session hooks
        hooks_output = os.path.join(tmpdir, "hooks.log")

        def _hooks_runner(record, progress):
            record("hooks-agent-start\n")
            progress(1, 1, "hooks-test")
            return make_agent_result(
                agent_id="agent-hooks",
                agent_type="local-subagent",
                text="hooks result",
                input_tokens=1,
                output_tokens=1,
                tool_use_count=1,
            )

        hooks_task = manager.register_async_agent(
            agent_id="agent-hooks",
            description="hooks test agent",
            prompt="test hooks",
            runner=_hooks_runner,
            output_file=hooks_output,
        )
        test_hooks = [
            SessionHookSpec(event_name="PreToolUse", hook_type="callback"),
            SessionHookSpec(event_name="PostToolUse", hook_type="callback"),
        ]
        manager.register_session_hooks(hooks_task.task_id, test_hooks)
        registered = manager.get_session_hooks(hooks_task.task_id)
        if len(registered) != 2:
            errors.append("session hooks were not registered correctly")
        if registered[0].event_name != "PreToolUse":
            errors.append("session hook event name order incorrect")
        hooks_events = [
            e
            for e in manager.list_events()
            if e.get("type") == "agent_session_hooks_registered"
            and e.get("task_id") == hooks_task.task_id
        ]
        if not hooks_events:
            errors.append("session hooks registration event not emitted")
        manager.wait_for_agent(hooks_task.task_id, timeout=5.0)
        cleaned_hooks = manager.get_session_hooks(hooks_task.task_id)
        if cleaned_hooks:
            errors.append("session hooks were not cleaned up after agent completion")

        # B5-M2: agent resume
        resume_output = os.path.join(tmpdir, "resume.log")

        def _resume_runner(record, progress):
            record("resume-agent-start\n")
            progress(2, 3, "resume-activity")
            return make_agent_result(
                agent_id="agent-resume",
                agent_type="local-subagent",
                text="resume result",
                input_tokens=2,
                output_tokens=3,
                tool_use_count=1,
            )

        resume_task = manager.register_async_agent(
            agent_id="agent-resume",
            description="resume test agent",
            prompt="test resume",
            runner=_resume_runner,
            output_file=resume_output,
        )
        manager.wait_for_agent(resume_task.task_id, timeout=5.0)
        snapshot = manager.store_resume_snapshot(resume_task.task_id)
        if not snapshot or snapshot.get("agent_id") != "agent-resume":
            errors.append("resume snapshot did not capture agent metadata")
        if snapshot.get("status") != TASK_STATUS_COMPLETED:
            errors.append("resume snapshot has wrong status")
        retrieved = manager.get_resume_snapshot(resume_task.task_id)
        if retrieved is None or retrieved.get("agent_id") != "agent-resume":
            errors.append("resume snapshot retrieval failed")
        resumed = manager.resume_agent(resume_task.task_id)
        if resumed.resume_state is None:
            errors.append("resumed agent state missing resume_state")
        resume_events = [
            e
            for e in manager.list_events()
            if e.get("type") == "agent_resumed"
            and e.get("task_id") == resume_task.task_id
        ]
        if not resume_events:
            errors.append("agent_resumed event not emitted")

        # B5-M3: skill MCP source discovery
        mcp_sources = [
            SkillMcpSource(
                server_name="test-server",
                skill_names=("skill-a", "skill-b"),
                source_type="mcp",
            ),
            SkillMcpSource(
                server_name="tool-server",
                skill_names=("tool-x",),
                source_type="mcp",
            ),
        ]
        manager.register_skill_mcp_sources(resume_task.task_id, mcp_sources)
        stored_sources = manager.get_skill_mcp_sources(resume_task.task_id)
        if len(stored_sources) != 2:
            errors.append("skill MCP sources not stored correctly")
        if stored_sources[0].server_name != "test-server":
            errors.append("skill MCP source server name incorrect")
        mcp_events = [
            e
            for e in manager.list_events()
            if e.get("type") == "agent_skill_mcp_sources_registered"
            and e.get("task_id") == resume_task.task_id
        ]
        if not mcp_events:
            errors.append("skill MCP sources registration event not emitted")
        source_event_server_names = mcp_events[0].get("server_names", [])
        if source_event_server_names != ["test-server", "tool-server"]:
            errors.append("MCP source event server names incorrect")

        # B5-M4: skill permission rules
        perm_rules = [
            SkillPermissionRule(
                skill_name="dangerous-skill",
                behavior="deny",
                rule_content="dangerous-skill",
                source="session",
            ),
            SkillPermissionRule(
                skill_name="safe-skill",
                behavior="allow",
                rule_content="safe-skill",
                source="session",
            ),
        ]
        manager.register_skill_permissions(resume_task.task_id, perm_rules)
        stored_rules = manager.get_skill_permissions(resume_task.task_id)
        if len(stored_rules) != 2:
            errors.append("skill permission rules not stored correctly")
        deny_result = manager.check_skill_permission(
            resume_task.task_id,
            "Skill",
            {"skill": "dangerous-skill"},
        )
        if deny_result != "deny":
            errors.append("skill permission deny rule did not match")
        allow_result = manager.check_skill_permission(
            resume_task.task_id,
            "Skill",
            {"skill": "safe-skill"},
        )
        if allow_result != "allow":
            errors.append("skill permission allow rule did not match")
        no_match = manager.check_skill_permission(
            resume_task.task_id,
            "Skill",
            {"skill": "unknown-skill"},
        )
        if no_match is not None:
            errors.append("skill permission should not match unknown skill")
        non_skill = manager.check_skill_permission(
            resume_task.task_id,
            "Bash",
            {"command": "ls"},
        )
        if non_skill is not None:
            errors.append("skill permission should not match non-Skill tool")
        perm_events = [
            e
            for e in manager.list_events()
            if e.get("type") == "agent_skill_permissions_registered"
            and e.get("task_id") == resume_task.task_id
        ]
        if not perm_events:
            errors.append("skill permission registration event not emitted")

    return (len(errors) == 0, tuple(errors))


__all__ = [
    "TASK_STATUS_PENDING",
    "TASK_STATUS_RUNNING",
    "TASK_STATUS_COMPLETED",
    "TASK_STATUS_FAILED",
    "TASK_STATUS_KILLED",
    "AgentOrchestrationManager",
    "AgentProgress",
    "AgentTaskState",
    "agent_progress_payload",
    "agent_summary_payload",
    "AgentToolResult",
    "AgentUsage",
    "ForegroundAgentHandle",
    "ProgressTracker",
    "SessionHookSpec",
    "SkillMcpSource",
    "SkillPermissionRule",
    "make_agent_result",
    "validate_agent_orchestration_contract",
]
