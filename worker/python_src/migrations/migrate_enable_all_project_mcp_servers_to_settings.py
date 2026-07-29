from __future__ import annotations

from ._shared import (
    get_local_settings,
    read_project_config,
    write_local_settings,
    write_project_config,
)


def migrate_enable_all_project_mcp_servers_to_settings(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> bool:
    project_config = read_project_config(project_root)
    has_any = (
        project_config.get("enableAllProjectMcpServers") is not None
        or bool(project_config.get("enabledMcpjsonServers"))
        or bool(project_config.get("disabledMcpjsonServers"))
    )
    if not has_any:
        return False

    local_settings = get_local_settings(
        config_home=config_home, project_root=project_root
    )
    updates = {}
    if project_config.get("enableAllProjectMcpServers") is not None:
        updates["enableAllProjectMcpServers"] = project_config[
            "enableAllProjectMcpServers"
        ]
    if project_config.get("enabledMcpjsonServers"):
        updates["enabledMcpjsonServers"] = list(
            dict.fromkeys(
                list(local_settings.get("enabledMcpjsonServers") or [])
                + list(project_config.get("enabledMcpjsonServers") or [])
            )
        )
    if project_config.get("disabledMcpjsonServers"):
        updates["disabledMcpjsonServers"] = list(
            dict.fromkeys(
                list(local_settings.get("disabledMcpjsonServers") or [])
                + list(project_config.get("disabledMcpjsonServers") or [])
            )
        )
    if updates:
        write_local_settings(
            updates,
            config_home=config_home,
            project_root=project_root,
        )
    for key in (
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
        "disabledMcpjsonServers",
    ):
        project_config.pop(key, None)
    write_project_config(project_root, project_config)
    return True
