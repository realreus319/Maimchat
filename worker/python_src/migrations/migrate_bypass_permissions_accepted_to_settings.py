from __future__ import annotations

from ._shared import get_global_config, save_global, write_user_settings


def migrate_bypass_permissions_accepted_to_settings(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> bool:
    config = get_global_config(config_home=config_home)
    if config.get("bypassPermissionsModeAccepted") is not True:
        return False
    write_user_settings(
        {"skipDangerousModePermissionPrompt": True},
        config_home=config_home,
        project_root=project_root,
    )
    save_global(
        lambda current: {
            key: value
            for key, value in current.items()
            if key != "bypassPermissionsModeAccepted"
        },
        config_home=config_home,
    )
    return True
