from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from ..bootstrap import getMainLoopModelOverride, setMainLoopModelOverride
from ..utils.config import get_global_config_for_home, save_global_config
from ..utils.settings.settings import (
    get_settings_for_source,
    update_settings_for_source,
)


@dataclass(frozen=True)
class MigrationContext:
    api_provider: str = ""
    user_type: str = ""
    legacy_model_remap_enabled: bool = False
    opus1m_merge_enabled: bool = False
    transcript_classifier_enabled: bool = False
    auto_mode_enabled_state: str = "disabled"
    is_pro_subscriber: bool = False
    is_max_subscriber: bool = False
    is_team_premium_subscriber: bool = False


def default_context() -> MigrationContext:
    return MigrationContext(
        api_provider=os.environ.get("CLAUDE_CODE_API_PROVIDER", ""),
        user_type=os.environ.get("USER_TYPE", ""),
        legacy_model_remap_enabled=_env_bool("CLAUDE_CODE_LEGACY_MODEL_REMAP_ENABLED"),
        opus1m_merge_enabled=_env_bool("CLAUDE_CODE_OPUS1M_MERGE_ENABLED"),
        transcript_classifier_enabled=_env_bool(
            "CLAUDE_CODE_TRANSCRIPT_CLASSIFIER_ENABLED"
        ),
        auto_mode_enabled_state=os.environ.get(
            "CLAUDE_CODE_AUTO_MODE_ENABLED_STATE",
            "disabled",
        ),
        is_pro_subscriber=_env_bool("CLAUDE_CODE_IS_PRO_SUBSCRIBER"),
        is_max_subscriber=_env_bool("CLAUDE_CODE_IS_MAX_SUBSCRIBER"),
        is_team_premium_subscriber=_env_bool("CLAUDE_CODE_IS_TEAM_PREMIUM_SUBSCRIBER"),
    )


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def get_user_settings(
    *,
    config_home: str | None,
    project_root: str | None,
) -> Dict[str, Any]:
    return (
        get_settings_for_source(
            "userSettings",
            config_home=config_home,
            project_root=project_root,
        )
        or {}
    )


def get_local_settings(
    *,
    config_home: str | None,
    project_root: str | None,
) -> Dict[str, Any]:
    return (
        get_settings_for_source(
            "localSettings",
            config_home=config_home,
            project_root=project_root,
        )
        or {}
    )


def get_global_config(
    *,
    config_home: str | None,
) -> Dict[str, Any]:
    return get_global_config_for_home(config_home)


def save_global(
    updater,
    *,
    config_home: str | None,
) -> Dict[str, Any]:
    return save_global_config(updater, config_home=config_home)


def write_user_settings(
    updates: Dict[str, Any],
    *,
    config_home: str | None,
    project_root: str | None,
) -> Dict[str, Any]:
    return update_settings_for_source(
        "userSettings",
        updates,
        config_home=config_home,
        project_root=project_root,
    )


def write_local_settings(
    updates: Dict[str, Any],
    *,
    config_home: str | None,
    project_root: str | None,
) -> Dict[str, Any]:
    return update_settings_for_source(
        "localSettings",
        updates,
        config_home=config_home,
        project_root=project_root,
    )


def get_project_config_path(project_root: str | None) -> Path:
    root = project_root or os.getcwd()
    return Path(root) / ".claude_py" / "claude.json"


def read_project_config(project_root: str | None) -> Dict[str, Any]:
    path = get_project_config_path(project_root)
    try:
        content = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    if not content.strip():
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_project_config(project_root: str | None, data: Dict[str, Any]) -> None:
    path = get_project_config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def current_main_loop_model_override() -> str | None:
    return getMainLoopModelOverride()


def set_main_loop_model_override(value: str | None) -> None:
    setMainLoopModelOverride(value)
