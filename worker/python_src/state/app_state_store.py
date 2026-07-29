"""Application state defaults and persistence wiring.

Python port of src/state/AppStateStore.ts (Task 28).

Defines ``AppState`` as a dataclass mirroring the TS ``AppState`` type and
``get_default_app_state()`` as the factory that produces initial values
matching the TS ``getDefaultAppState()`` function.

The full TS AppState has ~80 fields covering TUI, MCP, bridge, plugins,
teammates, etc.  The Python port keeps the same field set but marks TUI-only
fields as optional so non-TUI consumers can use a lightweight subset.

``create_app_state_store()`` wires up the ``onChange`` side-effect handler
that persists selected fields to ``~/.claude_py/state.json``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from ..plugins.builtin_plugins import LoadedPlugin, PluginError
from ..types.permissions import PermissionMode
from ..utils.model_selection import default_thinking_enabled, is_valid_effort_level
from ..utils.plugin_registry import (
    build_managed_plugins_runtime_state,
    get_managed_plugins_runtime_signature,
)
from ..utils.settings.schema_validation import (
    SchemaValidationIssue,
    validate_settings_payload,
)
from .store import Store


def _default_mcp_state() -> Dict[str, Any]:
    return {
        "clients": [],
        "tools": [],
        "commands": [],
        "resources": {},
        "pluginReconnectKey": 0,
    }


def _default_plugins_state() -> Dict[str, Any]:
    return {
        "enabled": [],
        "disabled": [],
        "commands": [],
        "errors": [],
        "installationStatus": {"marketplaces": [], "plugins": []},
        "needsRefresh": False,
    }


def _default_agent_definitions() -> Dict[str, Any]:
    return {"activeAgents": [], "allAgents": []}


def _default_file_history() -> Dict[str, Any]:
    return {
        "snapshots": [],
        "trackedFiles": [],
        "snapshotSequence": 0,
    }


def _default_notifications() -> Dict[str, Any]:
    return {"current": None, "queue": []}


def _default_elicitation() -> Dict[str, Any]:
    return {"queue": []}


def _default_worker_sandbox_permissions() -> Dict[str, Any]:
    return {"queue": [], "selectedIndex": 0}


def _default_prompt_suggestion() -> Dict[str, Any]:
    return {
        "text": None,
        "promptId": None,
        "shownAt": 0,
        "acceptedAt": 0,
        "generationRequestId": None,
    }


def _default_speculation() -> Dict[str, Any]:
    return {"status": "idle"}


def _default_skill_improvement() -> Dict[str, Any]:
    return {"suggestion": None}


@dataclass(frozen=True)
class AppStateValidationIssue:
    path: str
    message: str


@dataclass
class AppState:
    """Mirrors the TS ``AppState`` type from AppStateStore.ts.

    Field-for-field correspondence with the TypeScript source.
    Mutable so that ``message_delta``-style updates can patch in place.
    """

    settings: Dict[str, Any] = field(default_factory=dict)
    verbose: bool = False
    main_loop_model: Optional[str] = None
    main_loop_model_for_session: Optional[str] = None
    status_line_text: Optional[str] = None
    expanded_view: str = "none"
    is_brief_only: bool = False
    selected_ip_agent_index: int = -1
    coordinator_task_index: int = -1
    view_selection_mode: str = "none"
    footer_selection: Optional[str] = None
    tool_permission_context: Dict[str, Any] = field(default_factory=dict)
    spinner_tip: Optional[str] = None
    agent: Optional[str] = None
    kairos_enabled: bool = False
    remote_session_url: Optional[str] = None
    remote_connection_status: str = "connecting"
    remote_background_task_count: int = 0
    repl_bridge_enabled: bool = False
    repl_bridge_explicit: bool = False
    repl_bridge_outbound_only: bool = False
    repl_bridge_connected: bool = False
    repl_bridge_session_active: bool = False
    repl_bridge_reconnecting: bool = False
    repl_bridge_connect_url: Optional[str] = None
    repl_bridge_session_url: Optional[str] = None
    repl_bridge_environment_id: Optional[str] = None
    repl_bridge_session_id: Optional[str] = None
    repl_bridge_error: Optional[str] = None
    repl_bridge_initial_name: Optional[str] = None
    show_remote_callout: bool = False
    tasks: Dict[str, Any] = field(default_factory=dict)
    agent_name_registry: Dict[str, str] = field(default_factory=dict)
    foregrounded_task_id: Optional[str] = None
    viewing_agent_task_id: Optional[str] = None
    companion_reaction: Optional[str] = None
    companion_pet_at: Optional[int] = None
    mcp: Dict[str, Any] = field(default_factory=_default_mcp_state)
    plugins: Dict[str, Any] = field(default_factory=_default_plugins_state)
    agent_definitions: Dict[str, Any] = field(default_factory=_default_agent_definitions)
    file_history: Dict[str, Any] = field(default_factory=_default_file_history)
    attribution: Dict[str, Any] = field(default_factory=dict)
    todos: Dict[str, Any] = field(default_factory=dict)
    remote_agent_task_suggestions: List[Dict[str, str]] = field(default_factory=list)
    notifications: Dict[str, Any] = field(default_factory=_default_notifications)
    elicitation: Dict[str, Any] = field(default_factory=_default_elicitation)
    thinking_enabled: Optional[bool] = None
    prompt_suggestion_enabled: bool = False
    session_hooks: Dict[str, Any] = field(default_factory=dict)
    inbox: Dict[str, Any] = field(default_factory=lambda: {"messages": []})
    worker_sandbox_permissions: Dict[str, Any] = field(
        default_factory=_default_worker_sandbox_permissions
    )
    pending_worker_request: Optional[Dict[str, str]] = None
    pending_sandbox_request: Optional[Dict[str, str]] = None
    prompt_suggestion: Dict[str, Any] = field(default_factory=_default_prompt_suggestion)
    speculation: Dict[str, Any] = field(default_factory=_default_speculation)
    speculation_session_time_saved_ms: int = 0
    skill_improvement: Dict[str, Any] = field(default_factory=_default_skill_improvement)
    auth_version: int = 0
    initial_message: Optional[Dict[str, Any]] = None
    effort_value: Optional[str] = None
    active_overlays: Set[str] = field(default_factory=set)
    fast_mode: bool = False

    def __post_init__(self) -> None:
        normalize_app_state_in_place(self)


def get_default_app_state(
    *,
    settings: Dict[str, Any] | None = None,
    initial_mode: str = "default",
    settings_runtime_context: Mapping[str, Any] | None = None,
) -> AppState:
    """Create the default ``AppState`` matching TS ``getDefaultAppState()``.

    Parameters
    ----------
    settings:
        Pre-loaded merged settings (from ``get_initial_settings()``).
        If ``None``, an empty dict is used.
    initial_mode:
        The permission mode for ``tool_permission_context.mode``.
    """
    resolved_settings = settings if settings is not None else {}
    effort_value = resolved_settings.get("effortValue")
    app_state = AppState(
        settings=resolved_settings,
        tool_permission_context={"mode": initial_mode},
        plugins=build_managed_plugins_runtime_state(settings=resolved_settings),
        thinking_enabled=default_thinking_enabled(settings=resolved_settings),
        effort_value=effort_value if is_valid_effort_level(effort_value) else None,
        prompt_suggestion_enabled=False,
    )
    setattr(
        app_state,
        "_managed_plugins_runtime_signature",
        get_managed_plugins_runtime_signature(settings=resolved_settings),
    )
    if isinstance(settings_runtime_context, Mapping):
        from ..utils.settings.settings import bind_app_state_settings_runtime_context

        bind_app_state_settings_runtime_context(
            app_state,
            config_home=settings_runtime_context.get("config_home"),
            project_root=settings_runtime_context.get("project_root"),
            flag_path=settings_runtime_context.get("flag_path"),
            flag_inline=settings_runtime_context.get("flag_inline"),
            allowed_sources=settings_runtime_context.get("allowed_sources"),
        )
    return app_state


def create_app_state_store(
    *,
    initial_state: AppState | None = None,
) -> Store[AppState]:
    """Create a ``Store[AppState]`` wired with the onChange side-effect.

    The side-effect handler mirrors ``onChangeAppState``:
    - Persists selected fields to ``~/.claude_py/state.json`` when they change.
    """
    state = initial_state if initial_state is not None else get_default_app_state()

    def on_change(new_state: AppState, old_state: AppState) -> None:
        from .on_change_app_state import handle_app_state_change

        normalize_app_state_in_place(new_state)
        handle_app_state_change(new_state, old_state)

    return Store(state, on_change=on_change)


_SUPPORTED_PERMISSION_MODES = {mode.value for mode in PermissionMode}


def normalize_app_state_in_place(
    app_state: AppState,
) -> tuple[AppStateValidationIssue, ...]:
    issues: list[AppStateValidationIssue] = []

    app_state.settings = _normalize_settings(
        app_state.settings,
        path="settings",
        issues=issues,
    )
    app_state.tool_permission_context = _normalize_tool_permission_context(
        app_state.tool_permission_context,
        path="tool_permission_context",
        issues=issues,
    )
    app_state.session_hooks = _normalize_hooks_mapping(
        app_state.session_hooks,
        path="session_hooks",
        issues=issues,
    )
    app_state.mcp = _normalize_mcp_runtime_state(
        app_state.mcp,
        path="mcp",
        issues=issues,
    )
    app_state.plugins = _normalize_plugins_runtime_state(
        app_state.plugins,
        path="plugins",
        issues=issues,
    )
    app_state.notifications = _normalize_notifications_state(
        app_state.notifications,
        path="notifications",
        issues=issues,
    )
    app_state.elicitation = _normalize_message_queue_state(
        app_state.elicitation,
        path="elicitation",
        queue_key="queue",
        issues=issues,
    )
    app_state.inbox = _normalize_message_queue_state(
        app_state.inbox,
        path="inbox",
        queue_key="messages",
        issues=issues,
    )
    app_state.worker_sandbox_permissions = _normalize_worker_sandbox_state(
        app_state.worker_sandbox_permissions,
        path="worker_sandbox_permissions",
        issues=issues,
    )
    return tuple(issues)


def _append_issue(
    issues: list[AppStateValidationIssue],
    path: str,
    message: str,
) -> None:
    issues.append(AppStateValidationIssue(path=path, message=message))


def _normalize_settings(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "settings must be a mapping")
        return {}
    validated, schema_issues = validate_settings_payload(
        dict(raw),
        source="appState.settings",
    )
    for issue in schema_issues:
        _append_issue(
            issues,
            _rebase_schema_issue_path(issue, path, root_prefix=""),
            issue.message,
        )
    return validated


def _normalize_tool_permission_context(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "tool permission context must be a mapping")
        return {"mode": "default"}

    normalized = deepcopy(dict(raw))

    mode = normalized.get("mode")
    if not isinstance(mode, str) or mode.strip() not in _SUPPORTED_PERMISSION_MODES:
        if "mode" in normalized:
            _append_issue(
                issues,
                f"{path}.mode",
                "mode must be one of: " + ", ".join(sorted(_SUPPORTED_PERMISSION_MODES)),
            )
        normalized["mode"] = "default"
    else:
        normalized["mode"] = mode.strip()

    cwd = _normalize_optional_string(
        normalized.get("cwd"),
        path=f"{path}.cwd",
        issues=issues,
    )
    if cwd is None:
        normalized.pop("cwd", None)
    else:
        normalized["cwd"] = cwd

    additional = normalized.get("additional_working_directories")
    if additional is None:
        additional = normalized.get("additionalWorkingDirectories")
    if (
        "additional_working_directories" in normalized
        or "additionalWorkingDirectories" in normalized
    ):
        normalized["additional_working_directories"] = _normalize_additional_working_directories(
            additional,
            path=f"{path}.additional_working_directories",
            issues=issues,
        )
        if not normalized["additional_working_directories"]:
            normalized.pop("additional_working_directories", None)
    normalized.pop("additionalWorkingDirectories", None)

    for canonical, aliases in (
        ("always_allow_rules", ("always_allow_rules", "alwaysAllowRules")),
        ("always_deny_rules", ("always_deny_rules", "alwaysDenyRules")),
        ("always_ask_rules", ("always_ask_rules", "alwaysAskRules")),
    ):
        value = None
        found = False
        for alias in aliases:
            if alias in normalized:
                value = normalized.get(alias)
                found = True
                break
        if found:
            normalized[canonical] = _normalize_permission_rules(
                value,
                path=f"{path}.{canonical}",
                issues=issues,
            )
        else:
            normalized.pop(canonical, None)
        for alias in aliases:
            if alias != canonical:
                normalized.pop(alias, None)

    stripped = normalized.get("stripped_dangerous_rules")
    if stripped is None:
        stripped = normalized.get("strippedDangerousRules")
    if "stripped_dangerous_rules" in normalized or "strippedDangerousRules" in normalized:
        normalized["stripped_dangerous_rules"] = _normalize_optional_permission_rules(
            stripped,
            path=f"{path}.stripped_dangerous_rules",
            issues=issues,
        )
    else:
        normalized.pop("stripped_dangerous_rules", None)
    normalized.pop("strippedDangerousRules", None)

    approval_state = normalized.get("approval_state")
    if approval_state is None:
        approval_state = normalized.get("approvalState")
    if approval_state is not None and not isinstance(approval_state, Mapping):
        _append_issue(
            issues,
            f"{path}.approval_state",
            "approval_state must be a mapping when present",
        )
        approval_state = None
    if isinstance(approval_state, Mapping):
        normalized["approval_state"] = dict(approval_state)
    else:
        normalized.pop("approval_state", None)
    normalized.pop("approvalState", None)

    for canonical, aliases in (
        (
            "is_bypass_permissions_mode_available",
            ("is_bypass_permissions_mode_available", "isBypassPermissionsModeAvailable"),
        ),
        (
            "should_avoid_permission_prompts",
            ("should_avoid_permission_prompts", "shouldAvoidPermissionPrompts"),
        ),
        (
            "await_automated_checks_before_dialog",
            (
                "await_automated_checks_before_dialog",
                "awaitAutomatedChecksBeforeDialog",
            ),
        ),
    ):
        value = None
        found = False
        for alias in aliases:
            if alias in normalized:
                value = normalized.get(alias)
                found = True
                break
        if found:
            if not isinstance(value, bool):
                _append_issue(issues, f"{path}.{canonical}", "value must be a boolean")
                value = False
            normalized[canonical] = bool(value)
        else:
            normalized.pop(canonical, None)
        for alias in aliases:
            if alias != canonical:
                normalized.pop(alias, None)

    pre_plan_mode = normalized.get("pre_plan_mode")
    if pre_plan_mode is None:
        pre_plan_mode = normalized.get("prePlanMode")
    if "pre_plan_mode" in normalized or "prePlanMode" in normalized:
        normalized["pre_plan_mode"] = _normalize_optional_string(
            pre_plan_mode,
            path=f"{path}.pre_plan_mode",
            issues=issues,
        )
        if normalized["pre_plan_mode"] is None:
            normalized.pop("pre_plan_mode", None)
    else:
        normalized.pop("pre_plan_mode", None)
    normalized.pop("prePlanMode", None)
    return normalized


def _normalize_additional_working_directories(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Dict[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "additional working directories must be a mapping")
        return {}
    normalized: Dict[str, Dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            _append_issue(
                issues,
                path,
                "additional working directory keys must be non-empty strings",
            )
            continue
        if not isinstance(value, Mapping):
            _append_issue(
                issues,
                f"{path}.{key}",
                "entry must be a mapping",
            )
            continue
        entry_path = _normalize_optional_string(
            value.get("path"),
            path=f"{path}.{key}.path",
            issues=issues,
        )
        if entry_path is None:
            continue
        entry_source = _normalize_optional_string(
            value.get("source"),
            path=f"{path}.{key}.source",
            issues=issues,
        ) or "session"
        normalized[key.strip()] = {
            "path": entry_path,
            "source": entry_source,
        }
    return normalized


def _normalize_permission_rules(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, List[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "permission rules must be a mapping")
        return {}
    normalized: Dict[str, List[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            _append_issue(issues, path, "permission rule source keys must be strings")
            continue
        entries = _normalize_string_list(
            value,
            path=f"{path}.{key}",
            issues=issues,
        )
        if entries:
            normalized[key.strip()] = entries
    return normalized


def _normalize_optional_permission_rules(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, List[str]] | None:
    if raw is None:
        return None
    return _normalize_permission_rules(raw, path=path, issues=issues)


def _normalize_hooks_mapping(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "hooks must be a mapping")
        return {}
    normalized: Dict[str, Any] = {}
    for event_name, value in raw.items():
        if not isinstance(event_name, str) or not event_name.strip():
            _append_issue(issues, path, "hook event names must be non-empty strings")
            continue
        normalized_value = _normalize_hook_event_value(
            value,
            path=f"{path}.{event_name.strip()}",
            issues=issues,
        )
        if normalized_value is not None:
            normalized[event_name.strip()] = normalized_value
    return normalized


def _normalize_hook_event_value(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> object | None:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        normalized_items: list[object] = []
        for index, item in enumerate(raw):
            normalized = _normalize_hook_spec(
                item,
                path=f"{path}[{index}]",
                issues=issues,
            )
            if normalized is not None:
                normalized_items.append(normalized)
        return normalized_items
    return _normalize_hook_spec(raw, path=path, issues=issues)


def _normalize_hook_spec(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> object | None:
    if callable(raw):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            return stripped
        _append_issue(issues, path, "hook command must be a non-empty string")
        return None
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "hook spec must be a string, callable, object, or list")
        return None

    normalized = deepcopy(dict(raw))
    enabled = normalized.get("enabled")
    if "enabled" in normalized and not isinstance(enabled, bool):
        _append_issue(issues, f"{path}.enabled", "enabled must be a boolean")
    hook_type = normalized.get("type")
    if hook_type is not None:
        if not isinstance(hook_type, str) or not hook_type.strip():
            _append_issue(issues, f"{path}.type", "type must be a non-empty string")
        elif hook_type.strip() != hook_type:
            normalized["type"] = hook_type.strip()
    for key in ("command", "cmd", "shell", "callback", "function"):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if not isinstance(value, str) or not value.strip():
            _append_issue(
                issues,
                f"{path}.{key}",
                f"{key} must be a non-empty string",
            )
            continue
        normalized[key] = value.strip()
    for key in ("prompt", "url", "model", "if", "statusMessage"):
        if key not in normalized:
            continue
        value = normalized.get(key)
        if not isinstance(value, str) or not value.strip():
            _append_issue(
                issues,
                f"{path}.{key}",
                f"{key} must be a non-empty string",
            )
            continue
        normalized[key] = value.strip()
    if "timeout" in normalized:
        timeout = normalized.get("timeout")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or float(timeout) <= 0:
            _append_issue(issues, f"{path}.timeout", "timeout must be a positive number")
        else:
            normalized["timeout"] = float(timeout)
    if "once" in normalized and not isinstance(normalized.get("once"), bool):
        _append_issue(issues, f"{path}.once", "once must be a boolean")
    if "headers" in normalized:
        headers = normalized.get("headers")
        if not isinstance(headers, Mapping):
            _append_issue(
                issues,
                f"{path}.headers",
                "headers must be an object of string values",
            )
        else:
            normalized_headers: Dict[str, str] = {}
            for key, value in headers.items():
                if not isinstance(key, str) or not key.strip():
                    _append_issue(
                        issues,
                        f"{path}.headers",
                        "headers keys must be non-empty strings",
                    )
                    continue
                if not isinstance(value, str):
                    _append_issue(
                        issues,
                        f"{path}.headers.{key}",
                        "value must be a string",
                    )
                    continue
                normalized_headers[key.strip()] = value
            normalized["headers"] = normalized_headers
    if "allowedEnvVars" in normalized:
        normalized["allowedEnvVars"] = _normalize_string_list(
            normalized.get("allowedEnvVars"),
            path=f"{path}.allowedEnvVars",
            issues=issues,
        )
    return normalized


def _normalize_mcp_runtime_state(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return _default_mcp_state()
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "mcp runtime state must be a mapping")
        return _default_mcp_state()
    normalized = _default_mcp_state()
    for key in ("clients", "tools", "commands"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            _append_issue(issues, f"{path}.{key}", f"{key} must be a sequence")
            continue
        normalized[key] = list(value)
    resources = raw.get("resources")
    if resources is not None:
        if not isinstance(resources, Mapping):
            _append_issue(issues, f"{path}.resources", "resources must be a mapping")
        else:
            normalized["resources"] = {
                str(key): value
                for key, value in resources.items()
                if isinstance(key, str) and key.strip()
            }
            if len(normalized["resources"]) != len(resources):
                _append_issue(
                    issues,
                    f"{path}.resources",
                    "resource keys must be non-empty strings",
                )
    reconnect_key = raw.get("pluginReconnectKey")
    if reconnect_key is not None:
        if not isinstance(reconnect_key, int) or reconnect_key < 0:
            _append_issue(
                issues,
                f"{path}.pluginReconnectKey",
                "pluginReconnectKey must be a non-negative integer",
            )
        else:
            normalized["pluginReconnectKey"] = reconnect_key
    return normalized


def _normalize_plugins_runtime_state(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return _default_plugins_state()
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "plugin runtime state must be a mapping")
        return _default_plugins_state()
    normalized = _default_plugins_state()
    normalized["enabled"] = _normalize_plugin_entries(
        raw.get("enabled"),
        path=f"{path}.enabled",
        issues=issues,
    )
    normalized["disabled"] = _normalize_plugin_entries(
        raw.get("disabled"),
        path=f"{path}.disabled",
        issues=issues,
    )
    commands = raw.get("commands")
    if commands is not None:
        if not isinstance(commands, Sequence) or isinstance(
            commands,
            (str, bytes, bytearray),
        ):
            _append_issue(issues, f"{path}.commands", "commands must be a sequence")
        else:
            normalized["commands"] = list(commands)
    normalized["errors"] = _normalize_plugin_errors(
        raw.get("errors"),
        path=f"{path}.errors",
        issues=issues,
    )
    installation = raw.get("installationStatus")
    if installation is None:
        installation = raw.get("installation_status")
    normalized["installationStatus"] = _normalize_installation_status(
        installation,
        path=f"{path}.installationStatus",
        issues=issues,
    )
    needs_refresh = raw.get("needsRefresh")
    if needs_refresh is None:
        needs_refresh = raw.get("needs_refresh")
    if needs_refresh is not None and not isinstance(needs_refresh, bool):
        _append_issue(issues, f"{path}.needsRefresh", "needsRefresh must be a boolean")
        needs_refresh = False
    normalized["needsRefresh"] = bool(needs_refresh)
    return normalized


def _normalize_plugin_entries(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> List[LoadedPlugin]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        _append_issue(issues, path, "plugin entries must be a sequence")
        return []
    normalized: List[LoadedPlugin] = []
    for index, entry in enumerate(raw):
        plugin = _normalize_loaded_plugin(
            entry,
            path=f"{path}[{index}]",
            issues=issues,
        )
        if plugin is not None:
            normalized.append(plugin)
    return normalized


def _normalize_loaded_plugin(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> LoadedPlugin | None:
    if isinstance(raw, LoadedPlugin):
        manifest = raw.manifest if isinstance(raw.manifest, Mapping) else {}
        path_value = raw.path
        source = raw.source
        repository = raw.repository
        enabled = raw.enabled if isinstance(raw.enabled, bool) else None
        is_builtin = raw.is_builtin if isinstance(raw.is_builtin, bool) else None
        hooks_raw = raw.hooks_config
        mcp_raw = raw.mcp_servers
        name = raw.name
    elif isinstance(raw, Mapping):
        manifest = raw.get("manifest") if isinstance(raw.get("manifest"), Mapping) else {}
        name = raw.get("name")
        path_value = raw.get("path")
        source = raw.get("source")
        repository = raw.get("repository")
        enabled = raw.get("enabled") if isinstance(raw.get("enabled"), bool) else None
        is_builtin = (
            raw.get("is_builtin")
            if isinstance(raw.get("is_builtin"), bool)
            else raw.get("isBuiltin")
            if isinstance(raw.get("isBuiltin"), bool)
            else None
        )
        hooks_raw = raw.get("hooks_config")
        if hooks_raw is None:
            hooks_raw = raw.get("hooks")
        mcp_raw = raw.get("mcp_servers")
        if mcp_raw is None:
            mcp_raw = raw.get("mcpServers")
    else:
        _append_issue(issues, path, "plugin entry must be a mapping or LoadedPlugin")
        return None

    normalized_name = _normalize_optional_string(name, path=f"{path}.name", issues=issues)
    if normalized_name is None and isinstance(manifest, Mapping):
        normalized_name = _normalize_optional_string(
            manifest.get("name"),
            path=f"{path}.manifest.name",
            issues=issues,
        )
    if normalized_name is None:
        _append_issue(issues, path, "plugin entry requires a name")
        return None

    manifest_data = dict(manifest) if isinstance(manifest, Mapping) else {}
    manifest_data.setdefault("name", normalized_name)
    description = manifest_data.get("description")
    if not isinstance(description, str):
        description = ""
    manifest_data["description"] = description

    normalized_hooks = _normalize_hooks_mapping(
        hooks_raw,
        path=f"{path}.hooks",
        issues=issues,
    )
    normalized_mcp = _normalize_plugin_mcp_servers(
        mcp_raw,
        path=f"{path}.mcpServers",
        issues=issues,
    )
    return LoadedPlugin(
        name=normalized_name,
        manifest=manifest_data,
        path=_normalize_optional_string(
            path_value,
            path=f"{path}.path",
            issues=issues,
        ) or normalized_name,
        source=_normalize_optional_string(
            source,
            path=f"{path}.source",
            issues=issues,
        ) or normalized_name,
        repository=_normalize_optional_string(
            repository,
            path=f"{path}.repository",
            issues=issues,
        ) or normalized_name,
        enabled=enabled,
        is_builtin=is_builtin,
        hooks_config=normalized_hooks or None,
        mcp_servers=normalized_mcp or None,
    )


def _normalize_plugin_mcp_servers(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Mapping[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "mcpServers must be a mapping")
        return None
    validated, schema_issues = validate_settings_payload(
        {"mcpServers": dict(raw)},
        source="appState.plugins",
    )
    for issue in schema_issues:
        _append_issue(
            issues,
            _rebase_schema_issue_path(issue, path, root_prefix="mcpServers"),
            issue.message,
        )
    mcp_servers = validated.get("mcpServers")
    return dict(mcp_servers) if isinstance(mcp_servers, Mapping) else None


def _normalize_plugin_errors(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> List[PluginError]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        _append_issue(issues, path, "plugin errors must be a sequence")
        return []
    normalized: List[PluginError] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, PluginError):
            normalized.append(entry)
            continue
        if not isinstance(entry, Mapping):
            _append_issue(
                issues,
                f"{path}[{index}]",
                "plugin error entry must be a mapping or PluginError",
            )
            continue
        error_type = _normalize_optional_string(
            entry.get("type"),
            path=f"{path}[{index}].type",
            issues=issues,
        )
        source = _normalize_optional_string(
            entry.get("source"),
            path=f"{path}[{index}].source",
            issues=issues,
        )
        if error_type is None or source is None:
            continue
        validation_errors = _normalize_string_list(
            entry.get("validation_errors", entry.get("validationErrors")),
            path=f"{path}[{index}].validation_errors",
            issues=issues,
            allow_none=True,
        )
        normalized.append(
            PluginError(
                type=error_type,
                source=source,
                plugin=_normalize_optional_string(
                    entry.get("plugin"),
                    path=f"{path}[{index}].plugin",
                    issues=issues,
                ),
                path=_normalize_optional_string(
                    entry.get("path"),
                    path=f"{path}[{index}].path",
                    issues=issues,
                ),
                component=_normalize_optional_string(
                    entry.get("component"),
                    path=f"{path}[{index}].component",
                    issues=issues,
                ),
                error=_normalize_optional_string(
                    entry.get("error"),
                    path=f"{path}[{index}].error",
                    issues=issues,
                ),
                manifest_path=_normalize_optional_string(
                    entry.get("manifest_path", entry.get("manifestPath")),
                    path=f"{path}[{index}].manifest_path",
                    issues=issues,
                ),
                validation_errors=tuple(validation_errors or ()),
            )
        )
    return normalized


def _normalize_installation_status(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, List[str]]:
    if raw is None:
        return {"marketplaces": [], "plugins": []}
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "installation status must be a mapping")
        return {"marketplaces": [], "plugins": []}
    return {
        "marketplaces": _normalize_string_list(
            raw.get("marketplaces"),
            path=f"{path}.marketplaces",
            issues=issues,
            allow_none=True,
        )
        or [],
        "plugins": _normalize_string_list(
            raw.get("plugins"),
            path=f"{path}.plugins",
            issues=issues,
            allow_none=True,
        )
        or [],
    }


def _normalize_notifications_state(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return _default_notifications()
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "notifications must be a mapping")
        return _default_notifications()
    queue = raw.get("queue")
    if queue is None:
        normalized_queue: list[Any] = []
    elif isinstance(queue, Sequence) and not isinstance(queue, (str, bytes, bytearray)):
        normalized_queue = list(queue)
    else:
        _append_issue(issues, f"{path}.queue", "queue must be a sequence")
        normalized_queue = []
    return {"current": raw.get("current"), "queue": normalized_queue}


def _normalize_message_queue_state(
    raw: object,
    *,
    path: str,
    queue_key: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return {queue_key: []}
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "message queue state must be a mapping")
        return {queue_key: []}
    queue = raw.get(queue_key)
    if queue is None:
        return {queue_key: []}
    if not isinstance(queue, Sequence) or isinstance(queue, (str, bytes, bytearray)):
        _append_issue(issues, f"{path}.{queue_key}", f"{queue_key} must be a sequence")
        return {queue_key: []}
    return {queue_key: list(queue)}


def _normalize_worker_sandbox_state(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> Dict[str, Any]:
    if raw is None:
        return _default_worker_sandbox_permissions()
    if not isinstance(raw, Mapping):
        _append_issue(issues, path, "worker sandbox permissions must be a mapping")
        return _default_worker_sandbox_permissions()
    queue = raw.get("queue")
    if queue is None:
        normalized_queue: list[Any] = []
    elif isinstance(queue, Sequence) and not isinstance(queue, (str, bytes, bytearray)):
        normalized_queue = list(queue)
    else:
        _append_issue(issues, f"{path}.queue", "queue must be a sequence")
        normalized_queue = []
    selected_index = raw.get("selectedIndex")
    if not isinstance(selected_index, int) or selected_index < 0:
        if "selectedIndex" in raw:
            _append_issue(
                issues,
                f"{path}.selectedIndex",
                "selectedIndex must be a non-negative integer",
            )
        selected_index = 0
    return {"queue": normalized_queue, "selectedIndex": selected_index}


def _normalize_string_list(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
    allow_none: bool = False,
) -> List[str] | None:
    if raw is None and allow_none:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        _append_issue(issues, path, "value must be a sequence of strings")
        return [] if not allow_none else None
    normalized: List[str] = []
    seen: set[str] = set()
    for index, value in enumerate(raw):
        item = _normalize_optional_string(
            value,
            path=f"{path}[{index}]",
            issues=issues,
        )
        if item is None or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _normalize_optional_string(
    raw: object,
    *,
    path: str,
    issues: list[AppStateValidationIssue],
) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        _append_issue(issues, path, "value must be a non-empty string")
        return None
    return raw.strip()


def _rebase_schema_issue_path(
    issue: SchemaValidationIssue,
    path: str,
    *,
    root_prefix: str,
) -> str:
    issue_path = issue.path
    if root_prefix:
        if issue_path == root_prefix:
            return path
        if issue_path.startswith(f"{root_prefix}."):
            return f"{path}{issue_path[len(root_prefix):]}"
    if not issue_path:
        return path
    if path:
        return f"{path}.{issue_path}"
    return issue_path
