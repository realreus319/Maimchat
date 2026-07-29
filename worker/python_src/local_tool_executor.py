from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Mapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from .bootstrap import getOriginalCwd, setOriginalCwd
from .plugins.builtin_plugins import (
    get_builtin_plugin_skill_commands,
    get_builtin_plugins,
)
from .query import (
    AssistantMessage,
    CompactBoundaryMessage,
    Message,
    ProgressMessage,
    TextBlock,
    ToolExecutionUpdate,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    createCompactBoundaryMessage,
    createSystemMessage,
    createUserMessage,
)
from .query_streaming import (
    LocalEchoModelAdapter,
    ModelAdapter,
    QueryStreamConfig,
    _build_compaction_hook_payload,
    _extract_compaction_hook_instructions,
    _inject_compaction_hook_messages,
    build_compaction_attachment_messages,
    build_compaction_summary_message,
    create_model_adapter_from_env,
    stream_query_session,
)
from .services.mcp.client import (
    call_mcp_tool,
    clear_connection_cache,
    connect_servers_batched,
    disconnect_server,
    fetch_tools_for_client,
    list_mcp_resources,
    notify_vscode_file_updated,
    read_mcp_resource,
)
from .services.mcp.config import (
    dedupe_scoped_mcp_servers,
    filter_project_mcp_servers,
    get_project_mcp_configs,
)
from .services.mcp.normalization import normalize_name_for_mcp
from .services.mcp.types import ConfigScope, ScopedMcpServerConfig
from .services.auto_dream import maybe_run_auto_dream
from .skills.bundled_skills import get_bundled_skills
from .skills.load_skills_dir import (
    activate_conditional_skills_for_paths,
    add_skill_directories,
    discover_skill_dirs_for_paths,
    get_conditional_skill_count,
    get_dynamic_skills,
    load_skill_command_from_markdown,
)
from .state.app_state_store import (
    AppState,
    get_default_app_state,
)
from .tasks.agent_orchestration import (
    AgentOrchestrationManager,
    AgentTaskState,
    make_agent_result,
)
from .tasks.local_tasks import (
    LocalTaskManager,
    LocalTaskState,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_KILLED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
)
from .tools.SkillTool.skill_tool import SkillTool
from .tools.agent_tool.agent_tool import AgentTool
from .tools.ask_user_question import (
    ask_user_question,
    ask_user_question_disabled_message,
    is_ask_user_question_enabled,
    normalize_ask_user_questions,
)
from .tools.bash_tool import (
    MAX_OUTPUT_LENGTH as BASH_MAX_OUTPUT_LENGTH,
    BashInput,
    BashTool,
    _wrap_sandbox_command,
    is_search_or_read_bash_command,
    should_auto_background_bash_command,
)
from .tools.file_edit import file_edit, validate_edit_input
from .tools.file_read import file_read
from .tools.file_write import file_write, validate_write_input
from .tools.glob_tool import GlobInput, GlobTool
from .tools.grep_tool import GrepInput, GrepTool
from .tools.lsp_tool import lsp_call, notify_persistent_lsp_document_saved
from .tools.notebook_edit import notebook_edit
from .tools.registry import (
    BUILTIN_TOOL_REGISTRY,
    ToolDefinition as RegistryToolDefinition,
    find_tool_by_name,
)
from .tools.scheduler import (
    AssistantToolUseMessage as SchedulerAssistantToolUseMessage,
    ContextModifier as SchedulerContextModifier,
    MessageUpdate as SchedulerMessageUpdate,
    MessageUpdateLazy as SchedulerMessageUpdateLazy,
    StreamingToolExecutor as SchedulerStreamingToolExecutor,
    ToolCall as SchedulerToolCall,
    ToolDefinition as SchedulerToolDefinition,
    ToolSchedulerOptions,
    ToolUseContext,
)
from .tools.web_fetch import web_fetch
from .tools.web_search import web_search
from .types.permissions import (
    AdditionalWorkingDirectory,
    HookDecisionReason,
    OtherDecisionReason,
    PermissionAllowDecision,
    PermissionAskDecision,
    PermissionDenyDecision,
    RuleDecisionReason,
    SafetyCheckDecisionReason,
    ToolPermissionContext,
    WorkingDirDecisionReason,
)
from .utils.agents import AgentDefinition, build_agent_registry
from .utils.hooks import HookEventResult, dispatch_hook_event
from .utils.hooks import HookLifecycleEvent
from .utils.model_selection import resolve_main_loop_model
from .utils.plugin_registry import sync_managed_plugins_runtime_state
from .utils.permissions.denial_tracking import (
    build_denial_tracking_signature,
    record_permission_denial,
    reset_denial_tracking_state,
    should_fallback_to_interactive_prompt,
)
from .utils.permissions.approval import resolve_approval_state
from .utils.permissions.permissions import has_permissions_to_use_tool
from .utils.sandbox.sandbox_adapter import (
    is_sandboxing_enabled,
    should_use_sandbox,
)
from .utils.schema_registry import (
    normalize_app_state_via_registry,
    parse_mcp_config_via_registry,
    tool_definition_to_schema_payload,
    validate_tool_input_payload_via_registry,
    validate_tool_output_payload_via_registry,
)
from .utils.tokenization import count_text_tokens
from .utils.user_type import is_ant_user
from .utils.tasks import (
    TASK_COMPLETED,
    TASK_DELETED,
    TASK_IN_PROGRESS,
    TASK_PENDING,
    create_task,
    get_task,
    is_todo_v2_enabled,
    list_tasks,
    to_list_task,
    to_public_task,
    update_task,
)


SUPPORTED_TOOL_NAMES = frozenset(
    {
        "Agent",
        "AskUserQuestion",
        "Bash",
        "Config",
        "CronCreate",
        "CronDelete",
        "CronList",
        "CtxInspect",
        "Edit",
        "EnterWorktree",
        "EnterPlanMode",
        "ExitWorktree",
        "ExitPlanMode",
        "Glob",
        "Grep",
        "LSP",
        "ListMcpResourcesTool",
        "NotebookEdit",
        "Read",
        "ReadMcpResourceTool",
        "REPL",
        "SendUserMessage",
        "Skill",
        "Snip",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

_DEFAULT_TODO_KEY = "session"
_PLAN_MODE_ALLOWED_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "Config",
        "CronList",
        "EnterPlanMode",
        "ExitPlanMode",
        "Glob",
        "Grep",
        "CtxInspect",
        "LSP",
        "ListMcpResourcesTool",
        "Read",
        "ReadMcpResourceTool",
        "SendUserMessage",
        "Snip",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
    }
)
_DEFAULT_AGENT_MODEL = "claude-sonnet-4-5"
_DEFAULT_AGENT_TIMEOUT_SECONDS = 15.0
_DEFAULT_AGENT_AUTO_BACKGROUND_MS = 2_000
_DEFAULT_BASH_AUTO_BACKGROUND_MS = 15_000
_TASK_OUTPUT_DEFAULT_WAIT_MS = 30_000
_TASK_OUTPUT_MAX_WAIT_MS = 600_000
_DEFAULT_MAX_CONTEXT_TOKENS = 200_000
_MAX_AGENT_DEPTH = 3
_MAX_FILE_HISTORY_SNAPSHOTS = 100
_SESSION_MESSAGE_REPLACEMENT_KEY = "__replace_session_messages__"
_TOOL_RESULT_TRUNCATION_PREVIEW_CHARS = 4_096
_GATED_TOOL_ENV_VARS = {
    "CtxInspect": "CONTEXT_COLLAPSE",
    "LSP": "ENABLE_LSP_TOOL",
    "Snip": "HISTORY_SNIP",
}
_ANT_ONLY_TOOL_NAMES = frozenset()
_CRON_JOBS_SETTINGS_KEY = "_cronJobs"
_CRON_JOBS_FILE_NAME = "cron_jobs.json"


@dataclass(frozen=True)
class _ToolDispatchResult:
    payload: Mapping[str, Any] | str | None = None
    rendered: str | None = None
    block_content: Any = None
    new_context: Optional[Mapping[str, Any]] = None
    pre_updates: tuple[ToolExecutionUpdate, ...] = ()
    extra_updates: tuple[ToolExecutionUpdate, ...] = ()
    hook_events: tuple[tuple[str, Mapping[str, Any]], ...] = ()
    is_error: bool = False


@dataclass(frozen=True)
class _ToolExecutionOutcome:
    updates: tuple[ToolExecutionUpdate, ...]


@dataclass(frozen=True)
class _PermissionCheckResult:
    raw_input: Mapping[str, Any]
    blocked: _ToolDispatchResult | None = None
    extra_updates: tuple[ToolExecutionUpdate, ...] = ()


def _validation_error_result(validation: Any) -> _ToolDispatchResult:
    message = getattr(validation, "message", "") or "Invalid tool request."
    payload: dict[str, Any] = {
        "message": message,
        "error_code": getattr(validation, "error_code", 0),
    }
    metadata = getattr(validation, "metadata", None)
    if isinstance(metadata, Mapping) and metadata:
        payload["meta"] = dict(metadata)
        message = f"{message}\nmeta: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)}"
    return _ToolDispatchResult(payload=payload, rendered=message, is_error=True)


@dataclass(frozen=True)
class _SubagentInvocationOutcome:
    payload: Mapping[str, Any]
    extra_updates: tuple[ToolExecutionUpdate, ...] = ()


@dataclass(frozen=True)
class _ToolDispatchStreamEvent:
    update: ToolExecutionUpdate | None = None
    result: _ToolDispatchResult | None = None


_STREAMING_SESSION_END = object()


class _IncrementalToolRunSession:
    def __init__(
        self,
        owner: "LocalToolExecutor",
        *,
        signal: Optional[asyncio.Event] = None,
    ) -> None:
        self._owner = owner
        self._scheduler_tools = owner._scheduler_tool_definitions()
        self._scheduler_context = ToolUseContext(
            options=ToolSchedulerOptions(tools=self._scheduler_tools),
            state=dict(owner._current_permission_context()),
        )
        if _external_signal_is_set(signal):
            self._scheduler_context.abort_controller.abort(
                _external_signal_reason(signal)
            )
        self._executor = SchedulerStreamingToolExecutor(
            self._scheduler_tools,
            None,
            self._scheduler_context,
            owner._run_scheduler_tool_use,
        )
        self._signal = signal
        self._queue: asyncio.Queue[ToolExecutionUpdate | BaseException | object] = (
            asyncio.Queue()
        )
        self._pump_task: asyncio.Task[None] | None = None
        self._abort_watcher: asyncio.Task[None] | None = None
        self._finished = False
        self._closed = False
        if (
            signal is not None
            and not self._scheduler_context.abort_controller.signal.aborted
        ):
            self._abort_watcher = asyncio.create_task(
                _watch_external_signal(signal, self._scheduler_context.abort_controller)
            )

    async def add_tool_use_messages(
        self,
        tool_use_blocks: Sequence[AssistantMessage],
    ) -> None:
        if self._closed:
            raise RuntimeError("Streaming tool session is already closed")

        disabled_updates: list[ToolExecutionUpdate] = []
        added_tool = False
        if any(
            isinstance(block, ToolUseBlock) and block.name.startswith("mcp__")
            for assistant_message in tool_use_blocks
            for block in assistant_message.message.content
        ):
            await self._owner._ensure_mcp_clients_loaded()
        for assistant_message in tool_use_blocks:
            for block in assistant_message.message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                normalized_name = _normalize_tool_name(block.name)
                if (
                    normalized_name not in self._owner._enabled_tool_names()
                    and normalized_name not in self._owner._mcp_tool_lookup
                ):
                    if normalized_name == "AskUserQuestion" and not self._owner._is_tool_enabled(
                        normalized_name
                    ):
                        disabled_updates.append(
                            _tool_result_update(
                                tool_use_id=block.id,
                                rendered=ask_user_question_disabled_message(),
                                assistant_uuid=assistant_message.uuid,
                                is_error=True,
                            )
                        )
                    continue
                self._executor.add_tool(
                    SchedulerToolCall(
                        id=block.id,
                        name=normalized_name,
                        input=dict(block.input),
                    ),
                    SchedulerAssistantToolUseMessage(
                        uuid=assistant_message.uuid,
                        tool_use_ids=(block.id,),
                    ),
                )
                added_tool = True

        for update in disabled_updates:
            self._queue.put_nowait(update)

        if added_tool and (self._pump_task is None or self._pump_task.done()):
            self._pump_task = asyncio.create_task(self._pump_updates())

    async def _pump_updates(self) -> None:
        try:
            async for update in self._executor.get_remaining_results():
                normalized = self._owner._scheduler_message_update_to_query(update)
                if normalized is not None:
                    await self._queue.put(normalized)
        except Exception as exc:
            await self._queue.put(exc)
        finally:
            if self._finished:
                await self._queue.put(_STREAMING_SESSION_END)

    async def next_update(self) -> ToolExecutionUpdate | None:
        item = await self._queue.get()
        if item is _STREAMING_SESSION_END:
            return None
        if isinstance(item, BaseException):
            raise item
        return item

    def get_completed_updates_nowait(self) -> tuple[ToolExecutionUpdate, ...]:
        updates: list[ToolExecutionUpdate] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STREAMING_SESSION_END:
                self._finished = True
                self._queue.put_nowait(_STREAMING_SESSION_END)
                break
            if isinstance(item, BaseException):
                raise item
            updates.append(item)
        return tuple(updates)

    async def finish(self) -> None:
        self._finished = True
        if self._pump_task is None or self._pump_task.done():
            self._queue.put_nowait(_STREAMING_SESSION_END)

    def discard(self) -> None:
        self._executor.discard()
        self._scheduler_context.abort_controller.abort("streaming_fallback")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        if self._abort_watcher is not None:
            self._abort_watcher.cancel()
            try:
                await self._abort_watcher
            except asyncio.CancelledError:
                pass


def _structured_patch_payload(hunks: Sequence[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for hunk in hunks:
        payload.append(
            {
                "old_start": getattr(hunk, "old_start", None),
                "old_lines": getattr(hunk, "old_lines", None),
                "new_start": getattr(hunk, "new_start", None),
                "new_lines": getattr(hunk, "new_lines", None),
                "lines": list(getattr(hunk, "lines", ()) or ()),
            }
        )
    return payload


def _git_diff_payload(diff: Any) -> Mapping[str, Any] | None:
    if diff is None:
        return None
    payload = {
        "filename": getattr(diff, "filename", None),
        "status": getattr(diff, "status", None),
        "additions": getattr(diff, "additions", None),
        "deletions": getattr(diff, "deletions", None),
        "changes": getattr(diff, "changes", None),
        "patch": getattr(diff, "patch", None),
        "repository": getattr(diff, "repository", None),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _read_utf8_text_if_exists(file_path: str) -> str | None:
    try:
        with open(file_path, encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _coerce_history_snapshots(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    snapshots: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            snapshots.append(dict(item))
    return snapshots


def _coerce_history_tracked_files(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    tracked: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            continue
        if item not in tracked:
            tracked.append(item)
    return tracked


def _string_mapping_value(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _mapping_value_is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "skill",
        }
    return False


def _mcp_resource_path_hint(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path or value
    return unquote(path).replace("\\", "/").strip()


def _looks_like_mcp_skill_resource(resource: Mapping[str, Any]) -> bool:
    resource_type = (
        _string_mapping_value(resource, "resourceType")
        or _string_mapping_value(resource, "kind")
        or _string_mapping_value(resource, "type")
    )
    if resource_type is not None and resource_type.lower() == "skill":
        return True

    annotations = resource.get("annotations")
    if isinstance(annotations, Mapping):
        for key in (
            "skill",
            "isSkill",
            "claude:skill",
            "claudeSkill",
            "x-claude-skill",
        ):
            if _mapping_value_is_truthy(annotations.get(key)):
                return True

    for key in ("uri", "name", "title"):
        value = _string_mapping_value(resource, key)
        if value is None:
            continue
        normalized = _mcp_resource_path_hint(value).lower()
        basename = normalized.rsplit("/", 1)[-1]
        if basename == "skill.md" or basename.endswith(".skill.md"):
            return True
        if "/skills/" in normalized and basename.endswith(".md"):
            return True
    return False


def _normalize_mcp_skill_name(candidate: str) -> str:
    lowered = candidate.strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", lowered)
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized.strip("-.")


def _derive_mcp_skill_name(resource: Mapping[str, Any]) -> str | None:
    for key in ("skillName", "commandName", "slug"):
        explicit = _string_mapping_value(resource, key)
        if explicit is not None:
            normalized = _normalize_mcp_skill_name(explicit)
            if normalized:
                return normalized

    candidates: list[str] = []
    for key in ("uri", "name", "title"):
        raw_value = _string_mapping_value(resource, key)
        if raw_value is None:
            continue
        normalized = _mcp_resource_path_hint(raw_value)
        basename = normalized.rsplit("/", 1)[-1]
        basename_lower = basename.lower()
        if basename_lower == "skill.md":
            parent = normalized.rstrip("/").rsplit("/", 2)[-2] if "/" in normalized else ""
            if parent:
                candidates.append(parent)
            continue
        if basename_lower.endswith(".skill.md"):
            candidates.append(basename[:-9])
            continue
        if "/skills/" in normalized.lower() and basename_lower.endswith(".md"):
            candidates.append(basename[:-3])
            continue
        if basename and basename_lower != "skill.md":
            candidates.append(basename)

    for candidate in candidates:
        normalized = _normalize_mcp_skill_name(candidate)
        if normalized:
            return normalized
    return None


def _mcp_skill_resource_cache_key(resource: Mapping[str, Any]) -> str:
    parts = [
        _string_mapping_value(resource, "server") or "",
        _string_mapping_value(resource, "uri") or "",
        _string_mapping_value(resource, "name") or "",
        _string_mapping_value(resource, "title") or "",
        _string_mapping_value(resource, "mimeType") or "",
        _string_mapping_value(resource, "etag") or "",
        _string_mapping_value(resource, "version") or "",
    ]
    return "\x1f".join(parts)


def _cron_to_human_schedule(cron_expr: str) -> str:
    """Convert cron expression to human-readable schedule.

    Examples:
        "0 */6 * * *" -> "every 6 hours"
        "0 9 * * *" -> "every day at 9:00 AM"
        "30 4 * * 1-5" -> "weekdays at 4:30 AM"
        "0 */2 * * *" -> "every 2 hours"
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return f"cron: {cron_expr}"

    minute, hour, day_of_month, month, day_of_week = parts

    # Every N hours: "0 */N * * *"
    if minute == "0" and hour.startswith("*/"):
        try:
            hours = int(hour[2:])
            if hours == 1:
                return "every hour"
            if hours == 24:
                return "every day"
            return f"every {hours} hours"
        except ValueError:
            pass

    # Every N minutes: "*/N * * * *"
    if hour == "*" and minute.startswith("*/"):
        try:
            mins = int(minute[2:])
            if mins == 1:
                return "every minute"
            return f"every {mins} minutes"
        except ValueError:
            pass

    # Specific hour: "M H * * *"
    if day_of_month == "*" and month == "*" and day_of_week == "*":
        if hour != "*" and minute != "*":
            try:
                h = int(hour)
                m = int(minute)
                period = "AM" if h < 12 else "PM"
                display_hour = h if h <= 12 else h - 12
                if display_hour == 0:
                    display_hour = 12
                return f"every day at {display_hour}:{m:02d} {period}"
            except ValueError:
                pass

    # Weekdays at specific time: "M H * * 1-5"
    if day_of_month == "*" and month == "*" and day_of_week not in ("*", "?"):
        try:
            h = int(hour) if hour != "*" else 0
            m = int(minute) if minute != "*" else 0
            period = "AM" if h < 12 else "PM"
            display_hour = h if h <= 12 else h - 12
            if display_hour == 0:
                display_hour = 12
            days_map = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
                        "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday"}
            if "-" in day_of_week:
                return f"weekdays at {display_hour}:{m:02d} {period}"
            return f"{days_map.get(day_of_week, day_of_week)} at {display_hour}:{m:02d} {period}"
        except ValueError:
            pass

    # Monthly: "M H D * *"
    if day_of_month != "*" and day_of_month != "?":
        try:
            h = int(hour) if hour != "*" else 0
            m = int(minute) if minute != "*" else 0
            period = "AM" if h < 12 else "PM"
            display_hour = h if h <= 12 else h - 12
            if display_hour == 0:
                display_hour = 12
            return f"monthly on day {day_of_month} at {display_hour}:{m:02d} {period}"
        except ValueError:
            pass

    return f"cron: {cron_expr}"


@dataclass
class LocalToolExecutor:
    mcp_clients: Sequence[Any] = ()
    mcp_server_configs: Mapping[str, ScopedMcpServerConfig] = field(default_factory=dict)
    mcp_owner_id: str | None = None
    mcp_cleanup_owned_on_close: bool = False
    read_file_state: dict[str, Any] = field(default_factory=dict)
    app_state: AppState = field(default_factory=get_default_app_state)
    task_manager: LocalTaskManager = field(default_factory=LocalTaskManager)
    agent_manager: AgentOrchestrationManager = field(
        default_factory=AgentOrchestrationManager
    )
    session_messages: tuple[Message, ...] = ()
    scoped_hook_configs: tuple[Mapping[str, object], ...] = ()
    inline_agents: tuple[AgentDefinition, ...] = ()
    approval_prompt_handler: (
        Callable[[str, str, Mapping[str, Any], PermissionAskDecision], Any] | None
    ) = None
    hook_event_listener: Callable[[HookLifecycleEvent], Any] | None = None
    _mcp_clients_initialized: bool = field(default=False, init=False, repr=False)
    _task_event_cursor: int = field(default=0, init=False, repr=False)
    _agent_event_cursor: int = field(default=0, init=False, repr=False)
    _tool_schema_cache_key: tuple[str, ...] | None = field(
        default=None, init=False, repr=False
    )
    _tool_schema_cache: tuple[dict[str, object], ...] = field(
        default_factory=tuple, init=False, repr=False
    )
    _suppressed_task_event_ids: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _mcp_skill_commands: tuple[Any, ...] = field(
        default_factory=tuple, init=False, repr=False
    )
    _mcp_skill_cache_key: tuple[str, ...] | None = field(
        default=None, init=False, repr=False
    )
    _mcp_skill_cache_loaded: bool = field(default=False, init=False, repr=False)
    # MCP server tool surface, keyed by the ``mcp__<server>__<tool>`` identifier the
    # model sees. ``_mcp_tool_definitions`` feeds tool schemas / ToolSearch; the lookup
    # maps the identifier back to the (server name, raw tool name) used to invoke it.
    _mcp_tool_definitions: tuple[RegistryToolDefinition, ...] = field(
        default_factory=tuple, init=False, repr=False
    )
    _mcp_tool_lookup: dict[str, tuple[str, str]] = field(
        default_factory=dict, init=False, repr=False
    )
    _mcp_tools_loaded: bool = field(default=False, init=False, repr=False)
    _repl_namespace: dict[str, Any] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        normalize_app_state_via_registry(self.app_state)
        self.inline_agents = tuple(self.inline_agents or ())
        self._task_event_cursor = len(self.task_manager.list_events())
        self._agent_event_cursor = len(self.agent_manager.list_events())

    def _settings_runtime_context(self) -> Mapping[str, Any] | None:
        context = getattr(self.app_state, "_settings_runtime_context", None)
        if isinstance(context, Mapping):
            return context
        return None

    def _resolve_agent_definition(self, value: str | None) -> AgentDefinition | None:
        runtime_context = self._settings_runtime_context()
        config_home = None
        if isinstance(runtime_context, Mapping):
            raw_config_home = runtime_context.get("config_home")
            if isinstance(raw_config_home, str) and raw_config_home.strip():
                config_home = raw_config_home.strip()
        registry = build_agent_registry(
            config_home=config_home,
            inline_agents=self.inline_agents,
        )
        return registry.resolve(value)

    def get_tool_schemas(self) -> tuple[dict[str, object], ...]:
        self._prime_mcp_tools_sync()
        mcp_definitions = self._mcp_tool_definitions
        cache_key = tuple(sorted(self._enabled_tool_names())) + tuple(
            definition.name for definition in mcp_definitions
        )
        if self._tool_schema_cache_key == cache_key and self._tool_schema_cache:
            return self._tool_schema_cache

        definitions = []
        for tool_name in sorted(self._enabled_tool_names()):
            definition = BUILTIN_TOOL_REGISTRY.get(tool_name)
            if definition is not None:
                definitions.append(tool_definition_to_schema_payload(definition))
        for definition in mcp_definitions:
            definitions.append(tool_definition_to_schema_payload(definition))
        self._tool_schema_cache_key = cache_key
        self._tool_schema_cache = tuple(definitions)
        return self._tool_schema_cache

    def bind_session_messages(self, messages: Sequence[Message]) -> None:
        self.session_messages = tuple(messages)

    async def emit_hook_event(
        self,
        event_name: str,
        payload: Mapping[str, Any],
    ) -> HookEventResult:
        normalize_app_state_via_registry(self.app_state)
        result = await dispatch_hook_event(
            event_name,
            payload,
            app_state=self.app_state,
            cwd=self._working_directory(),
            scoped_hook_configs=self.scoped_hook_configs,
            on_event=self.hook_event_listener,
            runtime_context={
                "tool_executor": self,
                "session_messages": self.session_messages,
            },
        )
        if event_name == "PostSampling":
            await self._maybe_execute_auto_dream()
        return result

    async def _maybe_execute_auto_dream(self) -> None:
        context = self._current_permission_context()
        if context.get("_auto_dream_active"):
            return

        runtime_context = self._settings_runtime_context()
        config_home: str | None = None
        project_root: str | None = None
        if isinstance(runtime_context, Mapping):
            raw_config_home = runtime_context.get("config_home")
            if isinstance(raw_config_home, str) and raw_config_home.strip():
                config_home = raw_config_home.strip()
            raw_project_root = runtime_context.get("project_root")
            if isinstance(raw_project_root, str) and raw_project_root.strip():
                project_root = raw_project_root.strip()

        if config_home is None:
            raw_context_config_home = context.get("configHome") or context.get(
                "config_home"
            )
            if (
                isinstance(raw_context_config_home, str)
                and raw_context_config_home.strip()
            ):
                config_home = raw_context_config_home.strip()
        if project_root is None:
            project_root = self._working_directory()

        current_session_id = getattr(self.app_state, "repl_bridge_session_id", None)
        if not isinstance(current_session_id, str) or not current_session_id.strip():
            raw_session_id = context.get("sessionId") or context.get("session_id")
            current_session_id = (
                raw_session_id.strip()
                if isinstance(raw_session_id, str) and raw_session_id.strip()
                else None
            )

        async def _run_consolidation_agent(task: object) -> Mapping[str, Any]:
            original_context = self._current_permission_context()
            guarded_context = dict(original_context)
            guarded_context["_auto_dream_active"] = True
            self.app_state.tool_permission_context = guarded_context
            try:
                result = await self.run_agent_hook(
                    event_name="AutoDream",
                    prompt=str(getattr(task, "prompt", "")),
                    model=None,
                    timeout_seconds=600.0,
                    session_messages=self.session_messages,
                )
            finally:
                self.app_state.tool_permission_context = original_context
            return result if isinstance(result, Mapping) else {}

        await maybe_run_auto_dream(
            settings=self.app_state.settings,
            project_root=project_root,
            config_home=config_home,
            current_session_id=current_session_id,
            app_state=self.app_state,
            runner=_run_consolidation_agent,
        )

    async def run_agent_hook(
        self,
        *,
        event_name: str,
        prompt: str,
        model: str | None,
        timeout_seconds: float,
        session_messages: Sequence[Message] = (),
    ) -> object:
        from .query import QuerySession

        context = self._current_permission_context()
        current_depth = int(context.get("_agent_depth", 0))
        if current_depth >= _MAX_AGENT_DEPTH:
            return {"message": "Agent hook recursion depth exceeded", "level": "warning"}

        agent_id = f"hook-agent-{uuid.uuid4()}"
        child_context = dict(context)
        child_context["_agent_depth"] = current_depth + 1
        child_context["cwd"] = self._working_directory()
        child_context["mode"] = "dontAsk"

        child_settings = dict(self.app_state.settings)
        child_settings["hooks"] = {}
        child_app_state = get_default_app_state(
            settings=child_settings,
            initial_mode="dontAsk",
            settings_runtime_context=self._settings_runtime_context(),
        )
        child_app_state.main_loop_model = self.app_state.main_loop_model
        child_app_state.main_loop_model_for_session = model or self._selected_model()
        child_app_state.tool_permission_context = child_context
        child_app_state.session_hooks = {}

        child_executor = LocalToolExecutor(
            mcp_server_configs=self._child_mcp_server_configs(),
            mcp_owner_id=f"hook:{agent_id}",
            mcp_cleanup_owned_on_close=True,
            read_file_state=self.read_file_state,
            app_state=child_app_state,
            task_manager=self.task_manager,
            agent_manager=self.agent_manager,
            scoped_hook_configs=(),
            inline_agents=tuple(self.inline_agents),
        )
        try:
            model_adapter = (
                create_model_adapter_from_env(model=model or self._selected_model())
                or LocalEchoModelAdapter()
            )
            await child_executor._ensure_mcp_clients_loaded()
            base_messages = tuple(session_messages)
            session = QuerySession.fromMessages(base_messages).startTurn()
            session = session.appendMessage(createUserMessage(content=prompt))
            last_assistant_text = ""
            assistant_turns = 0

            async with asyncio.timeout(timeout_seconds):
                async for event in stream_query_session(
                    session,
                    model_adapter=model_adapter,
                    tool_executor=child_executor,
                    config=QueryStreamConfig(
                        tools=_filter_agent_hook_tool_schemas(
                            child_executor.get_tool_schemas()
                        ),
                        tool_executor_timeout_seconds=5.0,
                    ),
                ):
                    output = event.output
                    if not isinstance(output, AssistantMessage):
                        continue
                    assistant_turns += 1
                    if assistant_turns > 50:
                        return {
                            "message": "Agent hook exceeded max turns",
                            "level": "warning",
                        }
                    text = _assistant_message_text(output)
                    if text:
                        last_assistant_text = text
            if not last_assistant_text:
                return {
                    "message": f"Agent hook returned no assistant JSON for {event_name}",
                    "level": "warning",
                }
            parsed = _parse_hook_condition_json(last_assistant_text)
            if parsed is None:
                return {
                    "message": (
                        f"Agent hook returned invalid JSON for {event_name}: "
                        f"{last_assistant_text}"
                    ),
                    "level": "warning",
                }
            return parsed
        except TimeoutError:
            return {
                "message": f"Agent hook timed out after {timeout_seconds:.0f}s",
                "level": "warning",
            }
        except Exception as exc:
            return {
                "message": f"Agent hook failed for {event_name}: {exc}",
                "level": "warning",
            }
        finally:
            await child_executor.aclose()

    async def _drain_task_event_hook_updates(self) -> tuple[ToolExecutionUpdate, ...]:
        updates: list[ToolExecutionUpdate] = []
        task_events = self.task_manager.list_events()
        for event in task_events[self._task_event_cursor :]:
            updates.extend(
                await self._task_event_hook_updates(
                    event,
                    event_source="local_task_manager",
                )
            )
        self._task_event_cursor = len(task_events)

        agent_events = self.agent_manager.list_events()
        for event in agent_events[self._agent_event_cursor :]:
            updates.extend(
                await self._task_event_hook_updates(
                    event,
                    event_source="agent_orchestration",
                )
            )
        self._agent_event_cursor = len(agent_events)
        return tuple(updates)

    async def _ensure_mcp_clients_loaded(self) -> None:
        if self.mcp_clients:
            self._mcp_clients_initialized = True
            await self._refresh_mcp_skill_commands()
            # tools/list is fetched once per session; don't re-fetch on every
            # subsequent call (ToolSearch, each mcp tool dispatch, …).
            if not self._mcp_tools_loaded:
                await self._refresh_mcp_tool_definitions()
            return

        configs = self._resolved_mcp_server_configs()
        if not configs:
            self._mcp_skill_commands = ()
            self._mcp_skill_cache_key = ()
            self._mcp_skill_cache_loaded = True
            self._mcp_tool_definitions = ()
            self._mcp_tool_lookup = {}
            self._mcp_tools_loaded = True
            return
        if self._mcp_clients_initialized:
            return
        self._mcp_clients_initialized = True
        self.mcp_clients = tuple(
            await connect_servers_batched(
                dict(configs),
                owner_id=self.mcp_owner_id,
            )
        )
        await self._refresh_mcp_skill_commands()
        await self._refresh_mcp_tool_definitions()

    async def _refresh_mcp_tool_definitions(self) -> None:
        """Fetch tools/list from every connected MCP server and expose each as a
        deferred ``mcp__<server>__<tool>`` tool definition the model can discover
        via ToolSearch and invoke through ``_dispatch_tool``."""

        definitions: list[RegistryToolDefinition] = []
        lookup: dict[str, tuple[str, str]] = {}
        for client in self.mcp_clients:
            if getattr(client, "type", None) != "connected":
                continue
            server_name = str(getattr(client, "name", "") or "")
            if not server_name:
                continue
            tools = await fetch_tools_for_client(client)
            normalized_server = normalize_name_for_mcp(server_name)
            for tool in tools:
                if not isinstance(tool, Mapping):
                    continue
                raw_tool_name = tool.get("name")
                if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
                    continue
                qualified = (
                    f"mcp__{normalized_server}__{normalize_name_for_mcp(raw_tool_name)}"
                )
                if qualified in lookup:
                    continue
                raw_description = tool.get("description")
                description = (
                    raw_description
                    if isinstance(raw_description, str) and raw_description.strip()
                    else f"{raw_tool_name} (MCP tool from {server_name})"
                )
                raw_schema = tool.get("inputSchema")
                if not isinstance(raw_schema, Mapping):
                    raw_schema = {"type": "object", "properties": {}}
                definitions.append(
                    RegistryToolDefinition(
                        name=qualified,
                        description=description,
                        gate_type="",
                        gate_expression="",
                        linux_exposure="",
                        raw_input_schema=dict(raw_schema),
                        should_defer=True,
                        search_hint=f"{server_name} {raw_tool_name}",
                        source_module=f"mcp:{server_name}",
                    )
                )
                lookup[qualified] = (server_name, raw_tool_name)
        self._mcp_tool_definitions = tuple(definitions)
        self._mcp_tool_lookup = lookup
        self._mcp_tools_loaded = True
        # Schema cache is keyed on builtin tool names only; force a rebuild so the
        # next get_tool_schemas() picks up the freshly discovered MCP tools.
        self._tool_schema_cache_key = None
        self._tool_schema_cache = ()

    def _prime_mcp_tools_sync(self) -> None:
        if self._mcp_tools_loaded:
            return
        if not self.mcp_clients and not self._resolved_mcp_server_configs():
            self._mcp_tool_definitions = ()
            self._mcp_tool_lookup = {}
            self._mcp_tools_loaded = True
            return
        try:
            asyncio.get_running_loop()
            return
        except RuntimeError:
            pass
        try:
            asyncio.run(self._ensure_mcp_clients_loaded())
        except Exception:
            return

    async def _refresh_mcp_skill_commands(self) -> None:
        if not self.mcp_clients:
            self._mcp_skill_commands = ()
            self._mcp_skill_cache_key = ()
            self._mcp_skill_cache_loaded = True
            return

        try:
            resources = await list_mcp_resources(self.mcp_clients)
        except Exception:
            return

        skill_resources = [
            resource for resource in resources if _looks_like_mcp_skill_resource(resource)
        ]
        cache_key = tuple(
            sorted(_mcp_skill_resource_cache_key(resource) for resource in skill_resources)
        )
        if self._mcp_skill_cache_loaded and self._mcp_skill_cache_key == cache_key:
            return

        commands_by_name: dict[str, Any] = {}
        for resource in sorted(
            skill_resources,
            key=lambda item: (
                _string_mapping_value(item, "server") or "",
                _string_mapping_value(item, "uri") or "",
                _string_mapping_value(item, "name") or "",
            ),
        ):
            command = await self._load_mcp_origin_skill_command(resource)
            if command is None:
                continue
            commands_by_name[command.name] = command

        self._mcp_skill_commands = tuple(commands_by_name.values())
        self._mcp_skill_cache_key = cache_key
        self._mcp_skill_cache_loaded = True

    async def _load_mcp_origin_skill_command(
        self,
        resource: Mapping[str, Any],
    ) -> Any | None:
        server = _string_mapping_value(resource, "server")
        uri = _string_mapping_value(resource, "uri")
        if server is None or uri is None:
            return None

        skill_name = _derive_mcp_skill_name(resource)
        if skill_name is None:
            return None

        try:
            payload = await read_mcp_resource(self.mcp_clients, server, uri)
        except Exception:
            return None

        document = self._extract_mcp_skill_document(payload)
        if not document:
            return None

        try:
            return load_skill_command_from_markdown(
                skill_name=skill_name,
                document=document,
                source="mcp-origin",
                base_dir=None,
                loaded_from=f"mcp:{server}:{uri}",
                description_fallback_label="MCP Skill",
            )
        except Exception:
            return None

    def _extract_mcp_skill_document(self, payload: Mapping[str, Any]) -> str:
        contents = payload.get("contents")
        if not isinstance(contents, Sequence) or isinstance(
            contents, (str, bytes, bytearray)
        ):
            return ""

        parts: list[str] = []
        for content in contents:
            if not isinstance(content, Mapping):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
                continue
            blob_saved_to = content.get("blobSavedTo")
            mime_type = content.get("mimeType")
            if (
                isinstance(blob_saved_to, str)
                and blob_saved_to
                and isinstance(mime_type, str)
                and mime_type.startswith("text/")
            ):
                loaded = _read_utf8_text_if_exists(blob_saved_to)
                if loaded is not None and loaded.strip():
                    parts.append(loaded)
        return "\n\n".join(part.strip() for part in parts if part.strip()).strip()

    def _prime_mcp_skill_commands_sync(self) -> None:
        if self._mcp_skill_cache_loaded:
            return
        if not self.mcp_clients and not self._resolved_mcp_server_configs():
            self._mcp_skill_commands = ()
            self._mcp_skill_cache_key = ()
            self._mcp_skill_cache_loaded = True
            return
        try:
            asyncio.get_running_loop()
            return
        except RuntimeError:
            pass
        try:
            asyncio.run(self._ensure_mcp_clients_loaded())
        except Exception:
            return

    async def aclose(self) -> None:
        if not self.mcp_cleanup_owned_on_close or not self.mcp_owner_id:
            return

        owned_clients = tuple(self.mcp_clients)
        self.mcp_clients = ()
        for client in owned_clients:
            if getattr(client, "type", None) != "connected":
                continue
            try:
                await disconnect_server(
                    str(client.name),
                    client.config,
                    getattr(client, "scope", "user"),
                    owner_id=getattr(client, "owner_id", None) or self.mcp_owner_id,
                )
            except Exception:
                continue
        clear_connection_cache(owner_id=self.mcp_owner_id)

    def _resolved_mcp_server_configs(self) -> dict[str, ScopedMcpServerConfig]:
        normalize_app_state_via_registry(self.app_state)
        if self.mcp_server_configs:
            return dict(self.mcp_server_configs)

        configs: dict[str, ScopedMcpServerConfig] = {}
        configs.update(self._project_mcp_server_configs())
        configs.update(self._plugin_mcp_server_configs())
        configs.update(self._settings_mcp_server_configs())
        return dedupe_scoped_mcp_servers(configs)

    def _record_file_history_snapshot(
        self,
        *,
        file_path: str,
        original_content: str | None,
        tool_name: str,
        tool_use_id: str,
        assistant_uuid: str,
    ) -> None:
        tracking_path = os.path.abspath(file_path)
        file_history = self.app_state.file_history
        if not isinstance(file_history, dict):
            file_history = {}
        snapshots = _coerce_history_snapshots(file_history.get("snapshots"))
        tracked_files = _coerce_history_tracked_files(file_history.get("trackedFiles"))

        previous_snapshot = snapshots[-1] if snapshots else {}
        previous_backups = previous_snapshot.get("trackedFileBackups")
        tracked_file_backups: dict[str, Any] = {}
        if isinstance(previous_backups, Mapping):
            tracked_file_backups = dict(previous_backups)

        previous_backup = tracked_file_backups.get(tracking_path)
        previous_version = 0
        if isinstance(previous_backup, Mapping):
            raw_version = previous_backup.get("version", 0)
            if isinstance(raw_version, int):
                previous_version = raw_version
            elif isinstance(raw_version, str) and raw_version.isdigit():
                previous_version = int(raw_version)

        version = previous_version + 1
        timestamp_ms = int(time.time() * 1000)
        tracked_file_backups[tracking_path] = {
            "backupFileName": (
                None
                if original_content is None
                else f"inmemory:{assistant_uuid}:{tool_use_id}:v{version}"
            ),
            "backupTime": timestamp_ms,
            "backupTimeMs": timestamp_ms,
            "content": original_content,
            "version": version,
        }
        snapshot = {
            "messageId": assistant_uuid,
            "toolUseId": tool_use_id,
            "toolName": tool_name,
            "timestamp": timestamp_ms,
            "timestampMs": timestamp_ms,
            "trackedFileBackups": tracked_file_backups,
        }
        snapshots.append(snapshot)
        if len(snapshots) > _MAX_FILE_HISTORY_SNAPSHOTS:
            snapshots = snapshots[-_MAX_FILE_HISTORY_SNAPSHOTS:]
        if tracking_path not in tracked_files:
            tracked_files.append(tracking_path)

        raw_sequence = file_history.get("snapshotSequence", 0)
        if isinstance(raw_sequence, int):
            snapshot_sequence = raw_sequence + 1
        elif isinstance(raw_sequence, str) and raw_sequence.isdigit():
            snapshot_sequence = int(raw_sequence) + 1
        else:
            snapshot_sequence = len(snapshots)

        self.app_state.file_history = {
            **file_history,
            "snapshots": snapshots,
            "trackedFiles": tracked_files,
            "snapshotSequence": snapshot_sequence,
        }

    async def _discover_path_skills(
        self,
        file_paths: Sequence[str],
        *,
        tool_name: str,
        tool_use_id: str,
        assistant_uuid: str,
    ) -> tuple[ToolExecutionUpdate, ...]:
        if _is_env_truthy(os.environ.get("CLAUDE_CODE_SIMPLE")):
            return ()
        normalized_paths = tuple(
            os.path.abspath(path)
            for path in file_paths
            if isinstance(path, str) and path.strip()
        )
        if not normalized_paths:
            return ()
        cwd = self._working_directory()
        existing_dynamic_names = {command.name for command in get_dynamic_skills()}
        previous_conditional_count = get_conditional_skill_count()
        discovered = discover_skill_dirs_for_paths(normalized_paths, cwd)
        if discovered:
            add_skill_directories(discovered)
        activated = activate_conditional_skills_for_paths(normalized_paths, cwd)
        dynamic_skills = get_dynamic_skills()
        dynamic_skill_names = sorted({command.name for command in dynamic_skills})
        new_dynamic_names = sorted(
            set(dynamic_skill_names) - existing_dynamic_names
        )
        conditional_skill_count = get_conditional_skill_count()
        if (
            not discovered
            and not activated
            and not new_dynamic_names
            and conditional_skill_count == previous_conditional_count
        ):
            return ()
        hook_result = await self.emit_hook_event(
            "InstructionsLoaded",
            {
                "toolName": tool_name,
                "toolUseId": tool_use_id,
                "assistantUuid": assistant_uuid,
                "cwd": cwd,
                "filePaths": list(normalized_paths),
                "directories": list(discovered),
                "activatedSkillNames": list(activated),
                "newDynamicSkillNames": new_dynamic_names,
                "dynamicSkillCount": len(dynamic_skill_names),
                "conditionalSkillCount": conditional_skill_count,
                "source": "dynamic-skill-discovery",
            },
        )
        return tuple(self._hook_updates(hook_result.messages))

    def _settings_mcp_server_configs(self) -> dict[str, ScopedMcpServerConfig]:
        raw_settings = self.app_state.settings
        if not isinstance(raw_settings, Mapping):
            return {}
        raw_servers = raw_settings.get("mcpServers")
        return _parse_mcp_server_configs(raw_servers, scope="user")

    def _project_mcp_server_configs(self) -> dict[str, ScopedMcpServerConfig]:
        raw_settings = self.app_state.settings
        settings = raw_settings if isinstance(raw_settings, Mapping) else {}
        project_servers, _errors = get_project_mcp_configs(self._working_directory())
        return filter_project_mcp_servers(project_servers, settings)

    def _plugin_mcp_server_configs(self) -> dict[str, ScopedMcpServerConfig]:
        configs: dict[str, ScopedMcpServerConfig] = {}
        builtin_enabled, _ = get_builtin_plugins(self.app_state.settings)
        for plugin in builtin_enabled:
            configs.update(_plugin_mcp_servers(plugin, scope="managed"))
        sync_managed_plugins_runtime_state(self.app_state)
        raw_plugins = self.app_state.plugins.get("enabled", [])
        if isinstance(raw_plugins, Sequence) and not isinstance(
            raw_plugins,
            (str, bytes, bytearray),
        ):
            for plugin in raw_plugins:
                configs.update(_plugin_mcp_servers(plugin, scope="dynamic"))
        return configs

    def _child_mcp_server_configs(self) -> dict[str, ScopedMcpServerConfig]:
        configs = self._resolved_mcp_server_configs()
        for client in self.mcp_clients:
            name = getattr(client, "name", None)
            config = getattr(client, "config", None)
            scope = getattr(client, "scope", None)
            if not isinstance(name, str) or not name.strip() or config is None:
                continue
            if not isinstance(scope, str) or not scope:
                scope = "user"
            configs[name] = ScopedMcpServerConfig(config=config, scope=scope)
        return configs

    async def run(
        self,
        tool_use_blocks: Sequence[AssistantMessage],
        *,
        signal: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[ToolExecutionUpdate, None]:
        session = self.open_streaming_session(signal=signal)
        try:
            await session.add_tool_use_messages(tool_use_blocks)
            await session.finish()
            while True:
                update = await session.next_update()
                if update is None:
                    break
                yield update
        finally:
            await session.close()

    def open_streaming_session(
        self,
        *,
        signal: Optional[asyncio.Event] = None,
    ) -> _IncrementalToolRunSession:
        return _IncrementalToolRunSession(self, signal=signal)

    async def _run_scheduler_tool_use(
        self,
        tool_use: SchedulerToolCall,
        assistant_message: SchedulerAssistantToolUseMessage,
        _can_use_tool: Any,
        tool_use_context: ToolUseContext,
    ) -> AsyncGenerator[SchedulerMessageUpdateLazy, None]:
        block = ToolUseBlock(
            id=tool_use.id,
            name=tool_use.name,
            input=dict(tool_use.input),
        )
        async for update in self._stream_tool_block(
            block,
            assistant_uuid=assistant_message.uuid,
            tool_use_context=tool_use_context,
        ):
            yield SchedulerMessageUpdateLazy(
                message=update.message,
                context_modifier=self._scheduler_context_modifier(
                    tool_use.id,
                    update.newContext,
                ),
            )

    def _scheduler_context_modifier(
        self,
        tool_use_id: str,
        new_context: Mapping[str, Any] | None,
    ) -> SchedulerContextModifier | None:
        if new_context is None:
            return None

        def _modify_context(current: ToolUseContext) -> ToolUseContext:
            if _looks_like_permission_context(new_context):
                self.app_state.tool_permission_context = dict(new_context)
            return replace(current, state=dict(new_context))

        return SchedulerContextModifier(
            tool_use_id=tool_use_id,
            modify_context=_modify_context,
        )

    def _scheduler_message_update_to_query(
        self,
        update: SchedulerMessageUpdate,
    ) -> ToolExecutionUpdate | None:
        message = update.message
        if message is None:
            state = dict(update.new_context.state) if update.new_context is not None else None
            return ToolExecutionUpdate(newContext=state)

        normalized_message = _normalize_scheduler_message(message)
        if normalized_message is None:
            return None

        state = dict(update.new_context.state) if update.new_context is not None else None
        return ToolExecutionUpdate(
            message=normalized_message,
            newContext=state,
        )

    async def _execute_tool_block(
        self,
        block: ToolUseBlock,
        *,
        assistant_uuid: str,
        tool_use_context: ToolUseContext | None = None,
    ) -> _ToolExecutionOutcome | None:
        updates = [
            update
            async for update in self._stream_tool_block(
                block,
                assistant_uuid=assistant_uuid,
                tool_use_context=tool_use_context,
            )
        ]
        if not updates:
            return None
        return _ToolExecutionOutcome(updates=tuple(updates))

    async def _stream_tool_block(
        self,
        block: ToolUseBlock,
        *,
        assistant_uuid: str,
        tool_use_context: ToolUseContext | None = None,
    ) -> AsyncGenerator[ToolExecutionUpdate, None]:
        normalized_name = _normalize_tool_name(block.name)
        if (
            normalized_name not in self._enabled_tool_names()
            and normalized_name not in self._mcp_tool_lookup
        ):
            if normalized_name == "AskUserQuestion" and not self._is_tool_enabled(
                normalized_name
            ):
                yield _tool_result_update(
                    tool_use_id=block.id,
                    rendered=ask_user_question_disabled_message(),
                    assistant_uuid=assistant_uuid,
                    is_error=True,
                )
            return

        effective_input = dict(block.input)
        pre_hook = await self.emit_hook_event(
            "PreToolUse",
            self._tool_hook_payload(
                tool_name=normalized_name,
                raw_input=effective_input,
                tool_use_id=block.id,
                assistant_uuid=assistant_uuid,
            ),
        )
        for update in self._hook_updates(pre_hook.messages):
            yield update
        effective_input, pre_hook_decision = self._apply_hook_result_to_input(
            normalized_name,
            effective_input,
            pre_hook,
            hook_name="PreToolUse",
        )
        if pre_hook_decision is not None and not isinstance(
            pre_hook_decision,
            PermissionAllowDecision,
        ):
            pre_blocked = self._blocked_permission_result(
                normalized_name,
                pre_hook_decision,
            ) or _ToolDispatchResult(
                rendered=getattr(pre_hook_decision, "message", "Hook blocked tool use"),
                is_error=True,
            )
            failure_hook = await self.emit_hook_event(
                "PostToolUseFailure",
                self._tool_hook_payload(
                    tool_name=normalized_name,
                    raw_input=effective_input,
                    tool_use_id=block.id,
                    assistant_uuid=assistant_uuid,
                    result=pre_blocked,
                    error=pre_blocked.rendered,
                ),
            )
            for update in self._hook_updates(failure_hook.messages):
                yield update
            yield _tool_result_update(
                tool_use_id=block.id,
                rendered=pre_blocked.rendered or "",
                assistant_uuid=assistant_uuid,
                is_error=True,
                new_context=pre_blocked.new_context,
            )
            return

        permission_check: _PermissionCheckResult | None = None
        try:
            plan_mode_error = self._validate_plan_mode(normalized_name, effective_input)
            if plan_mode_error is not None:
                result = _ToolDispatchResult(rendered=plan_mode_error, is_error=True)
            else:
                # MCP tools are not in the builtin schema registry; their inputs
                # are validated by the MCP server itself on tools/call.
                if normalized_name not in self._mcp_tool_lookup:
                    validate_tool_input_payload_via_registry(
                        normalized_name,
                        effective_input,
                    )
                permission_check = await self._check_tool_permissions(
                    normalized_name,
                    effective_input,
                )
                for update in permission_check.extra_updates:
                    yield update
                if permission_check.blocked is not None:
                    result = permission_check.blocked
                elif normalized_name == "Bash" and not _bool_input(
                    permission_check.raw_input,
                    "run_in_background",
                    "runInBackground",
                ):
                    result = None
                    async for streamed in self._dispatch_bash_stream(
                        permission_check.raw_input,
                        tool_use_id=block.id,
                        tool_use_context=tool_use_context,
                    ):
                        if streamed.update is not None:
                            yield streamed.update
                            continue
                        result = streamed.result
                    if result is None:
                        raise RuntimeError("Streaming Bash execution completed without a result")
                else:
                    result = await self._dispatch_tool(
                        normalized_name,
                        permission_check.raw_input,
                        tool_use_id=block.id,
                        assistant_uuid=assistant_uuid,
                        tool_use_context=tool_use_context,
                    )
                if (
                    result is not None
                    and not result.is_error
                    and normalized_name not in self._mcp_tool_lookup
                ):
                    validate_tool_output_payload_via_registry(
                        normalized_name,
                        result.payload,
                    )
        except Exception as exc:
            failure_hook = await self.emit_hook_event(
                "PostToolUseFailure",
                self._tool_hook_payload(
                    tool_name=normalized_name,
                    raw_input=effective_input,
                    tool_use_id=block.id,
                    assistant_uuid=assistant_uuid,
                    error=str(exc),
                ),
            )
            for update in self._hook_updates(failure_hook.messages):
                yield update
            yield _tool_result_update(
                tool_use_id=block.id,
                rendered=str(exc),
                assistant_uuid=assistant_uuid,
                is_error=True,
            )
            return

        for update in result.pre_updates:
            yield update

        result_hook_updates = await self._dispatch_result_hook_updates(result)
        task_event_updates = await self._drain_task_event_hook_updates()
        post_hook = await self.emit_hook_event(
            "PostToolUseFailure" if result.is_error else "PostToolUse",
            self._tool_hook_payload(
                tool_name=normalized_name,
                raw_input=permission_check.raw_input
                if plan_mode_error is None
                else effective_input,
                tool_use_id=block.id,
                assistant_uuid=assistant_uuid,
                result=result,
                error=result.rendered if result.is_error else None,
            ),
        )
        for update in result_hook_updates:
            yield update
        for update in self._hook_updates(post_hook.messages):
            yield update
        rendered = result.rendered
        if rendered is None:
            rendered = _render_tool_payload(normalized_name, result.payload)
        rendered, raw_payload, mcp_meta = _truncate_tool_result_for_limit(
            normalized_name,
            rendered=rendered,
            raw_payload=result.payload if isinstance(result.payload, Mapping) else None,
        )
        for update in result.extra_updates:
            yield update
        for update in task_event_updates:
            yield update
        yield _tool_result_update(
            tool_use_id=block.id,
            rendered=rendered,
            assistant_uuid=assistant_uuid,
            block_content=result.block_content,
            raw_payload=raw_payload,
            is_error=result.is_error,
            new_context=result.new_context,
            mcp_meta=mcp_meta,
        )

    async def _check_tool_permissions(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
    ) -> _PermissionCheckResult:
        permission_tool_name, normalized_input = self._normalize_permission_input(
            tool_name, raw_input
        )
        context_raw = self._current_permission_context()
        permission_context = self._build_tool_permission_context(context_raw)
        approval_state = self._extract_permission_approval_state(context_raw)
        extra_updates: list[ToolExecutionUpdate] = []

        if permission_tool_name != tool_name:
            actual_tool_decision = has_permissions_to_use_tool(
                tool_name,
                dict(normalized_input),
                context=permission_context,
                approval_state=approval_state,
            )
            (
                normalized_input,
                actual_tool_decision,
                request_updates,
            ) = await self._run_permission_request_hooks(
                tool_name=tool_name,
                permission_tool_name=tool_name,
                raw_input=normalized_input,
                decision=actual_tool_decision,
            )
            extra_updates.extend(request_updates)
            actual_tool_decision = self._apply_permission_denial_tracking(
                tool_name=tool_name,
                permission_tool_name=tool_name,
                raw_input=normalized_input,
                permission_context=permission_context,
                decision=actual_tool_decision,
            )
            actual_tool_decision = await self._maybe_resolve_permission_prompt(
                tool_name=tool_name,
                permission_tool_name=tool_name,
                raw_input=normalized_input,
                permission_context=permission_context,
                decision=actual_tool_decision,
            )
            blocked = self._blocked_permission_result(
                tool_name,
                actual_tool_decision,
                enforce_default_prompt=False,
            )
            if blocked is not None:
                extra_updates.extend(
                    await self._permission_denied_updates(
                        tool_name=tool_name,
                        permission_tool_name=tool_name,
                        raw_input=normalized_input,
                        decision=actual_tool_decision,
                    )
                )
                return _PermissionCheckResult(
                    raw_input=normalized_input,
                    blocked=blocked,
                    extra_updates=tuple(extra_updates),
                )
            if isinstance(actual_tool_decision, PermissionAllowDecision) and isinstance(
                actual_tool_decision.updated_input, Mapping
            ):
                normalized_input = self._merge_permission_updated_input(
                    tool_name,
                    normalized_input,
                    actual_tool_decision.updated_input,
                )

        decision = has_permissions_to_use_tool(
            permission_tool_name,
            dict(normalized_input),
            context=permission_context,
            approval_state=approval_state,
        )
        (
            normalized_input,
            decision,
            request_updates,
        ) = await self._run_permission_request_hooks(
            tool_name=tool_name,
            permission_tool_name=permission_tool_name,
            raw_input=normalized_input,
            decision=decision,
        )
        extra_updates.extend(request_updates)
        decision = self._apply_permission_denial_tracking(
            tool_name=tool_name,
            permission_tool_name=permission_tool_name,
            raw_input=normalized_input,
            permission_context=permission_context,
            decision=decision,
        )
        decision = await self._maybe_resolve_permission_prompt(
            tool_name=tool_name,
            permission_tool_name=permission_tool_name,
            raw_input=normalized_input,
            permission_context=permission_context,
            decision=decision,
        )

        if isinstance(decision, PermissionAllowDecision):
            effective_input = normalized_input
            if isinstance(decision.updated_input, Mapping):
                effective_input = self._merge_permission_updated_input(
                    tool_name,
                    normalized_input,
                    decision.updated_input,
                )
            return _PermissionCheckResult(
                raw_input=effective_input,
                extra_updates=tuple(extra_updates),
            )

        blocked = self._blocked_permission_result(tool_name, decision)
        if blocked is not None:
            extra_updates.extend(
                await self._permission_denied_updates(
                    tool_name=tool_name,
                    permission_tool_name=permission_tool_name,
                    raw_input=normalized_input,
                    decision=decision,
                )
            )
            return _PermissionCheckResult(
                raw_input=normalized_input,
                blocked=blocked,
                extra_updates=tuple(extra_updates),
            )

        return _PermissionCheckResult(
            raw_input=normalized_input,
            extra_updates=tuple(extra_updates),
        )

    async def _maybe_resolve_permission_prompt(
        self,
        *,
        tool_name: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        permission_context: ToolPermissionContext,
        decision: object,
    ) -> object:
        if not isinstance(decision, PermissionAskDecision):
            return decision
        if permission_context.should_avoid_permission_prompts:
            return decision
        if self.approval_prompt_handler is None:
            return decision

        resolved = self.approval_prompt_handler(
            tool_name,
            permission_tool_name,
            dict(raw_input),
            decision,
        )
        if inspect.isawaitable(resolved):
            resolved = await resolved

        if resolved is None:
            return decision
        if isinstance(
            resolved,
            (PermissionAllowDecision, PermissionAskDecision, PermissionDenyDecision),
        ):
            return resolved
        if isinstance(resolved, Mapping):
            mapped = resolve_approval_state(
                permission_tool_name,
                raw_input,
                resolved,
                hook_name="InteractiveApprovalPrompt",
                hook_source="localToolExecutor",
            )
            return mapped or decision
        raise TypeError(
            "approval_prompt_handler must return a permission decision, mapping, or None"
        )

    async def _run_permission_request_hooks(
        self,
        *,
        tool_name: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        decision: object,
    ) -> tuple[dict[str, Any], object, tuple[ToolExecutionUpdate, ...]]:
        normalized_input = dict(raw_input)
        if not isinstance(decision, (PermissionAskDecision, PermissionDenyDecision)):
            return normalized_input, decision, ()

        hook_result = await self.emit_hook_event(
            "PermissionRequest",
            {
                "toolName": tool_name,
                "permissionToolName": permission_tool_name,
                "toolInput": dict(normalized_input),
                "permissionDecision": self._permission_decision_payload(decision),
            },
        )
        normalized_input, hook_decision = self._apply_hook_result_to_input(
            tool_name,
            normalized_input,
            hook_result,
            hook_name="PermissionRequest",
        )
        return (
            normalized_input,
            hook_decision or decision,
            self._hook_updates(hook_result.messages),
        )

    async def _permission_denied_updates(
        self,
        *,
        tool_name: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        decision: object,
    ) -> tuple[ToolExecutionUpdate, ...]:
        hook_result = await self.emit_hook_event(
            "PermissionDenied",
            {
                "toolName": tool_name,
                "permissionToolName": permission_tool_name,
                "toolInput": dict(raw_input),
                "permissionDecision": self._permission_decision_payload(decision),
            },
        )
        return self._hook_updates(hook_result.messages)

    def _normalize_permission_input(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        normalized_input = dict(raw_input)

        def _set_resolved_path(
            value: str | None,
            *,
            target_keys: Sequence[str],
        ) -> None:
            if value is None:
                return
            resolved = self._resolve_tool_path(value)
            for key in target_keys:
                normalized_input[key] = resolved

        if tool_name in {"Read", "Write", "Edit"}:
            path = _optional_string_input(raw_input, "file_path", "filePath")
            if path is None and tool_name == "Write":
                path = _optional_string_input(raw_input, "path")
            _set_resolved_path(
                path,
                target_keys=(
                    "file_path",
                    "filePath",
                    "path",
                ),
            )
            return tool_name, normalized_input

        if tool_name == "NotebookEdit":
            _set_resolved_path(
                _optional_string_input(raw_input, "notebook_path", "notebookPath"),
                target_keys=(
                    "notebook_path",
                    "notebookPath",
                    "file_path",
                    "path",
                ),
            )
            return "Edit", normalized_input

        if tool_name == "Glob":
            _set_resolved_path(
                _optional_string_input(raw_input, "path") or self._working_directory(),
                target_keys=("path",),
            )
            return "Glob", normalized_input

        if tool_name == "LSP":
            _set_resolved_path(
                _optional_string_input(raw_input, "filePath", "file_path"),
                target_keys=(
                    "file_path",
                    "filePath",
                    "path",
                ),
            )
            return "Read", normalized_input

        if tool_name == "Skill":
            skill = _optional_string_input(raw_input, "skill")
            if skill is None:
                return tool_name, normalized_input
            normalized_skill = skill.strip()
            if normalized_skill.startswith("/"):
                normalized_skill = normalized_skill[1:]
            if not normalized_skill:
                return tool_name, normalized_input

            normalized_input["resolved_skill_name"] = normalized_skill
            command = SkillTool.find_command(normalized_skill, self._load_skill_commands())
            if command is not None:
                normalized_input["resolved_skill_name"] = command.name
                normalized_input["skill_aliases"] = list(command.aliases)
            return tool_name, normalized_input

        if tool_name in {"EnterWorktree", "ExitWorktree"}:
            cwd = self._working_directory()
            normalized_input["path"] = cwd
            normalized_input["file_path"] = cwd
            return "Write", normalized_input

        return tool_name, normalized_input

    def _merge_permission_updated_input(
        self,
        tool_name: str,
        current_input: Mapping[str, Any],
        updated_input: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = dict(current_input)
        merged.update(updated_input)

        if tool_name == "NotebookEdit":
            updated_path = _optional_string_input(merged, "file_path", "path")
            if updated_path is not None:
                merged["notebook_path"] = updated_path
                merged["notebookPath"] = updated_path
            return merged

        if tool_name in {"Read", "Write", "Edit", "LSP"}:
            updated_path = _optional_string_input(merged, "file_path", "path")
            if updated_path is not None:
                merged["file_path"] = updated_path
                merged["filePath"] = updated_path
                if tool_name == "Write":
                    merged["path"] = updated_path
            return merged

        return merged

    def _should_block_permission_prompt(
        self,
        tool_name: str,
        decision: PermissionAskDecision,
    ) -> bool:
        if isinstance(
            decision.decision_reason,
            (
                HookDecisionReason,
                OtherDecisionReason,
                RuleDecisionReason,
                SafetyCheckDecisionReason,
                WorkingDirDecisionReason,
            ),
        ):
            return True
        return tool_name in {"Read", "Write", "Edit", "NotebookEdit", "Glob", "LSP"}

    def _blocked_permission_result(
        self,
        tool_name: str,
        decision: object,
        *,
        enforce_default_prompt: bool = True,
    ) -> _ToolDispatchResult | None:
        if isinstance(decision, PermissionDenyDecision):
            return _ToolDispatchResult(
                rendered=decision.message,
                is_error=True,
            )
        if isinstance(decision, PermissionAskDecision) and (
            self._should_block_permission_prompt(tool_name, decision)
            if enforce_default_prompt
            else isinstance(
                decision.decision_reason,
                (
                    HookDecisionReason,
                    OtherDecisionReason,
                    RuleDecisionReason,
                    SafetyCheckDecisionReason,
                    WorkingDirDecisionReason,
                ),
            )
        ):
            return _ToolDispatchResult(
                rendered=decision.message,
                is_error=True,
            )
        return None

    def _apply_hook_result_to_input(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
        hook_result: HookEventResult,
        *,
        hook_name: str,
    ) -> tuple[dict[str, Any], object | None]:
        effective_input = dict(raw_input)
        if hook_result.updated_input is not None:
            effective_input = self._merge_permission_updated_input(
                tool_name,
                effective_input,
                hook_result.updated_input,
            )

        if hook_result.approval_state is None:
            return effective_input, None

        decision = resolve_approval_state(
            tool_name,
            effective_input,
            hook_result.approval_state,
            hook_name=hook_name,
            hook_source=hook_result.decision_source,
        )
        if isinstance(
            decision,
            (PermissionAllowDecision, PermissionAskDecision),
        ) and isinstance(decision.updated_input, Mapping):
            effective_input = self._merge_permission_updated_input(
                tool_name,
                effective_input,
                decision.updated_input,
            )
        return effective_input, decision

    def _hook_updates(
        self,
        messages: Sequence[Message],
    ) -> tuple[ToolExecutionUpdate, ...]:
        return tuple(ToolExecutionUpdate(message=message) for message in messages)

    async def _task_event_hook_updates(
        self,
        event: Mapping[str, Any],
        *,
        event_source: str,
    ) -> tuple[ToolExecutionUpdate, ...]:
        hook = self._task_event_hook(event, event_source=event_source)
        if hook is None:
            return ()
        event_name, payload = hook
        hook_result = await self.emit_hook_event(event_name, payload)
        return self._hook_updates(hook_result.messages)

    async def _dispatch_result_hook_updates(
        self,
        result: _ToolDispatchResult,
    ) -> tuple[ToolExecutionUpdate, ...]:
        updates: list[ToolExecutionUpdate] = []
        for event_name, payload in result.hook_events:
            hook_result = await self.emit_hook_event(event_name, payload)
            updates.extend(self._hook_updates(hook_result.messages))
        return tuple(updates)

    def _task_event_hook(
        self,
        event: Mapping[str, Any],
        *,
        event_source: str,
    ) -> tuple[str, dict[str, Any]] | None:
        task_id = event.get("task_id")
        if isinstance(task_id, str) and task_id in self._suppressed_task_event_ids:
            return None
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            return None
        if event_type == "task_started":
            hook_name = "TaskCreated"
        elif event_type == "agent_spawned":
            if event.get("backgrounded") is not True:
                return None
            hook_name = "TeammateIdle"
        elif event_type == "agent_backgrounded":
            hook_name = "TeammateIdle"
        elif event_type == "task_state_changed":
            status = event.get("status")
            if status not in {
                TASK_STATUS_COMPLETED,
                TASK_STATUS_FAILED,
                TASK_STATUS_KILLED,
            }:
                return None
            hook_name = "TaskCompleted"
        elif event_type == "task_notification":
            hook_name = "Notification"
        else:
            return None
        return hook_name, self._task_event_hook_payload(
            event,
            event_source=event_source,
        )

    def _task_event_hook_payload(
        self,
        event: Mapping[str, Any],
        *,
        event_source: str,
    ) -> dict[str, Any]:
        task_id = event.get("task_id")
        task = self._get_known_task(task_id) if isinstance(task_id, str) else None
        payload: dict[str, Any] = {
            "eventType": event.get("type"),
            "eventSource": event_source,
            "timestampMs": event.get("timestamp_ms"),
            "taskId": task_id,
        }
        for source_key, target_key in (
            ("status", "status"),
            ("task_type", "taskType"),
            ("tool_use_id", "toolUseId"),
            ("description", "description"),
            ("workflow_name", "workflowName"),
            ("exit_code", "exitCode"),
            ("error", "error"),
            ("xml", "xml"),
            ("summary", "summary"),
            ("backgrounded", "backgrounded"),
        ):
            value = event.get(source_key)
            if value is not None:
                payload[target_key] = value
        if isinstance(task, LocalTaskState):
            payload.update(
                {
                    "description": task.description,
                    "taskType": task.task_type,
                    "status": task.status,
                    "outputFile": task.output_file,
                    "isBackgrounded": task.is_backgrounded,
                    "exitCode": task.exit_code,
                    "error": task.error,
                }
            )
        elif isinstance(task, AgentTaskState):
            payload.update(
                {
                    "description": task.description,
                    "taskType": "local_agent",
                    "agentId": task.agent_id,
                    "agentType": task.agent_type,
                    "status": task.status,
                    "outputFile": task.output_file,
                    "isBackgrounded": task.is_backgrounded,
                    "exitCode": task.exit_code,
                    "error": task.error,
                    "cwd": task.cwd,
                    "model": task.model,
                    "maxTokens": task.max_tokens,
                }
            )
            if task.result is not None:
                payload["result"] = task.result.as_tool_payload()
        return {
            key: value
            for key, value in payload.items()
            if value is not None and value != ""
        }

    def _permission_decision_payload(
        self,
        decision: object,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "behavior": getattr(decision, "behavior", None),
            "message": getattr(decision, "message", None),
        }
        updated_input = getattr(decision, "updated_input", None)
        if isinstance(updated_input, Mapping):
            payload["updatedInput"] = dict(updated_input)
        reason = getattr(decision, "decision_reason", None)
        if reason is None:
            return payload
        reason_payload = {"type": getattr(reason, "type", None)}
        for field_name in ("reason", "hook_name", "hook_source", "mode"):
            field_value = getattr(reason, field_name, None)
            if field_value is not None:
                reason_payload[field_name] = field_value
        rule = getattr(reason, "rule", None)
        if rule is not None:
            reason_payload["rule"] = {
                "source": getattr(getattr(rule, "source", None), "value", None)
                or getattr(rule, "source", None),
                "toolName": getattr(
                    getattr(getattr(rule, "rule_value", None), "tool_name", None),
                    "value",
                    None,
                )
                or getattr(getattr(rule, "rule_value", None), "tool_name", None),
                "ruleContent": getattr(
                    getattr(rule, "rule_value", None),
                    "rule_content",
                    None,
                ),
            }
        payload["decisionReason"] = reason_payload
        return payload

    def _tool_hook_payload(
        self,
        *,
        tool_name: str,
        raw_input: Mapping[str, Any],
        tool_use_id: str,
        assistant_uuid: str,
        result: _ToolDispatchResult | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toolName": tool_name,
            "toolInput": dict(raw_input),
            "toolUseId": tool_use_id,
            "assistantUuid": assistant_uuid,
        }
        if result is not None:
            payload["toolResult"] = {
                "payload": dict(result.payload)
                if isinstance(result.payload, Mapping)
                else result.payload,
                "rendered": result.rendered,
                "isError": result.is_error,
            }
        if error is not None:
            payload["error"] = error
        return payload

    def _file_changed_hook_payload(
        self,
        *,
        tool_name: str,
        raw_input: Mapping[str, Any],
        tool_use_id: str,
        assistant_uuid: str,
        file_path: str,
        change_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toolName": tool_name,
            "toolInput": dict(raw_input),
            "toolUseId": tool_use_id,
            "assistantUuid": assistant_uuid,
            "filePath": file_path,
            "cwd": self._working_directory(),
            "changeType": change_type,
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        return payload

    def _subagent_hook_payload(
        self,
        *,
        agent_id: str,
        agent_type: str,
        description: str,
        prompt: str,
        tool_use_id: str,
        run_in_background: bool,
        cwd: str,
        model: str | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agentId": agent_id,
            "agentType": agent_type,
            "description": description,
            "prompt": prompt,
            "toolUseId": tool_use_id,
            "runInBackground": run_in_background,
            "cwd": cwd,
        }
        if model is not None:
            payload["model"] = model
        if max_tokens is not None:
            payload["maxTokens"] = max_tokens
        return payload

    def _serialize_elicitation_questions(
        self,
        questions: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for question in questions:
            item: dict[str, Any] = {
                "id": question.get("id"),
                "header": question.get("header"),
                "question": question.get("question"),
                "multiSelect": bool(question.get("multiSelect")),
                "options": [
                    dict(option)
                    for option in question.get("options", ())
                    if isinstance(option, Mapping)
                ],
            }
            for optional_key in ("preview", "annotations", "metadata"):
                optional_value = question.get(optional_key)
                if optional_value is None:
                    continue
                item[optional_key] = (
                    dict(optional_value)
                    if isinstance(optional_value, Mapping)
                    else optional_value
                )
            serialized.append(item)
        return serialized

    def _elicitation_hook_payload(
        self,
        *,
        raw_input: Mapping[str, Any],
        tool_use_id: str,
        assistant_uuid: str,
        questions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        serialized_questions = self._serialize_elicitation_questions(questions)
        payload: dict[str, Any] = {
            "toolName": "AskUserQuestion",
            "toolInput": dict(raw_input),
            "toolUseId": tool_use_id,
            "assistantUuid": assistant_uuid,
            "questions": serialized_questions,
            "questionCount": len(serialized_questions),
            "questionIds": [
                question["id"]
                for question in serialized_questions
                if isinstance(question.get("id"), str)
            ],
        }
        if len(serialized_questions) == 1:
            only = serialized_questions[0]
            if isinstance(only.get("question"), str):
                payload["question"] = only["question"]
            if isinstance(only.get("header"), str):
                payload["header"] = only["header"]
        return payload

    def _elicitation_result_hook_payload(
        self,
        *,
        raw_input: Mapping[str, Any],
        tool_use_id: str,
        assistant_uuid: str,
        questions: Sequence[Mapping[str, Any]],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = self._elicitation_hook_payload(
            raw_input=raw_input,
            tool_use_id=tool_use_id,
            assistant_uuid=assistant_uuid,
            questions=questions,
        )
        payload["result"] = dict(result)
        answers = result.get("answers")
        if isinstance(answers, Mapping):
            payload["answers"] = dict(answers)
        selected = result.get("selected")
        if isinstance(selected, (str, list)):
            payload["selected"] = selected
        return payload

    def _extract_permission_approval_state(
        self,
        raw_context: Mapping[str, Any],
    ) -> Mapping[str, object] | None:
        approval_state = raw_context.get("approval_state")
        if isinstance(approval_state, Mapping):
            return dict(approval_state)
        approval_state = raw_context.get("approvalState")
        if isinstance(approval_state, Mapping):
            return dict(approval_state)
        return None

    def _update_permission_denial_tracking(
        self,
        state: Mapping[str, object],
    ) -> None:
        context = self._current_permission_context()
        context["denial_tracking"] = dict(state)
        context.pop("denialTracking", None)
        self.app_state.tool_permission_context = context

    def _apply_permission_denial_tracking(
        self,
        *,
        tool_name: str,
        permission_tool_name: str,
        raw_input: Mapping[str, Any],
        permission_context: ToolPermissionContext,
        decision: object,
    ) -> object:
        context = self._current_permission_context()
        tracking_state = context.get("denial_tracking")
        if tracking_state is None:
            tracking_state = context.get("denialTracking")

        if isinstance(decision, (PermissionAllowDecision, PermissionAskDecision)):
            self._update_permission_denial_tracking(
                reset_denial_tracking_state(tracking_state)
            )
            return decision

        if not isinstance(decision, PermissionDenyDecision):
            return decision

        next_state = record_permission_denial(
            tracking_state,
            build_denial_tracking_signature(
                permission_tool_name,
                raw_input,
                decision,
            ),
        )
        self._update_permission_denial_tracking(next_state)

        if permission_context.should_avoid_permission_prompts:
            return decision

        if not should_fallback_to_interactive_prompt(next_state):
            return decision

        self._update_permission_denial_tracking(
            reset_denial_tracking_state(next_state)
        )
        return PermissionAskDecision(
            message=(
                f"Repeated permission denials for {tool_name} require "
                "interactive confirmation before continuing."
            ),
            decision_reason=OtherDecisionReason(
                reason="Repeated permission denials require interactive confirmation"
            ),
        )

    def _build_tool_permission_context(
        self,
        raw_context: Mapping[str, Any],
    ) -> ToolPermissionContext:
        raw_mode = raw_context.get("mode")
        mode = str(raw_mode) if isinstance(raw_mode, str) and raw_mode.strip() else "default"
        cwd_value = raw_context.get("cwd")
        cwd = (
            _resolve_path_from_base(cwd_value, os.getcwd())
            if isinstance(cwd_value, str) and cwd_value.strip()
            else None
        )
        additional_working_directories = self._parse_additional_working_directories(
            raw_context,
            cwd,
        )
        allow_rules = self._parse_tool_permission_rules(
            raw_context,
            "always_allow_rules",
            "alwaysAllowRules",
        )
        allowed_prompts = _optional_string_list_input(raw_context, "allowedPrompts")
        if allowed_prompts:
            allow_rules = dict(allow_rules)
            session_rules = list(allow_rules.get("session", ()))
            session_rules.extend(
                prompt for prompt in allowed_prompts if prompt not in session_rules
            )
            allow_rules["session"] = session_rules
        return ToolPermissionContext(
            mode=mode,
            cwd=cwd,
            additional_working_directories=additional_working_directories,
            always_allow_rules=allow_rules,
            always_deny_rules=self._parse_tool_permission_rules(
                raw_context,
                "always_deny_rules",
                "alwaysDenyRules",
            ),
            always_ask_rules=self._parse_tool_permission_rules(
                raw_context,
                "always_ask_rules",
                "alwaysAskRules",
            ),
            is_bypass_permissions_mode_available=_bool_input(
                raw_context,
                "is_bypass_permissions_mode_available",
                "isBypassPermissionsModeAvailable",
            ),
            stripped_dangerous_rules=self._parse_optional_tool_permission_rules(
                raw_context,
                "stripped_dangerous_rules",
                "strippedDangerousRules",
            ),
            should_avoid_permission_prompts=(
                mode == "dontAsk"
                or _bool_input(
                    raw_context,
                    "should_avoid_permission_prompts",
                    "shouldAvoidPermissionPrompts",
                )
            ),
            await_automated_checks_before_dialog=_bool_input(
                raw_context,
                "await_automated_checks_before_dialog",
                "awaitAutomatedChecksBeforeDialog",
            ),
            pre_plan_mode=_optional_string_input(raw_context, "pre_plan_mode", "prePlanMode"),
        )

    def _parse_additional_working_directories(
        self,
        raw_context: Mapping[str, Any],
        cwd: str | None,
    ) -> dict[str, AdditionalWorkingDirectory]:
        directories: dict[str, AdditionalWorkingDirectory] = {}
        if cwd is not None:
            directories["cwd"] = AdditionalWorkingDirectory(path=cwd, source="session")

        raw_directories = raw_context.get("additional_working_directories")
        if not isinstance(raw_directories, Mapping):
            raw_directories = raw_context.get("additionalWorkingDirectories")
        if not isinstance(raw_directories, Mapping):
            return directories

        for key, value in raw_directories.items():
            if not isinstance(key, str) or not isinstance(value, Mapping):
                continue
            path_value = value.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                continue
            source_value = value.get("source")
            source = source_value if isinstance(source_value, str) and source_value.strip() else "session"
            directories[key] = AdditionalWorkingDirectory(
                path=_resolve_path_from_base(path_value, os.getcwd()),
                source=source,
            )
        return directories

    def _parse_tool_permission_rules(
        self,
        raw_context: Mapping[str, Any],
        snake_case_key: str,
        camel_case_key: str,
    ) -> dict[str, list[str]]:
        parsed = self._parse_optional_tool_permission_rules(
            raw_context,
            snake_case_key,
            camel_case_key,
        )
        return {} if parsed is None else parsed

    def _parse_optional_tool_permission_rules(
        self,
        raw_context: Mapping[str, Any],
        snake_case_key: str,
        camel_case_key: str,
    ) -> dict[str, list[str]] | None:
        raw_rules = raw_context.get(snake_case_key)
        if not isinstance(raw_rules, Mapping):
            raw_rules = raw_context.get(camel_case_key)
        if not isinstance(raw_rules, Mapping):
            return None

        parsed: dict[str, list[str]] = {}
        for source, rules in raw_rules.items():
            if not isinstance(source, str):
                continue
            if not isinstance(rules, Sequence) or isinstance(
                rules, (str, bytes, bytearray)
            ):
                continue
            normalized_rules = [
                rule for rule in rules if isinstance(rule, str) and rule.strip()
            ]
            if normalized_rules:
                parsed[source] = normalized_rules
        return parsed or None

    async def _dispatch_tool(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
        assistant_uuid: str,
        tool_use_context: ToolUseContext | None = None,
    ) -> _ToolDispatchResult:
        del tool_use_context
        plan_mode_error = self._validate_plan_mode(tool_name, raw_input)
        if plan_mode_error is not None:
            return _ToolDispatchResult(rendered=plan_mode_error, is_error=True)

        if tool_name == "ToolSearch":
            return await self._dispatch_tool_search(raw_input)

        if tool_name == "Read":
            file_path = self._resolve_tool_path(
                _string_input(raw_input, "file_path", "filePath")
            )
            skill_updates = await self._discover_path_skills(
                (file_path,),
                tool_name="Read",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            result = await asyncio.to_thread(
                file_read,
                file_path,
                offset=_positive_int_input(raw_input, "offset", default=1),
                limit=_optional_positive_int_input(raw_input, "limit"),
                pages=_optional_string_input(raw_input, "pages"),
                read_file_state=self.read_file_state,
            )
            return _ToolDispatchResult(
                payload=_read_result_payload(result),
                extra_updates=skill_updates,
            )
        if tool_name == "Write":
            file_path = self._resolve_tool_path(
                _string_input(raw_input, "file_path", "filePath")
            )
            skill_updates = await self._discover_path_skills(
                (file_path,),
                tool_name="Write",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            content = _string_input(raw_input, "content")
            current_permission_context = self._current_permission_context()
            validation = validate_write_input(
                file_path,
                content,
                permission_context=self._build_tool_permission_context(
                    current_permission_context
                ),
                read_file_state=self.read_file_state,
                project_root=self._working_directory(),
                config_home=_optional_string_input(current_permission_context, "configHome"),
            )
            if not validation.result:
                return _validation_error_result(validation)
            result = file_write(
                file_path,
                content,
                read_file_state=self.read_file_state,
                project_root=self._working_directory(),
                config_home=_optional_string_input(current_permission_context, "configHome"),
            )
            try:
                notify_persistent_lsp_document_saved(
                    file_path,
                    result.content,
                    cwd=self._working_directory(),
                )
            except Exception:
                pass
            await notify_vscode_file_updated(
                result.file_path,
                result.original_file,
                result.content,
                owner_id=self.mcp_owner_id,
            )
            self._record_file_history_snapshot(
                file_path=result.file_path,
                original_content=result.original_file,
                tool_name="Write",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            return _ToolDispatchResult(
                payload={
                    "result_type": result.result_type,
                    "file_path": result.file_path,
                    "content": result.content,
                    "original_file": result.original_file,
                    "structured_patch": _structured_patch_payload(result.structured_patch),
                    "gitDiff": _git_diff_payload(result.git_diff),
                },
                extra_updates=skill_updates,
                hook_events=(
                    (
                        "FileChanged",
                        self._file_changed_hook_payload(
                            tool_name="Write",
                            raw_input=raw_input,
                            tool_use_id=tool_use_id,
                            assistant_uuid=assistant_uuid,
                            file_path=result.file_path,
                            change_type="write",
                            metadata={
                                "existedBefore": result.original_file is not None,
                                "contentLength": len(result.content),
                                "previousContentLength": len(result.original_file or ""),
                            },
                        ),
                    ),
                ),
            )
        if tool_name == "Edit":
            file_path = self._resolve_tool_path(
                _string_input(raw_input, "file_path", "filePath")
            )
            skill_updates = await self._discover_path_skills(
                (file_path,),
                tool_name="Edit",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            old_string = _string_input(raw_input, "old_string", "oldString")
            new_string = _string_input(raw_input, "new_string", "newString")
            replace_all = _bool_input(raw_input, "replace_all", "replaceAll")
            current_permission_context = self._current_permission_context()
            validation = validate_edit_input(
                file_path,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
                permission_context=self._build_tool_permission_context(
                    current_permission_context
                ),
                read_file_state=self.read_file_state,
                project_root=self._working_directory(),
                config_home=_optional_string_input(current_permission_context, "configHome"),
            )
            if not validation.result:
                return _validation_error_result(validation)
            result = file_edit(
                file_path,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
                read_file_state=self.read_file_state,
                project_root=self._working_directory(),
                config_home=_optional_string_input(current_permission_context, "configHome"),
            )
            try:
                notify_persistent_lsp_document_saved(
                    file_path,
                    result.content,
                    cwd=self._working_directory(),
                )
            except Exception:
                pass
            await notify_vscode_file_updated(
                result.file_path,
                result.original_file,
                result.content,
                owner_id=self.mcp_owner_id,
            )
            self._record_file_history_snapshot(
                file_path=result.file_path,
                original_content=result.original_file,
                tool_name="Edit",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            return _ToolDispatchResult(
                payload={
                    "result_type": result.result_type,
                    "file_path": result.file_path,
                    "old_string": result.old_string,
                    "new_string": result.new_string,
                    "content": result.content,
                    "original_file": result.original_file,
                    "replace_all": result.replace_all,
                    "structured_patch": _structured_patch_payload(result.structured_patch),
                    "gitDiff": _git_diff_payload(result.git_diff),
                },
                extra_updates=skill_updates,
                hook_events=(
                    (
                        "FileChanged",
                        self._file_changed_hook_payload(
                            tool_name="Edit",
                            raw_input=raw_input,
                            tool_use_id=tool_use_id,
                            assistant_uuid=assistant_uuid,
                            file_path=result.file_path,
                            change_type="edit",
                            metadata={
                                "replaceAll": result.replace_all,
                                "contentLength": len(result.content),
                                "previousContentLength": len(result.original_file or ""),
                                "oldStringLength": len(result.old_string),
                                "newStringLength": len(result.new_string),
                            },
                        ),
                    ),
                ),
            )
        if tool_name == "NotebookEdit":
            notebook_path = self._resolve_tool_path(
                _string_input(raw_input, "notebook_path", "notebookPath")
            )
            skill_updates = await self._discover_path_skills(
                (notebook_path,),
                tool_name="NotebookEdit",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            original_file = _read_utf8_text_if_exists(notebook_path)
            result = notebook_edit(
                notebook_path,
                cell_id=_optional_string_input(raw_input, "cell_id", "cellId"),
                new_source=_optional_string_input(raw_input, "new_source", "newSource")
                or "",
                cell_type=_optional_string_input(raw_input, "cell_type", "cellType"),
                edit_mode=_optional_string_input(raw_input, "edit_mode", "editMode")
                or "replace",
                read_file_state=self.read_file_state,
            )
            updated_file = _read_utf8_text_if_exists(notebook_path)
            await notify_vscode_file_updated(
                notebook_path,
                original_file,
                updated_file,
                owner_id=self.mcp_owner_id,
            )
            self._record_file_history_snapshot(
                file_path=notebook_path,
                original_content=original_file,
                tool_name="NotebookEdit",
                tool_use_id=tool_use_id,
                assistant_uuid=assistant_uuid,
            )
            return _ToolDispatchResult(
                payload=result,
                extra_updates=skill_updates,
                hook_events=(
                    (
                        "FileChanged",
                        self._file_changed_hook_payload(
                            tool_name="NotebookEdit",
                            raw_input=raw_input,
                            tool_use_id=tool_use_id,
                            assistant_uuid=assistant_uuid,
                            file_path=notebook_path,
                            change_type="notebook_edit",
                            metadata={
                                "editMode": result.get("edit_mode"),
                                "cellId": result.get("cell_id"),
                                "cellType": result.get("cell_type"),
                                "newSourceLength": len(str(result.get("new_source") or "")),
                                "previousContentLength": len(original_file or ""),
                                "contentLength": len(updated_file or ""),
                            },
                        ),
                    ),
                ),
            )
        if tool_name == "Glob":
            result = await asyncio.to_thread(
                GlobTool.call,
                GlobInput(
                    pattern=_string_input(raw_input, "pattern"),
                    path=self._resolve_optional_tool_path(
                        _optional_string_input(raw_input, "path")
                    )
                    or self._working_directory(),
                ),
            )
            return _ToolDispatchResult(
                payload={
                    "filenames": result.filenames,
                    "durationMs": result.duration_ms,
                    "numFiles": result.num_files,
                    "truncated": result.truncated,
                }
            )
        if tool_name == "Grep":
            result = await asyncio.to_thread(
                GrepTool.call,
                GrepInput(
                    pattern=_string_input(raw_input, "pattern"),
                    path=self._resolve_optional_tool_path(
                        _optional_string_input(raw_input, "path")
                    )
                    or self._working_directory(),
                    glob=_optional_string_input(raw_input, "glob", "include"),
                    exclude=_optional_string_input(raw_input, "exclude"),
                    context_before=_optional_non_negative_int_input(
                        raw_input,
                        "-B",
                    ),
                    context_after=_optional_non_negative_int_input(
                        raw_input,
                        "-A",
                    ),
                    context_c=_optional_non_negative_int_input(raw_input, "-C"),
                    context=_optional_non_negative_int_input(raw_input, "context"),
                    show_line_numbers=_optional_bool_input(raw_input, "-n"),
                    case_insensitive=_bool_input(raw_input, "-i", default=False),
                    type=_optional_string_input(raw_input, "type"),
                    output_mode=_optional_string_input(
                        raw_input,
                        "output_mode",
                        default="files_with_matches",
                    )
                    or "files_with_matches",
                    head_limit=_optional_non_negative_int_input(
                        raw_input,
                        "head_limit",
                    ),
                    offset=_optional_non_negative_int_input(raw_input, "offset"),
                    multiline=_bool_input(raw_input, "multiline", default=False),
                ),
            )
            return _ToolDispatchResult(
                payload={
                    "mode": result.mode,
                    "filenames": result.filenames,
                    "numFiles": result.num_files,
                    "content": result.content,
                    "numLines": result.num_lines,
                    "numMatches": result.num_matches,
                    "appliedLimit": result.applied_limit,
                    "appliedOffset": result.applied_offset,
                }
            )
        if tool_name == "Bash":
            command = _string_input(raw_input, "command")
            description = _optional_string_input(raw_input, "description")
            run_in_background = _bool_input(
                raw_input,
                "run_in_background",
                "runInBackground",
            )
            if run_in_background:
                output_file = _allocate_background_output_file()
                task = self.task_manager.create_local_shell_task(
                    command=("bash", "-lc", command),
                    description=description or command,
                    output_file=output_file,
                    backgrounded=True,
                    backgrounded_by_user=True,
                    cwd=self._working_directory(),
                )
                self._store_task_state(task)
                return _ToolDispatchResult(
                    payload=_background_bash_payload(task)
                )
            result = BashTool.call(
                BashInput(
                    command=command,
                    timeout=_optional_positive_int_input(raw_input, "timeout"),
                    description=description,
                    run_in_background=False,
                    cwd=self._working_directory(),
                    dangerously_disable_sandbox=_bool_input(
                        raw_input,
                        "dangerously_disable_sandbox",
                        "dangerouslyDisableSandbox",
                    ),
                )
            )
            return _ToolDispatchResult(
                payload=_foreground_bash_payload(result),
                is_error=BashTool.is_failure(result),
            )
        if tool_name == "WebFetch":
            return _ToolDispatchResult(
                payload=await asyncio.to_thread(
                    web_fetch,
                    _string_input(raw_input, "url"),
                    _optional_string_input(raw_input, "prompt", default="") or "",
                    model=self._selected_model(),
                    skip_preflight=_bool_input(
                        self.app_state.settings,
                        "skipWebFetchPreflight",
                        default=False,
                    ),
                )
            )
        if tool_name == "WebSearch":
            return _ToolDispatchResult(
                payload=await asyncio.to_thread(
                    web_search,
                    _string_input(raw_input, "query"),
                    allowed_domains=_optional_string_list_input(
                        raw_input,
                        "allowed_domains",
                        "allowedDomains",
                    ),
                    blocked_domains=_optional_string_list_input(
                        raw_input,
                        "blocked_domains",
                        "blockedDomains",
                    ),
                    offset=_optional_non_negative_int_input(
                        raw_input,
                        "offset",
                    )
                    or 0,
                    max_results=_optional_positive_int_input(
                        raw_input,
                        "max_results",
                        "maxResults",
                    ),
                    model=self._selected_model(),
                )
            )
        if tool_name == "CtxInspect":
            return await asyncio.to_thread(self._dispatch_ctx_inspect)
        if tool_name == "Snip":
            return await self._dispatch_snip(assistant_uuid=assistant_uuid)
        if tool_name == "LSP":
            return _ToolDispatchResult(
                payload=await lsp_call(
                    operation=_string_input(raw_input, "operation"),
                    file_path=self._resolve_tool_path(
                        _string_input(raw_input, "filePath", "file_path")
                    ),
                    line=_positive_int_input(raw_input, "line", default=1),
                    character=_non_negative_int_input(
                        raw_input,
                        "character",
                        default=0,
                    ),
                    cwd=self._working_directory(),
                )
            )
        if tool_name == "EnterWorktree":
            return await self._dispatch_enter_worktree(
                raw_input,
                tool_use_id=tool_use_id,
            )
        if tool_name == "ExitWorktree":
            return await self._dispatch_exit_worktree(
                raw_input,
                tool_use_id=tool_use_id,
            )
        if tool_name == "AskUserQuestion":
            if not self._is_tool_enabled(tool_name):
                return _ToolDispatchResult(
                    rendered=ask_user_question_disabled_message(),
                    is_error=True,
                )
            effective_input = dict(raw_input)
            normalized_questions = normalize_ask_user_questions(effective_input)
            elicitation_hook = await self.emit_hook_event(
                "Elicitation",
                self._elicitation_hook_payload(
                    raw_input=effective_input,
                    tool_use_id=tool_use_id,
                    assistant_uuid=assistant_uuid,
                    questions=normalized_questions,
                ),
            )
            if elicitation_hook.updated_input is not None:
                effective_input.update(dict(elicitation_hook.updated_input))
                normalized_questions = normalize_ask_user_questions(effective_input)
            result_payload = ask_user_question(
                effective_input,
                normalized_questions=normalized_questions,
            )
            return _ToolDispatchResult(
                payload=result_payload,
                pre_updates=self._hook_updates(elicitation_hook.messages),
                hook_events=(
                    (
                        "ElicitationResult",
                        self._elicitation_result_hook_payload(
                            raw_input=effective_input,
                            tool_use_id=tool_use_id,
                            assistant_uuid=assistant_uuid,
                            questions=normalized_questions,
                            result=result_payload,
                        ),
                    ),
                ),
            )
        if tool_name == "EnterPlanMode":
            return self._enter_plan_mode()
        if tool_name == "ExitPlanMode":
            return self._exit_plan_mode(raw_input)
        if tool_name == "SendUserMessage":
            kairos_check_passed = self.app_state.kairos_enabled
            result = self._send_user_message(raw_input, tool_use_id=tool_use_id)
            if isinstance(result.payload, Mapping) and not kairos_check_passed:
                payload_dict = dict(result.payload)
                payload_dict["_kairos_warning"] = "SendUserMessage is optimized for KAIROS mode"
                return _ToolDispatchResult(
                    payload=payload_dict,
                    rendered=result.rendered,
                    block_content=result.block_content,
                    new_context=result.new_context,
                    pre_updates=result.pre_updates,
                    extra_updates=result.extra_updates,
                    hook_events=result.hook_events,
                    is_error=result.is_error,
                )
            return result
        if tool_name == "Config":
            if not is_ant_user():
                return _ToolDispatchResult(
                    rendered="ConfigTool is only available for ant users",
                    is_error=True,
                )
            return self._dispatch_config(raw_input)
        if tool_name == "REPL":
            if not is_ant_user():
                return _ToolDispatchResult(
                    rendered="REPL tool is only available for ant users",
                    is_error=True,
                )
            return self._dispatch_repl(raw_input)
        if tool_name == "CronCreate":
            if not is_ant_user():
                return _ToolDispatchResult(
                    rendered="CronCreate is only available for ant users",
                    is_error=True,
                )
            return self._dispatch_cron_create(raw_input)
        if tool_name == "CronDelete":
            if not is_ant_user():
                return _ToolDispatchResult(
                    rendered="CronDelete is only available for ant users",
                    is_error=True,
                )
            return self._dispatch_cron_delete(raw_input)
        if tool_name == "CronList":
            if not is_ant_user():
                return _ToolDispatchResult(
                    rendered="CronList is only available for ant users",
                    is_error=True,
                )
            return self._dispatch_cron_list()
        if tool_name == "Skill":
            return await self._dispatch_skill(raw_input, tool_use_id=tool_use_id)
        if tool_name == "Agent":
            return await self._dispatch_agent(raw_input, tool_use_id=tool_use_id)
        if tool_name == "ListMcpResourcesTool":
            await self._ensure_mcp_clients_loaded()
            resources = await list_mcp_resources(
                self.mcp_clients,
                server=_optional_string_input(raw_input, "server"),
            )
            return _ToolDispatchResult(payload={"resources": resources})
        if tool_name == "ReadMcpResourceTool":
            await self._ensure_mcp_clients_loaded()
            server = _string_input(raw_input, "server")
            uri = _string_input(raw_input, "uri")
            return _ToolDispatchResult(
                payload=await read_mcp_resource(self.mcp_clients, server, uri)
            )
        if tool_name == "TodoWrite":
            todos = _todo_list_input(raw_input)
            old_todos = list(self.app_state.todos.get(_DEFAULT_TODO_KEY, []))
            all_done = bool(todos) and all(
                item.get("status") == "completed" for item in todos
            )
            new_todos = [] if all_done else todos
            self.app_state.todos[_DEFAULT_TODO_KEY] = new_todos
            # B6-M3: Verification nudge when 3+ todos completed
            completed_count = sum(
                1 for item in todos if item.get("status") == "completed"
            )
            extra_updates: tuple[ToolExecutionUpdate, ...] = ()
            if completed_count >= 3:
                extra_updates = (
                    ToolExecutionUpdate(
                        message=createSystemMessage(
                            f"✓ {completed_count} todos marked as completed. Take a moment to verify progress before continuing.",
                            level="info",
                        )
                    ),
                )
            return _ToolDispatchResult(
                payload={
                    "oldTodos": old_todos,
                    "newTodos": todos,
                },
                extra_updates=extra_updates,
            )
        if tool_name == "TaskCreate":
            _require_todo_v2_enabled()
            task = create_task(
                subject=_string_input(raw_input, "subject"),
                description=_optional_string_input(raw_input, "description", default="")
                or "",
                active_form=_optional_string_input(raw_input, "activeForm"),
                metadata=_optional_object_input(raw_input, "metadata") or {},
            )
            self.app_state.tasks[task.id] = task
            return _ToolDispatchResult(
                payload={"task": {"id": task.id, "subject": task.subject}}
            )
        if tool_name == "TaskGet":
            _require_todo_v2_enabled()
            task_id = _string_input(raw_input, "taskId", "task_id")
            task = get_task(task_id)
            if task is None:
                return _ToolDispatchResult(payload={"task": None})
            self.app_state.tasks[task.id] = task
            return _ToolDispatchResult(payload={"task": to_public_task(task)})
        if tool_name == "TaskList":
            _require_todo_v2_enabled()
            tasks = list_tasks()
            completed_ids = {task.id for task in tasks if task.status == TASK_STATUS_COMPLETED}
            public_tasks = []
            for task in tasks:
                self.app_state.tasks[task.id] = task
                public_task = to_list_task(task, completed_ids)
                if public_task is not None:
                    public_tasks.append(public_task)
            return _ToolDispatchResult(payload={"tasks": public_tasks})
        if tool_name == "TaskUpdate":
            _require_todo_v2_enabled()
            task_id = _string_input(raw_input, "taskId", "task_id")
            status = _optional_string_input(raw_input, "status")
            if status is not None and status not in {
                TASK_PENDING,
                TASK_IN_PROGRESS,
                TASK_COMPLETED,
                TASK_DELETED,
            }:
                raise ValueError(f"Invalid status: {status}")
            task, updated_fields, error = update_task(
                task_id=task_id,
                subject=_optional_string_input(raw_input, "subject"),
                description=_optional_string_input(raw_input, "description"),
                active_form=_optional_string_input(raw_input, "activeForm"),
                owner=_optional_string_input(raw_input, "owner"),
                status=status,
                metadata=_optional_object_input(raw_input, "metadata"),
                add_blocks=_optional_string_list_input(raw_input, "addBlocks"),
                add_blocked_by=_optional_string_list_input(raw_input, "addBlockedBy"),
            )
            if task is None and status == TASK_DELETED and error is None:
                self.app_state.tasks.pop(task_id, None)
                return _ToolDispatchResult(
                    payload={
                        "success": True,
                        "taskId": task_id,
                        "updatedFields": updated_fields,
                    }
                )
            if task is None:
                return _ToolDispatchResult(
                    payload={
                        "success": False,
                        "taskId": task_id,
                        "updatedFields": updated_fields,
                        "error": error,
                    }
                )
            self.app_state.tasks[task.id] = task
            # B8-M1: TaskUpdate completion nudges
            extra_updates: tuple[ToolExecutionUpdate, ...] = ()
            if status == TASK_COMPLETED and task is not None:
                extra_updates = (
                    ToolExecutionUpdate(
                        message=createSystemMessage(
                            f"✓ Task '{task.subject}' marked as completed. Great progress!",
                            level="info",
                        )
                    ),
                )
            return _ToolDispatchResult(
                payload={
                    "success": True,
                    "taskId": task_id,
                    "updatedFields": updated_fields,
                },
                extra_updates=extra_updates,
            )
        if tool_name == "TaskOutput":
            return await asyncio.to_thread(self._dispatch_task_output, raw_input)
        if tool_name == "TaskStop":
            return self._dispatch_task_stop(raw_input)
        if tool_name.startswith("mcp__"):
            return await self._dispatch_mcp_tool(tool_name, raw_input)
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    async def _dispatch_mcp_tool(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        await self._ensure_mcp_clients_loaded()
        target = self._mcp_tool_lookup.get(tool_name)
        if target is None:
            return _ToolDispatchResult(
                rendered=f"Error: No such MCP tool available: {tool_name}",
                is_error=True,
            )
        server_name, raw_tool_name = target
        client = next(
            (
                candidate
                for candidate in self.mcp_clients
                if getattr(candidate, "type", None) == "connected"
                and str(getattr(candidate, "name", "")) == server_name
            ),
            None,
        )
        if client is None:
            return _ToolDispatchResult(
                rendered=f"Error: MCP server '{server_name}' is not connected",
                is_error=True,
            )
        arguments = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        result = await call_mcp_tool(client, raw_tool_name, arguments)
        is_error = bool(result.get("isError"))
        block_content = _mcp_content_to_tool_result_blocks(result.get("content"))
        return _ToolDispatchResult(
            payload=result,
            block_content=block_content,
            is_error=is_error,
        )

    async def _dispatch_tool_search(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        query = _string_input(raw_input, "query").strip()
        max_results = (
            _optional_positive_int_input(raw_input, "max_results", "maxResults") or 5
        )
        await self._ensure_mcp_clients_loaded()
        available_tools = [
            definition
            for definition in (
                BUILTIN_TOOL_REGISTRY.get(tool_name)
                for tool_name in sorted(self._enabled_tool_names())
            )
            if definition is not None
        ]
        available_tools.extend(self._mcp_tool_definitions)
        deferred_tools = [
            definition
            for definition in available_tools
            if _tool_definition_is_deferred(definition)
        ]
        pending_servers = list(_pending_mcp_server_names(self.app_state))
        selected_names = _parse_tool_search_select(query)
        if selected_names is not None:
            matches: list[str] = []
            for requested_name in selected_names:
                definition = _find_tool_definition_in_pool(
                    deferred_tools,
                    requested_name,
                ) or _find_tool_definition_in_pool(
                    available_tools,
                    requested_name,
                )
                if definition is not None and definition.name not in matches:
                    matches.append(definition.name)
            return _tool_search_dispatch_result(
                matches=matches,
                query=query,
                total_deferred_tools=len(deferred_tools),
                pending_servers=pending_servers if not matches else (),
            )

        matches = _tool_search_keyword_matches(
            query,
            deferred_tools=deferred_tools,
            available_tools=available_tools,
            max_results=max_results,
        )
        return _tool_search_dispatch_result(
            matches=matches,
            query=query,
            total_deferred_tools=len(deferred_tools),
            pending_servers=pending_servers if not matches else (),
        )

    def _scheduler_tool_definitions(self) -> tuple[SchedulerToolDefinition, ...]:
        definitions: list[SchedulerToolDefinition] = []
        concurrent_tools = {
            "CtxInspect",
            "Glob",
            "Grep",
            "ListMcpResourcesTool",
            "LSP",
            "Read",
            "ReadMcpResourceTool",
            "TaskGet",
            "TaskList",
            "TaskOutput",
            "ToolSearch",
            "WebFetch",
            "WebSearch",
        }
        for tool_name in sorted(self._enabled_tool_names()):
            registry_definition = BUILTIN_TOOL_REGISTRY.get(tool_name)
            aliases = registry_definition.aliases if registry_definition is not None else ()
            if tool_name == "Bash":
                definitions.append(
                    SchedulerToolDefinition(
                        name=tool_name,
                        aliases=aliases,
                        validate_input=BashTool.validate_input,
                        is_concurrency_safe=BashTool.is_concurrency_safe,
                        interrupt_behavior=lambda: "cancel",
                    )
                )
                continue
            definitions.append(
                SchedulerToolDefinition(
                    name=tool_name,
                    aliases=aliases,
                    is_concurrency_safe=(
                        (lambda _raw_input: True)
                        if tool_name in concurrent_tools
                        else (lambda _raw_input: False)
                    ),
                    interrupt_behavior=lambda: "cancel"
                    if tool_name in concurrent_tools
                    else "block",
                )
            )
        self._prime_mcp_tools_sync()
        for mcp_definition in self._mcp_tool_definitions:
            definitions.append(
                SchedulerToolDefinition(
                    name=mcp_definition.name,
                    is_concurrency_safe=lambda _raw_input: False,
                    interrupt_behavior=lambda: "block",
                )
            )
        return tuple(definitions)

    async def _dispatch_bash_stream(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
        tool_use_context: ToolUseContext | None,
    ) -> AsyncGenerator[_ToolDispatchStreamEvent, None]:
        command = _string_input(raw_input, "command")
        description = _optional_string_input(raw_input, "description")
        bash_input = BashInput(
            command=command,
            timeout=_optional_positive_int_input(raw_input, "timeout"),
            description=description,
            run_in_background=False,
            cwd=self._working_directory(),
            dangerously_disable_sandbox=_bool_input(
                raw_input,
                "dangerously_disable_sandbox",
                "dangerouslyDisableSandbox",
            ),
        )
        start = time.monotonic()
        resolved_command = bash_input.command
        use_sandbox = (
            not bash_input.dangerously_disable_sandbox
            and should_use_sandbox(resolved_command)
        )
        if use_sandbox and is_sandboxing_enabled():
            try:
                resolved_command = await _wrap_sandbox_command(resolved_command)
            except Exception as exc:
                yield _ToolDispatchStreamEvent(
                    result=_ToolDispatchResult(
                        payload={
                            "stdout": "",
                            "stderr": f"Sandbox error: {exc}",
                            "exit_code": 1,
                            "duration_ms": (time.monotonic() - start) * 1000,
                            "interrupted": False,
                            "timed_out": False,
                        },
                        is_error=True,
                    )
                )
                return

        if self._bash_auto_background_enabled(command):
            async for managed_event in self._dispatch_bash_stream_with_auto_background(
                bash_input=bash_input,
                resolved_command=resolved_command,
                command=command,
                description=description,
                tool_use_id=tool_use_id,
                tool_use_context=tool_use_context,
                start=start,
                use_sandbox=use_sandbox,
            ):
                yield managed_event
            return

        env = dict(os.environ)
        env["CI"] = "1"
        try:
            process = await asyncio.create_subprocess_shell(
                resolved_command,
                cwd=bash_input.cwd or os.getcwd(),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as exc:
            yield _ToolDispatchStreamEvent(
                result=_ToolDispatchResult(
                    payload={
                        "stdout": "",
                        "stderr": f"Process error: {exc}",
                        "exit_code": 1,
                        "duration_ms": (time.monotonic() - start) * 1000,
                        "interrupted": False,
                        "timed_out": False,
                    },
                    is_error=True,
                )
            )
            return

        output_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        stdout_buffer = ""
        stderr_buffer = ""
        progress_started = False
        last_progress_at = start
        heartbeat_seconds = 0.1
        progress_start_seconds = 0.15
        progress_dirty = False
        timed_out = False
        aborted = False
        timeout_seconds = BashTool.resolve_timeout(bash_input) / 1000.0
        deadline = start + timeout_seconds
        wait_task = asyncio.create_task(process.wait())

        async def _pump_stream(
            stream: asyncio.StreamReader | None,
            stream_name: str,
        ) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                await output_queue.put(
                    (stream_name, chunk.decode("utf-8", errors="replace"))
                )

        stdout_task = asyncio.create_task(_pump_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(_pump_stream(process.stderr, "stderr"))

        try:
            while True:
                if (
                    tool_use_context is not None
                    and tool_use_context.abort_controller.signal.aborted
                    and process.returncode is None
                ):
                    aborted = True
                    _terminate_process(process)

                now = time.monotonic()
                if process.returncode is None and now >= deadline:
                    timed_out = True
                    _terminate_process(process, force=True)

                queue_task = asyncio.create_task(output_queue.get())
                done, pending = await asyncio.wait(
                    (wait_task, queue_task),
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task in done:
                    stream_name, text = queue_task.result()
                    if stream_name == "stdout":
                        stdout_buffer = _append_limited_output(stdout_buffer, text)
                    else:
                        stderr_buffer = _append_limited_output(stderr_buffer, text)
                    progress_dirty = True
                else:
                    queue_task.cancel()
                    try:
                        await queue_task
                    except asyncio.CancelledError:
                        pass

                now = time.monotonic()
                should_emit_progress = (
                    process.returncode is None
                    and now - start >= progress_start_seconds
                    and (
                        progress_dirty
                        or not progress_started
                        or now - last_progress_at >= heartbeat_seconds
                    )
                )
                if should_emit_progress:
                    progress_started = True
                    progress_dirty = False
                    last_progress_at = now
                    progress_payload: dict[str, Any] = {
                        "step": "running",
                        "elapsedMs": int((now - start) * 1000),
                    }
                    stdout_tail = _tail_output(stdout_buffer)
                    stderr_tail = _tail_output(stderr_buffer)
                    if stdout_tail:
                        progress_payload["stdout"] = stdout_tail
                    if stderr_tail:
                        progress_payload["stderr"] = stderr_tail
                    yield _ToolDispatchStreamEvent(
                        update=ToolExecutionUpdate(
                            message=ProgressMessage(
                                data=progress_payload,
                                toolUseID=tool_use_id,
                                parentToolUseID=tool_use_id,
                            )
                        )
                    )

                if wait_task in done and output_queue.empty() and stdout_task.done() and stderr_task.done():
                    break
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            await asyncio.gather(wait_task, return_exceptions=True)

        abort_reason: str | None = None
        if tool_use_context is not None:
            await asyncio.sleep(0)
            abort_reason = _optional_abort_reason(tool_use_context.abort_controller.signal)
            if abort_reason is None and timed_out:
                await asyncio.sleep(0.01)
                abort_reason = _optional_abort_reason(
                    tool_use_context.abort_controller.signal
                )
        if abort_reason == "sibling_error":
            yield _ToolDispatchStreamEvent(
                result=_ToolDispatchResult(
                    rendered=_render_parallel_bash_cancelled_message(command),
                    is_error=True,
                )
            )
            return

        exit_code = process.returncode if process.returncode is not None else 0
        interrupted = aborted
        if exit_code == -signal.SIGINT:
            interrupted = True
            exit_code = 130
        elif exit_code < 0:
            interrupted = True

        result = BashTool.finalize_output(
            command=command,
            stdout=stdout_buffer,
            stderr=stderr_buffer,
            exit_code=exit_code,
            duration_ms=(time.monotonic() - start) * 1000,
            interrupted=interrupted,
            timed_out=timed_out,
            use_sandbox=use_sandbox,
            cwd=bash_input.cwd or os.getcwd(),
        )

        yield _ToolDispatchStreamEvent(
            result=_ToolDispatchResult(
                payload=_foreground_bash_payload(result),
                is_error=BashTool.is_failure(result),
            )
        )

    async def _dispatch_bash_stream_with_auto_background(
        self,
        *,
        bash_input: BashInput,
        resolved_command: str,
        command: str,
        description: str | None,
        tool_use_id: str,
        tool_use_context: ToolUseContext | None,
        start: float,
        use_sandbox: bool,
    ) -> AsyncGenerator[_ToolDispatchStreamEvent, None]:
        output_file = _allocate_background_output_file(prefix="bash")
        task = self.task_manager.create_local_shell_task(
            command=("bash", "-lc", resolved_command),
            description=description or command,
            output_file=output_file,
            backgrounded=False,
            cwd=bash_input.cwd or os.getcwd(),
        )
        self._suppressed_task_event_ids.add(task.id)

        stdout_buffer = ""
        last_log_length = 0
        progress_started = False
        last_progress_at = start
        heartbeat_seconds = 0.1
        progress_start_seconds = 0.15
        progress_dirty = False
        timed_out = False
        aborted = False
        timeout_seconds = BashTool.resolve_timeout(bash_input) / 1000.0
        deadline = start + timeout_seconds
        auto_background_deadline = start + (_DEFAULT_BASH_AUTO_BACKGROUND_MS / 1000.0)

        while True:
            current = self.task_manager.get_task(task.id)
            if (
                tool_use_context is not None
                and tool_use_context.abort_controller.signal.aborted
                and current.status in {TASK_STATUS_PENDING, TASK_STATUS_RUNNING}
            ):
                aborted = True
                current = self.task_manager.stop_task(task.id)

            if len(current.logs) > last_log_length:
                new_output = current.logs[last_log_length:]
                last_log_length = len(current.logs)
                stdout_buffer = _append_limited_output(stdout_buffer, new_output)
                progress_dirty = True

            now = time.monotonic()
            if current.status in {
                TASK_STATUS_COMPLETED,
                TASK_STATUS_FAILED,
                TASK_STATUS_KILLED,
            }:
                break

            if now >= deadline:
                timed_out = True
                current = self.task_manager.stop_task(task.id)
                if len(current.logs) > last_log_length:
                    new_output = current.logs[last_log_length:]
                    last_log_length = len(current.logs)
                    stdout_buffer = _append_limited_output(stdout_buffer, new_output)
                    progress_dirty = True
                break

            if now >= auto_background_deadline:
                current = self.task_manager.background_task(
                    task.id,
                    assistant_auto_backgrounded=True,
                )
                self._suppressed_task_event_ids.discard(task.id)
                self._store_task_state(current)
                yield _ToolDispatchStreamEvent(
                    result=_ToolDispatchResult(
                        payload=_background_bash_payload(current)
                    )
                )
                return

            should_emit_progress = (
                now - start >= progress_start_seconds
                and (
                    progress_dirty
                    or not progress_started
                    or now - last_progress_at >= heartbeat_seconds
                )
            )
            if should_emit_progress:
                progress_started = True
                progress_dirty = False
                last_progress_at = now
                progress_payload: dict[str, Any] = {
                    "step": "running",
                    "elapsedMs": int((now - start) * 1000),
                }
                stdout_tail = _tail_output(stdout_buffer)
                if stdout_tail:
                    progress_payload["stdout"] = stdout_tail
                yield _ToolDispatchStreamEvent(
                    update=ToolExecutionUpdate(
                        message=ProgressMessage(
                            data=progress_payload,
                            toolUseID=tool_use_id,
                            parentToolUseID=tool_use_id,
                        )
                    )
                )

            await asyncio.sleep(heartbeat_seconds)

        abort_reason: str | None = None
        if tool_use_context is not None:
            await asyncio.sleep(0)
            abort_reason = _optional_abort_reason(tool_use_context.abort_controller.signal)
            if abort_reason is None and timed_out:
                await asyncio.sleep(0.01)
                abort_reason = _optional_abort_reason(
                    tool_use_context.abort_controller.signal
                )
        if abort_reason == "sibling_error":
            yield _ToolDispatchStreamEvent(
                result=_ToolDispatchResult(
                    rendered=_render_parallel_bash_cancelled_message(command),
                    is_error=True,
                )
            )
            return

        current = self.task_manager.get_task(task.id)
        result = BashTool.finalize_output(
            command=command,
            stdout=stdout_buffer,
            stderr="",
            exit_code=current.exit_code if current.exit_code is not None else 0,
            duration_ms=(time.monotonic() - start) * 1000,
            interrupted=aborted or current.status == TASK_STATUS_KILLED,
            timed_out=timed_out,
            use_sandbox=use_sandbox,
            cwd=bash_input.cwd or os.getcwd(),
        )
        yield _ToolDispatchStreamEvent(
            result=_ToolDispatchResult(
                payload=_foreground_bash_payload(result),
                is_error=BashTool.is_failure(result),
            )
        )

    def _dispatch_task_output(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        task_id = _string_input(raw_input, "task_id", "taskId")
        block = _bool_input(raw_input, "block", default=True)
        timeout_ms = min(
            _non_negative_int_input(
                raw_input,
                "timeout",
                default=_TASK_OUTPUT_DEFAULT_WAIT_MS,
            ),
            _TASK_OUTPUT_MAX_WAIT_MS,
        )
        task = self._get_known_task(task_id)
        if task is None:
            raise ValueError(f"No task found with ID: {task_id}")
        if isinstance(task, AgentTaskState):
            if not block:
                current = self._refresh_task_state(task_id) or task
                retrieval_status = (
                    "success"
                    if current.status
                    in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_KILLED}
                    else "not_ready"
                )
                return _ToolDispatchResult(
                    payload={
                        "retrieval_status": retrieval_status,
                        "task": _task_output_payload(current),
                    }
                )
            waited = self.agent_manager.wait_for_agent(task_id, timeout=timeout_ms / 1000.0)
            self._store_task_state(waited)
            retrieval_status = (
                "timeout"
                if waited.status in {TASK_STATUS_PENDING, TASK_STATUS_RUNNING}
                else "success"
            )
            return _ToolDispatchResult(
                payload={
                    "retrieval_status": retrieval_status,
                    "task": _task_output_payload(waited),
                }
            )

        if not block:
            current = self._refresh_task_state(task_id) or task
            retrieval_status = (
                "success"
                if current.status not in {TASK_STATUS_PENDING, TASK_STATUS_RUNNING}
                else "not_ready"
            )
            return _ToolDispatchResult(
                payload={
                    "retrieval_status": retrieval_status,
                    "task": _task_output_payload(current),
                }
            )
        waited = self.task_manager.wait_for_task(task_id, timeout=timeout_ms / 1000.0)
        self._store_task_state(waited)
        if waited.status in {TASK_STATUS_PENDING, TASK_STATUS_RUNNING}:
            return _ToolDispatchResult(
                payload={
                    "retrieval_status": "timeout",
                    "task": _task_output_payload(waited),
                }
            )
        return _ToolDispatchResult(
            payload={
                "retrieval_status": "success",
                "task": _task_output_payload(waited),
            }
        )

    def _dispatch_config(self, raw_input: Mapping[str, Any]) -> _ToolDispatchResult:
        action = raw_input.get("action")
        setting = raw_input.get("setting")

        # Handle 'list' action - return all settings
        if action == "list":
            return _ToolDispatchResult(
                payload={
                    "action": "list",
                    "settings": dict(self.app_state.settings),
                }
            )

        # Handle 'delete' action - remove a setting
        if action == "delete":
            if not setting:
                return _ToolDispatchResult(
                    payload={
                        "action": "delete",
                        "success": False,
                        "error": "setting is required for delete action",
                    }
                )
            old_value = self.app_state.settings.get(setting)
            has_old_value = setting in self.app_state.settings
            if has_old_value:
                del self.app_state.settings[setting]
            return _ToolDispatchResult(
                payload={
                    "action": "delete",
                    "setting": setting,
                    "success": has_old_value,
                    "existed": has_old_value,
                    "oldValue": old_value,
                }
            )

        # Handle 'describe' action - return metadata about a setting
        if action == "describe":
            if not setting:
                return _ToolDispatchResult(
                    payload={
                        "action": "describe",
                        "success": False,
                        "error": "setting is required for describe action",
                    }
                )
            value = self.app_state.settings.get(setting)
            has_value = setting in self.app_state.settings
            # Infer type from value
            value_type: str | None = None
            if has_value:
                if isinstance(value, bool):
                    value_type = "boolean"
                elif isinstance(value, str):
                    value_type = "string"
                elif isinstance(value, (int, float)):
                    value_type = "number"
                elif isinstance(value, list):
                    value_type = "array"
                elif isinstance(value, dict):
                    value_type = "object"
                else:
                    value_type = "unknown"
            return _ToolDispatchResult(
                payload={
                    "action": "describe",
                    "setting": setting,
                    "exists": has_value,
                    "value": value,
                    "type": value_type,
                }
            )

        # Default behavior: get (no value) or set (with value)
        if not setting:
            return _ToolDispatchResult(
                payload={
                    "success": False,
                    "error": "setting is required",
                }
            )
        old_value = self.app_state.settings.get(setting)
        has_old_value = setting in self.app_state.settings
        if "value" not in raw_input:
            return _ToolDispatchResult(
                payload={
                    "setting": setting,
                    "value": old_value,
                    "exists": has_old_value,
                    "updated": False,
                }
            )
        new_value = raw_input.get("value")
        self.app_state.settings[setting] = new_value
        return _ToolDispatchResult(
            payload={
                "setting": setting,
                "oldValue": old_value,
                "value": new_value,
                "exists": True,
                "updated": True,
            }
        )

    def _dispatch_cron_create(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        cron = _string_input(raw_input, "cron")
        prompt = _string_input(raw_input, "prompt")
        job = {
            "id": f"cron-{uuid.uuid4().hex[:12]}",
            "cron": cron,
            "prompt": prompt,
            "recurring": _bool_input(raw_input, "recurring", default=True),
            "durable": _bool_input(raw_input, "durable", default=False),
            "createdAt": time.time(),
            "updatedAt": time.time(),
        }
        jobs = self._cron_jobs()
        jobs[job["id"]] = job
        self._save_cron_jobs_to_file(jobs)
        return _ToolDispatchResult(payload={"job": dict(job)})

    def _dispatch_cron_delete(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        job_id = _string_input(raw_input, "id")
        jobs = self._cron_jobs()
        deleted = jobs.pop(job_id, None)
        self._save_cron_jobs_to_file(jobs)
        return _ToolDispatchResult(
            payload={
                "success": deleted is not None,
                "id": job_id,
                "deletedJob": dict(deleted) if isinstance(deleted, Mapping) else None,
            }
        )

    def _dispatch_cron_list(self) -> _ToolDispatchResult:
        jobs = sorted(
            (dict(job) for job in self._cron_jobs().values() if isinstance(job, Mapping)),
            key=lambda job: (float(job.get("createdAt") or 0.0), str(job.get("id") or "")),
        )
        for job in jobs:
            cron_expr = job.get("cron")
            if isinstance(cron_expr, str):
                job["humanSchedule"] = _cron_to_human_schedule(cron_expr)
        return _ToolDispatchResult(payload={"jobs": jobs})

    def _cron_jobs(self) -> dict[str, Any]:
        value = self.app_state.settings.get(_CRON_JOBS_SETTINGS_KEY)
        if isinstance(value, dict):
            return value
        # Try to load from file on startup
        file_jobs = self._load_cron_jobs_from_file()
        if isinstance(file_jobs, dict):
            self.app_state.settings[_CRON_JOBS_SETTINGS_KEY] = file_jobs
            return file_jobs
        jobs: dict[str, Any] = {}
        self.app_state.settings[_CRON_JOBS_SETTINGS_KEY] = jobs
        return jobs

    def _cron_jobs_file_path(self) -> Path | None:
        """Get the path to the cron jobs JSON file."""
        runtime_context = self._settings_runtime_context()
        if not isinstance(runtime_context, Mapping):
            return None
        raw_config_home = runtime_context.get("config_home")
        if not isinstance(raw_config_home, str) or not raw_config_home.strip():
            return None
        return Path(raw_config_home.strip()) / _CRON_JOBS_FILE_NAME

    def _load_cron_jobs_from_file(self) -> dict[str, Any] | None:
        """Load cron jobs from JSON file if it exists."""
        file_path = self._cron_jobs_file_path()
        if file_path is None or not file_path.exists():
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def _save_cron_jobs_to_file(self, jobs: dict[str, Any]) -> None:
        """Save cron jobs to JSON file atomically (write to temp, then rename)."""
        file_path = self._cron_jobs_file_path()
        if file_path is None:
            return
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json", prefix="cron-jobs-", dir=file_path.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(jobs, f)
                os.replace(tmp_path, file_path)
            except OSError:
                # Clean up temp file on failure
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
        except OSError:
            pass

    def _dispatch_repl(self, raw_input: Mapping[str, Any]) -> _ToolDispatchResult:
        code = (
            _optional_string_input(raw_input, "code")
            or _optional_string_input(raw_input, "command")
            or _optional_string_input(raw_input, "input")
        )
        namespace = self._repl_namespace
        if not namespace:
            namespace.update(
                {
                    "__name__": "__claude_repl__",
                    "app_state": self.app_state,
                    "executor": self,
                }
            )
        if code is None or not code.strip():
            return _ToolDispatchResult(
                payload={
                    "status": "ready",
                    "variables": sorted(
                        key for key in namespace if not key.startswith("__")
                    ),
                }
            )

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        result: Any = None
        mode = "exec"
        try:
            try:
                compiled = compile(code, "<REPL>", "eval")
                mode = "eval"
            except SyntaxError:
                compiled = compile(code, "<REPL>", "exec")
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(
                stderr_buffer
            ):
                if mode == "eval":
                    result = eval(compiled, namespace, namespace)
                else:
                    exec(compiled, namespace, namespace)
        except Exception as exc:
            return _ToolDispatchResult(
                payload={
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "stdout": stdout_buffer.getvalue(),
                    "stderr": stderr_buffer.getvalue(),
                },
                is_error=True,
            )

        return _ToolDispatchResult(
            payload={
                "status": "ok",
                "mode": mode,
                "result": repr(result) if mode == "eval" else None,
                "stdout": stdout_buffer.getvalue(),
                "stderr": stderr_buffer.getvalue(),
                "variables": sorted(
                    key for key in namespace if not key.startswith("__")
                ),
            }
        )

    def _dispatch_task_stop(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        task_id = _optional_string_input(raw_input, "task_id", "taskId")
        shell_id = _optional_string_input(raw_input, "shell_id", "shellId")
        resolved_id = task_id or shell_id
        if resolved_id is None:
            raise ValueError("Missing required parameter: task_id")
        task = self._get_known_task(resolved_id)
        if task is None:
            raise ValueError(f"No task found with ID: {resolved_id}")
        if task.status != TASK_STATUS_RUNNING:
            if isinstance(task, AgentTaskState):
                return _ToolDispatchResult(
                    payload={
                        "message": (
                            f"Task {task.task_id} is already {task.status}: "
                            f"{task.description}"
                        ),
                        "task_id": task.task_id,
                        "task_type": "local_agent",
                        "command": task.description,
                        "status": task.status,
                    }
                )
            command = " ".join(task.command) if task.command else task.description
            return _ToolDispatchResult(
                payload={
                    "message": f"Task {task.id} is already {task.status}: {command}",
                    "task_id": task.id,
                    "task_type": task.task_type,
                    "command": command,
                    "status": task.status,
                }
            )
        if isinstance(task, AgentTaskState):
            stopped = self.agent_manager.kill_async_agent(resolved_id)
            self._store_task_state(stopped)
            return _ToolDispatchResult(
                payload={
                    "message": f"Successfully stopped task: {stopped.task_id} ({stopped.description})",
                    "task_id": stopped.task_id,
                    "task_type": "local_agent",
                    "command": stopped.description,
                }
            )
        stopped = self.task_manager.stop_task(resolved_id)
        self._store_task_state(stopped)
        command = " ".join(stopped.command) if stopped.command else stopped.description
        return _ToolDispatchResult(
            payload={
                "message": f"Successfully stopped task: {stopped.id} ({command})",
                "task_id": stopped.id,
                "task_type": stopped.task_type,
                "command": command,
            }
        )

    def _enter_plan_mode(self) -> _ToolDispatchResult:
        context = self._current_permission_context()
        if context.get("mode") != "plan":
            context["pre_plan_mode"] = context.get("mode") or "default"
        context["mode"] = "plan"
        self.app_state.tool_permission_context = context
        return _ToolDispatchResult(
            payload={
                "message": "Entered plan mode. Read-only tools remain available.",
            },
            new_context=context,
        )

    def _exit_plan_mode(
        self,
        raw_input: Mapping[str, Any],
    ) -> _ToolDispatchResult:
        context = self._current_permission_context()
        if context.get("mode") != "plan":
            raise ValueError("Not currently in plan mode")
        restored_mode = context.pop("pre_plan_mode", "default") or "default"
        context["mode"] = restored_mode
        allowed_prompts = _optional_string_list_input(raw_input, "allowedPrompts")
        if allowed_prompts:
            context["allowedPrompts"] = allowed_prompts
        else:
            context.pop("allowedPrompts", None)
        self.app_state.tool_permission_context = context
        return _ToolDispatchResult(
            payload={
                "plan": "Exited plan mode.",
                "warnings": [],
            },
            new_context=context,
        )

    def _send_user_message(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
    ) -> _ToolDispatchResult:
        message = _string_input(raw_input, "message")
        status = _optional_string_input(raw_input, "status", default="normal") or "normal"
        if status not in {"normal", "proactive"}:
            raise ValueError("status must be either 'normal' or 'proactive'")
        attachments = _optional_string_list_input(raw_input, "attachments") or []
        normalized_attachments = []
        for path in attachments:
            full_path = self._resolve_tool_path(path)
            if not os.path.exists(full_path):
                raise FileNotFoundError(f"Attachment does not exist: {path}")
            normalized_attachments.append(full_path)
        rendered_message = message.strip()
        if normalized_attachments:
            rendered_message += "\nAttachments: " + ", ".join(normalized_attachments)
        if status == "proactive":
            rendered_message = "[proactive] " + rendered_message
        return _ToolDispatchResult(
            payload={
                "delivered": True,
                "message": message,
                "status": status,
                "attachments": normalized_attachments,
            },
            extra_updates=(
                ToolExecutionUpdate(
                    message=createSystemMessage(
                        rendered_message,
                        level="info",
                        toolUseID=tool_use_id,
                    )
                ),
            ),
        )

    async def _dispatch_skill(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
    ) -> _ToolDispatchResult:
        skill = _string_input(raw_input, "skill")
        args = _optional_string_input(raw_input, "args", default="") or ""
        await self._ensure_mcp_clients_loaded()
        commands = self._load_skill_commands()
        validation = SkillTool.validate_input(skill, commands)
        if not validation.result:
            raise ValueError(validation.message)

        normalized = skill.strip()[1:] if skill.strip().startswith("/") else skill.strip()
        command = SkillTool.find_command(normalized, commands)
        if command is None:
            raise ValueError(f"Unknown skill: {normalized}")
        prompt_blocks = command.get_prompt_for_command(args, {"session_id": tool_use_id})
        injected_prompt = "\n".join(
            block.get("text", "")
            for block in prompt_blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        ).strip()

        if command.context == "fork":
            scoped_hook_configs: tuple[Mapping[str, object], ...] = ()
            if isinstance(command.hooks, Mapping):
                scoped_hook_configs = (command.hooks,)
            agent_outcome = await self._invoke_agent_tool(
                description=f"Run skill {normalized}",
                prompt=injected_prompt or f"Run skill {normalized}",
                tool_use_id=tool_use_id,
                run_in_background=False,
                model=command.model,
                cwd=None,
                max_tokens=None,
                agent_type=command.agent or "skill-agent",
                name=f"skill-{normalized}",
                scoped_hook_configs=scoped_hook_configs,
            )
            merged = dict(agent_outcome.payload)
            merged.update(
                {
                    "success": True,
                    "commandName": normalized,
                    "status": "forked",
                    "result": injected_prompt,
                    "allowedTools": list(command.allowed_tools),
                    "model": command.model,
                }
            )
            return _ToolDispatchResult(
                payload=merged,
                extra_updates=agent_outcome.extra_updates,
            )

        result = SkillTool.call(
            skill,
            args,
            commands,
            tool_use_context={"session_id": tool_use_id},
        )
        merged = dict(result)
        merged["result"] = injected_prompt
        merged["injectedPrompt"] = injected_prompt
        return _ToolDispatchResult(payload=merged)

    async def _dispatch_agent(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
    ) -> _ToolDispatchResult:
        requested_agent_type = _optional_string_input(
            raw_input,
            "subagent_type",
            "subagentType",
        ) or "local-subagent"
        agent_definition = self._resolve_agent_definition(requested_agent_type)
        resolved_model = _optional_string_input(raw_input, "model")
        if (
            resolved_model is None
            and agent_definition is not None
            and isinstance(agent_definition.model, str)
            and agent_definition.model.strip()
        ):
            resolved_model = agent_definition.model.strip()
        system_prompt = None
        if (
            agent_definition is not None
            and isinstance(agent_definition.prompt, str)
            and agent_definition.prompt.strip()
        ):
            system_prompt = agent_definition.prompt.strip()
        outcome = await self._invoke_agent_tool(
            description=_string_input(raw_input, "description"),
            prompt=_string_input(raw_input, "prompt"),
            tool_use_id=tool_use_id,
            run_in_background=_bool_input(
                raw_input,
                "run_in_background",
                "runInBackground",
            ),
            model=resolved_model,
            cwd=_optional_string_input(raw_input, "cwd"),
            max_tokens=_optional_positive_int_input(raw_input, "max_tokens"),
            agent_type=(
                agent_definition.resolved_agent_type
                if agent_definition is not None
                else requested_agent_type
            ),
            name=_optional_string_input(raw_input, "name"),
            system_prompt=system_prompt,
            scoped_hook_configs=(),
        )
        return _ToolDispatchResult(
            payload=outcome.payload,
            extra_updates=outcome.extra_updates,
        )

    async def _dispatch_enter_worktree(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
    ) -> _ToolDispatchResult:
        context = self._current_permission_context()
        active_worktree = context.get("worktree_path")
        if isinstance(active_worktree, str) and active_worktree.strip():
            return _ToolDispatchResult(
                rendered="Already inside a managed worktree; exit it before creating another.",
                is_error=True,
            )

        original_cwd = self._working_directory()
        try:
            worktree_name = _optional_string_input(raw_input, "name")
            repo_root, worktree_path = await asyncio.to_thread(
                _create_managed_worktree,
                original_cwd,
                worktree_name,
            )
        except Exception as exc:
            return _ToolDispatchResult(rendered=str(exc), is_error=True)

        setOriginalCwd(original_cwd)
        previous_directories = _serialize_additional_working_directories(
            self._parse_additional_working_directories(context, original_cwd)
        )
        updated_directories = dict(previous_directories)
        updated_directories["cwd"] = {
            "path": worktree_path,
            "source": "worktree",
        }
        updated_directories["original"] = {
            "path": original_cwd,
            "source": "worktree",
        }
        updated_directories["worktree"] = {
            "path": worktree_path,
            "source": "worktree",
        }

        new_context = dict(context)
        new_context["cwd"] = worktree_path
        new_context["additional_working_directories"] = updated_directories
        new_context["worktree_path"] = worktree_path
        new_context["worktree_repo_root"] = repo_root
        new_context["worktree_original_cwd"] = original_cwd
        new_context["worktree_managed"] = True
        new_context["_worktree_previous_additional_working_directories"] = (
            previous_directories
        )
        new_context.pop("additionalWorkingDirectories", None)

        extra_updates: list[ToolExecutionUpdate] = []
        create_payload = {
            "toolUseId": tool_use_id,
            "repoRoot": repo_root,
            "originalCwd": original_cwd,
            "worktreePath": worktree_path,
            "name": worktree_name,
        }
        create_hook = await self.emit_hook_event("WorktreeCreate", create_payload)
        extra_updates.extend(self._hook_updates(create_hook.messages))
        cwd_hook = await self.emit_hook_event(
            "CwdChanged",
            {
                **create_payload,
                "previousCwd": original_cwd,
                "cwd": worktree_path,
                "newCwd": worktree_path,
                "reason": "enter-worktree",
            },
        )
        extra_updates.extend(self._hook_updates(cwd_hook.messages))
        return _ToolDispatchResult(
            payload={
                "worktreePath": worktree_path,
                "message": f"Entered worktree at {worktree_path}",
            },
            new_context=new_context,
            extra_updates=tuple(extra_updates),
        )

    async def _dispatch_exit_worktree(
        self,
        raw_input: Mapping[str, Any],
        *,
        tool_use_id: str,
    ) -> _ToolDispatchResult:
        context = self._current_permission_context()
        worktree_path = context.get("worktree_path")
        if not isinstance(worktree_path, str) or not worktree_path.strip():
            return _ToolDispatchResult(
                rendered="No managed worktree is currently active.",
                is_error=True,
            )

        original_cwd = context.get("worktree_original_cwd")
        if not isinstance(original_cwd, str) or not original_cwd.strip():
            original_cwd = getOriginalCwd() or os.getcwd()
        action, discard_changes = _normalize_exit_worktree_action(
            _string_input(raw_input, "action"),
            discard_changes=_bool_input(
                raw_input,
                "discard_changes",
                "discardChanges",
            ),
        )
        try:
            await asyncio.to_thread(
                _exit_managed_worktree,
                worktree_path,
                action=action,
                discard_changes=discard_changes,
            )
        except Exception as exc:
            return _ToolDispatchResult(rendered=str(exc), is_error=True)

        previous_directories = _normalize_additional_directory_mapping(
            context.get("_worktree_previous_additional_working_directories")
        )
        if not previous_directories:
            previous_directories = {
                "cwd": {
                    "path": original_cwd,
                    "source": "session",
                }
            }
        else:
            previous_directories["cwd"] = {
                "path": original_cwd,
                "source": previous_directories.get("cwd", {}).get("source", "session"),
            }

        new_context = dict(context)
        new_context["cwd"] = original_cwd
        new_context["additional_working_directories"] = previous_directories
        new_context.pop("additionalWorkingDirectories", None)
        for key in (
            "worktree_path",
            "worktree_repo_root",
            "worktree_original_cwd",
            "worktree_managed",
            "_worktree_previous_additional_working_directories",
        ):
            new_context.pop(key, None)

        extra_updates: list[ToolExecutionUpdate] = []
        exit_payload = {
            "toolUseId": tool_use_id,
            "action": action,
            "discardChanges": discard_changes,
            "originalCwd": original_cwd,
            "worktreePath": worktree_path,
        }
        if action == "cleanup":
            remove_hook = await self.emit_hook_event("WorktreeRemove", exit_payload)
            extra_updates.extend(self._hook_updates(remove_hook.messages))
        cwd_hook = await self.emit_hook_event(
            "CwdChanged",
            {
                **exit_payload,
                "previousCwd": worktree_path,
                "cwd": original_cwd,
                "newCwd": original_cwd,
                "reason": "exit-worktree",
            },
        )
        extra_updates.extend(self._hook_updates(cwd_hook.messages))
        return _ToolDispatchResult(
            payload={
                "action": action,
                "originalCwd": original_cwd,
                "message": (
                    f"Exited worktree and returned to {original_cwd}"
                    if action == "return"
                    else f"Removed worktree and returned to {original_cwd}"
                ),
            },
            new_context=new_context,
            extra_updates=tuple(extra_updates),
        )

    async def _invoke_agent_tool(
        self,
        *,
        description: str,
        prompt: str,
        tool_use_id: str,
        run_in_background: bool,
        model: str | None,
        cwd: str | None,
        max_tokens: int | None,
        agent_type: str,
        name: str | None,
        system_prompt: str | None = None,
        scoped_hook_configs: Sequence[Mapping[str, object]] = (),
    ) -> _SubagentInvocationOutcome:
        agent_type = _normalize_agent_type(agent_type)
        agent_id = _slugify_agent_name(name or description) or f"agent-{uuid.uuid4().hex[:10]}"
        output_file = _allocate_background_output_file(prefix="agent")
        agent_cwd = self._resolve_agent_working_directory(cwd)
        extra_updates: list[ToolExecutionUpdate] = []
        start_payload = self._subagent_hook_payload(
            agent_id=agent_id,
            agent_type=agent_type,
            description=description,
            prompt=prompt,
            tool_use_id=tool_use_id,
            run_in_background=run_in_background,
            cwd=agent_cwd,
            model=model,
            max_tokens=max_tokens,
        )
        start_hook = await self.emit_hook_event("SubagentStart", start_payload)
        extra_updates.extend(self._hook_updates(start_hook.messages))
        child_hook_chain = tuple(self.scoped_hook_configs) + tuple(scoped_hook_configs)

        def _runner(record, progress, task_id=None):
            return self._run_agent_session_sync(
                agent_id=agent_id,
                prompt=prompt,
                record=record,
                progress=progress,
                task_id=task_id,
                cwd=agent_cwd,
                model=model,
                max_tokens=max_tokens,
                agent_type=agent_type,
                system_prompt=system_prompt,
                run_in_background=run_in_background,
                scoped_hook_configs=child_hook_chain,
            )

        agent_tool = AgentTool(manager=self.agent_manager)
        try:
            payload = await asyncio.to_thread(
                agent_tool.call,
                agent_id=agent_id,
                description=description,
                prompt=prompt,
                output_file=output_file,
                runner=_runner,
                should_run_async=run_in_background,
                agent_type=agent_type,
                cwd=agent_cwd,
                model=model,
                max_tokens=max_tokens,
                tool_use_id=tool_use_id,
                auto_background_ms=_DEFAULT_AGENT_AUTO_BACKGROUND_MS,
                timeout=_DEFAULT_AGENT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            stop_hook = await self.emit_hook_event(
                "SubagentStop",
                {
                    **start_payload,
                    "status": "failed",
                    "error": str(exc),
                },
            )
            extra_updates.extend(self._hook_updates(stop_hook.messages))
            raise
        if "taskId" in payload:
            self.app_state.tasks[str(payload["taskId"])] = self.agent_manager.get_agent_task(
                str(payload["taskId"])
            )
        stop_hook = await self.emit_hook_event(
            "SubagentStop",
            {
                **start_payload,
                "status": str(payload.get("status") or "completed"),
                "taskId": payload.get("taskId"),
                "error": payload.get("error"),
                "subagentResult": dict(payload),
            },
        )
        extra_updates.extend(self._hook_updates(stop_hook.messages))
        return _SubagentInvocationOutcome(
            payload=payload,
            extra_updates=tuple(extra_updates),
        )

    def _run_agent_session_sync(
        self,
        *,
        agent_id: str,
        prompt: str,
        record: Any,
        progress: Any,
        task_id: str | None,
        cwd: str,
        model: str | None,
        max_tokens: int | None,
        agent_type: str,
        system_prompt: str | None,
        run_in_background: bool,
        scoped_hook_configs: Sequence[Mapping[str, object]] = (),
    ) -> Any:
        context = self._current_permission_context()
        current_depth = int(context.get("_agent_depth", 0))
        if current_depth >= _MAX_AGENT_DEPTH:
            raise RuntimeError("Agent recursion depth exceeded")

        child_context = dict(context)
        child_context["_agent_depth"] = current_depth + 1
        child_context["cwd"] = cwd
        initial_mode = str(child_context.get("mode") or "default")
        if initial_mode not in {"bypassPermissions", "dontAsk", "plan"}:
            child_context["mode"] = "bubble"
            initial_mode = "bubble"
        child_app_state = get_default_app_state(
            settings=dict(self.app_state.settings),
            initial_mode=initial_mode,
            settings_runtime_context=self._settings_runtime_context(),
        )
        child_app_state.main_loop_model = self.app_state.main_loop_model
        child_app_state.main_loop_model_for_session = (
            model
            or self._selected_model()
        )
        child_app_state.tool_permission_context = child_context

        child_executor = LocalToolExecutor(
            mcp_server_configs=self._child_mcp_server_configs(),
            mcp_owner_id=f"agent:{agent_id}",
            mcp_cleanup_owned_on_close=True,
            read_file_state=self.read_file_state,
            app_state=child_app_state,
            task_manager=self.task_manager,
            agent_manager=self.agent_manager,
            scoped_hook_configs=tuple(scoped_hook_configs),
            inline_agents=tuple(self.inline_agents),
        )
        selected_model = (
            model
            or self._selected_model()
        )
        model_adapter = (
            create_model_adapter_from_env(model=selected_model)
            or LocalEchoModelAdapter()
        )

        def _background_request_resolver() -> bool:
            if run_in_background:
                return True
            if not isinstance(task_id, str) or not task_id:
                return False
            try:
                return self.agent_manager.get_agent_task(task_id).is_backgrounded
            except Exception:
                return False

        try:
            return asyncio.run(
                self._stream_agent_session(
                    agent_id=agent_id,
                    prompt=prompt,
                    record=record,
                    progress=progress,
                    model_adapter=model_adapter,
                    tool_executor=child_executor,
                    agent_type=agent_type,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    background_request_resolver=_background_request_resolver,
                )
            )
        finally:
            asyncio.run(child_executor.aclose())

    async def _stream_agent_session(
        self,
        *,
        agent_id: str,
        prompt: str,
        record: Any,
        progress: Any,
        model_adapter: Any,
        tool_executor: "LocalToolExecutor",
        agent_type: str,
        max_tokens: int | None,
        system_prompt: str | None,
        background_request_resolver: Callable[[], bool] | None,
    ) -> Any:
        from .query import QuerySession

        session = QuerySession().startTurn()
        session = session.appendMessage(createUserMessage(content=prompt))
        await tool_executor._ensure_mcp_clients_loaded()

        current_session = session
        assistant_texts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        total_tool_uses = 0

        async for event in stream_query_session(
            current_session,
            model_adapter=model_adapter,
            tool_executor=tool_executor,
            config=QueryStreamConfig(
                tools=tool_executor.get_tool_schemas(),
                tool_executor_timeout_seconds=5.0,
                system_prompt=system_prompt,
                background_request_resolver=background_request_resolver,
                options=(
                    {"max_tokens": max_tokens}
                    if max_tokens is not None
                    else None
                ),
            ),
        ):
            current_session = event.session
            output = event.output
            if isinstance(output, AssistantMessage):
                text = _assistant_message_text(output)
                if text:
                    assistant_texts.append(text)
                    record(text.rstrip() + "\n")
                usage = output.message.usage
                input_tokens = max(input_tokens, _usage_int(usage, "input_tokens"))
                output_tokens = max(output_tokens, _usage_int(usage, "output_tokens"))
                tool_names = [
                    block.name
                    for block in output.message.content
                    if isinstance(block, ToolUseBlock)
                ]
                if tool_names:
                    total_tool_uses += len(tool_names)
                    progress(
                        input_tokens,
                        output_tokens,
                        ", ".join(tool_names),
                    )
                elif text:
                    progress(input_tokens, output_tokens, text.splitlines()[0][:120])
            elif isinstance(output, UserMessage):
                content = output.message.content
                if isinstance(content, tuple):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            record(str(block.content).rstrip() + "\n")

        final_text = "\n\n".join(text for text in assistant_texts if text.strip()).strip()
        if not final_text:
            final_text = "Agent completed without textual output."
        return make_agent_result(
            agent_id=agent_id,
            agent_type=agent_type,
            text=final_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_use_count=total_tool_uses,
        )

    def _load_skill_commands(self) -> tuple[Any, ...]:
        self._prime_mcp_skill_commands_sync()
        project_skill_dir = os.path.join(self._working_directory(), ".claude_py", "skills")
        if os.path.isdir(project_skill_dir):
            add_skill_directories((project_skill_dir,))
        base_commands = [
            *self._mcp_skill_commands,
            *get_bundled_skills(),
            *get_builtin_plugin_skill_commands(self.app_state.settings),
        ]
        dynamic_commands = list(get_dynamic_skills())
        return tuple(SkillTool.get_all_commands(base_commands, dynamic_commands))

    def _enabled_tool_names(self) -> frozenset[str]:
        return frozenset(
            tool_name
            for tool_name in SUPPORTED_TOOL_NAMES
            if self._is_tool_enabled(tool_name)
        )

    def _is_tool_enabled(self, tool_name: str) -> bool:
        if not _tool_is_enabled(tool_name):
            return False
        if tool_name == "AskUserQuestion":
            return is_ask_user_question_enabled()
        return True

    def _selected_model(self) -> str:
        return resolve_main_loop_model(
            app_state=self.app_state,
            fallback=_DEFAULT_AGENT_MODEL,
        )

    def _bash_auto_background_enabled(self, command: str) -> bool:
        if _is_env_truthy(os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")):
            return False
        context = self._current_permission_context()
        try:
            if int(context.get("_agent_depth", 0) or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
        return should_auto_background_bash_command(command)

    def _make_compaction_model_adapter(self) -> ModelAdapter | None:
        return create_model_adapter_from_env(model=self._selected_model())

    def _working_directory(self) -> str:
        context = self._current_permission_context()
        raw_cwd = context.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            return _resolve_path_from_base(raw_cwd, os.getcwd())
        return os.getcwd()

    def _resolve_tool_path(self, path: str) -> str:
        return _resolve_path_from_base(path, self._working_directory())

    def _resolve_optional_tool_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        return self._resolve_tool_path(path)

    def _resolve_agent_working_directory(self, cwd: str | None) -> str:
        if cwd is None:
            return self._working_directory()
        return _resolve_path_from_base(cwd, self._working_directory())

    def _dispatch_ctx_inspect(self) -> _ToolDispatchResult:
        buckets: Counter[str] = Counter()
        for message in self.session_messages:
            buckets[_message_bucket(message)] += 1
        estimated_tokens = _estimate_messages_tokens(
            self.session_messages,
            model=self._selected_model(),
        )
        return _ToolDispatchResult(
            payload={
                "total_messages": len(self.session_messages),
                "by_type": dict(sorted(buckets.items())),
                "estimated_tokens": estimated_tokens,
                "max_context": _DEFAULT_MAX_CONTEXT_TOKENS,
            }
        )

    async def _dispatch_snip(self, *, assistant_uuid: str) -> _ToolDispatchResult:
        if not self.session_messages:
            return _ToolDispatchResult(
                payload={
                    "originalTokenCount": 0,
                    "newTokenCount": 0,
                    "snippedMessages": 0,
                }
            )

        assistant_index = next(
            (
                index
                for index, message in enumerate(self.session_messages)
                if getattr(message, "uuid", None) == assistant_uuid
            ),
            -1,
        )
        if assistant_index <= 0:
            token_count = _estimate_messages_tokens(
                self.session_messages,
                model=self._selected_model(),
            )
            return _ToolDispatchResult(
                payload={
                    "originalTokenCount": token_count,
                    "newTokenCount": token_count,
                    "snippedMessages": 0,
                }
            )

        removed_messages = tuple(
            message
            for message in self.session_messages[:assistant_index]
            if not isinstance(message, CompactBoundaryMessage)
        )
        removed_non_system = tuple(
            message for message in removed_messages if not _is_system_like_message(message)
        )
        if not removed_non_system:
            token_count = _estimate_messages_tokens(
                self.session_messages,
                model=self._selected_model(),
            )
            return _ToolDispatchResult(
                payload={
                    "originalTokenCount": token_count,
                    "newTokenCount": token_count,
                    "snippedMessages": 0,
                }
            )

        preserved_tail = tuple(self.session_messages[assistant_index:])
        preserved_message_uuids = tuple(
            message.uuid
            for message in preserved_tail
            if isinstance(
                message,
                (
                    AssistantMessage,
                    UserMessage,
                    CompactBoundaryMessage,
                ),
            )
        )
        deleted_tool_use_ids = tuple(
            block.id
            for message in removed_non_system
            if isinstance(message, AssistantMessage)
            for block in message.message.content
            if isinstance(block, ToolUseBlock)
        )
        original_token_count = _estimate_messages_tokens(
            self.session_messages,
            model=self._selected_model(),
        )
        system_prefix = tuple(
            message
            for message in self.session_messages[:assistant_index]
            if _is_system_like_message(message)
        )
        compaction_runtime_config = QueryStreamConfig(
            auto_compact_enabled=False,
            options={"max_tokens": 768},
        )
        compaction_model_adapter = self._make_compaction_model_adapter()
        compact_summary = await build_compaction_summary_message(
            removed_non_system,
            preserved_message_uuids=preserved_message_uuids,
            model_adapter=compaction_model_adapter,
            runtime_config=compaction_runtime_config,
            origin="snip",
        )
        compaction_attachments = build_compaction_attachment_messages(
            removed_non_system,
            preserved_messages=preserved_tail,
        )
        keep_tail_messages = max(
            1,
            sum(
                1
                for message in preserved_tail
                if not _is_system_like_message(message)
            ),
        )

        def _build_base_replacement_messages(
            *,
            new_token_count: int,
        ) -> tuple[Message, ...]:
            return (
                *system_prefix,
                createCompactBoundaryMessage(
                    trigger="manual_snip",
                    originalTokenCount=original_token_count,
                    newTokenCount=new_token_count,
                    deletedToolUseIds=deleted_tool_use_ids,
                    preservedMessageUuids=preserved_message_uuids,
                ),
                compact_summary,
                *preserved_tail,
                *compaction_attachments,
            )

        replacement_messages = _build_base_replacement_messages(new_token_count=0)
        pre_messages: tuple[Message, ...] = ()
        post_messages: tuple[Message, ...] = ()
        hook_payload = _build_compaction_hook_payload(
            self.session_messages,
            replacement_messages,
            trigger="manual_snip",
            keep_tail_messages=keep_tail_messages,
            max_context_tokens=_DEFAULT_MAX_CONTEXT_TOKENS,
            threshold_ratio=None,
        )
        pre_result = await self.emit_hook_event("PreCompact", hook_payload)
        pre_messages = tuple(getattr(pre_result, "messages", ()) or ())
        compaction_instructions = _extract_compaction_hook_instructions(pre_result)
        if compaction_instructions:
            compact_summary = await build_compaction_summary_message(
                removed_non_system,
                preserved_message_uuids=preserved_message_uuids,
                model_adapter=compaction_model_adapter,
                runtime_config=compaction_runtime_config,
                origin="snip",
                compaction_instructions=compaction_instructions,
            )
            replacement_messages = _build_base_replacement_messages(new_token_count=0)
            hook_payload = _build_compaction_hook_payload(
                self.session_messages,
                replacement_messages,
                trigger="manual_snip",
                keep_tail_messages=keep_tail_messages,
                max_context_tokens=_DEFAULT_MAX_CONTEXT_TOKENS,
                threshold_ratio=None,
            )
        post_result = await self.emit_hook_event("PostCompact", hook_payload)
        post_messages = tuple(getattr(post_result, "messages", ()) or ())

        replacement_messages = _inject_compaction_hook_messages(
            replacement_messages,
            pre_messages=pre_messages,
            post_messages=post_messages,
        )
        new_token_count = _estimate_messages_tokens(
            replacement_messages,
            model=self._selected_model(),
        )
        replacement_messages = _inject_compaction_hook_messages(
            _build_base_replacement_messages(
                new_token_count=new_token_count,
            ),
            pre_messages=pre_messages,
            post_messages=post_messages,
        )
        return _ToolDispatchResult(
            payload={
                "originalTokenCount": original_token_count,
                "newTokenCount": new_token_count,
                "snippedMessages": len(removed_non_system),
            },
            new_context={
                _SESSION_MESSAGE_REPLACEMENT_KEY: replacement_messages,
            },
        )

    def _validate_plan_mode(
        self,
        tool_name: str,
        raw_input: Mapping[str, Any],
    ) -> str | None:
        if self._current_permission_context().get("mode") != "plan":
            return None
        if tool_name in _PLAN_MODE_ALLOWED_TOOLS:
            return None
        if tool_name == "Bash":
            command = _string_input(raw_input, "command")
            flags = is_search_or_read_bash_command(command)
            if any(flags.values()) and not _bool_input(
                raw_input,
                "run_in_background",
                "runInBackground",
            ):
                return None
        return "Write tools are disabled while in plan mode"

    def _current_permission_context(self) -> dict[str, Any]:
        normalize_app_state_via_registry(self.app_state)
        raw = self.app_state.tool_permission_context
        return dict(raw) if isinstance(raw, Mapping) else {"mode": "default"}

    def _get_known_task(self, task_id: str) -> LocalTaskState | AgentTaskState | None:
        refreshed = self._refresh_task_state(task_id)
        if refreshed is not None:
            return refreshed
        cached = self.app_state.tasks.get(task_id)
        if isinstance(cached, (LocalTaskState, AgentTaskState)):
            return cached
        return None

    def _refresh_task_state(
        self,
        task_id: str,
    ) -> LocalTaskState | AgentTaskState | None:
        try:
            task = self.task_manager.get_task(task_id)
        except KeyError:
            task = None
        if task is not None:
            self._store_task_state(task)
            return task
        try:
            agent_task = self.agent_manager.get_agent_task(task_id)
        except KeyError:
            return None
        self._store_task_state(agent_task)
        return agent_task

    def _store_task_state(self, task: LocalTaskState | AgentTaskState) -> None:
        task_id = task.id if isinstance(task, LocalTaskState) else task.task_id
        self.app_state.tasks[task_id] = task


def _normalize_scheduler_message(message: Any) -> Message | None:
    if isinstance(
        message,
        (
            AssistantMessage,
            UserMessage,
            CompactBoundaryMessage,
            ProgressMessage,
        ),
    ) or getattr(message, "type", None) in {"attachment", "system", "tool_use_summary"}:
        return message

    message_type = getattr(message, "type", None)
    if message_type == "progress":
        data = getattr(message, "data", None)
        tool_use_id = getattr(message, "tool_use_id", None) or getattr(
            message,
            "toolUseID",
            None,
        )
        parent_tool_use_id = getattr(message, "parent_tool_use_id", None) or getattr(
            message,
            "parentToolUseID",
            None,
        )
        if (
            isinstance(data, Mapping)
            and isinstance(tool_use_id, str)
            and isinstance(parent_tool_use_id, str)
        ):
            return ProgressMessage(
                data=dict(data),
                toolUseID=tool_use_id,
                parentToolUseID=parent_tool_use_id,
            )
        return None

    if message_type != "user":
        return None

    raw_content = getattr(message, "content", None)
    if not isinstance(raw_content, tuple):
        return None
    normalized_blocks: list[ToolResultBlock] = []
    for block in raw_content:
        if getattr(block, "type", None) != "tool_result":
            return None
        tool_use_id = getattr(block, "tool_use_id", None) or getattr(
            block,
            "toolUseID",
            None,
        )
        if not isinstance(tool_use_id, str):
            return None
        normalized_blocks.append(
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=str(getattr(block, "content", "")),
                is_error=bool(getattr(block, "is_error", False)),
            )
        )
    return createUserMessage(
        content=tuple(normalized_blocks),
        toolUseResult=getattr(message, "tool_use_result", None)
        or getattr(message, "toolUseResult", None),
        toolUsePayload=getattr(message, "tool_use_payload", None)
        or getattr(message, "toolUsePayload", None),
        sourceToolAssistantUUID=getattr(message, "source_tool_assistant_uuid", None)
        or getattr(message, "sourceToolAssistantUUID", None),
    )


def _looks_like_permission_context(context: Mapping[str, Any]) -> bool:
    permission_keys = {
        "additionalWorkingDirectories",
        "additional_working_directories",
        "allowedPrompts",
        "alwaysAllowRules",
        "alwaysDenyRules",
        "alwaysAskRules",
        "always_allow_rules",
        "always_deny_rules",
        "always_ask_rules",
        "approvalState",
        "approval_state",
        "awaitAutomatedChecksBeforeDialog",
        "await_automated_checks_before_dialog",
        "cwd",
        "isBypassPermissionsModeAvailable",
        "is_bypass_permissions_mode_available",
        "mode",
        "prePlanMode",
        "pre_plan_mode",
        "shouldAvoidPermissionPrompts",
        "should_avoid_permission_prompts",
        "strippedDangerousRules",
        "stripped_dangerous_rules",
    }
    return any(key in context for key in permission_keys)


def _normalize_tool_name(name: str) -> str:
    definition = find_tool_by_name(name)
    if definition is None:
        return name
    return definition.name


def _tool_is_enabled(name: str) -> bool:
    if name == "ToolSearch":
        return _tool_search_enabled_optimistic()
    if name in _ANT_ONLY_TOOL_NAMES and os.environ.get("USER_TYPE") != "ant":
        return False
    env_var = _GATED_TOOL_ENV_VARS.get(name)
    if env_var is None:
        return True
    return _is_env_truthy(os.environ.get(env_var))


def _tool_search_enabled_optimistic() -> bool:
    raw = os.environ.get("ENABLE_TOOL_SEARCH")
    if raw is None or not raw.strip():
        return True
    return _is_env_truthy(raw)


def _is_env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _render_tool_payload(
    tool_name: str,
    payload: Mapping[str, Any] | str | None,
) -> str:
    if payload is None:
        return ""
    if tool_name == "TaskOutput":
        return _render_task_output_payload(payload)
    if tool_name == "TaskCreate":
        return _render_task_create_payload(payload)
    if tool_name == "TaskGet":
        return _render_task_get_payload(payload)
    if tool_name == "TaskList":
        return _render_task_list_payload(payload)
    if tool_name == "TaskUpdate":
        return _render_task_update_payload(payload)
    if tool_name == "TodoWrite":
        return _render_todo_write_payload(payload)
    return _render_payload(payload)


def _tool_result_size_limit(tool_name: str) -> int:
    definition = BUILTIN_TOOL_REGISTRY.get(tool_name) or find_tool_by_name(tool_name)
    if definition is None:
        return 0
    return max(int(definition.max_result_size_chars or 0), 0)


def _tool_result_supports_payload_truncation(tool_name: str) -> bool:
    return tool_name in {"ListMcpResourcesTool", "ReadMcpResourceTool"} or tool_name.startswith(
        "mcp__"
    )


def _format_tool_result_truncation(
    text: str,
    *,
    max_chars: int,
    label: str,
) -> tuple[str, Mapping[str, Any]] | None:
    if max_chars <= 0 or len(text) <= max_chars:
        return None
    marker = (
        f"[{label} truncated: total_chars={len(text)} limit_chars={max_chars}]"
    )
    metadata = {
        "label": label,
        "originalChars": len(text),
        "limitChars": max_chars,
    }
    if max_chars <= len(marker):
        return marker[:max_chars], metadata

    body_budget = max_chars - len(marker)
    if body_budget <= 1:
        return marker[:max_chars], metadata

    separator = "\n[...]\n"
    head_chars = min(max(body_budget * 2 // 3, 48), len(text))
    tail_chars = min(max(body_budget - head_chars - len(separator), 24), len(text))

    while head_chars >= 0 and tail_chars >= 0:
        parts = [marker]
        if head_chars > 0:
            parts.append(text[:head_chars].rstrip())
        if tail_chars > 0:
            parts.append("[...]")
            parts.append(text[-tail_chars:].lstrip())
        truncated = "\n".join(part for part in parts if part)
        if len(truncated) <= max_chars:
            return truncated, metadata
        if head_chars >= tail_chars and head_chars > 0:
            head_chars = max(0, head_chars - max(16, head_chars // 8))
            continue
        if tail_chars > 0:
            tail_chars = max(0, tail_chars - max(8, tail_chars // 8))
            continue
        break

    return marker[:max_chars], metadata


def _truncated_tool_payload_preview(
    raw_payload: Mapping[str, Any],
    *,
    preview: str,
    original_chars: int,
    limit_chars: int,
) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "truncated": True,
        "original_size_chars": original_chars,
        "limit_chars": limit_chars,
        "preview": preview,
    }
    for key in ("server", "uri"):
        value = raw_payload.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    return payload


def _truncate_tool_result_for_limit(
    tool_name: str,
    *,
    rendered: str,
    raw_payload: Mapping[str, Any] | None,
) -> tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    max_chars = _tool_result_size_limit(tool_name)
    truncated = _format_tool_result_truncation(
        rendered,
        max_chars=max_chars,
        label=f"{tool_name} result",
    )
    if truncated is None:
        return rendered, raw_payload, None

    truncated_text, metadata = truncated
    metadata = {
        "toolResultTruncated": {
            **dict(metadata),
            "toolName": tool_name,
        }
    }
    if raw_payload is not None and _tool_result_supports_payload_truncation(tool_name):
        preview_limit = min(
            max_chars,
            _TOOL_RESULT_TRUNCATION_PREVIEW_CHARS,
        )
        preview_result = _format_tool_result_truncation(
            rendered,
            max_chars=preview_limit,
            label=f"{tool_name} payload preview",
        )
        preview_text = truncated_text
        if preview_result is not None:
            preview_text = preview_result[0]
        raw_payload = _truncated_tool_payload_preview(
            raw_payload,
            preview=preview_text,
            original_chars=len(rendered),
            limit_chars=max_chars,
        )
    return truncated_text, raw_payload, metadata


def _tool_definition_is_deferred(definition: RegistryToolDefinition) -> bool:
    if definition.always_load:
        return False
    if definition.name == "ToolSearch":
        return False
    if definition.name.startswith("mcp__"):
        return True
    return bool(definition.should_defer)


def _pending_mcp_server_names(app_state: AppState) -> tuple[str, ...]:
    raw_mcp = getattr(app_state, "mcp", None)
    clients = raw_mcp.get("clients") if isinstance(raw_mcp, Mapping) else None
    if not isinstance(clients, Sequence) or isinstance(clients, (str, bytes, bytearray)):
        return ()
    pending: list[str] = []
    for client in clients:
        if isinstance(client, Mapping):
            client_type = client.get("type")
            client_name = client.get("name")
        else:
            client_type = getattr(client, "type", None)
            client_name = getattr(client, "name", None)
        if client_type == "pending" and isinstance(client_name, str) and client_name.strip():
            pending.append(client_name)
    return tuple(pending)


def _parse_tool_search_select(query: str) -> tuple[str, ...] | None:
    match = re.match(r"^select:(.+)$", query.strip(), re.IGNORECASE)
    if match is None:
        return None
    return tuple(part.strip() for part in match.group(1).split(",") if part.strip())


def _find_tool_definition_in_pool(
    pool: Sequence[RegistryToolDefinition],
    tool_name: str,
) -> RegistryToolDefinition | None:
    normalized = tool_name.strip().lower()
    if not normalized:
        return None
    for definition in pool:
        if definition.name.lower() == normalized:
            return definition
        if any(alias.lower() == normalized for alias in definition.aliases):
            return definition
    return None


def _parse_tool_search_name(name: str) -> tuple[list[str], str, bool]:
    if name.startswith("mcp__"):
        without_prefix = name.removeprefix("mcp__").lower()
        parts = [
            part
            for segment in without_prefix.split("__")
            for part in segment.split("_")
            if part
        ]
        return parts, without_prefix.replace("__", " ").replace("_", " "), True
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ")
    parts = [part for part in spaced.lower().split() if part]
    return parts, " ".join(parts), False


def _tool_matches_required_terms(
    definition: RegistryToolDefinition,
    required_terms: Sequence[str],
    term_patterns: Mapping[str, re.Pattern[str]],
) -> bool:
    if not required_terms:
        return True
    name_parts, full_name, _ = _parse_tool_search_name(definition.name)
    description = definition.description.lower()
    search_hint = definition.search_hint.lower()
    for term in required_terms:
        pattern = term_patterns[term]
        if term in name_parts:
            continue
        if any(term in part for part in name_parts):
            continue
        if term in full_name:
            continue
        if pattern.search(description):
            continue
        if search_hint and pattern.search(search_hint):
            continue
        return False
    return True


def _tool_search_score(
    definition: RegistryToolDefinition,
    terms: Sequence[str],
    term_patterns: Mapping[str, re.Pattern[str]],
) -> int:
    name_parts, full_name, is_mcp = _parse_tool_search_name(definition.name)
    description = definition.description.lower()
    search_hint = definition.search_hint.lower()
    score = 0
    for term in terms:
        pattern = term_patterns[term]
        if term in name_parts:
            score += 12 if is_mcp else 10
        elif any(term in part for part in name_parts):
            score += 6 if is_mcp else 5
        if term in full_name and score == 0:
            score += 3
        if search_hint and pattern.search(search_hint):
            score += 4
        if pattern.search(description):
            score += 2
    return score


def _tool_search_keyword_matches(
    query: str,
    *,
    deferred_tools: Sequence[RegistryToolDefinition],
    available_tools: Sequence[RegistryToolDefinition],
    max_results: int,
) -> list[str]:
    normalized_query = query.strip().lower()
    if not normalized_query:
        return []
    exact_match = _find_tool_definition_in_pool(
        deferred_tools,
        normalized_query,
    ) or _find_tool_definition_in_pool(
        available_tools,
        normalized_query,
    )
    if exact_match is not None:
        return [exact_match.name]
    if normalized_query.startswith("mcp__") and len(normalized_query) > len("mcp__"):
        prefix_matches = [
            definition.name
            for definition in deferred_tools
            if definition.name.lower().startswith(normalized_query)
        ]
        if prefix_matches:
            return prefix_matches[:max_results]
    raw_terms = [term for term in normalized_query.split() if term]
    if not raw_terms:
        return []
    required_terms = [
        term[1:]
        for term in raw_terms
        if term.startswith("+") and len(term) > 1
    ]
    optional_terms = [term for term in raw_terms if not term.startswith("+")]
    scoring_terms = required_terms + optional_terms if required_terms else raw_terms
    term_patterns = {
        term: re.compile(rf"\b{re.escape(term)}\b")
        for term in scoring_terms
    }
    candidates = [
        definition
        for definition in deferred_tools
        if _tool_matches_required_terms(definition, required_terms, term_patterns)
    ]
    scored = [
        (definition.name, _tool_search_score(definition, scoring_terms, term_patterns))
        for definition in candidates
    ]
    return [
        name
        for name, score in sorted(scored, key=lambda item: (-item[1], item[0]))
        if score > 0
    ][:max_results]


def _tool_search_dispatch_result(
    *,
    matches: Sequence[str],
    query: str,
    total_deferred_tools: int,
    pending_servers: Sequence[str] = (),
) -> _ToolDispatchResult:
    payload: dict[str, Any] = {
        "matches": list(matches),
        "query": query,
        "total_deferred_tools": total_deferred_tools,
    }
    if pending_servers:
        payload["pending_mcp_servers"] = list(pending_servers)
    if matches:
        return _ToolDispatchResult(
            payload=payload,
            block_content=[
                {
                    "type": "tool_reference",
                    "tool_name": name,
                }
                for name in matches
            ],
        )
    message = "No matching deferred tools found"
    if pending_servers:
        message += (
            ". Some MCP servers are still connecting: "
            + ", ".join(pending_servers)
            + ". Their tools will become available shortly; try searching again."
        )
    return _ToolDispatchResult(
        payload=payload,
        block_content=message,
    )


def _read_result_payload(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "result_type": getattr(result, "result_type", "text"),
        "file_path": getattr(result, "file_path", ""),
    }
    session_file_type = getattr(result, "session_file_type", None)
    if isinstance(session_file_type, str) and session_file_type:
        payload["session_file_type"] = session_file_type
    result_type = payload["result_type"]
    if result_type == "text":
        payload.update(
            {
                "content": getattr(result, "content", ""),
                "total_lines": getattr(result, "total_lines", 0),
                "offset": getattr(result, "offset", 1),
                "limit": getattr(result, "limit", None),
                "total_bytes": getattr(result, "total_bytes", 0),
                "read_bytes": getattr(result, "read_bytes", 0),
            }
        )
        return payload
    if result_type == "notebook":
        cells = getattr(result, "cells", None) or []
        payload.update(
            {
                "cells": cells,
                "cell_count": len(cells),
                "total_bytes": getattr(result, "total_bytes", 0),
                "read_bytes": getattr(result, "read_bytes", 0),
            }
        )
        return payload
    if result_type == "image":
        payload.update(
            {
                "base64_data": getattr(result, "base64_data", None),
                "media_type": getattr(result, "media_type", None),
                "dimensions": getattr(result, "dimensions", None),
                "original_size": getattr(result, "original_size", 0),
            }
        )
        return payload
    if result_type == "pdf":
        payload.update(
            {
                "base64_data": getattr(result, "base64_data", None),
                "media_type": getattr(result, "media_type", None),
                "original_size": getattr(result, "original_size", 0),
                "page_count": getattr(result, "page_count", None),
            }
        )
        return payload
    if result_type == "parts":
        payload.update(
            {
                "original_size": getattr(result, "original_size", 0),
                "page_count": getattr(result, "page_count", None),
                "pages": getattr(result, "pages", None),
                "output_dir": getattr(result, "output_dir", None),
                "count": getattr(result, "count", None),
            }
        )
        return payload
    payload["content"] = getattr(result, "content", "")
    return payload


def _mcp_content_to_tool_result_blocks(content: Any) -> Any:
    """Convert an MCP tool-call ``content`` payload into Anthropic tool_result
    content blocks. Text blocks pass through; MCP image blocks (``data`` +
    ``mimeType``) become base64 image source blocks so screenshots render."""

    if content is None:
        return "(empty result)"
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(
        content, (bytes, bytearray)
    ):
        return json.dumps(content, sort_keys=True)

    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, Mapping):
            blocks.append({"type": "text", "text": str(item)})
            continue
        item_type = item.get("type")
        if item_type == "text":
            blocks.append({"type": "text", "text": str(item.get("text", ""))})
        elif item_type == "image" and item.get("data"):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": str(item.get("mimeType") or "image/png"),
                        "data": str(item.get("data")),
                    },
                }
            )
        else:
            blocks.append({"type": "text", "text": json.dumps(dict(item), sort_keys=True)})
    if not blocks:
        return "(empty result)"
    return blocks


def _tool_result_update(
    *,
    tool_use_id: str,
    rendered: str,
    assistant_uuid: str,
    block_content: Any = None,
    raw_payload: Mapping[str, Any] | None = None,
    is_error: bool = False,
    new_context: Optional[Mapping[str, Any]] = None,
    mcp_meta: Optional[Mapping[str, Any]] = None,
) -> ToolExecutionUpdate:
    content = rendered if block_content is None else block_content
    if is_error:
        content = f"<tool_use_error>{rendered}</tool_use_error>"
    return ToolExecutionUpdate(
        message=createUserMessage(
            content=(
                ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=content,
                    is_error=is_error,
                ),
            ),
            toolUseResult=rendered,
            toolUsePayload=raw_payload,
            mcpMeta=mcp_meta,
            sourceToolAssistantUUID=assistant_uuid,
        ),
        newContext=new_context,
    )


def _render_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _is_system_like_message(message: Message) -> bool:
    return isinstance(message, CompactBoundaryMessage) or getattr(message, "type", "") == "system"


def _message_bucket(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        return "assistant"
    if isinstance(message, UserMessage):
        content = message.message.content
        if (
            isinstance(content, tuple)
            and content
            and all(isinstance(block, ToolResultBlock) for block in content)
        ):
            return "tool_result"
        return "user"
    return getattr(message, "subtype", getattr(message, "type", "unknown"))


def _estimate_messages_tokens(
    messages: Sequence[Message],
    *,
    model: str | None = None,
) -> int:
    return sum(_estimate_message_tokens(message, model=model) for message in messages)


def _estimate_message_tokens(
    message: Message,
    *,
    model: str | None = None,
) -> int:
    rendered = _render_message_for_estimation(message)
    if not rendered:
        return 0
    return _count_text_tokens(rendered, model=model)


def _count_text_tokens(text: str, *, model: str | None = None) -> int:
    return count_text_tokens(text, model=model)


def _render_message_for_estimation(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        parts: list[str] = []
        for block in message.message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                parts.append(block.name)
                if block.input:
                    parts.append(json.dumps(block.input, ensure_ascii=False, sort_keys=True))
        return "\n".join(part for part in parts if part)
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, str):
            return content
        parts = []
        for block in content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(str(block.content))
        return "\n".join(part for part in parts if part)
    if isinstance(message, CompactBoundaryMessage):
        return (
            f"{message.trigger} {message.originalTokenCount} "
            f"{message.newTokenCount}"
        )
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    summary = getattr(message, "summary", None)
    if isinstance(summary, str):
        return summary
    return ""


def _build_snip_summary(messages: Sequence[Message]) -> str:
    buckets: Counter[str] = Counter(_message_bucket(message) for message in messages)
    fragments = [f"{count} {bucket}" for bucket, count in sorted(buckets.items())]
    summary = ", ".join(fragments) if fragments else "no prior messages"
    return (
        "Earlier conversation history was compacted. "
        f"Removed {len(messages)} messages ({summary})."
    )


def _render_todo_write_payload(payload: Mapping[str, Any] | str) -> str:
    del payload
    return (
        "Todos have been modified successfully. Ensure that you continue "
        "to use the todo list to track your progress. Please proceed with "
        "the current tasks if applicable"
    )


def _render_task_create_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    task = payload.get("task")
    if not isinstance(task, Mapping):
        return "Task created"
    return f"Task #{task['id']} created successfully: {task['subject']}"


def _render_task_get_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    task = payload.get("task")
    if not isinstance(task, Mapping):
        return "Task not found"
    parts = [f"Task #{task['id']}: {task['subject']}", f"Status: {task['status']}"]
    description = task.get("description")
    if isinstance(description, str) and description:
        parts.append(f"Description: {description}")
    blocked_by = task.get("blockedBy")
    if isinstance(blocked_by, list) and blocked_by:
        parts.append("Blocked by: " + ", ".join(f"#{value}" for value in blocked_by))
    blocks = task.get("blocks")
    if isinstance(blocks, list) and blocks:
        parts.append("Blocks: " + ", ".join(f"#{value}" for value in blocks))
    return "\n".join(parts)


def _render_task_list_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "No tasks found"
    lines: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        line = f"#{task['id']} [{task['status']}] {task['subject']}"
        owner = task.get("owner")
        if isinstance(owner, str) and owner:
            line += f" ({owner})"
        blocked_by = task.get("blockedBy")
        if isinstance(blocked_by, list) and blocked_by:
            line += (
                " [blocked by " + ", ".join(f"#{value}" for value in blocked_by) + "]"
            )
        lines.append(line)
    return "\n".join(lines) if lines else "No tasks found"


def _render_task_update_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    if payload.get("success") is not True:
        error = payload.get("error")
        return str(error) if isinstance(error, str) and error else "Task not found"
    task_id = payload.get("taskId")
    updated_fields = payload.get("updatedFields")
    if isinstance(updated_fields, list) and updated_fields:
        if updated_fields == ["deleted"]:
            return f"Updated task #{task_id} deleted"
        return f"Updated task #{task_id} " + ", ".join(
            str(value) for value in updated_fields
        )
    return f"Updated task #{task_id}"


def _render_task_output_payload(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    parts = [
        f"<retrieval_status>{payload['retrieval_status']}</retrieval_status>",
    ]
    task = payload.get("task")
    if isinstance(task, Mapping):
        parts.append(f"<task_id>{task['task_id']}</task_id>")
        parts.append(f"<task_type>{task['task_type']}</task_type>")
        parts.append(f"<status>{task['status']}</status>")
        output_file = task.get("output_file")
        if isinstance(output_file, str) and output_file:
            parts.append(f"<output_file>{output_file}</output_file>")
        persisted_size = task.get("persistedOutputSize")
        if isinstance(persisted_size, int):
            parts.append(f"<persisted_output_size>{persisted_size}</persisted_output_size>")
        is_backgrounded = task.get("is_backgrounded")
        if isinstance(is_backgrounded, bool):
            parts.append(
                f"<is_backgrounded>{str(is_backgrounded).lower()}</is_backgrounded>"
            )
        exit_code = task.get("exit_code")
        if exit_code is not None:
            parts.append(f"<exit_code>{exit_code}</exit_code>")
        output = task.get("output")
        if isinstance(output, str) and output.strip():
            parts.append(f"<output>\n{output.rstrip()}\n</output>")
        error = task.get("error")
        if isinstance(error, str) and error:
            parts.append(f"<error>{error}</error>")
    return "\n\n".join(parts)


def _string_input(raw_input: Mapping[str, Any], *keys: str) -> str:
    value = _lookup_input(raw_input, *keys)
    if not isinstance(value, str) or not value.strip():
        joined = "/".join(keys)
        raise ValueError(f"Expected non-empty string for {joined}")
    return value


def _optional_string_input(
    raw_input: Mapping[str, Any],
    *keys: str,
    default: Optional[str] = None,
) -> Optional[str]:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return default
    if not isinstance(value, str):
        joined = "/".join(keys)
        raise ValueError(f"Expected string for {joined}")
    return value


def _optional_object_input(
    raw_input: Mapping[str, Any],
    *keys: str,
) -> dict[str, Any] | None:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        joined = "/".join(keys)
        raise ValueError(f"Expected object for {joined}")
    return dict(value)


def _optional_string_list_input(
    raw_input: Mapping[str, Any],
    *keys: str,
) -> list[str] | None:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        joined = "/".join(keys)
        raise ValueError(f"Expected array of non-empty strings for {joined}")
    return [str(item) for item in value]


def _positive_int_input(
    raw_input: Mapping[str, Any],
    *keys: str,
    default: int,
) -> int:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return default
    if not isinstance(value, int) or value < 1:
        joined = "/".join(keys)
        raise ValueError(f"Expected positive integer for {joined}")
    return value


def _optional_positive_int_input(
    raw_input: Mapping[str, Any],
    *keys: str,
) -> int | None:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return None
    if not isinstance(value, int) or value < 1:
        joined = "/".join(keys)
        raise ValueError(f"Expected positive integer for {joined}")
    return value


def _optional_non_negative_int_input(
    raw_input: Mapping[str, Any],
    *keys: str,
) -> int | None:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        joined = "/".join(keys)
        raise ValueError(f"Expected non-negative integer for {joined}")
    return value


def _bool_input(
    raw_input: Mapping[str, Any],
    *keys: str,
    default: bool = False,
) -> bool:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return default
    if not isinstance(value, bool):
        joined = "/".join(keys)
        raise ValueError(f"Expected boolean for {joined}")
    return value


def _optional_bool_input(
    raw_input: Mapping[str, Any],
    *keys: str,
) -> bool | None:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return None
    if not isinstance(value, bool):
        joined = "/".join(keys)
        raise ValueError(f"Expected boolean for {joined}")
    return value


def _non_negative_int_input(
    raw_input: Mapping[str, Any],
    *keys: str,
    default: int,
) -> int:
    value = _lookup_input(raw_input, *keys)
    if value is None:
        return default
    if not isinstance(value, int) or value < 0:
        joined = "/".join(keys)
        raise ValueError(f"Expected non-negative integer for {joined}")
    return value


def _lookup_input(raw_input: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw_input:
            return raw_input[key]
    return None


def _todo_list_input(raw_input: Mapping[str, Any]) -> list[dict[str, str]]:
    value = _lookup_input(raw_input, "todos")
    if not isinstance(value, list):
        raise ValueError("Expected array for todos")
    todos: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Expected each todo item to be an object")
        content = item.get("content")
        active_form = item.get("activeForm")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Expected non-empty string for todos[].content")
        if not isinstance(active_form, str) or not active_form.strip():
            raise ValueError("Expected non-empty string for todos[].activeForm")
        if status not in {"pending", "in_progress", "completed"}:
            raise ValueError("Invalid todos[].status value")
        todos.append(
            {
                "content": content,
                "activeForm": active_form,
                "status": status,
            }
        )
    return todos


def _task_output_payload(task: LocalTaskState | AgentTaskState) -> dict[str, Any]:
    if isinstance(task, AgentTaskState):
        content_blocks = []
        if task.result is not None:
            content_blocks = [
                text
                for text in task.result.content
                if isinstance(text, str) and text.strip()
            ]
        output = task.logs
        if content_blocks:
            output = (output.rstrip() + "\n\n" if output.strip() else "") + "\n\n".join(
                content_blocks
            )
        return {
            "task_id": task.task_id,
            "task_type": "local_agent",
            "status": task.status,
            "description": task.description,
            "output": output,
            "exit_code": task.exit_code,
            "error": task.error,
            "is_backgrounded": task.is_backgrounded,
            "output_file": task.output_file,
            "persistedOutputPath": task.output_file,
            "persistedOutputSize": _file_size(task.output_file),
        }
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "description": task.description,
        "output": task.logs,
        "exit_code": task.exit_code,
        "error": task.error,
        "is_backgrounded": task.is_backgrounded,
        "backgroundTaskId": task.id if task.task_type == "local_bash" else None,
        "backgroundedByUser": (
            task.backgrounded_by_user if task.task_type == "local_bash" else None
        ),
        "assistantAutoBackgrounded": (
            task.assistant_auto_backgrounded
            if task.task_type == "local_bash"
            else None
        ),
        "output_file": task.output_file,
        "persistedOutputPath": task.output_file,
        "persistedOutputSize": _file_size(task.output_file),
        "pid": task.pid if task.task_type == "local_bash" else None,
        "created_at_ms": task.created_at_ms,
        "started_at_ms": task.started_at_ms,
        "ended_at_ms": task.ended_at_ms,
    }


def _allocate_background_output_file(*, prefix: str = "task") -> str:
    directory = os.path.join(tempfile.gettempdir(), "claude-code-python-port-tasks")
    os.makedirs(directory, exist_ok=True)
    filename = f"{prefix}-{int(time.time() * 1000)}.log"
    return os.path.join(directory, filename)


def _foreground_bash_payload(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "result_type": result.result_type,
        "file_path": result.file_path,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "interrupted": result.interrupted,
        "timed_out": result.timed_out,
        "semantic_success": result.semantic_success,
        "semantic_status": result.semantic_status,
        "semantic_message": result.semantic_message,
        "git_operation": result.git_operation,
        "git_branch": result.git_branch,
        "git_commit_shas": list(result.git_commit_shas),
        "git_pr_urls": list(result.git_pr_urls),
        "git_index_lock_error": result.git_index_lock_error,
        "code_indexing_tool": result.code_indexing_tool,
        "sleep_pattern_detected": result.sleep_pattern_detected,
        "backgrounding_suggestion": result.backgrounding_suggestion,
        "base64_data": result.base64_data,
        "media_type": result.media_type,
        "dimensions": result.dimensions,
        "original_size": result.original_size,
    }
    if result.claude_code_hints:
        payload["claude_code_hints"] = list(result.claude_code_hints)
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != [] and value != ""
    }


def _background_bash_payload(task: LocalTaskState) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "description": task.description,
        "output_file": task.output_file,
        "backgroundTaskId": task.id,
        "backgroundedByUser": task.backgrounded_by_user,
        "assistantAutoBackgrounded": task.assistant_auto_backgrounded,
        "persistedOutputPath": task.output_file,
        "persistedOutputSize": _file_size(task.output_file),
        "pid": task.pid,
        "created_at_ms": task.created_at_ms,
        "started_at_ms": task.started_at_ms,
    }


def _file_size(path: str | None) -> int | None:
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _resolve_path_from_base(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_dir, expanded))


def _require_todo_v2_enabled() -> None:
    if not is_todo_v2_enabled():
        raise ValueError("Task V2 tools are disabled")


def _assistant_message_text(message: AssistantMessage) -> str:
    parts: list[str] = []
    for block in message.message.content:
        if isinstance(block, TextBlock) and block.text.strip():
            parts.append(block.text.strip())
    return "\n".join(parts).strip()


def _filter_agent_hook_tool_schemas(
    schemas: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    blocked_names = {"Agent", "AskUserQuestion"}
    filtered: list[Mapping[str, Any]] = []
    for schema in schemas:
        name = schema.get("name")
        if isinstance(name, str) and name in blocked_names:
            continue
        filtered.append(schema)
    return tuple(filtered)


def _parse_hook_condition_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("ok"), bool):
        return None
    normalized: dict[str, Any] = {"ok": payload["ok"]}
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        normalized["reason"] = reason.strip()
    return normalized


def _external_signal_is_set(signal: Any) -> bool:
    if signal is None:
        return False
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        try:
            return bool(is_set())
        except TypeError:
            pass
    aborted = getattr(signal, "aborted", None)
    return bool(aborted)


def _external_signal_reason(signal: Any) -> str:
    if signal is None:
        return "interrupt"
    for key in ("reason", "abort_reason", "abortReason"):
        value = getattr(signal, key, None)
        if isinstance(value, str) and value:
            return value
    return "interrupt"


def _optional_abort_reason(signal: Any) -> str | None:
    if not _external_signal_is_set(signal):
        return None
    return _external_signal_reason(signal)


async def _wait_for_external_signal(signal: Any) -> None:
    if signal is None:
        return
    if _external_signal_is_set(signal):
        return
    wait = getattr(signal, "wait", None)
    if callable(wait):
        result = wait()
        if inspect.isawaitable(result):
            await result
        return
    while not _external_signal_is_set(signal):
        await asyncio.sleep(0.01)


async def _watch_external_signal(signal: Any, controller: Any) -> None:
    await _wait_for_external_signal(signal)
    controller.abort(_external_signal_reason(signal))


def _append_limited_output(current: str, text: str) -> str:
    combined = f"{current}{text}"
    if len(combined) <= BASH_MAX_OUTPUT_LENGTH:
        return combined
    marker = "\n...[truncated]...\n"
    keep = max(BASH_MAX_OUTPUT_LENGTH - len(marker), 0)
    if keep == 0:
        return combined[-BASH_MAX_OUTPUT_LENGTH:]
    return f"{marker}{combined[-keep:]}"


def _tail_output(text: str, *, max_chars: int = 4_000) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n...[truncated]...\n"
    keep = max(max_chars - len(marker), 0)
    if keep == 0:
        return text[-max_chars:]
    return f"{marker}{text[-keep:]}"


def _terminate_process(process: asyncio.subprocess.Process, *, force: bool = False) -> None:
    if process.returncode is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGINT
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        if force:
            process.kill()
        else:
            process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return


def _render_parallel_bash_cancelled_message(command: str) -> str:
    summary = command.strip()
    if summary:
        if len(summary) > 40:
            summary = summary[:40] + "..."
        return f"Cancelled: parallel tool call Bash({summary}) errored"
    return "Cancelled: parallel tool call Bash errored"


def _usage_int(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


def _slugify_agent_name(value: str) -> str:
    cleaned = "".join(
        char.lower() if char.isalnum() else "-"
        for char in value.strip()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:48]


def _normalize_agent_type(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "local-subagent"
    normalized = _slugify_agent_name(value) or "local-subagent"
    mapping = {
        "brief": "brief",
        "claude-code": "claude-code-guide",
        "claude-code-guide": "claude-code-guide",
        "claudecodeguide": "claude-code-guide",
        "dream": "dream",
        "explore": "explore",
        "explorer": "explore",
        "general": "general-purpose",
        "general-purpose": "general-purpose",
        "generalpurpose": "general-purpose",
        "guide": "claude-code-guide",
        "local": "local-subagent",
        "local-subagent": "local-subagent",
        "plan": "planner",
        "planner": "planner",
        "skill-agent": "skill-agent",
        "subagent": "local-subagent",
        "verification": "verification",
        "verify": "verification",
    }
    return mapping.get(normalized, normalized)


def _parse_mcp_server_configs(
    raw_servers: object,
    *,
    scope: ConfigScope,
) -> dict[str, ScopedMcpServerConfig]:
    if not isinstance(raw_servers, Mapping):
        return {}
    result = parse_mcp_config_via_registry(
        {"mcpServers": dict(raw_servers)},
        scope=scope,
    )
    if result.value is None:
        return {}
    fatal_errors = {
        issue.path for issue in result.issues if issue.severity == "fatal"
    }
    return {
        name: ScopedMcpServerConfig(config=config, scope=scope)
        for name, config in result.value.mcp_servers.items()
        if f"mcpServers.{name}" not in fatal_errors
    }


def _plugin_mcp_servers(
    plugin: object,
    *,
    scope: ConfigScope,
) -> dict[str, ScopedMcpServerConfig]:
    raw_servers: object | None = None
    if isinstance(plugin, Mapping):
        raw_servers = plugin.get("mcp_servers")
        if raw_servers is None:
            raw_servers = plugin.get("mcpServers")
    else:
        raw_servers = getattr(plugin, "mcp_servers", None)
    return _parse_mcp_server_configs(raw_servers, scope=scope)


def _serialize_additional_working_directories(
    directories: Mapping[str, AdditionalWorkingDirectory],
) -> dict[str, dict[str, str]]:
    serialized: dict[str, dict[str, str]] = {}
    for key, value in directories.items():
        if not isinstance(key, str) or not isinstance(value, AdditionalWorkingDirectory):
            continue
        serialized[key] = {
            "path": value.path,
            "source": value.source,
        }
    return serialized


def _normalize_additional_directory_mapping(
    raw_directories: object,
) -> dict[str, dict[str, str]]:
    if not isinstance(raw_directories, Mapping):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for key, value in raw_directories.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        path = value.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        source = value.get("source")
        normalized[key] = {
            "path": os.path.abspath(path),
            "source": source if isinstance(source, str) and source.strip() else "session",
        }
    return normalized


def _run_git_command(
    cwd: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", *args),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(stderr) from exc


def _git_repo_root(cwd: str) -> str:
    return _run_git_command(cwd, "rev-parse", "--show-toplevel").stdout.strip()


def _unique_worktree_path(parent: str, name: str) -> str:
    candidate = os.path.join(parent, name)
    if not os.path.exists(candidate):
        return candidate
    for index in range(2, 1000):
        suffixed = os.path.join(parent, f"{name}-{index}")
        if not os.path.exists(suffixed):
            return suffixed
    raise RuntimeError("Could not allocate a unique worktree directory")


def _create_managed_worktree(cwd: str, name: str | None) -> tuple[str, str]:
    repo_root = _git_repo_root(cwd)
    worktree_root = os.path.join(repo_root, ".claude_py", "worktrees")
    os.makedirs(worktree_root, exist_ok=True)
    slug = _slugify_agent_name(name or os.path.basename(cwd) or "worktree") or "worktree"
    worktree_path = _unique_worktree_path(worktree_root, slug)
    try:
        _run_git_command(repo_root, "worktree", "add", "--detach", worktree_path, "HEAD")
    except Exception:
        if os.path.isdir(worktree_path):
            shutil.rmtree(worktree_path, ignore_errors=True)
        raise
    return repo_root, worktree_path


def _reset_worktree_changes(worktree_path: str) -> None:
    _run_git_command(worktree_path, "reset", "--hard", "HEAD")
    _run_git_command(worktree_path, "clean", "-fd")


def _normalize_exit_worktree_action(
    action: str,
    *,
    discard_changes: bool,
) -> tuple[str, bool]:
    normalized = _slugify_agent_name(action)
    if normalized in {"cleanup", "clean", "delete", "discard", "remove"}:
        return "cleanup", discard_changes or normalized == "discard"
    if normalized in {"keep", "leave", "restore", "return"}:
        return "return", discard_changes
    raise ValueError(f"Unsupported ExitWorktree action: {action}")


def _exit_managed_worktree(
    worktree_path: str,
    *,
    action: str,
    discard_changes: bool,
) -> None:
    if discard_changes:
        _reset_worktree_changes(worktree_path)
    if action != "cleanup":
        return
    repo_root = _git_repo_root(worktree_path)
    args = ["worktree", "remove"]
    if discard_changes:
        args.append("--force")
    args.append(worktree_path)
    _run_git_command(repo_root, *args)
    if os.path.isdir(worktree_path):
        shutil.rmtree(worktree_path, ignore_errors=True)


__all__ = [
    "LocalToolExecutor",
    "SUPPORTED_TOOL_NAMES",
]
