from __future__ import annotations

from ...memdir.team_mem_paths import is_team_mem_path
from .secret_scanner import scan_for_secrets


def check_team_mem_secrets(
    file_path: str,
    content: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> str | None:
    if not is_team_mem_path(
        file_path,
        project_root=project_root,
        config_home=config_home,
    ):
        return None

    matches = scan_for_secrets(content)
    if not matches:
        return None

    labels = ", ".join(match.label for match in matches)
    return (
        "Content contains potential secrets ({labels}) and cannot be written to team memory. "
        "Team memory is shared with all repository collaborators. "
        "Remove the sensitive content and try again."
    ).format(labels=labels)
