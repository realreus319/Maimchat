"""
Main permission evaluation pipeline.

Port of src/utils/permissions/permissions.ts. Orchestrates the
deny → ask → tool-specific → safety → bypass → allow → default-ask
decision sequence (``hasPermissionsToUseToolInner``).
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

from ...types.permissions import (
    AsyncAgentDecisionReason,
    HookDecisionReason,
    ModeDecisionReason,
    OtherDecisionReason,
    PermissionAskDecision,
    PermissionAllowDecision,
    PermissionBehavior,
    PermissionDecision,
    PermissionDenyDecision,
    PermissionDecisionReason,
    PermissionPassthrough,
    PermissionResult,
    PermissionRule,
    RuleDecisionReason,
    SafetyCheckDecisionReason,
    ToolPermissionContext,
    WorkingDirDecisionReason,
)
from .approval import deny_for_unavailable_prompts, resolve_approval_state
from .classifier_decision import classify_automated_permission_decision
from .filesystem import (
    check_read_permission_for_tool,
    check_write_permission_for_tool,
    get_allow_rules,
    get_ask_rules,
    get_deny_rules,
)
from .permission_rule_parser import (
    permission_rule_value_to_string,
)
from .shell_rule_matching import match_command_against_rule


def _tool_name_matches_rule(tool_name: str, rule_tool_name: str) -> bool:
    if rule_tool_name == tool_name:
        return True
    if any(ch in rule_tool_name for ch in "*?[]"):
        return fnmatch.fnmatchcase(tool_name, rule_tool_name)
    return False


def _extract_bash_command(tool_input: Mapping[str, Any]) -> str | None:
    for key in ("command", "cmd"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_webfetch_hostname(tool_input: Mapping[str, Any]) -> str | None:
    url = tool_input.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    hostname = urlparse(candidate).hostname
    if not isinstance(hostname, str) or not hostname.strip():
        return None
    return hostname.lower()


def _match_webfetch_domain(hostname: str, rule_content: str) -> bool:
    normalized = rule_content.strip()
    if normalized.lower().startswith("domain:"):
        normalized = normalized[len("domain:") :]
    normalized = normalized.strip().lower()
    if not normalized:
        return False
    if any(ch in normalized for ch in "*?[]"):
        return fnmatch.fnmatchcase(hostname, normalized)
    return hostname == normalized or hostname.endswith("." + normalized)


def _extract_skill_names(tool_input: Mapping[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []

    def _append_candidate(value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = value.strip()
        if normalized.startswith("/"):
            normalized = normalized[1:]
        if not normalized or normalized in candidates:
            return
        candidates.append(normalized)

    for key in ("resolved_skill_name", "skill"):
        _append_candidate(tool_input.get(key))

    aliases = tool_input.get("skill_aliases")
    if isinstance(aliases, (list, tuple)):
        for alias in aliases:
            _append_candidate(alias)

    return tuple(candidates)


def _match_skill_name(tool_input: Mapping[str, Any], rule_content: str) -> bool:
    normalized_rule = rule_content.strip()
    if normalized_rule.startswith("/"):
        normalized_rule = normalized_rule[1:]
    if not normalized_rule:
        return False

    skill_names = _extract_skill_names(tool_input)
    if not skill_names:
        return False

    if any(ch in normalized_rule for ch in "*?[]"):
        return any(fnmatch.fnmatchcase(skill_name, normalized_rule) for skill_name in skill_names)
    return normalized_rule in skill_names


def tool_matches_rule(
    tool_name: str,
    tool_input: Mapping[str, Any],
    rule: PermissionRule,
) -> bool:
    """True when *rule* matches *tool_name* and supported content filters."""
    if not _tool_name_matches_rule(tool_name, rule.rule_value.tool_name):
        return False

    rule_content = rule.rule_value.rule_content
    if rule_content is None:
        return True

    if tool_name == "Bash":
        command = _extract_bash_command(tool_input)
        return command is not None and match_command_against_rule(command, rule_content)

    if tool_name == "WebFetch":
        hostname = _extract_webfetch_hostname(tool_input)
        return hostname is not None and _match_webfetch_domain(hostname, rule_content)

    if tool_name == "Skill":
        return _match_skill_name(tool_input, rule_content)

    return False


def tool_always_allowed_rule(
    context: ToolPermissionContext,
    tool_name: str,
    tool_input: Optional[Mapping[str, Any]] = None,
) -> Optional[PermissionRule]:
    tool_input = tool_input or {}
    for rule in get_allow_rules(context):
        if tool_matches_rule(tool_name, tool_input, rule):
            return rule
    return None


def get_deny_rule_for_tool(
    context: ToolPermissionContext,
    tool_name: str,
    tool_input: Optional[Mapping[str, Any]] = None,
) -> Optional[PermissionRule]:
    tool_input = tool_input or {}
    for rule in get_deny_rules(context):
        if tool_matches_rule(tool_name, tool_input, rule):
            return rule
    return None


def get_ask_rule_for_tool(
    context: ToolPermissionContext,
    tool_name: str,
    tool_input: Optional[Mapping[str, Any]] = None,
) -> Optional[PermissionRule]:
    tool_input = tool_input or {}
    for rule in get_ask_rules(context):
        if tool_matches_rule(tool_name, tool_input, rule):
            return rule
    return None


def create_permission_request_message(
    tool_name: str,
    decision_reason: Optional[PermissionDecisionReason] = None,
) -> str:
    if decision_reason is not None:
        if isinstance(decision_reason, RuleDecisionReason):
            rule_str = permission_rule_value_to_string(
                decision_reason.rule.rule_value.tool_name,
                decision_reason.rule.rule_value.rule_content,
            )
            return (
                f"Permission rule '{rule_str}' requires approval "
                f"for this {tool_name} command"
            )
        if isinstance(decision_reason, WorkingDirDecisionReason):
            return decision_reason.reason
        if isinstance(decision_reason, SafetyCheckDecisionReason):
            return decision_reason.reason
        if isinstance(decision_reason, OtherDecisionReason):
            return decision_reason.reason
        if isinstance(decision_reason, AsyncAgentDecisionReason):
            return decision_reason.reason
        if isinstance(decision_reason, HookDecisionReason):
            if decision_reason.reason:
                return (
                    f"Hook '{decision_reason.hook_name}' blocked this action: "
                    f"{decision_reason.reason}"
                )
            return (
                f"Hook '{decision_reason.hook_name}' requires approval "
                f"for this {tool_name} command"
            )
        if isinstance(decision_reason, ModeDecisionReason):
            return (
                f"Current permission mode requires approval "
                f"for this {tool_name} command"
            )
    return f"Claude requested permissions to use {tool_name}, but you haven't granted it yet."


def _make_passthrough(tool_name: str) -> PermissionPassthrough:
    return PermissionPassthrough(
        message=create_permission_request_message(tool_name),
    )


def _get_tool_permission_result(
    tool_name: str,
    path: Optional[str],
    context: ToolPermissionContext,
) -> PermissionResult:
    """Compute the tool-specific permission result.

    For file-edit/read tools this delegates to the filesystem pipeline.
    For everything else, returns passthrough.
    """
    if tool_name in ("Edit", "Write", "MultEdit") and path is not None:
        return check_write_permission_for_tool(tool_name, path, context)

    if tool_name in ("Read", "Glob") and path is not None:
        return check_read_permission_for_tool(tool_name, path, context)

    return _make_passthrough(tool_name)


def has_permissions_to_use_tool(
    tool_name: str,
    tool_input: Optional[Dict] = None,
    context: Optional[ToolPermissionContext] = None,
    approval_state: Optional[Mapping[str, object]] = None,
) -> PermissionDecision:
    """Synchronous permission evaluation pipeline.

    Mirrors ``hasPermissionsToUseToolInner`` from permissions.ts:
    1a. Deny rules for entire tool
    1b. Ask rules for entire tool
    1c. Tool-specific permission check
    1d. Tool implementation denied
    1f. Content-specific ask rules
    1g. Safety checks (bypass-immune)
    2a. bypassPermissions / plan-with-bypass mode
    2b. Entire tool always allowed
    3.  Passthrough -> ask
    """
    context = context if context is not None else ToolPermissionContext()
    tool_input = tool_input if tool_input is not None else {}

    # 1a. Entire tool is denied
    deny_rule = get_deny_rule_for_tool(context, tool_name, tool_input)
    if deny_rule:
        return PermissionDenyDecision(
            message=f"Permission to use {tool_name} has been denied.",
            decision_reason=RuleDecisionReason(rule=deny_rule),
        )

    # 1b. Entire tool has an ask rule
    ask_rule = get_ask_rule_for_tool(context, tool_name, tool_input)
    if ask_rule:
        return PermissionAskDecision(
            message=create_permission_request_message(tool_name),
            decision_reason=RuleDecisionReason(rule=ask_rule),
        )

    # 1c. Tool-specific permission check
    path = tool_input.get("file_path") or tool_input.get("path")
    tool_result = _get_tool_permission_result(tool_name, path, context)

    # 1d. Tool implementation denied
    if isinstance(tool_result, PermissionDenyDecision):
        return tool_result

    # 1f. Content-specific ask rules
    dr = tool_result.decision_reason
    if (
        isinstance(tool_result, PermissionAskDecision)
        and isinstance(dr, RuleDecisionReason)
        and dr.rule.rule_behavior == PermissionBehavior.ASK
    ):
        return tool_result

    # 1g. Safety checks are bypass-immune
    if isinstance(tool_result, PermissionAskDecision) and isinstance(
        dr, SafetyCheckDecisionReason
    ):
        return tool_result

    # 2a. bypassPermissions mode (or plan with bypass available)
    should_bypass = context.mode == "bypassPermissions" or (
        context.mode == "plan" and context.is_bypass_permissions_mode_available
    )
    if should_bypass:
        return PermissionAllowDecision(
            decision_reason=ModeDecisionReason(mode=context.mode),
        )

    # 2b. Entire tool is allowed by rule
    always_allowed = tool_always_allowed_rule(context, tool_name, tool_input)
    if always_allowed:
        return PermissionAllowDecision(
            decision_reason=RuleDecisionReason(rule=always_allowed),
        )

    automated_decision = classify_automated_permission_decision(
        tool_name,
        tool_input,
        context,
        tool_result,
    )
    if automated_decision is not None:
        return automated_decision

    # 3. Passthrough -> ask
    if (
        isinstance(tool_result, PermissionPassthrough)
        or tool_result.behavior == "passthrough"
    ):
        result = PermissionAskDecision(
            message=create_permission_request_message(
                tool_name, tool_result.decision_reason
            ),
            decision_reason=tool_result.decision_reason,
        )
        if context.should_avoid_permission_prompts:
            resolved = resolve_approval_state(tool_name, tool_input, approval_state)
            if resolved is not None:
                if isinstance(resolved, PermissionAskDecision):
                    return deny_for_unavailable_prompts(tool_name)
                return resolved
            return deny_for_unavailable_prompts(tool_name)
        return result

    if isinstance(
        tool_result,
        (PermissionAllowDecision, PermissionAskDecision, PermissionDenyDecision),
    ):
        if (
            isinstance(tool_result, PermissionAskDecision)
            and context.should_avoid_permission_prompts
        ):
            resolved = resolve_approval_state(tool_name, tool_input, approval_state)
            if resolved is not None:
                if isinstance(resolved, PermissionAskDecision):
                    return deny_for_unavailable_prompts(tool_name)
                return resolved
            return deny_for_unavailable_prompts(tool_name)
        return tool_result

    result = PermissionAskDecision(
        message=create_permission_request_message(tool_name),
    )
    if context.should_avoid_permission_prompts:
        resolved = resolve_approval_state(tool_name, tool_input, approval_state)
        if resolved is not None:
            if isinstance(resolved, PermissionAskDecision):
                return deny_for_unavailable_prompts(tool_name)
            return resolved
        return deny_for_unavailable_prompts(tool_name)
    return result
