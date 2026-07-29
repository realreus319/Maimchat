"""Permission mode transition helpers.

This mirrors the safety role of the TypeScript permission setup state machine:
when entering automated modes, broad Bash allow-rules are stripped and retained
so they can be restored when the session leaves those modes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, MutableMapping, Sequence

from .permission_rule_parser import permission_rule_value_from_string


AUTOMATED_PERMISSION_MODES = frozenset({"auto", "bubble"})
_RULE_SOURCE_KEYS = (
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
    "cliArg",
    "command",
    "session",
)
_MUTATING_BASH_PREFIXES = (
    "rm",
    "rmdir",
    "mv",
    "cp",
    "chmod",
    "chown",
    "chgrp",
    "touch",
    "ln",
    "mkdir",
    "git push",
    "git commit",
    "git reset",
    "git clean",
    "git checkout",
    "git switch",
    "npm",
    "pnpm",
    "yarn",
    "pip",
    "uv",
    "python",
    "python3",
    "bash",
    "sh",
    "sudo",
    "docker",
    "docker-compose",
)


@dataclass(frozen=True)
class PermissionTransitionState:
    mode: str
    context: dict[str, Any]
    stripped_dangerous_rules: dict[str, list[str]] = field(default_factory=dict)
    restored_rules: dict[str, list[str]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def transition_permission_mode(
    current_context_or_mode: Mapping[str, Any] | str | None,
    target_mode: str,
    permissions: Mapping[str, Sequence[str]] | None = None,
) -> PermissionTransitionState:
    """Transition a permission context into *target_mode*.

    The first argument accepts either a full ``tool_permission_context`` mapping
    or the current mode string.  The latter form is useful for tests and mirrors
    the TS helper signature that receives mode plus permissions separately.
    """

    if isinstance(current_context_or_mode, Mapping):
        context: dict[str, Any] = deepcopy(dict(current_context_or_mode))
        current_mode = _string_value(context.get("mode")) or "default"
    else:
        current_mode = (
            current_context_or_mode
            if isinstance(current_context_or_mode, str) and current_context_or_mode.strip()
            else "default"
        )
        context = {"mode": current_mode}
        if permissions is not None:
            context["always_allow_rules"] = _normalize_rules_by_source(permissions)

    normalized_target = target_mode.strip() if isinstance(target_mode, str) and target_mode.strip() else "default"
    context["mode"] = normalized_target
    warnings: list[str] = []
    stripped: dict[str, list[str]] = {}
    restored: dict[str, list[str]] = {}

    if normalized_target in AUTOMATED_PERMISSION_MODES:
        stripped = strip_dangerous_permissions_for_auto_mode(context)
        if stripped:
            context["stripped_dangerous_rules"] = _merge_rules_by_source(
                _normalize_rules_by_source(context.get("stripped_dangerous_rules")),
                stripped,
            )
            context["strippedDangerousRules"] = deepcopy(context["stripped_dangerous_rules"])
            warnings.append(
                "Stripped broad Bash allow-rules while entering automated permission mode."
            )
    elif current_mode in AUTOMATED_PERMISSION_MODES:
        restored = restore_dangerous_permissions(context)
        if restored:
            warnings.append("Restored Bash allow-rules after leaving automated permission mode.")

    return PermissionTransitionState(
        mode=normalized_target,
        context=context,
        stripped_dangerous_rules=stripped,
        restored_rules=restored,
        warnings=tuple(warnings),
    )


def strip_dangerous_permissions_for_auto_mode(
    context: MutableMapping[str, Any],
) -> dict[str, list[str]]:
    allow_rules = _normalize_rules_by_source(
        context.get("always_allow_rules") or context.get("alwaysAllowRules")
    )
    stripped: dict[str, list[str]] = {}
    remaining: dict[str, list[str]] = {}

    for source, rules in allow_rules.items():
        for rule in rules:
            target = stripped if is_dangerous_bash_permission(rule) else remaining
            target.setdefault(source, []).append(rule)

    context["always_allow_rules"] = remaining
    context["alwaysAllowRules"] = deepcopy(remaining)
    return stripped


def restore_dangerous_permissions(
    context: MutableMapping[str, Any],
) -> dict[str, list[str]]:
    stripped = _normalize_rules_by_source(
        context.get("stripped_dangerous_rules") or context.get("strippedDangerousRules")
    )
    if not stripped:
        return {}
    allow_rules = _normalize_rules_by_source(
        context.get("always_allow_rules") or context.get("alwaysAllowRules")
    )
    merged = _merge_rules_by_source(allow_rules, stripped)
    context["always_allow_rules"] = merged
    context["alwaysAllowRules"] = deepcopy(merged)
    context["stripped_dangerous_rules"] = {}
    context["strippedDangerousRules"] = {}
    return stripped


def find_overly_broad_bash_permissions(
    permissions: Mapping[str, Sequence[str]] | Sequence[str],
) -> dict[str, list[str]]:
    rules_by_source = (
        {"session": list(permissions)}
        if isinstance(permissions, Sequence) and not isinstance(permissions, (str, bytes, bytearray))
        else _normalize_rules_by_source(permissions)
    )
    return {
        source: [rule for rule in rules if is_dangerous_bash_permission(rule)]
        for source, rules in rules_by_source.items()
        if any(is_dangerous_bash_permission(rule) for rule in rules)
    }


def is_dangerous_bash_permission(rule: object) -> bool:
    if not isinstance(rule, str) or not rule.strip():
        return False
    try:
        parsed = permission_rule_value_from_string(rule)
    except Exception:
        return False
    if parsed.get("tool_name") != "Bash":
        return False
    content = parsed.get("rule_content")
    if not isinstance(content, str) or not content.strip():
        return True
    normalized = " ".join(content.strip().split()).lower()
    if normalized in {"*", "bash", "sh", "sudo", ":*", "bash:*", "sh:*", "sudo:*"}:
        return True
    if normalized.endswith(":*"):
        prefix = normalized[:-2].strip()
        return not prefix or any(prefix == item or prefix.startswith(item + " ") for item in _MUTATING_BASH_PREFIXES)
    if "*" in normalized:
        return True
    return any(normalized == item or normalized.startswith(item + " ") for item in _MUTATING_BASH_PREFIXES)


def _normalize_rules_by_source(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        return {}
    normalized: dict[str, list[str]] = {}
    for source, values in raw.items():
        if not isinstance(source, str) or not source:
            continue
        if source not in _RULE_SOURCE_KEYS:
            continue
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            continue
        items: list[str] = []
        for value in values:
            if isinstance(value, str) and value.strip() and value not in items:
                items.append(value)
        if items:
            normalized[source] = items
    return normalized


def _merge_rules_by_source(
    base: Mapping[str, Sequence[str]],
    incoming: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    merged = _normalize_rules_by_source(base)
    for source, rules in _normalize_rules_by_source(incoming).items():
        bucket = merged.setdefault(source, [])
        for rule in rules:
            if rule not in bucket:
                bucket.append(rule)
    return merged


def _string_value(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
