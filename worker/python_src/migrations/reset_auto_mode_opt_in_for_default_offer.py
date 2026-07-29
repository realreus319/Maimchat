from __future__ import annotations

from ._shared import (
    MigrationContext,
    default_context,
    get_global_config,
    get_user_settings,
    save_global,
    write_user_settings,
)


def reset_auto_mode_opt_in_for_default_offer(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    if not migration_context.transcript_classifier_enabled:
        return False
    config = get_global_config(config_home=config_home)
    if config.get("hasResetAutoModeOptInForDefaultOffer") is True:
        return False
    settings = get_user_settings(config_home=config_home, project_root=project_root)
    permissions = settings.get("permissions") or {}
    if migration_context.auto_mode_enabled_state != "enabled":
        return False
    if settings.get("skipAutoPermissionPrompt") is not True:
        return False
    if permissions.get("defaultMode") == "auto":
        return False
    write_user_settings(
        {"skipAutoPermissionPrompt": None},
        config_home=config_home,
        project_root=project_root,
    )
    save_global(
        lambda current: {**current, "hasResetAutoModeOptInForDefaultOffer": True},
        config_home=config_home,
    )
    return True
