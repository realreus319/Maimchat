"""
Port of src/utils/permissions/shellRuleMatching.ts.
Exact/prefix/wildcard permission rule matching for shell tools.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional, Union

from ..bash_ast import iter_analyzable_command_texts


_ESCAPED_STAR_PLACEHOLDER = "\x00ESCAPED_STAR\x00"
_ESCAPED_BACKSLASH_PLACEHOLDER = "\x00ESCAPED_BACKSLASH\x00"


@dataclass(frozen=True)
class ExactRule:
    type: Literal["exact"] = "exact"
    command: str = ""


@dataclass(frozen=True)
class PrefixRule:
    type: Literal["prefix"] = "prefix"
    prefix: str = ""


@dataclass(frozen=True)
class WildcardRule:
    type: Literal["wildcard"] = "wildcard"
    pattern: str = ""


ShellPermissionRule = Union[ExactRule, PrefixRule, WildcardRule]


def permission_rule_extract_prefix(permission_rule: str) -> Optional[str]:
    m = re.match(r"^(.+):\*$", permission_rule)
    return m.group(1) if m else None


def has_wildcards(pattern: str) -> bool:
    if pattern.endswith(":*"):
        return False
    for i, ch in enumerate(pattern):
        if ch == "*":
            backslash_count = 0
            j = i - 1
            while j >= 0 and pattern[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return True
    return False


def match_wildcard_pattern(
    pattern: str,
    command: str,
    case_insensitive: bool = False,
) -> bool:
    trimmed = pattern.strip()
    processed = ""
    i = 0
    while i < len(trimmed):
        ch = trimmed[i]
        if ch == "\\" and i + 1 < len(trimmed):
            nxt = trimmed[i + 1]
            if nxt == "*":
                processed += _ESCAPED_STAR_PLACEHOLDER
                i += 2
                continue
            elif nxt == "\\":
                processed += _ESCAPED_BACKSLASH_PLACEHOLDER
                i += 2
                continue
        processed += ch
        i += 1

    escaped = re.sub(r"[.+?^${}()|[\]\\'\"]", lambda m: "\\" + m.group(0), processed)
    with_wildcards = escaped.replace("*", ".*")

    regex_pattern = with_wildcards.replace(_ESCAPED_STAR_PLACEHOLDER, "\\*").replace(
        _ESCAPED_BACKSLASH_PLACEHOLDER, "\\\\"
    )

    unescaped_star_count = processed.count("*")
    if regex_pattern.endswith(" .*") and unescaped_star_count == 1:
        regex_pattern = regex_pattern[:-3] + "( .*)?"

    flags = re.DOTALL | (re.IGNORECASE if case_insensitive else 0)
    return bool(re.fullmatch(regex_pattern, command, flags))


def parse_permission_rule(permission_rule: str) -> ShellPermissionRule:
    prefix = permission_rule_extract_prefix(permission_rule)
    if prefix is not None:
        return PrefixRule(prefix=prefix)
    if has_wildcards(permission_rule):
        return WildcardRule(pattern=permission_rule)
    return ExactRule(command=permission_rule)


def match_command_against_rule(command: str, rule_content: str) -> bool:
    parsed = parse_permission_rule(rule_content)
    candidates = tuple(dict.fromkeys((command.strip(), *iter_analyzable_command_texts(command))))
    if parsed.type == "exact":
        return any(candidate == parsed.command for candidate in candidates)
    elif parsed.type == "prefix":
        return any(candidate.startswith(parsed.prefix) for candidate in candidates)
    elif parsed.type == "wildcard":
        return any(match_wildcard_pattern(parsed.pattern, candidate) for candidate in candidates)
    return False
