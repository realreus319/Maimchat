from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..skills.bundled_skills import BundledSkillDefinition, SkillCommand

BUILTIN_MARKETPLACE_NAME = "builtin"


@dataclass
class BuiltinPluginDefinition:
    name: str
    description: str
    version: Optional[str] = None
    skills: Sequence[BundledSkillDefinition] = field(default_factory=tuple)
    hooks: Optional[Mapping[str, object]] = None
    mcp_servers: Optional[Mapping[str, object]] = None
    is_available: Optional[Callable[[], bool]] = None
    default_enabled: bool = True


@dataclass
class LoadedPlugin:
    name: str
    manifest: Mapping[str, object]
    path: str
    source: str
    repository: str
    enabled: Optional[bool] = None
    is_builtin: Optional[bool] = None
    hooks_config: Optional[Mapping[str, object]] = None
    mcp_servers: Optional[Mapping[str, object]] = None


@dataclass
class PluginError:
    type: str
    source: str
    plugin: Optional[str] = None
    path: Optional[str] = None
    component: Optional[str] = None
    error: Optional[str] = None
    manifest_path: Optional[str] = None
    validation_errors: Sequence[str] = field(default_factory=tuple)


PLUGIN_ERROR_TYPES = (
    "path-not-found",
    "git-auth-failed",
    "git-timeout",
    "network-error",
    "manifest-parse-error",
    "manifest-validation-error",
    "plugin-not-found",
    "marketplace-not-found",
    "marketplace-load-failed",
    "mcp-config-invalid",
    "mcp-server-suppressed-duplicate",
    "lsp-config-invalid",
    "lsp-server-start-failed",
    "lsp-server-crashed",
    "lsp-request-timeout",
    "lsp-request-failed",
    "hook-load-failed",
    "component-load-failed",
    "mcpb-download-failed",
    "mcpb-extract-failed",
    "mcpb-invalid-manifest",
    "marketplace-blocked-by-policy",
    "dependency-unsatisfied",
    "plugin-cache-miss",
    "generic-error",
)


@dataclass
class PluginLoadResult:
    enabled: List[LoadedPlugin] = field(default_factory=list)
    disabled: List[LoadedPlugin] = field(default_factory=list)
    errors: List[PluginError] = field(default_factory=list)


_BUILTIN_PLUGINS: Dict[str, BuiltinPluginDefinition] = {}


def register_builtin_plugin(definition: BuiltinPluginDefinition) -> None:
    _BUILTIN_PLUGINS[definition.name] = definition


def clear_builtin_plugins() -> None:
    _BUILTIN_PLUGINS.clear()


def is_builtin_plugin_id(plugin_id: str) -> bool:
    return plugin_id.endswith("@{}".format(BUILTIN_MARKETPLACE_NAME))


def get_builtin_plugin_definition(name: str) -> Optional[BuiltinPluginDefinition]:
    return _BUILTIN_PLUGINS.get(name)


def get_builtin_plugins(
    settings: Optional[Mapping[str, object]] = None,
) -> Tuple[List[LoadedPlugin], List[LoadedPlugin]]:
    enabled: List[LoadedPlugin] = []
    disabled: List[LoadedPlugin] = []
    enabled_plugins = _extract_enabled_plugins(settings)
    for name, definition in _BUILTIN_PLUGINS.items():
        if definition.is_available is not None and not definition.is_available():
            continue
        plugin_id = "{}@{}".format(name, BUILTIN_MARKETPLACE_NAME)
        user_setting = enabled_plugins.get(plugin_id)
        is_enabled = (
            user_setting
            if isinstance(user_setting, bool)
            else definition.default_enabled
        )
        plugin = LoadedPlugin(
            name=name,
            manifest={
                "name": name,
                "description": definition.description,
                "version": definition.version,
            },
            path=BUILTIN_MARKETPLACE_NAME,
            source=plugin_id,
            repository=plugin_id,
            enabled=is_enabled,
            is_builtin=True,
            hooks_config=definition.hooks,
            mcp_servers=definition.mcp_servers,
        )
        if is_enabled:
            enabled.append(plugin)
        else:
            disabled.append(plugin)
    return enabled, disabled


def _extract_enabled_plugins(
    settings: Optional[Mapping[str, object]],
) -> Mapping[str, object]:
    if settings is None:
        return {}
    raw = settings.get("enabledPlugins")
    return raw if isinstance(raw, dict) else {}


def get_builtin_plugin_skill_commands(
    settings: Optional[Mapping[str, object]] = None,
) -> List[SkillCommand]:
    enabled, _ = get_builtin_plugins(settings)
    commands: List[SkillCommand] = []
    for plugin in enabled:
        definition = _BUILTIN_PLUGINS.get(plugin.name)
        if definition is None:
            continue
        for skill in definition.skills:
            commands.append(skill_definition_to_command(skill))
    return commands


def skill_definition_to_command(definition: BundledSkillDefinition) -> SkillCommand:
    return SkillCommand(
        name=definition.name,
        description=definition.description,
        aliases=tuple(definition.aliases),
        allowed_tools=tuple(definition.allowed_tools),
        argument_hint=definition.argument_hint,
        when_to_use=definition.when_to_use,
        model=definition.model,
        disable_model_invocation=definition.disable_model_invocation,
        user_invocable=definition.user_invocable,
        content_length=0,
        source="bundled",
        loaded_from="bundled",
        hooks=definition.hooks,
        context=definition.context,
        agent=definition.agent,
        is_enabled=definition.is_enabled,
        is_hidden=not definition.user_invocable,
        get_prompt_for_command=definition.get_prompt_for_command,
    )


def load_plugin_directory(path: str, source: str = "local") -> PluginLoadResult:
    result = PluginLoadResult()
    if not os.path.isdir(path):
        result.errors.append(
            PluginError(
                type="path-not-found",
                source=source,
                path=path,
                component="plugin",
            )
        )
        return result
    manifest_path = os.path.join(path, "plugin.json")
    if not os.path.isfile(manifest_path):
        result.errors.append(
            PluginError(
                type="path-not-found",
                source=source,
                path=manifest_path,
                component="plugin",
            )
        )
        return result
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except json.JSONDecodeError as exc:
        result.errors.append(
            PluginError(
                type="manifest-parse-error",
                source=source,
                manifest_path=manifest_path,
                error=str(exc),
            )
        )
        return result
    errors = _validate_manifest(manifest)
    if errors:
        result.errors.append(
            PluginError(
                type="manifest-validation-error",
                source=source,
                manifest_path=manifest_path,
                validation_errors=tuple(errors),
            )
        )
        return result
    plugin_name = str(manifest["name"])
    result.enabled.append(
        LoadedPlugin(
            name=plugin_name,
            manifest=manifest,
            path=path,
            source=source,
            repository=source,
            enabled=True,
            is_builtin=False,
            hooks_config=manifest.get("hooks") if isinstance(manifest, dict) else None,
            mcp_servers=manifest.get("mcpServers")
            if isinstance(manifest, dict)
            else None,
        )
    )
    return result


def _validate_manifest(manifest: Any) -> List[str]:
    if not isinstance(manifest, dict):
        return ["Manifest must contain a JSON object"]
    errors: List[str] = []
    for required_field in ("name", "description"):
        value = manifest.get(required_field)
        if not isinstance(value, str) or not value.strip():
            errors.append("{} must be a non-empty string".format(required_field))
    return errors


def get_plugin_error_message(error: PluginError) -> str:
    if error.type == "generic-error":
        return error.error or "Plugin error"
    if error.type == "path-not-found":
        return "Path not found: {} ({})".format(error.path, error.component)
    if error.type == "manifest-parse-error":
        return "Manifest parse error: {}".format(error.error)
    if error.type == "manifest-validation-error":
        return "Manifest validation failed: {}".format(
            ", ".join(error.validation_errors)
        )
    if error.type == "git-auth-failed":
        return "Git authentication failed: {}".format(error.error or error.plugin)
    if error.type == "git-timeout":
        return "Git operation timed out: {}".format(error.error or error.plugin)
    if error.type == "network-error":
        return "Network error: {}".format(error.error or error.plugin)
    if error.type == "plugin-not-found":
        return "Plugin not found: {}".format(error.plugin)
    if error.type == "marketplace-not-found":
        return "Marketplace not found: {}".format(error.error or error.plugin)
    if error.type == "marketplace-load-failed":
        return "Failed to load marketplace: {}".format(error.error)
    if error.type == "mcp-config-invalid":
        return "Invalid MCP configuration: {}".format(error.error)
    if error.type == "mcp-server-suppressed-duplicate":
        return "MCP server duplicate suppressed: {}".format(error.error or error.plugin)
    if error.type == "lsp-config-invalid":
        return "Invalid LSP configuration: {}".format(error.error)
    if error.type == "lsp-server-start-failed":
        return "Failed to start LSP server: {}".format(error.error)
    if error.type == "lsp-server-crashed":
        return "LSP server crashed: {}".format(error.error)
    if error.type == "lsp-request-timeout":
        return "LSP request timed out: {}".format(error.error)
    if error.type == "lsp-request-failed":
        return "LSP request failed: {}".format(error.error)
    if error.type == "hook-load-failed":
        return "Failed to load plugin hook: {}".format(error.error)
    if error.type == "component-load-failed":
        return "Failed to load plugin component: {}".format(error.error)
    if error.type == "mcpb-download-failed":
        return "Failed to download MCPB: {}".format(error.error)
    if error.type == "mcpb-extract-failed":
        return "Failed to extract MCPB: {}".format(error.error)
    if error.type == "mcpb-invalid-manifest":
        return "Invalid MCPB manifest: {}".format(error.error)
    if error.type == "marketplace-blocked-by-policy":
        return "Marketplace blocked by policy: {}".format(error.error or error.plugin)
    if error.type == "dependency-unsatisfied":
        return "Plugin dependency not satisfied: {}".format(error.error)
    if error.type == "plugin-cache-miss":
        return "Plugin cache miss: {}".format(error.plugin)
    return error.error or error.type
