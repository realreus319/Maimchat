from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Sequence

from ...services.mcp.config import parse_mcp_config
from ...types.permissions import PermissionMode


@dataclass(frozen=True)
class SchemaValidationIssue:
    path: str
    message: str
    file: str | None = None


_SUPPORTED_THEMES = {"dark", "light"}
_SUPPORTED_EFFORT_VALUES = {"low", "medium", "high", "max"}
_SUPPORTED_PERMISSION_MODES = {mode.value for mode in PermissionMode}


def validate_settings_payload(
    raw: Mapping[str, Any],
    *,
    file_path: str | None = None,
    source: str | None = None,
) -> tuple[Dict[str, Any], tuple[SchemaValidationIssue, ...]]:
    issues: list[SchemaValidationIssue] = []
    validated = deepcopy(dict(raw))

    _validate_string_field(validated, "model", issues, file_path=file_path)
    _validate_string_field(validated, "defaultModel", issues, file_path=file_path)
    _validate_string_field(validated, "fallbackModel", issues, file_path=file_path)
    _validate_bool_field(validated, "fastMode", issues, file_path=file_path)
    _validate_bool_field(
        validated,
        "skipAutoPermissionPrompt",
        issues,
        file_path=file_path,
    )
    _validate_bool_field(
        validated,
        "enableAllProjectMcpServers",
        issues,
        file_path=file_path,
    )
    _validate_enum_field(
        validated,
        "theme",
        _SUPPORTED_THEMES,
        issues,
        file_path=file_path,
        normalize=lambda value: str(value).strip().lower(),
    )
    _validate_enum_field(
        validated,
        "effortValue",
        _SUPPORTED_EFFORT_VALUES,
        issues,
        file_path=file_path,
        normalize=lambda value: str(value).strip().lower(),
    )
    _validate_string_dict_field(validated, "env", issues, file_path=file_path)
    _validate_string_list_field(
        validated,
        "enabledMcpjsonServers",
        issues,
        file_path=file_path,
        dedupe=True,
    )
    _validate_string_list_field(
        validated,
        "disabledMcpjsonServers",
        issues,
        file_path=file_path,
        dedupe=True,
    )

    if "enabledPlugins" in validated:
        enabled_plugins = _validate_enabled_plugins(
            validated.get("enabledPlugins"),
            "enabledPlugins",
            issues,
            file_path=file_path,
        )
        if enabled_plugins is None:
            validated.pop("enabledPlugins", None)
        else:
            validated["enabledPlugins"] = enabled_plugins

    if "permissions" in validated:
        permissions = _validate_permissions_config(
            validated.get("permissions"),
            "permissions",
            issues,
            file_path=file_path,
        )
        if permissions is None:
            validated.pop("permissions", None)
        else:
            validated["permissions"] = permissions

    if "sandbox" in validated:
        sandbox = _validate_sandbox_config(
            validated.get("sandbox"),
            "sandbox",
            issues,
            file_path=file_path,
        )
        if sandbox is None:
            validated.pop("sandbox", None)
        else:
            validated["sandbox"] = sandbox

    if "hooks" in validated:
        hooks = _validate_hooks_config(
            validated.get("hooks"),
            "hooks",
            issues,
            file_path=file_path,
        )
        if hooks is None:
            validated.pop("hooks", None)
        else:
            validated["hooks"] = hooks

    if "mcpServers" in validated:
        mcp_servers = _validate_mcp_servers(
            validated.get("mcpServers"),
            "mcpServers",
            issues,
            file_path=file_path,
            source=source,
        )
        if mcp_servers is None:
            validated.pop("mcpServers", None)
        else:
            validated["mcpServers"] = mcp_servers

    return validated, tuple(issues)


def validate_global_config_payload(
    raw: Mapping[str, Any],
    *,
    defaults: Mapping[str, Any],
    file_path: str | None = None,
) -> tuple[Dict[str, Any], tuple[SchemaValidationIssue, ...]]:
    issues: list[SchemaValidationIssue] = []
    validated = deepcopy(dict(raw))

    _validate_int_field(
        validated,
        "numStartups",
        issues,
        file_path=file_path,
        minimum=0,
        fallback=defaults.get("numStartups"),
    )
    _validate_enum_field(
        validated,
        "theme",
        _SUPPORTED_THEMES,
        issues,
        file_path=file_path,
        normalize=lambda value: str(value).strip().lower(),
        fallback=defaults.get("theme"),
    )
    _validate_string_field(
        validated,
        "preferredNotifChannel",
        issues,
        file_path=file_path,
        fallback=defaults.get("preferredNotifChannel"),
    )
    _validate_bool_field(
        validated,
        "verbose",
        issues,
        file_path=file_path,
        fallback=defaults.get("verbose"),
    )
    _validate_bool_field(
        validated,
        "autoCompactEnabled",
        issues,
        file_path=file_path,
        fallback=defaults.get("autoCompactEnabled"),
    )
    _validate_bool_field(
        validated,
        "showTurnDuration",
        issues,
        file_path=file_path,
        fallback=defaults.get("showTurnDuration"),
    )
    _validate_string_dict_field(
        validated,
        "env",
        issues,
        file_path=file_path,
        fallback=defaults.get("env"),
    )
    _validate_dict_field(
        validated,
        "tipsHistory",
        issues,
        file_path=file_path,
        fallback=defaults.get("tipsHistory"),
    )
    _validate_int_field(
        validated,
        "memoryUsageCount",
        issues,
        file_path=file_path,
        minimum=0,
        fallback=defaults.get("memoryUsageCount"),
    )
    _validate_int_field(
        validated,
        "promptQueueUseCount",
        issues,
        file_path=file_path,
        minimum=0,
        fallback=defaults.get("promptQueueUseCount"),
    )
    _validate_bool_field(
        validated,
        "todoFeatureEnabled",
        issues,
        file_path=file_path,
        fallback=defaults.get("todoFeatureEnabled"),
    )
    _validate_bool_field(
        validated,
        "showExpandedTodos",
        issues,
        file_path=file_path,
        fallback=defaults.get("showExpandedTodos"),
    )
    _validate_int_field(
        validated,
        "messageIdleNotifThresholdMs",
        issues,
        file_path=file_path,
        minimum=0,
        fallback=defaults.get("messageIdleNotifThresholdMs"),
    )
    _validate_bool_field(
        validated,
        "autoConnectIde",
        issues,
        file_path=file_path,
        fallback=defaults.get("autoConnectIde"),
    )
    _validate_bool_field(
        validated,
        "autoInstallIdeExtension",
        issues,
        file_path=file_path,
        fallback=defaults.get("autoInstallIdeExtension"),
    )
    _validate_bool_field(
        validated,
        "fileCheckpointingEnabled",
        issues,
        file_path=file_path,
        fallback=defaults.get("fileCheckpointingEnabled"),
    )
    _validate_bool_field(
        validated,
        "terminalProgressBarEnabled",
        issues,
        file_path=file_path,
        fallback=defaults.get("terminalProgressBarEnabled"),
    )
    _validate_bool_field(
        validated,
        "respectGitignore",
        issues,
        file_path=file_path,
        fallback=defaults.get("respectGitignore"),
    )
    _validate_bool_field(
        validated,
        "copyFullResponse",
        issues,
        file_path=file_path,
        fallback=defaults.get("copyFullResponse"),
    )

    if "cachedStatsigGates" in validated:
        gates = _validate_bool_dict(
            validated.get("cachedStatsigGates"),
            "cachedStatsigGates",
            issues,
            file_path=file_path,
        )
        if gates is None:
            validated["cachedStatsigGates"] = deepcopy(
                defaults.get("cachedStatsigGates", {})
            )
        else:
            validated["cachedStatsigGates"] = gates
    elif "cachedStatsigGates" in defaults:
        validated["cachedStatsigGates"] = deepcopy(defaults["cachedStatsigGates"])

    for key, default_value in defaults.items():
        if key not in validated:
            validated[key] = deepcopy(default_value)

    return validated, tuple(issues)


def _validate_permissions_config(
    raw: object,
    base_path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> Dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=base_path,
                message="permissions must be an object",
                file=file_path,
            )
        )
        return None

    validated = deepcopy(dict(raw))
    for key in ("allow", "deny", "ask"):
        _validate_string_list_field(
            validated,
            key,
            issues,
            file_path=file_path,
            parent_path=base_path,
            dedupe=True,
        )
    _validate_enum_field(
        validated,
        "defaultMode",
        _SUPPORTED_PERMISSION_MODES,
        issues,
        file_path=file_path,
        parent_path=base_path,
    )
    return validated


def _validate_sandbox_config(
    raw: object,
    base_path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> Dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=base_path,
                message="sandbox must be an object",
                file=file_path,
            )
        )
        return None

    validated = deepcopy(dict(raw))
    for key in (
        "enabled",
        "failIfUnavailable",
        "autoAllowBashIfSandboxed",
        "allowUnsandboxedCommands",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
    ):
        _validate_bool_field(
            validated,
            key,
            issues,
            file_path=file_path,
            parent_path=base_path,
        )

    _validate_string_list_field(
        validated,
        "enabledPlatforms",
        issues,
        file_path=file_path,
        parent_path=base_path,
        dedupe=True,
    )
    _validate_string_list_field(
        validated,
        "excludedCommands",
        issues,
        file_path=file_path,
        parent_path=base_path,
        dedupe=True,
    )

    if "ignoreViolations" in validated:
        ignore_violations = validated.get("ignoreViolations")
        if not isinstance(ignore_violations, Mapping):
            issues.append(
                SchemaValidationIssue(
                    path=f"{base_path}.ignoreViolations",
                    message="ignoreViolations must be an object",
                    file=file_path,
                )
            )
            validated.pop("ignoreViolations", None)
        else:
            normalized_ignore: Dict[str, list[str]] = {}
            for key, value in ignore_violations.items():
                if not isinstance(key, str):
                    issues.append(
                        SchemaValidationIssue(
                            path=f"{base_path}.ignoreViolations",
                            message="ignoreViolations keys must be strings",
                            file=file_path,
                        )
                    )
                    continue
                normalized_ignore[key] = _normalize_string_sequence(
                    value,
                    f"{base_path}.ignoreViolations.{key}",
                    issues,
                    file_path=file_path,
                )
            validated["ignoreViolations"] = normalized_ignore

    if "network" in validated:
        network = validated.get("network")
        if not isinstance(network, Mapping):
            issues.append(
                SchemaValidationIssue(
                    path=f"{base_path}.network",
                    message="network must be an object",
                    file=file_path,
                )
            )
            validated.pop("network", None)
        else:
            normalized_network = deepcopy(dict(network))
            _validate_string_list_field(
                normalized_network,
                "allowedDomains",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.network",
                dedupe=True,
            )
            _validate_string_list_field(
                normalized_network,
                "allowUnixSockets",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.network",
                dedupe=True,
            )
            for key in (
                "allowManagedDomainsOnly",
                "allowAllUnixSockets",
                "allowLocalBinding",
            ):
                _validate_bool_field(
                    normalized_network,
                    key,
                    issues,
                    file_path=file_path,
                    parent_path=f"{base_path}.network",
                )
            for key in ("httpProxyPort", "socksProxyPort"):
                _validate_int_field(
                    normalized_network,
                    key,
                    issues,
                    file_path=file_path,
                    parent_path=f"{base_path}.network",
                    minimum=0,
                )
            validated["network"] = normalized_network

    if "filesystem" in validated:
        filesystem = validated.get("filesystem")
        if not isinstance(filesystem, Mapping):
            issues.append(
                SchemaValidationIssue(
                    path=f"{base_path}.filesystem",
                    message="filesystem must be an object",
                    file=file_path,
                )
            )
            validated.pop("filesystem", None)
        else:
            normalized_filesystem = deepcopy(dict(filesystem))
            for key in ("allowWrite", "denyWrite", "denyRead", "allowRead"):
                _validate_string_list_field(
                    normalized_filesystem,
                    key,
                    issues,
                    file_path=file_path,
                    parent_path=f"{base_path}.filesystem",
                )
            _validate_bool_field(
                normalized_filesystem,
                "allowManagedReadPathsOnly",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.filesystem",
            )
            validated["filesystem"] = normalized_filesystem

    if "ripgrep" in validated:
        ripgrep = validated.get("ripgrep")
        if not isinstance(ripgrep, Mapping):
            issues.append(
                SchemaValidationIssue(
                    path=f"{base_path}.ripgrep",
                    message="ripgrep must be an object",
                    file=file_path,
                )
            )
            validated.pop("ripgrep", None)
        else:
            normalized_ripgrep = deepcopy(dict(ripgrep))
            _validate_string_field(
                normalized_ripgrep,
                "command",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.ripgrep",
            )
            _validate_string_list_field(
                normalized_ripgrep,
                "args",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.ripgrep",
            )
            _validate_string_field(
                normalized_ripgrep,
                "argv0",
                issues,
                file_path=file_path,
                parent_path=f"{base_path}.ripgrep",
                allow_empty=False,
            )
            validated["ripgrep"] = normalized_ripgrep

    return validated


def _validate_hooks_config(
    raw: object,
    base_path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> Dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=base_path,
                message="hooks must be an object",
                file=file_path,
            )
        )
        return None

    validated: Dict[str, Any] = {}
    for event_name, value in raw.items():
        if not isinstance(event_name, str) or not event_name.strip():
            issues.append(
                SchemaValidationIssue(
                    path=base_path,
                    message="hook event names must be non-empty strings",
                    file=file_path,
                )
            )
            continue
        event_path = f"{base_path}.{event_name}"
        normalized = _validate_hook_event_value(
            value,
            event_path,
            issues,
            file_path=file_path,
        )
        if normalized is not None:
            validated[event_name] = normalized
    return validated


def _validate_hook_event_value(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> object | None:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        normalized_items: list[object] = []
        for index, item in enumerate(raw):
            normalized_item = _validate_hook_spec(
                item,
                f"{path}[{index}]",
                issues,
                file_path=file_path,
            )
            if normalized_item is not None:
                normalized_items.append(normalized_item)
        return normalized_items
    return _validate_hook_spec(raw, path, issues, file_path=file_path)


def _validate_hook_spec(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> object | None:
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            return stripped
        issues.append(
            SchemaValidationIssue(
                path=path,
                message="hook command must be a non-empty string",
                file=file_path,
            )
        )
        return None

    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=path,
                message="hook spec must be a string, object, or list",
                file=file_path,
            )
        )
        return None

    validated = deepcopy(dict(raw))
    if "enabled" in validated and not isinstance(validated["enabled"], bool):
        issues.append(
            SchemaValidationIssue(
                path=f"{path}.enabled",
                message="enabled must be a boolean",
                file=file_path,
            )
        )
        validated.pop("enabled", None)

    if "type" in validated:
        hook_type = validated.get("type")
        if not isinstance(hook_type, str) or hook_type not in {
            "command",
            "callback",
            "function",
            "prompt",
            "agent",
            "http",
        }:
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.type",
                    message=(
                        "type must be one of: command, callback, function, prompt, "
                        "agent, http"
                    ),
                    file=file_path,
                )
            )
            validated.pop("type", None)

    for key in ("command", "cmd", "shell", "callback", "function"):
        if key in validated:
            value = validated.get(key)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    SchemaValidationIssue(
                        path=f"{path}.{key}",
                        message=f"{key} must be a non-empty string",
                        file=file_path,
                    )
                )
                validated.pop(key, None)
            else:
                validated[key] = value.strip()

    for key in ("prompt", "url", "model", "if", "statusMessage"):
        if key in validated:
            value = validated.get(key)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    SchemaValidationIssue(
                        path=f"{path}.{key}",
                        message=f"{key} must be a non-empty string",
                        file=file_path,
                    )
                )
                validated.pop(key, None)
            else:
                validated[key] = value.strip()

    if "timeout" in validated:
        timeout_value = validated.get("timeout")
        if not isinstance(timeout_value, (int, float)) or isinstance(timeout_value, bool):
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.timeout",
                    message="timeout must be a positive number",
                    file=file_path,
                )
            )
            validated.pop("timeout", None)
        elif float(timeout_value) <= 0:
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.timeout",
                    message="timeout must be a positive number",
                    file=file_path,
                )
            )
            validated.pop("timeout", None)
        else:
            validated["timeout"] = float(timeout_value)

    if "once" in validated and not isinstance(validated["once"], bool):
        issues.append(
            SchemaValidationIssue(
                path=f"{path}.once",
                message="once must be a boolean",
                file=file_path,
            )
        )
        validated.pop("once", None)

    if "headers" in validated:
        headers = validated.get("headers")
        if not isinstance(headers, Mapping):
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.headers",
                    message="headers must be an object of string values",
                    file=file_path,
                )
            )
            validated.pop("headers", None)
        else:
            normalized_headers: Dict[str, str] = {}
            for key, value in headers.items():
                if not isinstance(key, str) or not key.strip():
                    issues.append(
                        SchemaValidationIssue(
                            path=f"{path}.headers",
                            message="headers keys must be non-empty strings",
                            file=file_path,
                        )
                    )
                    continue
                if not isinstance(value, str):
                    issues.append(
                        SchemaValidationIssue(
                            path=f"{path}.headers.{key}",
                            message="value must be a string",
                            file=file_path,
                        )
                    )
                    continue
                normalized_headers[key.strip()] = value
            validated["headers"] = normalized_headers

    if "allowedEnvVars" in validated:
        normalized_env = _normalize_string_sequence(
            validated.get("allowedEnvVars"),
            f"{path}.allowedEnvVars",
            issues,
            file_path=file_path,
        )
        if normalized_env is None:
            validated.pop("allowedEnvVars", None)
        else:
            validated["allowedEnvVars"] = _dedupe_preserve_order(normalized_env)

    return validated


def _validate_mcp_servers(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    source: str | None,
) -> Dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=path,
                message="mcpServers must be an object",
                file=file_path,
            )
        )
        return None

    parsed, validation_errors = parse_mcp_config(
        {"mcpServers": dict(raw)},
        expand_vars=False,
        scope=_settings_source_to_mcp_scope(source),
        file_path=file_path,
    )
    for validation_error in validation_errors:
        issues.append(
            SchemaValidationIssue(
                path=validation_error.path or path,
                message=validation_error.message,
                file=file_path,
            )
        )

    if parsed is None:
        return {}

    normalized_servers: Dict[str, Any] = {}
    for name, config in parsed.mcp_servers.items():
        normalized_servers[name] = asdict(config)
    return normalized_servers


def _validate_enabled_plugins(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> Dict[str, bool] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=path,
                message="enabledPlugins must be an object",
                file=file_path,
            )
        )
        return None

    validated: Dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            issues.append(
                SchemaValidationIssue(
                    path=path,
                    message="enabledPlugins keys must be non-empty strings",
                    file=file_path,
                )
            )
            continue
        if not isinstance(value, bool):
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.{key}",
                    message="plugin enablement must be a boolean",
                    file=file_path,
                )
            )
            continue
        validated[key] = value
    return validated


def _validate_bool_dict(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> Dict[str, bool] | None:
    if not isinstance(raw, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=path,
                message=f"{path.rsplit('.', 1)[-1]} must be an object",
                file=file_path,
            )
        )
        return None

    validated: Dict[str, bool] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            issues.append(
                SchemaValidationIssue(
                    path=path,
                    message="mapping keys must be non-empty strings",
                    file=file_path,
                )
            )
            continue
        if not isinstance(value, bool):
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}.{key}",
                    message="value must be a boolean",
                    file=file_path,
                )
            )
            continue
        validated[key] = value
    return validated


def _validate_dict_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    fallback: object | None = None,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    if isinstance(value, Mapping):
        target[key] = deepcopy(dict(value))
        return
    issues.append(
        SchemaValidationIssue(
            path=_field_path(key, parent_path),
            message=f"{key} must be an object",
            file=file_path,
        )
    )
    if fallback is not None:
        target[key] = deepcopy(fallback)
    else:
        target.pop(key, None)


def _validate_string_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    fallback: object | None = None,
    allow_empty: bool = False,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed or allow_empty:
            target[key] = trimmed if trimmed or not allow_empty else value
            return
    issues.append(
        SchemaValidationIssue(
            path=_field_path(key, parent_path),
            message=f"{key} must be a non-empty string",
            file=file_path,
        )
    )
    if fallback is not None:
        target[key] = deepcopy(fallback)
    else:
        target.pop(key, None)


def _validate_bool_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    fallback: object | None = None,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    if isinstance(target.get(key), bool):
        return
    issues.append(
        SchemaValidationIssue(
            path=_field_path(key, parent_path),
            message=f"{key} must be a boolean",
            file=file_path,
        )
    )
    if fallback is not None:
        target[key] = deepcopy(fallback)
    else:
        target.pop(key, None)


def _validate_int_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    minimum: int | None = None,
    fallback: object | None = None,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        if minimum is None or value >= minimum:
            return
    minimum_message = "" if minimum is None else f" >= {minimum}"
    issues.append(
        SchemaValidationIssue(
            path=_field_path(key, parent_path),
            message=f"{key} must be an integer{minimum_message}",
            file=file_path,
        )
    )
    if fallback is not None:
        target[key] = deepcopy(fallback)
    else:
        target.pop(key, None)


def _validate_enum_field(
    target: Dict[str, Any],
    key: str,
    allowed: set[str],
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    normalize=None,
    fallback: object | None = None,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    if isinstance(value, str):
        candidate = normalize(value) if callable(normalize) else value
        if candidate in allowed:
            target[key] = candidate
            return
    issues.append(
        SchemaValidationIssue(
            path=_field_path(key, parent_path),
            message=f"{key} must be one of: {', '.join(sorted(allowed))}",
            file=file_path,
        )
    )
    if fallback is not None:
        target[key] = deepcopy(fallback)
    else:
        target.pop(key, None)


def _validate_string_dict_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    fallback: object | None = None,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    if not isinstance(value, Mapping):
        issues.append(
            SchemaValidationIssue(
                path=_field_path(key, parent_path),
                message=f"{key} must be an object of string values",
                file=file_path,
            )
        )
        if fallback is not None:
            target[key] = deepcopy(fallback)
        else:
            target.pop(key, None)
        return

    normalized: Dict[str, str] = {}
    for child_key, child_value in value.items():
        child_path = f"{_field_path(key, parent_path)}.{child_key}"
        if not isinstance(child_key, str) or not child_key.strip():
            issues.append(
                SchemaValidationIssue(
                    path=_field_path(key, parent_path),
                    message=f"{key} keys must be non-empty strings",
                    file=file_path,
                )
            )
            continue
        if not isinstance(child_value, str):
            issues.append(
                SchemaValidationIssue(
                    path=child_path,
                    message="value must be a string",
                    file=file_path,
                )
            )
            continue
        normalized[child_key] = child_value
    target[key] = normalized


def _validate_string_list_field(
    target: Dict[str, Any],
    key: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
    parent_path: str | None = None,
    fallback: object | None = None,
    dedupe: bool = False,
) -> None:
    if key not in target:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        return
    value = target.get(key)
    path = _field_path(key, parent_path)
    normalized = _normalize_string_sequence(
        value,
        path,
        issues,
        file_path=file_path,
    )
    if normalized is None:
        if fallback is not None:
            target[key] = deepcopy(fallback)
        else:
            target.pop(key, None)
        return
    target[key] = _dedupe_preserve_order(normalized) if dedupe else normalized


def _normalize_string_sequence(
    raw: object,
    path: str,
    issues: list[SchemaValidationIssue],
    *,
    file_path: str | None,
) -> list[str] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        issues.append(
            SchemaValidationIssue(
                path=path,
                message="must be an array of strings",
                file=file_path,
            )
        )
        return None

    normalized: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            issues.append(
                SchemaValidationIssue(
                    path=f"{path}[{index}]",
                    message="must be a non-empty string",
                    file=file_path,
                )
            )
            continue
        normalized.append(item)
    return normalized


def _field_path(key: str, parent_path: str | None) -> str:
    return f"{parent_path}.{key}" if parent_path else key


def _settings_source_to_mcp_scope(source: str | None) -> str:
    if source == "userSettings":
        return "user"
    if source == "projectSettings":
        return "project"
    if source == "policySettings":
        return "managed"
    if source == "flagSettings":
        return "dynamic"
    return "local"


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
