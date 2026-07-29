"""Setting source definitions and ordering.

Python port of src/utils/settings/constants.ts.

Defines the five setting sources in merge-priority order (low to high).
Later sources override earlier ones.  Policy settings use first-source-wins
internally but still participate in the merge chain at the correct position.

The ``--setting-sources`` CLI flag controls which of user/project/local are
included; policy and flag are always active.
"""

from __future__ import annotations

from typing import List, Sequence

from ..config import get_claude_config_home

# ---------------------------------------------------------------------------
# Setting source identifiers — order matters (low→high priority)
# ---------------------------------------------------------------------------

SETTING_SOURCES: List[str] = [
    "userSettings",  # ~/.claude_py/settings.json
    "projectSettings",  # $PWD/.claude_py/settings.json
    "localSettings",  # $PWD/.claude_py/settings.local.json
    "flagSettings",  # --settings CLI flag / SDK inline
    "policySettings",  # managed-settings.json + drop-ins
]

# Short names accepted by --setting-sources
_SHORT_NAMES = {
    "user": "userSettings",
    "project": "projectSettings",
    "local": "localSettings",
}

# Sources that are always included regardless of --setting-sources
_ALWAYS_INCLUDED = {"flagSettings", "policySettings"}

# Sources that can be edited by the user (excludes policy and flag)
EDITABLE_SOURCES = ("userSettings", "projectSettings", "localSettings")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def parse_setting_sources_flag(flag: str) -> List[str]:
    """Parse ``--setting-sources`` value into a list of source identifiers.

    Accepts comma-separated short names: user, project, local.
    Raises ``ValueError`` on unknown names.
    """
    if flag == "":
        return []
    result: List[str] = []
    for name in flag.split(","):
        key = name.strip()
        if key in _SHORT_NAMES:
            result.append(_SHORT_NAMES[key])
        else:
            valid = ", ".join(sorted(_SHORT_NAMES))
            raise ValueError(
                f"Invalid setting source: {key}. Valid options are: {valid}"
            )
    return result


def get_enabled_setting_sources(
    allowed_sources: Sequence[str] | None = None,
) -> List[str]:
    result_set = set(allowed_sources or SETTING_SOURCES)
    result_set |= _ALWAYS_INCLUDED
    # Preserve canonical ordering
    return [s for s in SETTING_SOURCES if s in result_set]


def get_setting_file_path(
    source: str,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
) -> str | None:
    """Return the settings file path for *source*, or ``None`` if N/A.

    Parameters
    ----------
    config_home:
        Override for ``~/.claude_py`` directory (tests only).
    project_root:
        Override for the project working directory.
    flag_path:
        Explicit path from ``--settings`` (only used for flagSettings).
    """
    import os

    if source == "userSettings":
        home = config_home or get_claude_config_home()
        return os.path.join(home, "settings.json")
    if source == "projectSettings":
        root = project_root or os.getcwd()
        return os.path.join(root, ".claude_py", "settings.json")
    if source == "localSettings":
        root = project_root or os.getcwd()
        return os.path.join(root, ".claude_py", "settings.local.json")
    if source == "policySettings":
        home = config_home or get_claude_config_home()
        return os.path.join(home, "managed-settings.json")
    if source == "flagSettings":
        return flag_path
    return None
