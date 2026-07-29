"""
Permission rule value parsing and serialization.

Port of src/utils/permissions/permissionRuleParser.ts. Parses the
``ToolName(content)`` format with proper parenthesis escaping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Maps legacy tool names to their current canonical names.
LEGACY_TOOL_NAME_ALIASES: dict[str, str] = {
    "Task": "Agent",
    "KillShell": "TaskStop",
    "AgentOutputTool": "TaskOutput",
    "BashOutputTool": "TaskOutput",
}


def normalize_legacy_tool_name(name: str) -> str:
    return LEGACY_TOOL_NAME_ALIASES.get(name, name)


def get_legacy_tool_names(canonical_name: str) -> list[str]:
    result: list[str] = []
    for legacy, canonical in LEGACY_TOOL_NAME_ALIASES.items():
        if canonical == canonical_name:
            result.append(legacy)
    return result


def escape_rule_content(content: str) -> str:
    """Escape special characters in rule content for safe storage.

    Escaping order matters:
    1. Escape existing backslashes first (\\ -> \\\\)
    2. Then escape parentheses (( -> \\(, ) -> \\))
    """
    return content.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def unescape_rule_content(content: str) -> str:
    """Unescape special characters in rule content after parsing.

    Unescaping order (reverse of escaping):
    1. Unescape parentheses first (\\( -> (, \\) -> ))
    2. Then unescape backslashes (\\\\ -> \\)
    """
    return content.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def _find_first_unescaped_char(s: str, ch: str) -> int:
    """Find the index of the first unescaped occurrence of a character."""
    for i, c in enumerate(s):
        if c == ch:
            backslash_count = 0
            j = i - 1
            while j >= 0 and s[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return i
    return -1


def _find_last_unescaped_char(s: str, ch: str) -> int:
    """Find the index of the last unescaped occurrence of a character."""
    for i in range(len(s) - 1, -1, -1):
        if s[i] == ch:
            backslash_count = 0
            j = i - 1
            while j >= 0 and s[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return i
    return -1


def permission_rule_value_from_string(rule_string: str) -> dict:
    """Parse a permission rule string into tool_name and optional rule_content.

    Format: ``ToolName`` or ``ToolName(content)``

    Content may contain escaped parentheses: \\( and \\)
    """
    open_paren_index = _find_first_unescaped_char(rule_string, "(")
    if open_paren_index == -1:
        return {"tool_name": normalize_legacy_tool_name(rule_string)}

    close_paren_index = _find_last_unescaped_char(rule_string, ")")
    if close_paren_index == -1 or close_paren_index <= open_paren_index:
        return {"tool_name": normalize_legacy_tool_name(rule_string)}

    if close_paren_index != len(rule_string) - 1:
        return {"tool_name": normalize_legacy_tool_name(rule_string)}

    tool_name = rule_string[:open_paren_index]
    raw_content = rule_string[open_paren_index + 1 : close_paren_index]

    if not tool_name:
        return {"tool_name": normalize_legacy_tool_name(rule_string)}

    # Empty content (e.g., "Bash()") or standalone wildcard -> tool-wide rule
    if raw_content == "" or raw_content == "*":
        return {"tool_name": normalize_legacy_tool_name(tool_name)}

    rule_content = unescape_rule_content(raw_content)
    return {
        "tool_name": normalize_legacy_tool_name(tool_name),
        "rule_content": rule_content,
    }


def permission_rule_value_to_string(
    tool_name: str, rule_content: Optional[str] = None
) -> str:
    """Convert a permission rule value to its string representation."""
    if not rule_content:
        return tool_name
    escaped = escape_rule_content(rule_content)
    return f"{tool_name}({escaped})"
