from __future__ import annotations

from ._shared import (
    MigrationContext,
    default_context,
    get_user_settings,
    write_user_settings,
)


def migrate_fennec_to_opus(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    context: MigrationContext | None = None,
) -> bool:
    migration_context = context or default_context()
    if migration_context.user_type != "ant":
        return False
    model = get_user_settings(config_home=config_home, project_root=project_root).get(
        "model"
    )
    if model == "fennec-latest[1m]":
        write_user_settings(
            {"model": "opus[1m]"}, config_home=config_home, project_root=project_root
        )
        return True
    if model == "fennec-latest":
        write_user_settings(
            {"model": "opus"}, config_home=config_home, project_root=project_root
        )
        return True
    if model in {"fennec-fast-latest", "opus-4-5-fast"}:
        write_user_settings(
            {"model": "opus[1m]", "fastMode": True},
            config_home=config_home,
            project_root=project_root,
        )
        return True
    return False
