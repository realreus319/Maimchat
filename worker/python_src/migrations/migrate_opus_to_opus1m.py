from __future__ import annotations

from ._shared import (
    MigrationContext,
    default_context,
    get_user_settings,
    write_user_settings,
)


def migrate_opus_to_opus1m(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    if not migration_context.opus1m_merge_enabled:
        return False
    model = get_user_settings(config_home=config_home, project_root=project_root).get(
        "model"
    )
    if model != "opus":
        return False
    write_user_settings(
        {"model": "opus[1m]"}, config_home=config_home, project_root=project_root
    )
    return True
