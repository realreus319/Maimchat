"""
Permission result types.

Port of src/utils/permissions/PermissionResult.ts.
"""

from __future__ import annotations

from ...types.permissions import (
    PermissionAllowDecision,
    PermissionAskDecision,
    PermissionDecision,
    PermissionDenyDecision,
    PermissionResult,
)


def get_rule_behavior_description(behavior: str) -> str:
    """Get prose description for a permission behavior."""
    if behavior == "allow":
        return "allowed"
    elif behavior == "deny":
        return "denied"
    else:
        return "asked for confirmation for"
