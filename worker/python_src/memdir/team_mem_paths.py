from __future__ import annotations

import os
import subprocess

from ..utils.config import get_claude_config_home

MAX_SANITIZED_LENGTH = 200
_GIT_TIMEOUT_SECONDS = 5.0


def _to_base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = []
    current = value
    while current > 0:
        current, remainder = divmod(current, 36)
        result.append(digits[remainder])
    return "".join(reversed(result))


def _djb2_hash(value: str) -> int:
    hashed = 5381
    for char in value:
        hashed = ((hashed << 5) + hashed) + ord(char)
    return abs(hashed)


def sanitize_path(name: str) -> str:
    sanitized = "".join(
        char if char.isalnum() else "-"
        for char in name
    )
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return "{}-{}".format(
        sanitized[:MAX_SANITIZED_LENGTH],
        _to_base36(_djb2_hash(name)),
    )


def _find_canonical_git_root(project_root: str) -> str | None:
    try:
        probe = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    resolved = probe.stdout.strip()
    if not resolved:
        return None
    return os.path.realpath(os.path.abspath(resolved))


def _canonical_project_root(project_root: str | None = None) -> str:
    base = os.path.realpath(os.path.abspath(project_root or os.getcwd()))
    return _find_canonical_git_root(base) or base


def get_projects_dir(*, config_home: str | None = None) -> str:
    return os.path.join(config_home or get_claude_config_home(), "projects")


def get_auto_mem_path(
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> str:
    return os.path.join(
        get_projects_dir(config_home=config_home),
        sanitize_path(_canonical_project_root(project_root)),
        "memory",
    )


def get_team_mem_path(
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> str:
    return os.path.join(
        get_auto_mem_path(project_root=project_root, config_home=config_home),
        "team",
    )


def is_team_mem_path(
    file_path: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> bool:
    candidate = os.path.realpath(os.path.abspath(file_path))
    team_dir = os.path.realpath(
        os.path.abspath(
            get_team_mem_path(project_root=project_root, config_home=config_home)
        )
    )
    try:
        return os.path.commonpath((team_dir, candidate)) == team_dir
    except ValueError:
        return False


def is_team_mem_file(
    file_path: str,
    *,
    project_root: str | None = None,
    config_home: str | None = None,
) -> bool:
    return is_team_mem_path(
        file_path,
        project_root=project_root,
        config_home=config_home,
    )
