"""Session-local permission denial tracking helpers."""

from __future__ import annotations

from typing import Any, Mapping

from ...types.permissions import PermissionDenyDecision, RuleDecisionReason
from .permission_rule_parser import permission_rule_value_to_string

# TS equivalent: maxConsecutive=3, maxTotal=20 (two-dimensional tracking)
DENIAL_LIMITS = {
    "max_consecutive": 3,
    "max_total": 20,
}


def normalize_denial_tracking_state(raw_state: object) -> dict[str, object]:
    if not isinstance(raw_state, Mapping):
        return {
            "last_signature": None,
            "consecutive_count": 0,
            "total_count": 0,
            "max_consecutive": DENIAL_LIMITS["max_consecutive"],
            "max_total": DENIAL_LIMITS["max_total"],
            # Backward compatibility keys
            "count": 0,
            "threshold": DENIAL_LIMITS["max_consecutive"],
        }

    last_signature = raw_state.get("last_signature")
    if not isinstance(last_signature, str) or not last_signature.strip():
        last_signature = None

    # New two-dimensional tracking keys
    consecutive_count = raw_state.get("consecutive_count")
    if not isinstance(consecutive_count, int) or consecutive_count < 0:
        consecutive_count = 0

    total_count = raw_state.get("total_count")
    if not isinstance(total_count, int) or total_count < 0:
        total_count = 0

    max_consecutive = raw_state.get("max_consecutive")
    if not isinstance(max_consecutive, int) or max_consecutive < 1:
        max_consecutive = DENIAL_LIMITS["max_consecutive"]

    max_total = raw_state.get("max_total")
    if not isinstance(max_total, int) or max_total < 1:
        max_total = DENIAL_LIMITS["max_total"]

    # Backward compatibility: migrate from old count/threshold if present
    count = raw_state.get("count")
    if isinstance(count, int) and count >= 0:
        # Migrate: use count as total_count if total_count is 0
        if total_count == 0 and consecutive_count == 0:
            total_count = count

    threshold = raw_state.get("threshold")
    if isinstance(threshold, int) and threshold >= 1:
        max_consecutive = threshold

    return {
        "last_signature": last_signature,
        "consecutive_count": consecutive_count,
        "total_count": total_count,
        "max_consecutive": max_consecutive,
        "max_total": max_total,
        # Backward compatibility keys
        "count": total_count,
        "threshold": max_consecutive,
    }


def build_denial_tracking_signature(
    tool_name: str,
    tool_input: Mapping[str, Any],
    decision: PermissionDenyDecision,
) -> str:
    parts = [tool_name]
    reason = decision.decision_reason
    if isinstance(reason, RuleDecisionReason):
        parts.append(
            permission_rule_value_to_string(
                reason.rule.rule_value.tool_name,
                reason.rule.rule_value.rule_content,
            )
        )
    elif decision.message:
        parts.append(decision.message.strip())

    for key in (
        "command",
        "cmd",
        "url",
        "path",
        "file_path",
        "filePath",
        "notebook_path",
        "notebookPath",
    ):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    return " | ".join(parts)


def record_permission_denial(
    raw_state: object,
    signature: str,
) -> dict[str, object]:
    state = normalize_denial_tracking_state(raw_state)
    # Always increment total_count
    state["total_count"] = int(state["total_count"]) + 1
    state["count"] = state["total_count"]

    if state["last_signature"] == signature:
        # Same signature: increment consecutive_count
        state["consecutive_count"] = int(state["consecutive_count"]) + 1
    else:
        # Different signature: reset consecutive_count to 1, update last_signature
        state["last_signature"] = signature
        state["consecutive_count"] = 1

    return state


def reset_denial_tracking_state(raw_state: object = None) -> dict[str, object]:
    state = normalize_denial_tracking_state(raw_state)
    state["last_signature"] = None
    state["consecutive_count"] = 0
    state["total_count"] = 0
    # Backward compatibility keys
    state["count"] = 0
    return state


def should_fallback_to_interactive_prompt(raw_state: object) -> bool:
    state = normalize_denial_tracking_state(raw_state)
    return (
        int(state["consecutive_count"]) >= int(state["max_consecutive"])
        or int(state["total_count"]) >= int(state["max_total"])
    )