from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "EDITABLE_SOURCES",
    "SETTING_SOURCES",
    "SettingsWithErrors",
    "ValidationError",
    "_deep_merge",
    "bind_app_state_settings_runtime_context",
    "get_enabled_setting_sources",
    "get_initial_settings",
    "get_setting_file_path",
    "get_settings_for_source",
    "get_settings_runtime_signature",
    "get_settings_with_errors",
    "load_settings_from_disk",
    "parse_settings_file",
    "parse_setting_sources_flag",
    "reset_settings_cache",
    "sync_app_state_settings_from_disk",
    "update_settings_for_source",
]

_SETTINGS_EXPORTS = {
    "SettingsWithErrors",
    "ValidationError",
    "_deep_merge",
    "bind_app_state_settings_runtime_context",
    "get_initial_settings",
    "get_settings_for_source",
    "get_settings_runtime_signature",
    "get_settings_with_errors",
    "load_settings_from_disk",
    "parse_settings_file",
    "reset_settings_cache",
    "sync_app_state_settings_from_disk",
    "update_settings_for_source",
}

_CONSTANT_EXPORTS = {
    "EDITABLE_SOURCES",
    "SETTING_SOURCES",
    "get_enabled_setting_sources",
    "get_setting_file_path",
    "parse_setting_sources_flag",
}


def __getattr__(name: str) -> Any:
    if name in _SETTINGS_EXPORTS:
        module = import_module(".settings", __name__)
        return getattr(module, name)
    if name in _CONSTANT_EXPORTS:
        module = import_module(".constants", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
