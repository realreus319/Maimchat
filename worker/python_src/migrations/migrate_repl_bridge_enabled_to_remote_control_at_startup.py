from __future__ import annotations

from ._shared import get_global_config, save_global


def migrate_repl_bridge_enabled_to_remote_control_at_startup(
    *,
    config_home: str | None = None,
) -> bool:
    config = get_global_config(config_home=config_home)
    if "replBridgeEnabled" not in config:
        return False
    if config.get("remoteControlAtStartup") is not None:
        return False
    old_value = bool(config.get("replBridgeEnabled"))
    save_global(
        lambda current: {
            **{
                key: value
                for key, value in current.items()
                if key != "replBridgeEnabled"
            },
            "remoteControlAtStartup": old_value,
        },
        config_home=config_home,
    )
    return True
