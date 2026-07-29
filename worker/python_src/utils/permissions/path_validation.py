"""
Path validation utilities for permission checks.

Port of src/utils/permissions/pathValidation.ts. Provides tilde expansion
and dangerous-removal-path detection used by the permission pipeline.
"""

from __future__ import annotations

import os
import re
from os.path import dirname
from typing import Optional


def expand_tilde(path: str) -> str:
    """Expand ``~`` at the start of a path to the user's home directory.

    Mirrors the TS ``expandTilde`` — only bare ``~`` and ``~/`` (plus the
    Windows ``~\\\\``) forms are expanded.  ``~username`` is deliberately
    *not* expanded (security: avoids TOCTOU with shell expansion).
    """
    if path == "~" or path.startswith("~/") or path.startswith("~\\"):
        return os.path.expanduser(path)
    return path


_WINDOWS_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:/?$")
_WINDOWS_DRIVE_CHILD_RE = re.compile(r"^[A-Za-z]:/[^/]+$")


def is_dangerous_removal_path(resolved_path: str) -> bool:
    """Check if *resolved_path* is dangerous for ``rm``/``rmdir`` operations.

    Dangerous paths are:
    - Wildcard ``*`` or paths ending with ``/*``
    - Root directory ``/``
    - Home directory ``~``
    - Direct children of root (``/usr``, ``/tmp``, ``/etc``)
    - Windows drive roots (``C:\\``) and direct children (``C:\\Windows``)
    """
    # Collapse runs of separators (handles PowerShell C:\\Windows)
    forward = resolved_path.replace("\\", "/")
    while "//" in forward:
        forward = forward.replace("//", "/")

    if forward == "*" or forward.endswith("/*"):
        return True

    normalized = "/" if forward == "/" else forward.rstrip("/")

    if normalized == "/":
        return True

    if _WINDOWS_DRIVE_ROOT_RE.match(normalized):
        return True

    home = os.path.expanduser("~").replace("\\", "/")
    if normalized == home:
        return True

    # Direct children of root: /usr, /tmp, /etc (but not /usr/local)
    parent = dirname(normalized)
    if parent == "/":
        return True

    if _WINDOWS_DRIVE_CHILD_RE.match(normalized):
        return True

    return False
