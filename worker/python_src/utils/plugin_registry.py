from __future__ import annotations

import enum
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..plugins.builtin_plugins import (
    LoadedPlugin,
    PluginError,
    PluginLoadResult,
    get_plugin_error_message,
    load_plugin_directory,
)

LOCAL_MARKETPLACE_NAME = "local"
PLUGINS_FILE_NAME = "plugins.json"
MANAGED_PLUGINS_DIR_NAME = "plugins"


class PluginScope(enum.Enum):
    """Scope for plugin installation (mirrors TS PluginScopeSchema).

    Priority (lowest to highest):
        MANAGED  – enterprise/system-wide, read-only
        USER     – user's global settings (~/.claude_py/)
        PROJECT  – shared project settings ($project/.claude_py/)
        LOCAL    – personal project overrides ($project/.claude_py/settings.local.json)

    Higher numeric value = higher priority during resolution.
    """

    MANAGED = 0
    USER = 1
    PROJECT = 2
    LOCAL = 3


# Ordered from lowest to highest priority – used by resolution logic.
_PLUGIN_SCOPE_PRIORITY: List[PluginScope] = [
    PluginScope.MANAGED,
    PluginScope.USER,
    PluginScope.PROJECT,
    PluginScope.LOCAL,
]


def local_plugin_id(name: str) -> str:
    return "{}@{}".format(name, LOCAL_MARKETPLACE_NAME)


def get_plugins_file_path(config_home: str | None = None) -> str:
    return os.path.join(_resolve_config_home(config_home), PLUGINS_FILE_NAME)


def get_managed_plugins_dir(config_home: str | None = None) -> str:
    return os.path.join(_resolve_config_home(config_home), MANAGED_PLUGINS_DIR_NAME)


@dataclass(frozen=True)
class InstalledPluginRecord:
    name: str
    install_path: str
    description: str = ""
    version: Optional[str] = None
    source_path: Optional[str] = None
    scope: PluginScope = PluginScope.USER

    @property
    def lookup_key(self) -> str:
        return self.name

    @property
    def plugin_id(self) -> str:
        return local_plugin_id(self.name)

    def to_registry_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "installPath": self.install_path,
            "description": self.description,
            "scope": self.scope.name.lower(),
        }
        if self.version is not None:
            payload["version"] = self.version
        if self.source_path is not None:
            payload["sourcePath"] = self.source_path
        return payload

    def to_public_payload(self, *, enabled: bool) -> Dict[str, Any]:
        payload = self.to_registry_payload()
        payload["enabled"] = enabled
        payload["pluginId"] = self.plugin_id
        payload["source"] = LOCAL_MARKETPLACE_NAME
        return payload


def load_managed_plugin_records(
    *,
    config_home: str | None = None,
) -> Dict[str, InstalledPluginRecord]:
    path = get_plugins_file_path(config_home)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    records_raw = raw.get("plugins") if isinstance(raw, Mapping) else None
    if not isinstance(records_raw, Mapping):
        return {}

    records: Dict[str, InstalledPluginRecord] = {}
    for value in records_raw.values():
        record = _record_from_payload(value)
        if record is not None:
            records[record.lookup_key] = record
    return records


def save_managed_plugin_records(
    records: Mapping[str, InstalledPluginRecord],
    *,
    config_home: str | None = None,
) -> None:
    path = get_plugins_file_path(config_home)
    payload = {
        "plugins": {
            name: records[name].to_registry_payload()
            for name in sorted(records)
        }
    }
    _write_json_file(path, payload)


def validate_plugin_directory(path: str) -> LoadedPlugin:
    resolved = os.path.abspath(path)
    result = load_plugin_directory(resolved, source=LOCAL_MARKETPLACE_NAME)
    if result.errors:
        raise ValueError(get_plugin_error_message(result.errors[0]))
    if not result.enabled:
        raise ValueError("Plugin manifest did not produce a loadable plugin")
    return result.enabled[0]


def install_plugin_from_directory(
    path: str,
    *,
    config_home: str | None = None,
    needs_refresh: bool = True,
    scope: PluginScope = PluginScope.USER,
) -> InstalledPluginRecord:
    plugin = validate_plugin_directory(path)
    normalized_source = os.path.abspath(path)
    install_path = os.path.join(
        get_managed_plugins_dir(config_home),
        _slugify_plugin_name(plugin.name),
    )
    _stage_plugin_directory(normalized_source, install_path)
    record = InstalledPluginRecord(
        name=plugin.name,
        install_path=install_path,
        description=_manifest_description(plugin.manifest),
        version=_manifest_version(plugin.manifest),
        source_path=normalized_source,
        scope=scope,
    )
    records = load_managed_plugin_records(config_home=config_home)
    records[record.lookup_key] = record
    save_managed_plugin_records(records, config_home=config_home)
    _set_plugin_enabled_flag(record.name, True, config_home=config_home)
    if needs_refresh:
        _mark_plugins_for_refresh(config_home=config_home)
    return record


def _mark_plugins_for_refresh(*, config_home: str | None = None) -> None:
    from .settings import update_settings_for_source
    update_settings_for_source(
        "userSettings",
        {"_plugins_needs_refresh": True},
        config_home=config_home,
    )


def get_installed_plugin(
    name: str,
    *,
    config_home: str | None = None,
) -> Optional[InstalledPluginRecord]:
    normalized_name = name.strip()
    if not normalized_name:
        return None
    return load_managed_plugin_records(config_home=config_home).get(normalized_name)


def update_installed_plugin(
    name: str,
    *,
    config_home: str | None = None,
) -> InstalledPluginRecord:
    record = get_installed_plugin(name, config_home=config_home)
    if record is None:
        raise ValueError("Plugin not found: {}".format(name))
    if not isinstance(record.source_path, str) or not record.source_path.strip():
        raise ValueError(
            "Plugin {} does not have a recorded source path for update".format(name)
        )

    current_enabled = _is_plugin_enabled(record.name, config_home=config_home)
    updated_plugin = validate_plugin_directory(record.source_path)
    if updated_plugin.name != record.name:
        raise ValueError(
            "Updated plugin name mismatch: expected {}, got {}".format(
                record.name,
                updated_plugin.name,
            )
        )

    updated = install_plugin_from_directory(record.source_path, config_home=config_home)
    if not current_enabled:
        _set_plugin_enabled_flag(record.name, False, config_home=config_home)
    return updated


def find_plugin_reverse_dependents(
    name: str,
    *,
    config_home: str | None = None,
) -> list[str]:
    """Find plugins that may depend on the given plugin.

    Returns a list of plugin names that have a dependency relationship with
    the specified plugin. An empty list means no reverse dependencies found.
    """
    records = load_managed_plugin_records(config_home=config_home)
    dependents: list[str] = []

    # Check each plugin's manifest for dependencies on the target plugin
    for plugin_name, record in records.items():
        if plugin_name == name:
            continue
        try:
            manifest_path = os.path.join(record.install_path, "plugin.json")
            if os.path.isfile(manifest_path):
                with open(manifest_path, encoding="utf-8") as f:
                    import json
                    manifest = json.load(f)
                deps = manifest.get("dependencies", [])
                if isinstance(deps, list) and name in deps:
                    dependents.append(plugin_name)
        except (OSError, json.JSONDecodeError):
            continue

    return dependents


def uninstall_installed_plugin(
    name: str,
    *,
    config_home: str | None = None,
    force: bool = False,
) -> InstalledPluginRecord:
    record = get_installed_plugin(name, config_home=config_home)
    if record is None:
        raise ValueError("Plugin not found: {}".format(name))

    dependents = find_plugin_reverse_dependents(name, config_home=config_home)
    if dependents and not force:
        raise ValueError(
            "Plugin '{}' has reverse dependents: {} (use force=True to uninstall anyway)".format(
                name, ", ".join(dependents)
            )
        )

    records = load_managed_plugin_records(config_home=config_home)
    records.pop(record.lookup_key, None)
    save_managed_plugin_records(records, config_home=config_home)
    _set_plugin_enabled_flag(record.name, None, config_home=config_home)
    shutil.rmtree(record.install_path, ignore_errors=True)
    return record


def set_installed_plugin_enabled(
    name: str,
    enabled: bool,
    *,
    config_home: str | None = None,
) -> InstalledPluginRecord:
    record = get_installed_plugin(name, config_home=config_home)
    if record is None:
        raise ValueError("Plugin not found: {}".format(name))
    _set_plugin_enabled_flag(record.name, enabled, config_home=config_home)
    return record


def list_installed_plugins(
    *,
    settings: Optional[Mapping[str, object]] = None,
    config_home: str | None = None,
) -> Dict[str, Any]:
    resolved_settings = _resolve_settings(settings, config_home=config_home)
    all_records = load_managed_plugin_records(config_home=config_home)
    records = _resolve_plugin_by_scope(all_records)
    plugins = [
        record.to_public_payload(
            enabled=_is_plugin_enabled(record.name, settings=resolved_settings)
        )
        for _, record in sorted(records.items())
    ]
    return {
        "plugins": plugins,
        "errors": [],
    }


def format_plugin_listing(payload: Mapping[str, object]) -> str:
    raw_plugins = payload.get("plugins")
    if not isinstance(raw_plugins, list) or not raw_plugins:
        return "No local plugins installed.\n"

    lines = ["{} local plugins".format(len(raw_plugins))]
    for raw in raw_plugins:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name", "unknown"))
        description = str(raw.get("description", "")).strip()
        version = raw.get("version")
        state = "enabled" if raw.get("enabled") else "disabled"
        line = "- {} [{}]".format(name, state)
        if isinstance(version, str) and version.strip():
            line += " · {}".format(version.strip())
        if description:
            line += " · {}".format(description)
        lines.append(line)
    return "\n".join(lines) + "\n"


def load_managed_plugins(
    *,
    settings: Optional[Mapping[str, object]] = None,
    config_home: str | None = None,
) -> PluginLoadResult:
    resolved_settings = _resolve_settings(settings, config_home=config_home)
    all_records = load_managed_plugin_records(config_home=config_home)
    records = _resolve_plugin_by_scope(all_records)
    result = PluginLoadResult()

    for _, record in sorted(records.items()):
        loaded = load_plugin_directory(record.install_path, source=LOCAL_MARKETPLACE_NAME)
        if loaded.errors:
            result.errors.extend(loaded.errors)
            continue
        if not loaded.enabled:
            result.errors.append(
                PluginError(
                    type="generic-error",
                    source=LOCAL_MARKETPLACE_NAME,
                    plugin=record.name,
                    path=record.install_path,
                    error="Plugin manifest did not produce a loadable plugin",
                )
            )
            continue
        plugin = loaded.enabled[0]
        runtime_plugin = LoadedPlugin(
            name=plugin.name,
            manifest=plugin.manifest,
            path=plugin.path,
            source=LOCAL_MARKETPLACE_NAME,
            repository=record.plugin_id,
            enabled=_is_plugin_enabled(record.name, settings=resolved_settings),
            is_builtin=False,
            hooks_config=plugin.hooks_config,
            mcp_servers=plugin.mcp_servers,
        )
        if runtime_plugin.enabled is False:
            result.disabled.append(runtime_plugin)
        else:
            result.enabled.append(runtime_plugin)
    return result


def build_managed_plugins_runtime_state(
    *,
    settings: Optional[Mapping[str, object]] = None,
    config_home: str | None = None,
) -> Dict[str, Any]:
    result = load_managed_plugins(settings=settings, config_home=config_home)
    names = sorted(
        {
            plugin.name
            for plugin in (*result.enabled, *result.disabled)
        }
    )
    return {
        "enabled": result.enabled,
        "disabled": result.disabled,
        "commands": [],
        "errors": result.errors,
        "installationStatus": {
            "marketplaces": [LOCAL_MARKETPLACE_NAME] if names else [],
            "plugins": names,
        },
        "needsRefresh": False,
    }


def get_managed_plugins_runtime_signature(
    *,
    settings: Optional[Mapping[str, object]] = None,
    config_home: str | None = None,
) -> tuple[object, ...]:
    resolved_settings = _resolve_settings(settings, config_home=config_home)
    records = load_managed_plugin_records(config_home=config_home)
    signature: list[object] = [
        ("config_home", os.path.abspath(_resolve_config_home(config_home))),
        ("registry", _path_signature(get_plugins_file_path(config_home))),
    ]
    for _, record in sorted(records.items()):
        signature.append(
            (
                record.name,
                os.path.abspath(record.install_path),
                _plugin_enabled_for_settings(record.name, resolved_settings),
                _path_signature(os.path.join(record.install_path, "plugin.json")),
            )
        )
    return tuple(signature)


def sync_managed_plugins_runtime_state(
    app_state: object,
    *,
    force: bool = False,
    config_home: str | None = None,
) -> bool:
    settings = getattr(app_state, "settings", None)
    resolved_settings = settings if isinstance(settings, Mapping) else {}
    records = load_managed_plugin_records(config_home=config_home)
    next_signature = get_managed_plugins_runtime_signature(
        settings=resolved_settings,
        config_home=config_home,
    )
    current_signature = getattr(app_state, "_managed_plugins_runtime_signature", None)
    if not force and current_signature == next_signature:
        return False
    setattr(app_state, "_managed_plugins_runtime_signature", next_signature)
    managed_runtime_state = build_managed_plugins_runtime_state(
        settings=resolved_settings,
        config_home=config_home,
    )
    setattr(
        app_state,
        "plugins",
        _merge_managed_plugins_runtime_state(
            getattr(app_state, "plugins", None),
            managed_runtime_state,
            records=records,
        ),
    )
    return True


def _parse_scope(raw: object) -> PluginScope:
    if isinstance(raw, str) and raw.strip():
        normalized = raw.strip().upper()
        try:
            return PluginScope[normalized]
        except KeyError:
            pass
    if isinstance(raw, (int, float)):
        try:
            return PluginScope(int(raw))
        except ValueError:
            pass
    return PluginScope.USER


def _resolve_plugin_by_scope(
    records: Mapping[str, InstalledPluginRecord],
) -> Dict[str, InstalledPluginRecord]:
    """Merge same-named plugins across scopes, keeping highest priority."""
    by_name: Dict[str, List[InstalledPluginRecord]] = {}
    for record in records.values():
        by_name.setdefault(record.name, []).append(record)
    resolved: Dict[str, InstalledPluginRecord] = {}
    for name, candidates in by_name.items():
        winner = max(candidates, key=lambda r: r.scope.value)
        resolved[name] = winner
    return resolved


def _record_from_payload(raw: object) -> Optional[InstalledPluginRecord]:
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("name")
    install_path = raw.get("installPath")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(install_path, str) or not install_path.strip():
        return None
    description = raw.get("description")
    version = raw.get("version")
    source_path = raw.get("sourcePath")
    scope = _parse_scope(raw.get("scope"))
    return InstalledPluginRecord(
        name=name.strip(),
        install_path=os.path.abspath(install_path),
        description=description.strip() if isinstance(description, str) else "",
        version=version.strip() if isinstance(version, str) and version.strip() else None,
        source_path=(
            os.path.abspath(source_path)
            if isinstance(source_path, str) and source_path.strip()
            else None
        ),
        scope=scope,
    )


def _resolve_config_home(config_home: str | None) -> str:
    if config_home:
        return config_home
    from .config import get_claude_config_home

    return get_claude_config_home()


def _resolve_settings(
    settings: Optional[Mapping[str, object]],
    *,
    config_home: str | None = None,
) -> Mapping[str, object]:
    if settings is not None:
        return settings
    from .settings import get_initial_settings

    return get_initial_settings(config_home=config_home)


def _is_plugin_enabled(
    name: str,
    *,
    settings: Optional[Mapping[str, object]] = None,
    config_home: str | None = None,
) -> bool:
    resolved_settings = _resolve_settings(settings, config_home=config_home)
    return _plugin_enabled_for_settings(name, resolved_settings)


def _plugin_enabled_for_settings(
    name: str,
    settings: Mapping[str, object],
) -> bool:
    enabled_plugins = settings.get("enabledPlugins")
    if not isinstance(enabled_plugins, Mapping):
        return True
    value = enabled_plugins.get(local_plugin_id(name))
    return True if not isinstance(value, bool) else value


def _set_plugin_enabled_flag(
    name: str,
    enabled: Optional[bool],
    *,
    config_home: str | None = None,
) -> None:
    from .settings import update_settings_for_source

    update_settings = {"enabledPlugins": {local_plugin_id(name): enabled}}
    update_settings_for_source(
        "userSettings",
        update_settings,
        config_home=config_home,
    )


def _merge_managed_plugins_runtime_state(
    existing_plugins: object,
    managed_plugins: Mapping[str, object],
    *,
    records: Mapping[str, InstalledPluginRecord],
) -> Dict[str, Any]:
    existing = existing_plugins if isinstance(existing_plugins, Mapping) else {}
    enabled = _preserve_non_managed_plugin_entries(existing.get("enabled"), records)
    enabled.extend(_coerce_sequence(managed_plugins.get("enabled")))
    disabled = _preserve_non_managed_plugin_entries(existing.get("disabled"), records)
    disabled.extend(_coerce_sequence(managed_plugins.get("disabled")))
    errors = _preserve_non_managed_plugin_errors(existing.get("errors"), records)
    errors.extend(_coerce_sequence(managed_plugins.get("errors")))
    commands = _coerce_sequence(existing.get("commands"))
    if not commands:
        commands = _coerce_sequence(managed_plugins.get("commands"))
    return {
        "enabled": enabled,
        "disabled": disabled,
        "commands": commands,
        "errors": errors,
        "installationStatus": _merge_installation_status(
            existing,
            managed_plugins,
        ),
        "needsRefresh": _coerce_bool(
            existing.get("needsRefresh"),
            default=_coerce_bool(managed_plugins.get("needsRefresh"), default=False),
        ),
    }


def _preserve_non_managed_plugin_entries(
    raw_entries: object,
    records: Mapping[str, InstalledPluginRecord],
) -> list[object]:
    return [
        entry
        for entry in _coerce_sequence(raw_entries)
        if not _is_managed_runtime_plugin_entry(entry, records)
    ]


def _preserve_non_managed_plugin_errors(
    raw_errors: object,
    records: Mapping[str, InstalledPluginRecord],
) -> list[object]:
    return [
        error
        for error in _coerce_sequence(raw_errors)
        if not _is_managed_plugin_error(error, records)
    ]


def _merge_installation_status(
    existing_plugins: Mapping[str, object],
    managed_plugins: Mapping[str, object],
) -> Dict[str, list[str]]:
    existing_status = existing_plugins.get("installationStatus")
    if not isinstance(existing_status, Mapping):
        existing_status = existing_plugins.get("installation_status")
    managed_status = managed_plugins.get("installationStatus")
    marketplaces = _merge_string_lists(
        _extract_status_list(existing_status, "marketplaces"),
        _extract_status_list(managed_status, "marketplaces"),
    )
    plugin_names = _merge_string_lists(
        _extract_status_list(existing_status, "plugins"),
        _extract_status_list(managed_status, "plugins"),
    )
    return {
        "marketplaces": marketplaces,
        "plugins": plugin_names,
    }


def _extract_status_list(
    status: object,
    key: str,
) -> list[str]:
    if not isinstance(status, Mapping):
        return []
    return [
        value.strip()
        for value in _coerce_sequence(status.get(key))
        if isinstance(value, str) and value.strip()
    ]


def _merge_string_lists(*lists: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for values in lists:
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _is_managed_runtime_plugin_entry(
    plugin: object,
    records: Mapping[str, InstalledPluginRecord],
) -> bool:
    name = _plugin_entry_name(plugin)
    if name is None:
        return False
    record = records.get(name)
    if record is None:
        return False
    path = _plugin_entry_path(plugin)
    if path is not None and path == os.path.abspath(record.install_path):
        return True
    repository = _plugin_entry_repository(plugin)
    return repository == record.plugin_id


def _is_managed_plugin_error(
    error: object,
    records: Mapping[str, InstalledPluginRecord],
) -> bool:
    plugin_name = _plugin_error_value(error, "plugin")
    if plugin_name is not None and plugin_name in records:
        source = _plugin_error_value(error, "source")
        if source in {None, "", LOCAL_MARKETPLACE_NAME}:
            return True
    path = _plugin_error_value(error, "path") or _plugin_error_value(error, "manifest_path")
    if path is None:
        return False
    normalized_path = os.path.abspath(path)
    for record in records.values():
        install_path = os.path.abspath(record.install_path)
        if normalized_path == install_path or normalized_path.startswith(install_path + os.sep):
            return True
    return False


def _plugin_entry_name(plugin: object) -> str | None:
    if isinstance(plugin, LoadedPlugin):
        raw_name = plugin.name
    elif isinstance(plugin, Mapping):
        raw_name = plugin.get("name")
    else:
        return None
    return raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None


def _plugin_entry_path(plugin: object) -> str | None:
    if isinstance(plugin, LoadedPlugin):
        raw_path = plugin.path
    elif isinstance(plugin, Mapping):
        raw_path = plugin.get("path")
    else:
        return None
    return os.path.abspath(raw_path) if isinstance(raw_path, str) and raw_path.strip() else None


def _plugin_entry_repository(plugin: object) -> str | None:
    if isinstance(plugin, LoadedPlugin):
        raw_repository = plugin.repository
    elif isinstance(plugin, Mapping):
        raw_repository = plugin.get("repository")
    else:
        return None
    return (
        raw_repository.strip()
        if isinstance(raw_repository, str) and raw_repository.strip()
        else None
    )


def _plugin_error_value(error: object, field: str) -> str | None:
    raw_value: object | None = None
    if isinstance(error, PluginError):
        raw_value = getattr(error, field, None)
    elif isinstance(error, Mapping):
        raw_value = error.get(field)
        if raw_value is None and field == "manifest_path":
            raw_value = error.get("manifestPath")
    return raw_value.strip() if isinstance(raw_value, str) and raw_value.strip() else None


def _coerce_sequence(raw: object) -> list[object]:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return list(raw)
    return []


def _coerce_bool(raw: object, *, default: bool) -> bool:
    return raw if isinstance(raw, bool) else default


def _manifest_description(manifest: Mapping[str, object]) -> str:
    description = manifest.get("description")
    return description.strip() if isinstance(description, str) else ""


def _manifest_version(manifest: Mapping[str, object]) -> Optional[str]:
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version.strip()


def _slugify_plugin_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())
    return slug.strip("-") or "plugin"


def _stage_plugin_directory(source_path: str, install_path: str) -> None:
    parent = os.path.dirname(install_path)
    os.makedirs(parent, exist_ok=True)
    tmp_parent = tempfile.mkdtemp(
        dir=parent,
        prefix=".plugin-install.",
    )
    staged_path = os.path.join(tmp_parent, os.path.basename(install_path))
    try:
        shutil.copytree(source_path, staged_path)
        if os.path.isdir(install_path):
            shutil.rmtree(install_path)
        elif os.path.exists(install_path):
            os.unlink(install_path)
        os.replace(staged_path, install_path)
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)


def _write_json_file(path: str, payload: Mapping[str, object]) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=parent,
        prefix=".plugins.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _path_signature(path: str) -> tuple[object, ...]:
    resolved = os.path.abspath(path)
    try:
        stat_result = os.stat(resolved)
    except OSError:
        return (resolved, None, None, None)
    return (
        resolved,
        stat_result.st_mtime_ns,
        stat_result.st_size,
        "dir" if os.path.isdir(resolved) else "file",
    )
