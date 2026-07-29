"""
Permission mode definitions and helpers.

Port of src/utils/permissions/PermissionMode.ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Re-export from types
from ...types.permissions import (
    EXTERNAL_PERMISSION_MODES,
    INTERNAL_PERMISSION_MODES,
    PERMISSION_MODES,
)

VALID_MODES = set(PERMISSION_MODES)


def permission_mode_from_string(mode_str: str) -> str:
    """Convert a string to a valid PermissionMode, defaulting to 'default'."""
    if mode_str in VALID_MODES:
        return mode_str
    return "default"


def is_default_mode(mode: Optional[str]) -> bool:
    return mode == "default" or mode is None


@dataclass(frozen=True)
class ModeConfig:
    title: str
    short_title: str
    symbol: str
    color: str
    external: str


PERMISSION_MODE_CONFIG: dict[str, ModeConfig] = {
    "default": ModeConfig(
        title="Default",
        short_title="Default",
        symbol="",
        color="text",
        external="default",
    ),
    "plan": ModeConfig(
        title="Plan Mode",
        short_title="Plan",
        symbol="⏸",
        color="planMode",
        external="plan",
    ),
    "auto": ModeConfig(
        title="Auto",
        short_title="Auto",
        symbol="◌",
        color="autoAccept",
        external="auto",
    ),
    "acceptEdits": ModeConfig(
        title="Accept edits",
        short_title="Accept",
        symbol="⏵⏵",
        color="autoAccept",
        external="acceptEdits",
    ),
    "bypassPermissions": ModeConfig(
        title="Bypass Permissions",
        short_title="Bypass",
        symbol="⏵⏵",
        color="error",
        external="bypassPermissions",
    ),
    "dontAsk": ModeConfig(
        title="Don't Ask",
        short_title="DontAsk",
        symbol="⏵⏵",
        color="error",
        external="dontAsk",
    ),
    "bubble": ModeConfig(
        title="Bubble",
        short_title="Bubble",
        symbol="◌",
        color="planMode",
        external="default",
    ),
}


def _get_mode_config(mode: str) -> ModeConfig:
    return PERMISSION_MODE_CONFIG.get(mode, PERMISSION_MODE_CONFIG["default"])


def permission_mode_title(mode: str) -> str:
    return _get_mode_config(mode).title


def permission_mode_short_title(mode: str) -> str:
    return _get_mode_config(mode).short_title


def to_external_permission_mode(mode: str) -> str:
    return _get_mode_config(mode).external
