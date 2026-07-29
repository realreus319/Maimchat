from __future__ import annotations

import time

from ._shared import (
    MigrationContext,
    default_context,
    get_global_config,
    get_user_settings,
    save_global,
    write_user_settings,
)

_SONNET_45_MODELS = {
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-5-20250929[1m]",
    "sonnet-4-5-20250929",
    "sonnet-4-5-20250929[1m]",
}


def migrate_sonnet45_to_sonnet46(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    if migration_context.api_provider != "firstParty":
        return False
    if not (
        migration_context.is_pro_subscriber
        or migration_context.is_max_subscriber
        or migration_context.is_team_premium_subscriber
    ):
        return False
    model = get_user_settings(config_home=config_home, project_root=project_root).get(
        "model"
    )
    if model not in _SONNET_45_MODELS:
        return False
    next_model = "sonnet[1m]" if str(model).endswith("[1m]") else "sonnet"
    write_user_settings(
        {"model": next_model}, config_home=config_home, project_root=project_root
    )
    global_config = get_global_config(config_home=config_home)
    if int(global_config.get("numStartups", 0) or 0) > 1:
        save_global(
            lambda current: {
                **current,
                "sonnet45To46MigrationTimestamp": int(time.time() * 1000),
            },
            config_home=config_home,
        )
    return True
