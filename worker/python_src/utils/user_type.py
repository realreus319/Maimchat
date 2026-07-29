from __future__ import annotations

import os


def get_user_type() -> str:
    """Return the CLAUDE_CODE_USER_TYPE from environment, defaulting to 'ant'."""
    return os.environ.get("CLAUDE_CODE_USER_TYPE", "ant")


def is_ant_user() -> bool:
    """Return True if the current user is an ant user (full-access)."""
    return get_user_type() == "ant"