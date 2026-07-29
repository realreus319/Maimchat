from __future__ import annotations

from ._shared import (
    current_main_loop_model_override,
    get_global_config,
    get_user_settings,
    save_global,
    set_main_loop_model_override,
    write_user_settings,
)

_TARGET_MODEL = "sonnet-4-5-20250929[1m]"


def migrate_sonnet1m_to_sonnet45(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> bool:
    config = get_global_config(config_home=config_home)
    if config.get("sonnet1m45MigrationComplete") is True:
        return False
    model = get_user_settings(config_home=config_home, project_root=project_root).get(
        "model"
    )
    changed = False
    if model == "sonnet[1m]":
        write_user_settings(
            {"model": _TARGET_MODEL},
            config_home=config_home,
            project_root=project_root,
        )
        changed = True
    if current_main_loop_model_override() == "sonnet[1m]":
        set_main_loop_model_override(_TARGET_MODEL)
        changed = True
    save_global(
        lambda current: {**current, "sonnet1m45MigrationComplete": True},
        config_home=config_home,
    )
    return changed or True
