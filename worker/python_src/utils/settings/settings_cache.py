"""Session-level cache for parsed settings files and per-source results.

Python port of src/utils/settings/settingsCache.ts (subset).

Stores parsed file contents and per-source settings to avoid redundant I/O.
Call ``reset_settings_cache`` when any settings file changes on disk.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...bootstrap import getInlinePlugins
from .constants import get_enabled_setting_sources, get_setting_file_path

_PLUGIN_SETTINGS_ALLOWLIST = frozenset({"agent"})


class _SettingsCache:
    """Module-level singleton — one cache per process."""

    def __init__(self) -> None:
        self.parsed_files: Dict[str, Optional[Tuple[Any, List[Any]]]] = {}
        self.parsed_file_signatures: Dict[str, Tuple[bool, Optional[int], Optional[int]]] = {}
        self.source_cache: Dict[str, Optional[Any]] = {}
        self.session_cache: Optional[Any] = None
        self.plugin_settings_base: Optional[Dict[str, Any]] = None
        self.plugin_settings_signature: Optional[Tuple[Any, ...]] = None

    def reset(self) -> None:
        self.parsed_files.clear()
        self.parsed_file_signatures.clear()
        self.source_cache.clear()
        self.session_cache = None
        self.plugin_settings_base = None
        self.plugin_settings_signature = None


_CACHE = _SettingsCache()


def get_cached_parsed_file(
    path: str,
    *,
    signature: Tuple[bool, Optional[int], Optional[int]] | None = None,
) -> Optional[Tuple[Any, List[Any]]]:
    if signature is not None and _CACHE.parsed_file_signatures.get(path) != signature:
        return None
    return _CACHE.parsed_files.get(path)


def set_cached_parsed_file(
    path: str,
    result: Tuple[Any, List[Any]],
    *,
    signature: Tuple[bool, Optional[int], Optional[int]] | None = None,
) -> None:
    _CACHE.parsed_files[path] = result
    if signature is not None:
        _CACHE.parsed_file_signatures[path] = signature


def get_cached_settings_for_source(source: str) -> Optional[Any]:
    val = _CACHE.source_cache.get(source)
    return val  # None vs missing is intentional


def set_cached_settings_for_source(source: str, result: Any) -> None:
    _CACHE.source_cache[source] = result


def get_session_settings_cache() -> Optional[Any]:
    return _CACHE.session_cache


def set_session_settings_cache(result: Any) -> None:
    _CACHE.session_cache = result


def _parse_plugin_settings(raw: object) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    filtered: Dict[str, Any] = {}
    agent = raw.get("agent")
    if isinstance(agent, str) and agent.strip():
        filtered["agent"] = agent.strip()
    return filtered or None


def _read_json_object(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def _plugin_file_signature(path: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        stat_result = os.stat(path)
    except OSError:
        return (None, None)
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _inline_plugin_paths() -> Tuple[str, ...]:
    paths: list[str] = []
    for plugin_path in getInlinePlugins():
        if not isinstance(plugin_path, str) or not plugin_path.strip():
            continue
        paths.append(os.path.abspath(plugin_path))
    return tuple(dict.fromkeys(paths))


def _extract_enabled_plugins(raw: object) -> Dict[str, bool]:
    if not isinstance(raw, Mapping):
        return {}
    enabled_plugins = raw.get("enabledPlugins")
    if not isinstance(enabled_plugins, Mapping):
        return {}

    normalized: Dict[str, bool] = {}
    for key, value in enabled_plugins.items():
        if isinstance(key, str) and key.strip() and isinstance(value, bool):
            normalized[key.strip()] = value
    return normalized


def _enabled_plugin_sources_signature(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Tuple[Any, ...]:
    enabled_sources = tuple(
        get_enabled_setting_sources(allowed_sources=allowed_sources)
    )
    signature: list[Any] = [enabled_sources]
    for source in enabled_sources:
        file_path = get_setting_file_path(
            source,
            config_home=config_home,
            project_root=project_root,
            flag_path=flag_path,
        )
        if file_path is None:
            continue
        normalized = os.path.abspath(file_path)
        mtime_ns, size = _plugin_file_signature(normalized)
        signature.append((source, normalized, mtime_ns, size))

    inline_enabled = tuple(sorted(_extract_enabled_plugins(flag_inline).items()))
    signature.append(("flagInlineEnabledPlugins", inline_enabled))
    return tuple(signature)


def _resolve_enabled_plugin_overrides(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Dict[str, bool]:
    overrides: Dict[str, bool] = {}
    enabled_sources = get_enabled_setting_sources(allowed_sources=allowed_sources)
    for source in enabled_sources:
        file_path = get_setting_file_path(
            source,
            config_home=config_home,
            project_root=project_root,
            flag_path=flag_path,
        )
        if file_path is not None:
            raw = _read_json_object(file_path)
            if raw is not None:
                overrides.update(_extract_enabled_plugins(raw))
        if source == "flagSettings" and flag_inline is not None:
            overrides.update(_extract_enabled_plugins(flag_inline))
    return overrides


def _managed_plugin_paths(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Tuple[str, ...]:
    from ..plugin_registry import load_managed_plugin_records, local_plugin_id

    enabled_plugins = _resolve_enabled_plugin_overrides(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )
    records = load_managed_plugin_records(config_home=config_home)
    paths: list[str] = []
    for _, record in sorted(records.items()):
        if enabled_plugins.get(local_plugin_id(record.name), True):
            paths.append(os.path.abspath(record.install_path))
    return tuple(dict.fromkeys(paths))


def _all_plugin_paths(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Tuple[str, ...]:
    managed = _managed_plugin_paths(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )
    return tuple(dict.fromkeys((*managed, *_inline_plugin_paths())))


def _plugin_settings_signature(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Tuple[Any, ...]:
    from ..plugin_registry import get_plugins_file_path

    registry_path = os.path.abspath(get_plugins_file_path(config_home))
    registry_mtime, registry_size = _plugin_file_signature(registry_path)
    sources_signature = _enabled_plugin_sources_signature(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )
    plugin_paths = _all_plugin_paths(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )

    signature: list[Any] = [
        ("pluginRegistry", registry_path, registry_mtime, registry_size),
        sources_signature,
    ]
    for plugin_path in plugin_paths:
        settings_mtime, settings_size = _plugin_file_signature(
            os.path.join(plugin_path, "settings.json")
        )
        manifest_mtime, manifest_size = _plugin_file_signature(
            os.path.join(plugin_path, "plugin.json")
        )
        signature.append(
            (
                plugin_path,
                settings_mtime,
                settings_size,
                manifest_mtime,
                manifest_size,
            )
        )
    return tuple(signature)


def _load_plugin_settings_for_path(path: str) -> Optional[Dict[str, Any]]:
    settings_path = os.path.join(path, "settings.json")
    settings_json = _read_json_object(settings_path)
    parsed = _parse_plugin_settings(settings_json)
    if parsed is not None:
        return parsed

    manifest_path = os.path.join(path, "plugin.json")
    manifest = _read_json_object(manifest_path)
    if manifest is None:
        return None
    return _parse_plugin_settings(manifest.get("settings"))


def get_plugin_settings_base(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    allowed_sources: Sequence[str] | None = None,
    flag_inline: Mapping[str, object] | None = None,
) -> Optional[Dict]:
    signature = _plugin_settings_signature(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )
    if signature == _CACHE.plugin_settings_signature:
        return deepcopy(_CACHE.plugin_settings_base)

    merged: Dict[str, Any] = {}
    for plugin_path in _all_plugin_paths(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    ):
        plugin_settings = _load_plugin_settings_for_path(plugin_path)
        if plugin_settings:
            merged.update(plugin_settings)

    _CACHE.plugin_settings_signature = signature
    _CACHE.plugin_settings_base = merged or None
    return deepcopy(_CACHE.plugin_settings_base)


def reset_settings_cache() -> None:
    _CACHE.reset()
