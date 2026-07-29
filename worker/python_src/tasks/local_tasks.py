from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape


TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_KILLED = "killed"
_TERMINAL_STATUSES = frozenset(
    (TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_KILLED)
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class LocalTaskState:
    id: str
    description: str
    task_type: str
    status: str = TASK_STATUS_PENDING
    is_backgrounded: bool = False
    backgrounded_by_user: bool = False
    assistant_auto_backgrounded: bool = False
    created_at_ms: int = field(default_factory=_now_ms)
    started_at_ms: int = field(default_factory=_now_ms)
    ended_at_ms: Optional[int] = None
    tool_use_id: Optional[str] = None
    output_file: Optional[str] = None
    output_offset: int = 0
    exit_code: Optional[int] = None
    logs: str = ""
    notified: bool = False
    agent_type: Optional[str] = None
    command: Tuple[str, ...] = ()
    error: Optional[str] = None
    pid: Optional[int] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def build_task_notification_xml(task: LocalTaskState) -> str:
    parts = ["<task-notification>"]
    parts.append("  <task-id>{}</task-id>".format(escape(task.id)))
    if task.tool_use_id:
        parts.append("  <tool-use-id>{}</tool-use-id>".format(escape(task.tool_use_id)))
    if task.output_file:
        parts.append("  <output-file>{}</output-file>".format(escape(task.output_file)))
    parts.append("  <status>{}</status>".format(escape(task.status)))
    if task.task_type == "local_bash":
        verb = "completed" if task.status == TASK_STATUS_COMPLETED else task.status
        summary = 'Background command "{}" {} (exit code {})'.format(
            task.description,
            verb,
            task.exit_code if task.exit_code is not None else "unknown",
        )
    else:
        verb = "completed" if task.status == TASK_STATUS_COMPLETED else task.status
        summary = 'Background task "{}" {}'.format(task.description, verb)
    parts.append("  <summary>{}</summary>".format(escape(summary)))
    parts.append("</task-notification>")
    return "\n".join(parts)


class LocalTaskManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._tasks: Dict[str, LocalTaskState] = {}
        self._events: List[dict] = []
        self._workers: Dict[str, threading.Thread] = {}
        self._processes: Dict[str, subprocess.Popen] = {}

    def _next_task_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return "{}{}".format(prefix, self._counter)

    def _record_event(self, payload: dict) -> None:
        payload = dict(payload)
        payload.setdefault("timestamp_ms", _now_ms())
        self._events.append(payload)

    def get_task(self, task_id: str) -> LocalTaskState:
        with self._lock:
            return replace(self._tasks[task_id])

    def list_events(self) -> Tuple[dict, ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def create_local_shell_task(
        self,
        *,
        command: Sequence[str],
        description: str,
        output_file: str,
        tool_use_id: Optional[str] = None,
        backgrounded: bool = False,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
        cwd: Optional[str] = None,
    ) -> LocalTaskState:
        task_id = self._next_task_id("t")
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
        )
        state = LocalTaskState(
            id=task_id,
            description=description,
            task_type="local_bash",
            status=TASK_STATUS_RUNNING,
            is_backgrounded=backgrounded,
            backgrounded_by_user=backgrounded_by_user,
            assistant_auto_backgrounded=assistant_auto_backgrounded,
            tool_use_id=tool_use_id,
            output_file=str(output_path),
            command=tuple(command),
            pid=process.pid,
        )
        worker = threading.Thread(
            target=self._watch_shell_process,
            args=(task_id, process),
            daemon=True,
        )
        with self._lock:
            self._tasks[task_id] = state
            self._processes[task_id] = process
            self._workers[task_id] = worker
            self._record_event(
                {
                    "type": "task_started",
                    "task_id": task_id,
                    "task_type": state.task_type,
                    "description": description,
                    "tool_use_id": tool_use_id,
                }
            )
        worker.start()
        return self.get_task(task_id)

    def create_local_main_session_task(
        self,
        *,
        description: str,
        runner: Callable[[Callable[[str], None]], None],
        output_file: str,
        tool_use_id: Optional[str] = None,
    ) -> LocalTaskState:
        task_id = self._next_task_id("s")
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        state = LocalTaskState(
            id=task_id,
            description=description,
            task_type="local_agent",
            agent_type="main-session",
            status=TASK_STATUS_RUNNING,
            is_backgrounded=True,
            tool_use_id=tool_use_id,
            output_file=str(output_path),
        )
        worker = threading.Thread(
            target=self._run_main_session_task,
            args=(task_id, runner),
            daemon=True,
        )
        with self._lock:
            self._tasks[task_id] = state
            self._workers[task_id] = worker
            self._record_event(
                {
                    "type": "task_started",
                    "task_id": task_id,
                    "task_type": state.task_type,
                    "description": description,
                    "tool_use_id": tool_use_id,
                    "workflow_name": "main-session",
                }
            )
        worker.start()
        return self.get_task(task_id)

    def background_task(
        self,
        task_id: str,
        *,
        backgrounded_by_user: bool = False,
        assistant_auto_backgrounded: bool = False,
    ) -> LocalTaskState:
        with self._lock:
            task = self._tasks[task_id]
            if task.is_backgrounded or task.is_terminal:
                return replace(task)
            updated = replace(
                task,
                is_backgrounded=True,
                backgrounded_by_user=(
                    task.backgrounded_by_user or backgrounded_by_user
                ),
                assistant_auto_backgrounded=(
                    task.assistant_auto_backgrounded
                    or assistant_auto_backgrounded
                ),
            )
            self._tasks[task_id] = updated
            self._record_event(
                {
                    "type": "task_backgrounded",
                    "task_id": task_id,
                    "task_type": task.task_type,
                }
            )
            return replace(updated)

    def stop_task(self, task_id: str) -> LocalTaskState:
        process = None
        with self._lock:
            task = self._tasks[task_id]
            if task.is_terminal:
                return replace(task)
            process = self._processes.get(task_id)
        if process is not None:
            process.kill()
        return self._transition_task(
            task_id,
            status=TASK_STATUS_KILLED,
            exit_code=-9,
            error="killed",
        )

    def wait_for_task(self, task_id: str, timeout: float = 5.0) -> LocalTaskState:
        worker = self._workers.get(task_id)
        if worker is not None:
            worker.join(timeout)
        return self.get_task(task_id)

    def _append_logs(self, task_id: str, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            task = self._tasks[task_id]
            updated_logs = task.logs + chunk
            updated = replace(task, logs=updated_logs, output_offset=len(updated_logs))
            self._tasks[task_id] = updated
            output_file = updated.output_file
        if output_file:
            with open(output_file, "a", encoding="utf-8") as handle:
                handle.write(chunk)

    def _transition_task(
        self,
        task_id: str,
        *,
        status: str,
        exit_code: Optional[int],
        error: Optional[str] = None,
    ) -> LocalTaskState:
        with self._lock:
            task = self._tasks[task_id]
            if task.is_terminal:
                return replace(task)
            updated = replace(
                task,
                status=status,
                exit_code=exit_code,
                ended_at_ms=_now_ms(),
                error=error,
            )
            should_notify = updated.is_backgrounded and not updated.notified
            if should_notify:
                updated = replace(updated, notified=True)
            self._tasks[task_id] = updated
            self._record_event(
                {
                    "type": "task_state_changed",
                    "task_id": task_id,
                    "task_type": updated.task_type,
                    "status": status,
                    "exit_code": exit_code,
                }
            )
            if should_notify:
                self._record_event(
                    {
                        "type": "task_notification",
                        "task_id": task_id,
                        "status": status,
                        "xml": build_task_notification_xml(updated),
                    }
                )
            self._processes.pop(task_id, None)
            return replace(updated)

    def _watch_shell_process(self, task_id: str, process: subprocess.Popen) -> None:
        if process.stdout is not None:
            for chunk in iter(process.stdout.readline, ""):
                if not chunk:
                    break
                self._append_logs(task_id, chunk)
        return_code = process.wait()
        status = TASK_STATUS_COMPLETED if return_code == 0 else TASK_STATUS_FAILED
        self._transition_task(
            task_id,
            status=status,
            exit_code=return_code,
            error=None if return_code == 0 else "shell command failed",
        )

    def _run_main_session_task(
        self,
        task_id: str,
        runner: Callable[[Callable[[str], None]], None],
    ) -> None:
        def record(message: str) -> None:
            self._append_logs(task_id, message)

        try:
            runner(record)
        except Exception as exc:
            self._append_logs(task_id, traceback.format_exc())
            self._transition_task(
                task_id,
                status=TASK_STATUS_FAILED,
                exit_code=1,
                error=str(exc),
            )
            return
        self._transition_task(
            task_id,
            status=TASK_STATUS_COMPLETED,
            exit_code=0,
            error=None,
        )


def validate_local_task_contract() -> Tuple[bool, Tuple[str, ...]]:
    manager = LocalTaskManager()
    errors: List[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        shell_output = os.path.join(tmpdir, "shell.log")
        fail_output = os.path.join(tmpdir, "fail.log")
        session_output = os.path.join(tmpdir, "session.log")

        shell_task = manager.create_local_shell_task(
            command=(
                sys.executable,
                "-c",
                "import time; print('boot'); time.sleep(0.05); print('done')",
            ),
            description="demo-shell",
            output_file=shell_output,
            backgrounded=False,
        )
        if shell_task.is_backgrounded:
            errors.append("shell task should start in foreground before backgrounding")
        backgrounded_shell = manager.background_task(shell_task.id)
        if not backgrounded_shell.is_backgrounded:
            errors.append("shell task did not flip to backgrounded state")
        completed_shell = manager.wait_for_task(shell_task.id, timeout=3.0)
        if completed_shell.status != TASK_STATUS_COMPLETED:
            errors.append("backgrounded shell task did not complete successfully")
        if "boot" not in completed_shell.logs or "done" not in completed_shell.logs:
            errors.append("backgrounded shell task logs were not captured")

        def _run_session(record: Callable[[str], None]) -> None:
            record("session-start\n")
            time.sleep(0.02)
            record("session-complete\n")

        session_task = manager.create_local_main_session_task(
            description="demo-session",
            runner=_run_session,
            output_file=session_output,
        )
        if not session_task.is_backgrounded:
            errors.append("main session task should start backgrounded")
        completed_session = manager.wait_for_task(session_task.id, timeout=3.0)
        if completed_session.status != TASK_STATUS_COMPLETED:
            errors.append("main session task did not complete successfully")
        if "session-complete" not in completed_session.logs:
            errors.append("main session transcript was not recorded")

        failing_task = manager.create_local_shell_task(
            command=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('before\\n'); sys.stderr.write('boom\\n'); raise SystemExit(7)",
            ),
            description="failing-shell",
            output_file=fail_output,
            backgrounded=True,
        )
        failed_shell = manager.wait_for_task(failing_task.id, timeout=3.0)
        if failed_shell.status != TASK_STATUS_FAILED:
            errors.append("failing shell task did not enter failed state")
        if failed_shell.exit_code != 7:
            errors.append("failing shell task exit code drifted from subprocess result")
        if "boom" not in failed_shell.logs:
            errors.append("failing shell task stderr was not captured in logs")

        notifications = [
            event
            for event in manager.list_events()
            if event.get("type") == "task_notification"
        ]
        if len(notifications) < 3:
            errors.append("expected terminal notifications for backgrounded tasks")
        else:
            failed_notifications = [
                event
                for event in notifications
                if event.get("task_id") == failed_shell.id
            ]
            if len(failed_notifications) != 1:
                errors.append(
                    "failing task emitted duplicate or missing terminal notification"
                )
            else:
                failed_xml = failed_notifications[0]["xml"]
                if "<status>failed</status>" not in failed_xml:
                    errors.append("failed task notification XML missing failed status")
                if "exit code 7" not in failed_xml:
                    errors.append(
                        "failed task notification XML missing exit code summary"
                    )

    return (len(errors) == 0, tuple(errors))
