from __future__ import annotations

import time

from ._shared import MigrationContext, default_context, get_global_config, save_global
from ..utils.settings.settings import get_initial_settings


def reset_pro_to_opus_default(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    config = get_global_config(config_home=config_home)
    if config.get("opusProMigrationComplete") is True:
        return False
    settings = get_initial_settings(config_home=config_home, project_root=project_root)
    timestamp = None
    if (
        migration_context.api_provider == "firstParty"
        and migration_context.is_pro_subscriber
        and settings.get("model") is None
    ):
        timestamp = int(time.time() * 1000)

    def updater(current):
        next_config = {**current, "opusProMigrationComplete": True}
        if timestamp is not None:
            next_config["opusProMigrationTimestamp"] = timestamp
        return next_config

    save_global(updater, config_home=config_home)
    return True
