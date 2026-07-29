"""
Filesystem permission checks and gitignore-style rule matching.

Port of src/utils/permissions/filesystem.ts.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple, Union

from ...types.permissions import (
    ModeDecisionReason,
    OtherDecisionReason,
    PermissionAskDecision,
    PermissionAllowDecision,
    PermissionBehavior,
    PermissionDenyDecision,
    PermissionDecision,
    PermissionRule,
    PermissionRuleSource,
    RuleDecisionReason,
    SafetyCheckDecisionReason,
    ToolPermissionContext,
    WorkingDirDecisionReason,
)
from .permission_rule_parser import (
    permission_rule_value_from_string,
)
from .path_validation import expand_tilde


DANGEROUS_FILES: Tuple[str, ...] = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
    ".claude_py.json",
)

DANGEROUS_DIRECTORIES: Tuple[str, ...] = (
    ".git",
    ".vscode",
    ".idea",
    ".claude_py",
)

SEP = os.sep

SETTING_SOURCES: List[str] = [
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
]

PERMISSION_RULE_SOURCES: List[str] = SETTING_SOURCES + [
    "cliArg",
    "command",
    "session",
]

FILE_EDIT_TOOL_NAME = "Edit"
FILE_READ_TOOL_NAME = "Read"


def normalize_case_for_comparison(path: str) -> str:
    return path.lower()


def is_dangerous_file_path_to_auto_edit(path: str) -> bool:
    absolute_path = os.path.abspath(expand_tilde(path))
    segments = absolute_path.split(SEP)
    filename = segments[-1] if segments else ""

    if path.startswith("\\\\") or path.startswith("//"):
        return True

    for i, segment in enumerate(segments):
        lowered = normalize_case_for_comparison(segment)
        for ddir in DANGEROUS_DIRECTORIES:
            if lowered != normalize_case_for_comparison(ddir):
                continue
            if ddir == ".claude_py":
                nxt = segments[i + 1] if i + 1 < len(segments) else None
                if nxt and normalize_case_for_comparison(nxt) == "worktrees":
                    break
            return True

    if filename:
        lowered_name = normalize_case_for_comparison(filename)
        for df in DANGEROUS_FILES:
            if normalize_case_for_comparison(df) == lowered_name:
                return True

    return False


def _has_suspicious_windows_path_pattern(path: str) -> bool:
    import re

    if re.search(r"~\d", path):
        return True
    if path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        return True
    if path.startswith("//?/") or path.startswith("//./"):
        return True
    if re.search(r"[.\s]+$", path):
        return True
    if re.search(r"\.(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", path, re.IGNORECASE):
        return True
    if re.search(r"(^|/|\\)\.{3,}(/|\\|$)", path):
        return True
    return False


class SafetyCheckResult:
    __slots__ = ("safe", "message", "classifier_approvable")

    def __init__(self, message: str, classifier_approvable: bool) -> None:
        self.safe = False
        self.message = message
        self.classifier_approvable = classifier_approvable


def check_path_safety_for_auto_edit(
    path: str,
    precomputed_paths: Optional[Sequence[str]] = None,
) -> Optional[SafetyCheckResult]:
    paths = precomputed_paths or [os.path.abspath(expand_tilde(path))]

    for p in paths:
        if _has_suspicious_windows_path_pattern(p):
            return SafetyCheckResult(
                message=(
                    f"Claude requested permissions to write to {path}, "
                    "which contains a suspicious Windows path pattern "
                    "that requires manual approval."
                ),
                classifier_approvable=False,
            )

    for p in paths:
        if is_dangerous_file_path_to_auto_edit(p):
            return SafetyCheckResult(
                message=(
                    f"Claude requested permissions to edit {path} "
                    "which is a sensitive file."
                ),
                classifier_approvable=True,
            )

    return None


def path_in_working_path(path: str, working_path: str) -> bool:
    abs_path = os.path.abspath(expand_tilde(path))
    abs_work = os.path.abspath(expand_tilde(working_path))

    abs_path = abs_path.replace("/private/var/", "/var/").replace(
        "/private/tmp/", "/tmp/"
    )
    abs_path = abs_path.replace("/private/tmp", "/tmp")
    abs_work = abs_work.replace("/private/var/", "/var/").replace(
        "/private/tmp/", "/tmp/"
    )
    abs_work = abs_work.replace("/private/tmp", "/tmp")

    case_path = normalize_case_for_comparison(abs_path)
    case_work = normalize_case_for_comparison(abs_work)

    try:
        rel = posixpath.relpath(case_path, case_work)
    except ValueError:
        return False

    if rel == ".":
        return True

    if ".." in rel.split(posixpath.sep):
        return False

    return not posixpath.isabs(rel)


def all_working_directories(context: ToolPermissionContext) -> List[str]:
    cwd = context.cwd or os.getcwd()
    dirs = [cwd]
    for _, awd in context.additional_working_directories.items():
        dirs.append(awd.path)
    return dirs


def path_in_allowed_working_path(
    path: str,
    context: ToolPermissionContext,
    precomputed_paths: Optional[Sequence[str]] = None,
) -> bool:
    paths = precomputed_paths or [os.path.abspath(expand_tilde(path))]
    working = all_working_directories(context)
    return all(any(path_in_working_path(p, wp) for wp in working) for p in paths)


def get_allow_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    return [
        PermissionRule(
            source=PermissionRuleSource(source),
            rule_behavior=PermissionBehavior.ALLOW,
            rule_value=_parse_rule_value(rs),
        )
        for source in PERMISSION_RULE_SOURCES
        for rs in (context.always_allow_rules.get(source) or [])
    ]


def get_deny_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    return [
        PermissionRule(
            source=PermissionRuleSource(source),
            rule_behavior=PermissionBehavior.DENY,
            rule_value=_parse_rule_value(rs),
        )
        for source in PERMISSION_RULE_SOURCES
        for rs in (context.always_deny_rules.get(source) or [])
    ]


def get_ask_rules(context: ToolPermissionContext) -> List[PermissionRule]:
    return [
        PermissionRule(
            source=PermissionRuleSource(source),
            rule_behavior=PermissionBehavior.ASK,
            rule_value=_parse_rule_value(rs),
        )
        for source in PERMISSION_RULE_SOURCES
        for rs in (context.always_ask_rules.get(source) or [])
    ]


def _parse_rule_value(rule_string: str):
    from ...types.permissions import PermissionRuleValue

    parsed = permission_rule_value_from_string(rule_string)
    return PermissionRuleValue(
        tool_name=parsed["tool_name"],
        rule_content=parsed.get("rule_content"),
    )


def get_rule_by_contents_for_tool_name(
    context: ToolPermissionContext,
    tool_name: str,
    behavior: PermissionBehavior,
) -> Dict[str, PermissionRule]:
    rule_map: Dict[str, PermissionRule] = {}
    if behavior == PermissionBehavior.ALLOW:
        rules = get_allow_rules(context)
    elif behavior == PermissionBehavior.DENY:
        rules = get_deny_rules(context)
    else:
        rules = get_ask_rules(context)

    for rule in rules:
        if (
            rule.rule_value.tool_name == tool_name
            and rule.rule_value.rule_content is not None
            and rule.rule_behavior == behavior
        ):
            rule_map[rule.rule_value.rule_content] = rule
    return rule_map


def _pattern_with_root(
    pattern: str,
    source: str,
    cwd: str,
) -> Tuple[str, Optional[str]]:
    """Return (relative_pattern, root_or_none).

    - ``//path`` → root ``/``
    - ``~/path`` → root ``homedir()``
    - ``/path``  → root is source-dependent (cwd for session/cliArg/command)
    - ``path``   → root None (match anywhere)
    - ``./path`` → strip ``./``, root None
    """
    if pattern.startswith("//"):
        return (pattern[1:], "/")

    if pattern.startswith("~/"):
        return (pattern[1:], os.path.expanduser("~"))

    if pattern.startswith("/"):
        if source in ("cliArg", "command", "session"):
            root = cwd
        else:
            root = os.getcwd()
        return (pattern, root)

    cleaned = pattern
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return (cleaned, None)


def _get_patterns_by_root(
    context: ToolPermissionContext,
    tool_type: str,
    behavior: PermissionBehavior,
    cwd: str,
) -> Dict[Optional[str], List[Tuple[str, PermissionRule]]]:
    tool_name = FILE_EDIT_TOOL_NAME if tool_type == "edit" else FILE_READ_TOOL_NAME
    rules = get_rule_by_contents_for_tool_name(context, tool_name, behavior)

    result: Dict[Optional[str], List[Tuple[str, PermissionRule]]] = {}
    for pattern_str, rule in rules.items():
        rel_pattern, root = _pattern_with_root(pattern_str, rule.source.value, cwd)
        bucket = result.setdefault(root, [])
        bucket.append((rel_pattern, rule))
    return result


@dataclass(frozen=True)
class _CompiledGitignorePattern:
    pattern: str
    negated: bool
    directory_only: bool
    root_relative: bool


def _compile_gitignore_pattern(raw_pattern: str) -> Optional[_CompiledGitignorePattern]:
    pattern = raw_pattern.strip()
    if not pattern:
        return None

    negated = False
    if pattern.startswith("!"):
        negated = True
        pattern = pattern[1:]

    if pattern.startswith("./"):
        pattern = pattern[2:]

    root_relative = pattern.startswith("/")
    if root_relative:
        pattern = pattern[1:]

    directory_only = False
    if pattern.endswith("/**"):
        directory_only = True
        pattern = pattern[:-3]
    elif pattern.endswith("/"):
        directory_only = True
        pattern = pattern[:-1]

    pattern = pattern.strip("/")
    if not pattern:
        return None

    return _CompiledGitignorePattern(
        pattern=pattern,
        negated=negated,
        directory_only=directory_only,
        root_relative=root_relative,
    )


def _split_path_segments(path: str) -> Tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment and segment != ".")


def _match_segment_sequence(
    path_segments: Tuple[str, ...],
    pattern_segments: Tuple[str, ...],
) -> bool:
    if not pattern_segments:
        return not path_segments

    head = pattern_segments[0]
    tail = pattern_segments[1:]

    if head == "**":
        return _match_segment_sequence(path_segments, tail) or (
            bool(path_segments)
            and _match_segment_sequence(path_segments[1:], pattern_segments)
        )

    if not path_segments or not fnmatch.fnmatchcase(path_segments[0], head):
        return False

    return _match_segment_sequence(path_segments[1:], tail)


def _matches_compiled_pattern(
    compiled: _CompiledGitignorePattern,
    rel_path: str,
) -> bool:
    path_segments = _split_path_segments(rel_path)
    if not path_segments:
        return False

    pattern_segments = _split_path_segments(compiled.pattern)
    if not pattern_segments:
        return False

    full_path_pattern = compiled.root_relative or "/" in compiled.pattern
    if full_path_pattern:
        if compiled.directory_only:
            for prefix_len in range(1, len(path_segments) + 1):
                if _match_segment_sequence(path_segments[:prefix_len], pattern_segments):
                    return True
            return False
        return _match_segment_sequence(path_segments, pattern_segments)

    if compiled.directory_only:
        if len(path_segments) == 1:
            return fnmatch.fnmatchcase(path_segments[0], compiled.pattern)
        return any(
            fnmatch.fnmatchcase(component, compiled.pattern)
            for component in path_segments[:-1]
        )

    return any(
        fnmatch.fnmatchcase(component, compiled.pattern) for component in path_segments
    )


def _gitignore_match(
    patterns: Sequence[Tuple[str, PermissionRule]],
    rel_path: str,
) -> Optional[PermissionRule]:
    """Return the last effective gitignore-style match for ``rel_path``.

    Supports:
    - leading ``!`` negation
    - root-anchored ``/pattern``
    - directory-only ``foo/`` and ``foo/**``
    - ``**`` segment wildcards
    - basename matching for patterns without slashes
    """
    matched_rule: Optional[PermissionRule] = None

    for raw_pattern, rule in patterns:
        compiled = _compile_gitignore_pattern(raw_pattern)
        if compiled is None or not _matches_compiled_pattern(compiled, rel_path):
            continue
        if compiled.negated:
            matched_rule = None
            continue
        matched_rule = rule

    return matched_rule


def matching_rule_for_input(
    path: str,
    context: ToolPermissionContext,
    tool_type: str,
    behavior: PermissionBehavior,
    cwd: Optional[str] = None,
) -> Optional[PermissionRule]:
    """Find the first permission rule matching *path* for the given tool type
    and behavior. *tool_type* is ``'edit'`` or ``'read'``.
    """
    file_abs = os.path.abspath(expand_tilde(path))
    cwd = cwd or context.cwd or os.getcwd()

    patterns_by_root = _get_patterns_by_root(context, tool_type, behavior, cwd)

    for root, patterns in patterns_by_root.items():
        ref = root if root is not None else cwd
        try:
            rel = posixpath.relpath(file_abs, ref)
        except ValueError:
            continue

        if rel.startswith("../"):
            continue

        matched_rule = _gitignore_match(patterns, rel)
        if matched_rule is not None:
            return matched_rule

    return None


def check_read_permission_for_tool(
    tool_name: str,
    path: str,
    context: ToolPermissionContext,
) -> PermissionDecision:
    abs_path = os.path.abspath(expand_tilde(path))

    if path.startswith("\\\\") or path.startswith("//"):
        return PermissionAskDecision(
            message=(
                f"Claude requested permissions to read from {path}, "
                "which appears to be a UNC path that could access network resources."
            ),
            decision_reason=OtherDecisionReason(
                reason="UNC path detected (defense-in-depth check)"
            ),
        )

    deny_rule = matching_rule_for_input(path, context, "read", PermissionBehavior.DENY)
    if deny_rule:
        return PermissionDenyDecision(
            message=f"Permission to read {path} has been denied.",
            decision_reason=RuleDecisionReason(rule=deny_rule),
        )

    ask_rule = matching_rule_for_input(path, context, "read", PermissionBehavior.ASK)
    if ask_rule:
        return PermissionAskDecision(
            message=(
                f"Claude requested permissions to read from {path}, "
                "but you haven't granted it yet."
            ),
            decision_reason=RuleDecisionReason(rule=ask_rule),
        )

    if path_in_allowed_working_path(path, context):
        return PermissionAllowDecision(
            decision_reason=ModeDecisionReason(mode="default"),
        )

    allow_rule = matching_rule_for_input(
        path, context, "read", PermissionBehavior.ALLOW
    )
    if allow_rule:
        return PermissionAllowDecision(
            decision_reason=RuleDecisionReason(rule=allow_rule),
        )

    return PermissionAskDecision(
        message=(
            f"Claude requested permissions to read from {path}, "
            "but you haven't granted it yet."
        ),
        decision_reason=WorkingDirDecisionReason(
            reason="Path is outside allowed working directories"
        ),
    )


def check_write_permission_for_tool(
    tool_name: str,
    path: str,
    context: ToolPermissionContext,
) -> PermissionDecision:
    abs_path = os.path.abspath(expand_tilde(path))

    deny_rule = matching_rule_for_input(path, context, "edit", PermissionBehavior.DENY)
    if deny_rule:
        return PermissionDenyDecision(
            message=f"Permission to edit {path} has been denied.",
            decision_reason=RuleDecisionReason(rule=deny_rule),
        )

    safety = check_path_safety_for_auto_edit(path)
    if safety is not None:
        return PermissionAskDecision(
            message=safety.message,
            decision_reason=SafetyCheckDecisionReason(
                reason=safety.message,
                classifier_approvable=safety.classifier_approvable,
            ),
        )

    ask_rule = matching_rule_for_input(path, context, "edit", PermissionBehavior.ASK)
    if ask_rule:
        return PermissionAskDecision(
            message=(
                f"Claude requested permissions to write to {path}, "
                "but you haven't granted it yet."
            ),
            decision_reason=RuleDecisionReason(rule=ask_rule),
        )

    in_wd = path_in_allowed_working_path(path, context)
    if context.mode == "acceptEdits" and in_wd:
        return PermissionAllowDecision(
            decision_reason=ModeDecisionReason(mode="acceptEdits"),
        )

    allow_rule = matching_rule_for_input(
        path, context, "edit", PermissionBehavior.ALLOW
    )
    if allow_rule:
        return PermissionAllowDecision(
            decision_reason=RuleDecisionReason(rule=allow_rule),
        )

    return PermissionAskDecision(
        message=(
            f"Claude requested permissions to write to {path}, "
            "but you haven't granted it yet."
        ),
        decision_reason=(
            None
            if in_wd
            else WorkingDirDecisionReason(
                reason="Path is outside allowed working directories"
            )
        ),
    )
