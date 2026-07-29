from __future__ import annotations

import asyncio
import importlib
import inspect
import ipaddress
import json
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..plugins.builtin_plugins import LoadedPlugin
from ..query import (
    AssistantMessage,
    AttachmentMessage,
    CompactBoundaryMessage,
    Message,
    ProgressMessage,
    SystemInformationalMessage,
    ToolUseSummaryMessage,
    UserMessage,
    createSystemMessage,
)
from ..state.app_state_store import AppState
from ..tools.anthropic_sidequery import extract_first_text_block, send_anthropic_message
from ..types.permissions import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)
from .permissions.permission_rule_parser import permission_rule_value_from_string
from .permissions.permissions import tool_matches_rule
from .plugin_registry import sync_managed_plugins_runtime_state
from .schema_registry import (
    normalize_app_state_via_registry,
    normalize_approval_state_via_registry,
)
from .settings import sync_app_state_settings_from_disk

_MESSAGE_TYPES = (
    AssistantMessage,
    AttachmentMessage,
    CompactBoundaryMessage,
    ProgressMessage,
    SystemInformationalMessage,
    ToolUseSummaryMessage,
    UserMessage,
)
_LOCK_TYPE = type(threading.Lock())


def _hook_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class HookStartedEvent:
    event_name: str
    source: str
    hook_kind: str
    timestamp: str = field(default_factory=_hook_now_iso)


@dataclass(frozen=True)
class HookProgressEvent:
    event_name: str
    source: str
    stage: str
    timestamp: str = field(default_factory=_hook_now_iso)
    message: str | None = None
    level: str | None = None
    detail: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class HookResponseEvent:
    event_name: str
    source: str
    hook_kind: str
    status: str
    timestamp: str = field(default_factory=_hook_now_iso)
    message_count: int = 0
    updated_input: Mapping[str, Any] | None = None
    approval_state: Mapping[str, object] | None = None
    decision_source: str | None = None
    error: str | None = None


HookLifecycleEvent = HookStartedEvent | HookProgressEvent | HookResponseEvent
HookEventListener = Callable[[HookLifecycleEvent], object]


@dataclass(frozen=True)
class HookEventResult:
    messages: tuple[Message, ...] = ()
    updated_input: Mapping[str, Any] | None = None
    approval_state: Mapping[str, object] | None = None
    decision_source: str | None = None


@dataclass(frozen=True)
class _HookSource:
    name: str
    hooks: Mapping[str, object]


async def dispatch_hook_event(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    app_state: AppState,
    cwd: str | None = None,
    scoped_hook_configs: Sequence[Mapping[str, object]] = (),
    on_event: HookEventListener | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> HookEventResult:
    normalize_app_state_via_registry(app_state)
    sources = _collect_hook_sources(app_state, scoped_hook_configs, cwd=cwd)
    if not sources:
        return HookEventResult()

    current_payload = dict(payload)
    messages: list[Message] = []
    latest_input: Mapping[str, Any] | None = None

    for source in sources:
        for hook_spec in _hook_specs_for_event(source.hooks, event_name):
            result = await _run_hook_spec(
                hook_spec,
                payload=current_payload,
                app_state=app_state,
                source=source.name,
                cwd=cwd,
                on_event=on_event,
                event_name=event_name,
                settings=app_state.settings,
                runtime_context=runtime_context,
            )
            if result.messages:
                messages.extend(result.messages)
            if result.updated_input is not None:
                latest_input = dict(result.updated_input)
                if isinstance(current_payload.get("toolInput"), Mapping):
                    merged_tool_input = dict(current_payload["toolInput"])
                    merged_tool_input.update(result.updated_input)
                    current_payload["toolInput"] = merged_tool_input
                    latest_input = merged_tool_input
            if result.approval_state is not None:
                return HookEventResult(
                    messages=tuple(messages),
                    updated_input=latest_input,
                    approval_state=result.approval_state,
                    decision_source=result.decision_source,
                )

    return HookEventResult(messages=tuple(messages), updated_input=latest_input)


def dispatch_hook_event_sync(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    app_state: AppState,
    cwd: str | None = None,
    scoped_hook_configs: Sequence[Mapping[str, object]] = (),
    on_event: HookEventListener | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> HookEventResult:
    async def _dispatch() -> HookEventResult:
        return await dispatch_hook_event(
            event_name,
            payload,
            app_state=app_state,
            cwd=cwd,
            scoped_hook_configs=scoped_hook_configs,
            on_event=on_event,
            runtime_context=runtime_context,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_dispatch())

    result_box: dict[str, HookEventResult] = {}
    error_box: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result_box["result"] = asyncio.run(_dispatch())
        except BaseException as exc:  # pragma: no cover - defensive path
            error_box["error"] = exc

    thread = threading.Thread(target=_runner, name=f"hook-sync-{event_name}", daemon=True)
    thread.start()
    thread.join()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("result", HookEventResult())


def _collect_hook_sources(
    app_state: AppState,
    scoped_hook_configs: Sequence[Mapping[str, object]],
    *,
    cwd: str | None = None,
) -> tuple[_HookSource, ...]:
    sources: list[_HookSource] = []
    sync_app_state_settings_from_disk(app_state, project_root=cwd)
    sync_managed_plugins_runtime_state(app_state)
    settings_hooks = app_state.settings.get("hooks")
    if isinstance(settings_hooks, Mapping):
        sources.append(_HookSource(name="settings", hooks=settings_hooks))
    if isinstance(app_state.session_hooks, Mapping):
        sources.append(_HookSource(name="session", hooks=app_state.session_hooks))

    plugins = app_state.plugins.get("enabled")
    if isinstance(plugins, Sequence) and not isinstance(
        plugins,
        (str, bytes, bytearray),
    ):
        for index, plugin in enumerate(plugins):
            hooks_config = _plugin_hooks(plugin)
            if not isinstance(hooks_config, Mapping):
                continue
            name = _plugin_name(plugin, index)
            sources.append(_HookSource(name=f"plugin:{name}", hooks=hooks_config))

    for index, hooks in enumerate(scoped_hook_configs):
        if isinstance(hooks, Mapping):
            sources.append(_HookSource(name=f"scoped:{index}", hooks=hooks))
    return tuple(sources)


def _plugin_hooks(plugin: object) -> Mapping[str, object] | None:
    if isinstance(plugin, LoadedPlugin):
        return plugin.hooks_config
    if isinstance(plugin, Mapping):
        hooks_config = plugin.get("hooks_config")
        if isinstance(hooks_config, Mapping):
            return hooks_config
        hooks_config = plugin.get("hooks")
        if isinstance(hooks_config, Mapping):
            return hooks_config
    return None


def _plugin_name(plugin: object, index: int) -> str:
    if isinstance(plugin, LoadedPlugin):
        return plugin.name
    if isinstance(plugin, Mapping):
        raw_name = plugin.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            return raw_name
    return str(index)


def _hook_specs_for_event(
    hook_config: Mapping[str, object],
    event_name: str,
) -> tuple[object, ...]:
    raw = hook_config.get(event_name)
    if raw is None:
        return ()
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(raw)
    return (raw,)


async def _run_hook_spec(
    hook_spec: object,
    *,
    payload: Mapping[str, Any],
    app_state: AppState,
    source: str,
    cwd: str | None,
    on_event: HookEventListener | None,
    event_name: str,
    settings: Mapping[str, Any],
    runtime_context: Mapping[str, Any] | None,
) -> HookEventResult:
    hook_kind = _describe_hook_spec(hook_spec)
    once_key: str | None = None
    if _hook_spec_once_enabled(hook_spec) and _hook_spec_should_invoke(hook_spec, payload):
        once_key, should_skip = _reserve_hook_once_execution(
            app_state,
            source=source,
            event_name=event_name,
            hook_spec=hook_spec,
        )
    else:
        should_skip = False
    await _emit_hook_lifecycle_event(
        on_event,
        HookStartedEvent(
            event_name=event_name,
            source=source,
            hook_kind=hook_kind,
        ),
    )
    if should_skip:
        await _emit_hook_lifecycle_event(
            on_event,
            HookResponseEvent(
                event_name=event_name,
                source=source,
                hook_kind=hook_kind,
                status="skipped",
            ),
        )
        return HookEventResult()
    try:
        raw_result = await _invoke_hook_spec(
            hook_spec,
            payload=payload,
            cwd=cwd,
            event_name=event_name,
            settings=settings,
            runtime_context=runtime_context,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        if once_key is not None:
            _finish_hook_once_execution(app_state, once_key, completed=False)
        result = HookEventResult(
            messages=(
                createSystemMessage(
                    f"Hook {source} for {event_name} failed: {exc}",
                    "warning",
                ),
            )
        )
        await _emit_hook_progress_events(on_event, event_name, source, result)
        await _emit_hook_lifecycle_event(
            on_event,
            HookResponseEvent(
                event_name=event_name,
                source=source,
                hook_kind=hook_kind,
                status="error",
                message_count=len(result.messages),
                error=str(exc),
            ),
        )
        return result
    if once_key is not None:
        _finish_hook_once_execution(app_state, once_key, completed=True)
    result = _normalize_hook_result(raw_result, source=source)
    await _emit_hook_progress_events(on_event, event_name, source, result)
    await _emit_hook_lifecycle_event(
        on_event,
        HookResponseEvent(
            event_name=event_name,
            source=source,
            hook_kind=hook_kind,
            status="ok",
            message_count=len(result.messages),
            updated_input=result.updated_input,
            approval_state=result.approval_state,
            decision_source=result.decision_source,
        ),
    )
    return result


def _hook_spec_once_enabled(hook_spec: object) -> bool:
    return isinstance(hook_spec, Mapping) and hook_spec.get("once") is True


def _hook_spec_should_invoke(
    hook_spec: object,
    payload: Mapping[str, Any],
) -> bool:
    if not isinstance(hook_spec, Mapping):
        return True
    if hook_spec.get("enabled") is False:
        return False
    return _hook_if_condition_matches(hook_spec, payload)


def _reserve_hook_once_execution(
    app_state: AppState,
    *,
    source: str,
    event_name: str,
    hook_spec: Mapping[str, object],
) -> tuple[str, bool]:
    state = _hook_once_state(app_state)
    key = _hook_once_key(source=source, event_name=event_name, hook_spec=hook_spec)
    with state["lock"]:
        seen = state["seen"]
        inflight = state["inflight"]
        if key in seen or key in inflight:
            return key, True
        inflight.add(key)
    return key, False


def _finish_hook_once_execution(
    app_state: AppState,
    key: str,
    *,
    completed: bool,
) -> None:
    state = _hook_once_state(app_state)
    with state["lock"]:
        inflight = state["inflight"]
        inflight.discard(key)
        if completed:
            state["seen"].add(key)


def _hook_once_state(app_state: AppState) -> dict[str, object]:
    state = getattr(app_state, "_hook_once_state", None)
    if not isinstance(state, dict):
        state = {
            "seen": set(),
            "inflight": set(),
            "lock": threading.Lock(),
        }
        setattr(app_state, "_hook_once_state", state)
        return state

    seen = state.get("seen")
    if not isinstance(seen, set):
        seen = set()
        state["seen"] = seen

    inflight = state.get("inflight")
    if not isinstance(inflight, set):
        inflight = set()
        state["inflight"] = inflight

    lock = state.get("lock")
    if not isinstance(lock, _LOCK_TYPE):
        lock = threading.Lock()
        state["lock"] = lock
    return state


def _hook_once_key(
    *,
    source: str,
    event_name: str,
    hook_spec: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        _normalize_hook_spec_for_once_key(hook_spec),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{source}:{event_name}:{encoded}"


def _normalize_hook_spec_for_once_key(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if callable(value):
        module = getattr(value, "__module__", None) or "<unknown>"
        qualname = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
        return {"__callable__": f"{module}:{qualname or repr(value)}"}
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_hook_spec_for_once_key(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_hook_spec_for_once_key(item) for item in value]
    return {"__repr__": repr(value)}


async def _emit_hook_lifecycle_event(
    listener: HookEventListener | None,
    event: HookLifecycleEvent,
) -> None:
    if listener is None:
        return
    try:
        callback_result = listener(event)
        if inspect.isawaitable(callback_result):
            await callback_result
    except Exception:  # pragma: no cover - observer failures must not break hooks
        return


async def _emit_hook_progress_events(
    listener: HookEventListener | None,
    event_name: str,
    source: str,
    result: HookEventResult,
) -> None:
    if listener is None:
        return
    for message in result.messages:
        await _emit_hook_lifecycle_event(
            listener,
            HookProgressEvent(
                event_name=event_name,
                source=source,
                stage="message",
                message=_message_preview(message),
                level=getattr(message, "level", None),
                detail={"message_type": getattr(message, "type", type(message).__name__)},
            ),
        )
    if result.updated_input is not None:
        await _emit_hook_lifecycle_event(
            listener,
            HookProgressEvent(
                event_name=event_name,
                source=source,
                stage="updated_input",
                detail=dict(result.updated_input),
            ),
        )
    if result.approval_state is not None:
        behavior = result.approval_state.get("behavior")
        await _emit_hook_lifecycle_event(
            listener,
            HookProgressEvent(
                event_name=event_name,
                source=source,
                stage="approval_state",
                message=behavior if isinstance(behavior, str) else None,
                detail=dict(result.approval_state),
            ),
        )


def _message_preview(message: Message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(message, ProgressMessage):
        title = message.title or message.content or "Progress"
        return str(title)
    return type(message).__name__


def _describe_hook_spec(hook_spec: object) -> str:
    if isinstance(hook_spec, str):
        return "command"
    if callable(hook_spec):
        return "callback"
    if isinstance(hook_spec, Mapping):
        hook_type = hook_spec.get("type")
        if isinstance(hook_type, str) and hook_type.strip():
            return hook_type.strip()
        if any(key in hook_spec for key in ("command", "cmd", "shell")):
            return "command"
        if any(key in hook_spec for key in ("callback", "function")):
            return "callback"
        return "mapping"
    return type(hook_spec).__name__


async def _invoke_hook_spec(
    hook_spec: object,
    *,
    payload: Mapping[str, Any],
    cwd: str | None,
    event_name: str,
    settings: Mapping[str, Any],
    runtime_context: Mapping[str, Any] | None,
) -> object:
    if hook_spec is None:
        return None
    if isinstance(hook_spec, str):
        return await _run_command_hook(hook_spec, payload=payload, cwd=cwd)
    if callable(hook_spec):
        return await _invoke_callable_hook(hook_spec, payload)
    if isinstance(hook_spec, Mapping):
        if hook_spec.get("enabled") is False:
            return None
        if not _hook_if_condition_matches(hook_spec, payload):
            return None
        hook_type = hook_spec.get("type")
        if hook_type == "command" or any(
            key in hook_spec for key in ("command", "cmd", "shell")
        ):
            command = _extract_command_hook_command(hook_spec)
            return await _run_command_hook(command, payload=payload, cwd=cwd)
        if hook_type == "prompt" or (
            hook_type is None and "prompt" in hook_spec and "url" not in hook_spec
        ):
            return await _run_prompt_hook(
                hook_spec,
                event_name=event_name,
                payload=payload,
                runtime_context=runtime_context,
            )
        if hook_type == "http" or (
            hook_type is None and "url" in hook_spec and "headers" in hook_spec
        ):
            return await _run_http_hook(
                hook_spec,
                event_name=event_name,
                payload=payload,
                settings=settings,
            )
        if hook_type == "agent":
            return await _run_agent_hook(
                hook_spec,
                event_name=event_name,
                payload=payload,
                runtime_context=runtime_context,
            )
        if hook_type in {"callback", "function"} or any(
            key in hook_spec for key in ("callback", "function")
        ):
            resolved = _extract_callback_hook(hook_spec)
            return await _invoke_callable_hook(resolved, payload)
        return hook_spec
    return hook_spec


def _extract_command_hook_command(hook_spec: Mapping[str, object]) -> str:
    for key in ("command", "cmd", "shell"):
        if key not in hook_spec:
            continue
        value = hook_spec.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Hook {key} must be a non-empty string")
        return value
    raise ValueError("Command hook must define command/cmd/shell")


def _extract_callback_hook(hook_spec: Mapping[str, object]) -> Callable[..., object]:
    for key in ("callback", "function"):
        if key not in hook_spec:
            continue
        resolved = _resolve_callback(hook_spec.get(key))
        if resolved is None:
            raise ValueError(f"Hook {key} could not be resolved")
        return resolved
    raise ValueError("Callback hook must define callback/function")


async def _invoke_callable_hook(
    callback: Callable[..., object],
    payload: Mapping[str, Any],
) -> object:
    result = callback(payload)
    if inspect.isawaitable(result):
        return await result
    return result


def _resolve_callback(value: object) -> Callable[..., object] | None:
    if callable(value):
        return value
    if not isinstance(value, str) or not value.strip():
        return None

    module_name: str
    attr_name: str
    if ":" in value:
        module_name, attr_name = value.split(":", 1)
    else:
        module_name, _, attr_name = value.rpartition(".")
    if not module_name or not attr_name:
        return None
    module = importlib.import_module(module_name)
    callback = getattr(module, attr_name)
    return callback if callable(callback) else None


async def _run_command_hook(
    command: str,
    *,
    payload: Mapping[str, Any],
    cwd: str | None,
) -> object:
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.getcwd(),
    )
    stdout, stderr = await process.communicate(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    stdout_text = stdout.decode("utf-8", "replace").strip()
    stderr_text = stderr.decode("utf-8", "replace").strip()
    if process.returncode != 0:
        message = stderr_text or stdout_text or f"Hook command exited with {process.returncode}"
        return {"message": message, "level": "warning"}
    if not stdout_text:
        return None
    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        return {"message": stdout_text, "level": "info"}


def _hook_if_condition_matches(
    hook_spec: Mapping[str, object],
    payload: Mapping[str, Any],
) -> bool:
    condition = hook_spec.get("if")
    if not isinstance(condition, str) or not condition.strip():
        return True

    tool_name = payload.get("toolName")
    tool_input = payload.get("toolInput")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return False
    if not isinstance(tool_input, Mapping):
        tool_input = {}

    parsed = permission_rule_value_from_string(condition.strip())
    rule = PermissionRule(
        source=PermissionRuleSource.SESSION,
        rule_behavior=PermissionBehavior.ALLOW,
        rule_value=PermissionRuleValue(
            tool_name=str(parsed.get("tool_name") or ""),
            rule_content=(
                str(parsed["rule_content"])
                if isinstance(parsed.get("rule_content"), str)
                else None
            ),
        ),
    )
    return tool_matches_rule(tool_name.strip(), tool_input, rule)


async def _run_prompt_hook(
    hook_spec: Mapping[str, object],
    *,
    event_name: str,
    payload: Mapping[str, Any],
    runtime_context: Mapping[str, Any] | None,
) -> object:
    prompt = _required_string_field(hook_spec, "prompt")
    timeout_seconds = _optional_positive_float_field(hook_spec, "timeout") or 30.0
    model = _optional_nonempty_string_field(hook_spec, "model")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    conversation = _serialize_hook_conversation_context(runtime_context)
    user_prompt = _build_prompt_hook_input(
        _substitute_hook_arguments(prompt, payload_json),
        conversation=conversation,
    )
    system_prompt = (
        "You are evaluating an internal Claude Code hook.\n"
        "Return only a JSON object matching one of these forms:\n"
        '{"ok": true}\n'
        '{"ok": false, "reason": "short explanation"}'
    )

    response = await asyncio.wait_for(
        asyncio.to_thread(
            send_anthropic_message,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            model=model,
            max_tokens=256,
        ),
        timeout=timeout_seconds,
    )
    text = extract_first_text_block(response)
    parsed = _parse_hook_condition_response(text)
    if parsed is None:
        return {
            "message": (
                f"Prompt hook returned invalid JSON for {event_name}: "
                f"{(text or '').strip() or '<empty>'}"
            ),
            "level": "warning",
        }
    if parsed["ok"]:
        return None
    return _blocking_result_for_event(
        event_name,
        parsed.get("reason") or "Prompt hook condition was not met",
        hook_kind="Prompt",
        timeout_seconds=timeout_seconds,
    )


async def _run_http_hook(
    hook_spec: Mapping[str, object],
    *,
    event_name: str,
    payload: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> object:
    url = _required_string_field(hook_spec, "url")
    _validate_http_hook_url(url)
    allowlist = settings.get("allowedHttpHookUrls")
    if isinstance(allowlist, Sequence) and not isinstance(
        allowlist, (str, bytes, bytearray)
    ):
        patterns = [
            pattern.strip()
            for pattern in allowlist
            if isinstance(pattern, str) and pattern.strip()
        ]
        if patterns and not any(_url_matches_pattern(url, pattern) for pattern in patterns):
            return {
                "message": f"HTTP hook blocked: {url} does not match allowedHttpHookUrls",
                "level": "warning",
            }

    hook_allowed_env = _string_sequence_field(hook_spec.get("allowedEnvVars"))
    policy_allowed_env = _string_sequence_field(settings.get("httpHookAllowedEnvVars"))
    effective_allowed_env = (
        tuple(var for var in hook_allowed_env if var in policy_allowed_env)
        if policy_allowed_env is not None
        else hook_allowed_env
    ) or ()
    headers = {"content-type": "application/json"}
    raw_headers = hook_spec.get("headers")
    if isinstance(raw_headers, Mapping):
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(value, str):
                continue
            headers[key.strip()] = _interpolate_allowed_env_vars(
                value,
                allowed_env_vars=frozenset(effective_allowed_env),
            )

    timeout_seconds = _optional_positive_float_field(hook_spec, "timeout") or 600.0
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        status_code, body = await asyncio.wait_for(
            asyncio.to_thread(_perform_http_hook_request, request, timeout_seconds),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return {
            "message": f"HTTP hook timed out after {timeout_seconds:.0f}s",
            "level": "warning",
        }
    except Exception as exc:
        return {
            "message": f"HTTP hook failed for {event_name}: {exc}",
            "level": "warning",
        }

    text = body.strip()
    if status_code < 200 or status_code >= 300:
        return {
            "message": (
                f"HTTP hook returned {status_code} for {event_name}"
                + (f": {text}" if text else "")
            ),
            "level": "warning",
        }
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text, "level": "info"}


async def _run_agent_hook(
    hook_spec: Mapping[str, object],
    *,
    event_name: str,
    payload: Mapping[str, Any],
    runtime_context: Mapping[str, Any] | None,
) -> object:
    executor = (
        runtime_context.get("tool_executor")
        if isinstance(runtime_context, Mapping)
        else None
    )
    run_agent_hook = getattr(executor, "run_agent_hook", None)
    if not callable(run_agent_hook):
        return {
            "message": "Agent hook requires a LocalToolExecutor runtime context",
            "level": "warning",
        }

    prompt = _required_string_field(hook_spec, "prompt")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    processed_prompt = _substitute_hook_arguments(prompt, payload_json)
    result = run_agent_hook(
        event_name=event_name,
        prompt=processed_prompt,
        model=_optional_nonempty_string_field(hook_spec, "model"),
        timeout_seconds=_optional_positive_float_field(hook_spec, "timeout") or 60.0,
        session_messages=_runtime_session_messages(runtime_context),
    )
    if inspect.isawaitable(result):
        result = await result
    if result is None or isinstance(result, HookEventResult):
        return result
    if isinstance(result, Mapping) and isinstance(result.get("ok"), bool):
        if result["ok"]:
            return None
        return _blocking_result_for_event(
            event_name,
            str(result.get("reason") or "Agent hook condition was not met"),
            hook_kind="Agent",
        )
    return result


def _required_string_field(hook_spec: Mapping[str, object], key: str) -> str:
    value = hook_spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Hook {key} must be a non-empty string")
    return value.strip()


def _optional_nonempty_string_field(
    hook_spec: Mapping[str, object],
    key: str,
) -> str | None:
    value = hook_spec.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _optional_positive_float_field(
    hook_spec: Mapping[str, object],
    key: str,
) -> float | None:
    value = hook_spec.get(key)
    if isinstance(value, (int, float)) and float(value) > 0:
        return float(value)
    return None


def _string_sequence_field(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    return tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )


def _serialize_hook_conversation_context(
    runtime_context: Mapping[str, Any] | None,
) -> str | None:
    session_messages = _runtime_session_messages(runtime_context)
    if not session_messages:
        return None
    rendered: list[str] = []
    for message in session_messages[-12:]:
        role = getattr(message, "type", type(message).__name__)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            rendered.append(f"{role}: {content.strip()}")
            continue
        if isinstance(message, AssistantMessage):
            text = " ".join(
                block.text.strip()
                for block in message.message.content
                if getattr(block, "type", None) == "text"
                and isinstance(getattr(block, "text", None), str)
                and block.text.strip()
            ).strip()
            if text:
                rendered.append(f"assistant: {text}")
    if not rendered:
        return None
    return "\n".join(rendered)


def _runtime_session_messages(
    runtime_context: Mapping[str, Any] | None,
) -> tuple[Message, ...]:
    if not isinstance(runtime_context, Mapping):
        return ()
    messages = runtime_context.get("session_messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return ()
    return tuple(message for message in messages if isinstance(message, _MESSAGE_TYPES))


def _build_prompt_hook_input(prompt: str, *, conversation: str | None) -> str:
    if not conversation:
        return prompt
    return (
        "Conversation context:\n"
        f"{conversation}\n\n"
        "Hook evaluation request:\n"
        f"{prompt}"
    )


def _substitute_hook_arguments(prompt: str, payload_json: str) -> str:
    substituted = prompt
    try:
        parsed = json.loads(payload_json)
    except Exception:
        parsed = None
    if isinstance(parsed, Sequence) and not isinstance(parsed, (str, bytes, bytearray)):
        for index, value in enumerate(parsed):
            replacement = json.dumps(value, ensure_ascii=False)
            substituted = substituted.replace(f"$ARGUMENTS[{index}]", replacement)
            substituted = substituted.replace(f"${index}", replacement)
    substituted = substituted.replace("$ARGUMENTS", payload_json)
    return substituted if substituted != prompt else f"{prompt.rstrip()}\n\n{payload_json}"


def _parse_hook_condition_response(text: str | None) -> dict[str, str | bool] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("ok"), bool):
        return None
    parsed: dict[str, str | bool] = {"ok": bool(payload["ok"])}
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        parsed["reason"] = reason.strip()
    return parsed


def _blocking_result_for_event(
    event_name: str,
    reason: str,
    *,
    hook_kind: str,
    timeout_seconds: float | None = None,
) -> Mapping[str, object]:
    message = f"{hook_kind} hook condition was not met: {reason}"
    normalized_event = event_name.strip()
    if normalized_event in {"Stop", "StopFailure"}:
        return {"behavior": "continue", "message": message}
    if normalized_event in {"PreToolUse", "PermissionRequest"}:
        return {"behavior": "deny", "message": message}
    if timeout_seconds is not None and timeout_seconds > 0:
        return {"message": message, "level": "warning"}
    return {"message": message, "level": "warning"}


def _validate_http_hook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HTTP hook only supports http and https URLs")
    if parsed.username or parsed.password:
        raise ValueError("HTTP hook URLs with embedded credentials are not supported")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("HTTP hook URL must include a hostname")
    if host == "localhost":
        return
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if literal_ip.is_loopback:
            return
        if (
            literal_ip.is_private
            or literal_ip.is_link_local
            or literal_ip.is_multicast
            or literal_ip.is_reserved
        ):
            raise ValueError("HTTP hook URL resolves to a private or reserved address")
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise ValueError(f"HTTP hook hostname lookup failed: {exc}") from exc
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if resolved.is_loopback:
            continue
        if (
            resolved.is_private
            or resolved.is_link_local
            or resolved.is_multicast
            or resolved.is_reserved
        ):
            raise ValueError("HTTP hook URL resolves to a private or reserved address")


def _url_matches_pattern(url: str, pattern: str) -> bool:
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.match(f"^{escaped}$", url) is not None


def _interpolate_allowed_env_vars(
    value: str,
    *,
    allowed_env_vars: frozenset[str],
) -> str:
    def _replace(match: re.Match[str]) -> str:
        env_name = match.group(1) or match.group(2)
        if env_name not in allowed_env_vars:
            return ""
        return os.environ.get(env_name, "")

    sanitized = re.sub(
        r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)",
        _replace,
        value,
    )
    return sanitized.replace("\r", "").replace("\n", "").replace("\x00", "")


def _perform_http_hook_request(request: Request, timeout_seconds: float) -> tuple[int, str]:
    with urlopen(request, timeout=timeout_seconds) as response:
        status_code = getattr(response, "status", None) or response.getcode()
        body = response.read().decode("utf-8", "replace")
    return int(status_code), body


def _normalize_hook_result(
    raw_result: object,
    *,
    source: str,
) -> HookEventResult:
    if raw_result is None:
        return HookEventResult()
    if isinstance(raw_result, HookEventResult):
        return raw_result
    if isinstance(raw_result, _MESSAGE_TYPES):
        return HookEventResult(messages=(raw_result,))
    if isinstance(raw_result, str):
        return HookEventResult(messages=(createSystemMessage(raw_result, "info"),))
    if isinstance(raw_result, Sequence) and not isinstance(
        raw_result,
        (str, bytes, bytearray),
    ):
        aggregated_messages: list[Message] = []
        updated_input: Mapping[str, Any] | None = None
        approval_state: Mapping[str, object] | None = None
        decision_source: str | None = None
        for item in raw_result:
            normalized = _normalize_hook_result(item, source=source)
            aggregated_messages.extend(normalized.messages)
            if normalized.updated_input is not None:
                updated_input = normalized.updated_input
            if normalized.approval_state is not None:
                approval_state = normalized.approval_state
                decision_source = normalized.decision_source or source
                break
        return HookEventResult(
            messages=tuple(aggregated_messages),
            updated_input=updated_input,
            approval_state=approval_state,
            decision_source=decision_source,
        )
    if isinstance(raw_result, Mapping):
        messages: list[Message] = []
        behavior = raw_result.get("behavior")
        if "messages" in raw_result:
            nested = _normalize_hook_result(raw_result.get("messages"), source=source)
            messages.extend(nested.messages)
        if behavior is None and "message" in raw_result:
            message = raw_result.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(
                    createSystemMessage(
                        message,
                        str(raw_result.get("level") or "info"),
                    )
                )
            elif message is not None:
                messages.append(
                    createSystemMessage(
                        f"Hook {source} returned non-string message field",
                        "warning",
                    )
                )
        if "content_blocks" in raw_result:
            try:
                messages.extend(_messages_from_content_blocks(raw_result["content_blocks"]))
            except ValueError as exc:
                messages.append(
                    createSystemMessage(
                        f"Hook {source} returned invalid content_blocks: {exc}",
                        "warning",
                    )
                )

        approval_state: Mapping[str, object] | None = None
        try:
            approval_state = _normalize_hook_approval_state(raw_result)
        except ValueError as exc:
            messages.append(
                createSystemMessage(
                    f"Hook {source} returned invalid approval state: {exc}",
                    "warning",
                )
            )

        updated_input: Mapping[str, Any] | None = None
        if "updated_input" in raw_result:
            try:
                updated_input = _normalize_updated_input(raw_result.get("updated_input"))
            except ValueError as exc:
                messages.append(
                    createSystemMessage(
                        f"Hook {source} returned invalid updated_input: {exc}",
                        "warning",
                    )
                )

        return HookEventResult(
            messages=tuple(messages),
            updated_input=updated_input,
            approval_state=approval_state,
            decision_source=source if approval_state is not None else None,
        )
    return HookEventResult(
        messages=(
            createSystemMessage(str(raw_result), "info"),
        )
    )


def _messages_from_content_blocks(blocks: object) -> tuple[Message, ...]:
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes, bytearray)):
        raise ValueError("content_blocks must be a sequence")
    messages: list[Message] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise ValueError(f"content_blocks[{index}] must be a mapping")
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if text is None:
            continue
        if not isinstance(text, str):
            raise ValueError(f"content_blocks[{index}].text must be a string")
        if text.strip():
            messages.append(createSystemMessage(text, "info"))
    return tuple(messages)


def _normalize_updated_input(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("updated_input must be a mapping")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("updated_input keys must be non-empty strings")
        normalized[key] = item
    return normalized


def _normalize_hook_approval_state(
    raw_result: Mapping[str, object],
) -> Mapping[str, object] | None:
    raw_state = raw_result.get("approval_state")
    if raw_state is None:
        raw_state = raw_result.get("approvalState")
    if raw_state is None and isinstance(raw_result.get("behavior"), str):
        raw_state = raw_result
    if raw_state is None:
        return None
    if not isinstance(raw_state, Mapping):
        raise ValueError("approval_state must be a mapping")
    behavior = raw_state.get("behavior")
    if isinstance(behavior, str) and behavior.strip().lower() in {
        "continue",
        "retry",
        "stop",
        "prevent",
    }:
        return dict(raw_state)
    try:
        normalize_approval_state_via_registry(raw_state)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return dict(raw_state)
