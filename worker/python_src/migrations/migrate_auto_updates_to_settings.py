from __future__ import annotations

from ._shared import (
    get_global_config,
    get_user_settings,
    save_global,
    write_user_settings,
)


def migrate_auto_updates_to_settings(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> bool:
    config = get_global_config(config_home=config_home)
    if config.get("autoUpdates") is not False:
        return False
    if config.get("autoUpdatesProtectedForNative") is True:
        return False

    user_settings = get_user_settings(
        config_home=config_home, project_root=project_root
    )
    env_settings = dict(user_settings.get("env") or {})
    env_settings["DISABLE_AUTOUPDATER"] = "1"
    write_user_settings(
        {"env": env_settings},
        config_home=config_home,
        project_root=project_root,
    )
    save_global(
        lambda current: {
            key: value
            for key, value in current.items()
            if key not in {"autoUpdates", "autoUpdatesProtectedForNative"}
        },
        config_home=config_home,
    )
    return True
