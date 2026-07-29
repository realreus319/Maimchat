from __future__ import annotations

import time

from ._shared import (
    MigrationContext,
    default_context,
    get_user_settings,
    save_global,
    write_user_settings,
)

_LEGACY_OPUS_MODELS = {
    "claude-opus-4-20250514",
    "claude-opus-4-1-20250805",
    "claude-opus-4-0",
    "claude-opus-4-1",
}


def migrate_legacy_opus_to_current(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    if migration_context.api_provider != "firstParty":
        return False
    if not migration_context.legacy_model_remap_enabled:
        return False
    model = get_user_settings(config_home=config_home, project_root=project_root).get(
        "model"
    )
    if model not in _LEGACY_OPUS_MODELS:
        return False
    write_user_settings(
        {"model": "opus"}, config_home=config_home, project_root=project_root
    )
    save_global(
        lambda current: {
            **current,
            "legacyOpusMigrationTimestamp": int(time.time() * 1000),
        },
        config_home=config_home,
    )
    return True
