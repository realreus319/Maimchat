"""
Conservative automated permission-mode classifier.

This mirrors the role of the TS transcript/yolo classifier without relying on
remote services. ``auto`` and internal ``bubble`` mode only auto-allow a
bounded set of clearly read-only operations and block ambiguous or mutating
actions for manual approval.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, NamedTuple

from ...utils.bash_ast import analyze_bash_security, parse_bash_for_security
from ...types.permissions import (
    ModeDecisionReason,
    OtherDecisionReason,
    PermissionAllowDecision,
    PermissionAskDecision,
    PermissionDecision,
    PermissionDenyDecision,
    PermissionPassthrough,
    ToolPermissionContext,
)
from .filesystem import path_in_allowed_working_path
from .path_validation import is_dangerous_removal_path

_AUTOMATED_PERMISSION_MODES = frozenset({"auto", "bubble"})

# ---------------------------------------------------------------------------
# Confidence levels for classification decisions
# ---------------------------------------------------------------------------

class _Confidence:
    """Confidence level constants for bash command classification."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class _ClassifyResult(NamedTuple):
    """Result of bash command semantic classification with confidence."""
    category: str
    confidence: str
    danger_detail: str = ""


# ---------------------------------------------------------------------------
# Semantic command sets (expanded for broader recognition)
# ---------------------------------------------------------------------------

_BASH_SEARCH_COMMANDS = frozenset(
    {
        "find",
        "grep",
        "rg",
        "ag",
        "ack",
        "locate",
        "which",
        "whereis",
    }
)

_BASH_READ_COMMANDS = frozenset(
    {
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "stat",
        "file",
        "strings",
        "jq",
        "awk",
        "cut",
        "sort",
        "uniq",
        "tr",
        "hexdump",
        "xxd",
        "od",
        "md5sum",
        "sha256sum",
        "sha1sum",
        "b2sum",
        "cksum",
    }
)

_BASH_LIST_COMMANDS = frozenset({"ls", "tree", "du", "df", "free", "uptime", "uname", "hostname"})

_BASH_SEMANTIC_NEUTRAL_COMMANDS = frozenset({"echo", "printf", "true", "false", ":", "date", "whoami", "id", "pwd", "env", "printenv"})

# Package managers — classified as "edit" with MEDIUM confidence (they mutate
# system state but are common development operations)
_PACKAGE_MANAGER_COMMANDS = frozenset(
    {
        "apt", "apt-get", "apt-cache", "dpkg", "snap",
        "yum", "dnf", "rpm", "zypper",
        "pip", "pip3", "pipx", "conda", "mamba",
        "npm", "npx", "yarn", "pnpm", "bun",
        "cargo", "go",
        "gem", "bundle",
        "composer",
    }
)

# Build tools — classified as "edit" with MEDIUM confidence
_BUILD_TOOL_COMMANDS = frozenset(
    {
        "make", "cmake", "ninja", "meson", "bazel", "gradle", "mvn",
        "ant", "scons", "tup", "just", "task",
    }
)

# Version control systems (beyond git) — read subcommands classified at MEDIUM
_VCS_COMMANDS = frozenset({"svn", "hg", "bzr", "darcs", "fossil"})

# VCS read-like subcommands for svn/hg
_VCS_READ_SUBCOMMANDS = frozenset(
    {
        "info", "log", "diff", "status", "cat", "list", "ls",
        "annotate", "blame", "praise", "heads", "tags", "branches",
        "identify", "summary", "manifest", "locate", "root",
    }
)

# Commands that are always destructive regardless of arguments
_BASH_DESTRUCTIVE_COMMANDS = frozenset(
    {
        "rm", "rmdir", "mkfs", "poweroff", "reboot", "shutdown",
        "halt", "init", "systemctl", "killall", "pkill",
        "dd", "shred",
    }
)

_BASH_EDIT_COMMANDS = frozenset({"sed", "awk", "tee", "mv", "cp", "install", "chmod", "chown", "chgrp", "ln", "mkdir", "touch", "truncate"})

_SED_PRINT_ONLY_SCRIPT_RE = re.compile(
    r"^\s*(?:\d+|\$)(?:\s*,\s*(?:\d+|\$))?\s*p\s*$"
)

_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "tag",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "describe",
        "blame",
        "shortlog",
        "stash",
        "remote",
        "config",
    }
)

_GIT_READ_FLAGS = frozenset({"--list", "-v", "--show", "list", "show"})

_GIT_DESTRUCTIVE_SUBCOMMANDS = frozenset(
    {
        "push --force",
        "push -f",
        "reset --hard",
        "clean -f",
        "clean -fd",
        "checkout --",
        "rebase",
        "filter-branch",
        "submodule deinit",
    }
)

_BASH_COMMAND_CATEGORY = {
    "git": "git",
    "read": "read",
    "list": "list",
    "edit": "edit",
    "destructive": "destructive",
    "unknown": "unknown",
}

# ---------------------------------------------------------------------------
# Dangerous argument patterns
# ---------------------------------------------------------------------------

# Patterns that indicate dangerous argument usage.  Each entry is a tuple of
# (compiled_regex, description).  The regex is applied to the raw argv joined
# as a string for simple matching.
_DANGEROUS_ARG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # rm -rf with aggressive flags
    (re.compile(r"\brm\s+.*-[a-zA-Z]*r[a-zA-Z]*f"), "rm with recursive+force flags"),
    (re.compile(r"\brm\s+.*-[a-zA-Z]*f[a-zA-Z]*r"), "rm with force+recursive flags"),
    # Fork bomb
    (re.compile(r":\(\)\{.*:\|:&\s*\}\s*;"), "fork bomb pattern"),
    # chmod 777
    (re.compile(r"\bchmod\s+777\b"), "chmod 777 (world-writable)"),
    # curl/wget piped to shell
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh"), "curl piped to shell"),
    (re.compile(r"\bwget\b.*\|\s*(ba)?sh"), "wget piped to shell"),
    # sudo with dangerous commands
    (re.compile(r"\bsudo\s+.*\b(rm|rmdir|mkfs|dd|shred|chmod|chown)\b"), "sudo with destructive command"),
    # Redirect to system paths
    (re.compile(r">\s*/etc/"), "redirect to /etc/"),
    (re.compile(r">\s*/boot/"), "redirect to /boot/"),
    (re.compile(r">\s*/dev/sd"), "redirect to block device"),
    (re.compile(r">\s*/dev/null"), "redirect to /dev/null is safe"),
    # overwrite without prompting
    (re.compile(r"\bcp\s+.*-f\b"), "cp with force flag"),
    (re.compile(r"\bmv\s+.*-f\b"), "mv with force flag"),
    # git push --force with lease is somewhat safer but still force
    (re.compile(r"\bgit\s+push\s+.*--force-with-lease"), "git push --force-with-lease"),
]

# Sudo escalation: prefix detection
_SUDO_PREFIX_RE = re.compile(r"^\s*sudo(\s+-[A-Za-z])?(\s+--)?\s+")


# ---------------------------------------------------------------------------
# _classify_bash_args_for_danger
# ---------------------------------------------------------------------------

def _classify_bash_args_for_danger(command: str) -> tuple[bool, str]:
    """Check bash command arguments for dangerous patterns.

    Returns (is_dangerous, detail) where *detail* is a human-readable
    explanation of the first matched danger pattern, or empty string.
    """
    for pattern, description in _DANGEROUS_ARG_PATTERNS:
        if pattern.search(command):
            # Special case: >/dev/null is safe, not dangerous
            if description == "redirect to /dev/null is safe":
                continue
            return True, description
    return False, ""


def _has_sudo_escalation(command: str) -> bool:
    """Return True if the command starts with sudo."""
    return bool(_SUDO_PREFIX_RE.match(command))


def _is_read_only_sed_segment(segment: Any, *, has_output_redirection: bool) -> bool:
    """Return True for the common ``sed -n '1,20p' file`` read pattern."""
    if has_output_redirection:
        return False

    argv = tuple(str(arg) for arg in getattr(segment, "argv", ()) or ())
    if not argv or argv[0] != "sed":
        return False

    quiet = False
    scripts: list[str] = []
    first_positional_script_seen = False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if arg in {"-i", "--in-place"} or arg.startswith("--in-place="):
            return False
        if arg.startswith("-") and not arg.startswith("--") and "i" in arg[1:]:
            return False
        if arg in {"-n", "--quiet", "--silent"}:
            quiet = True
            index += 1
            continue
        if arg == "-e":
            if index + 1 >= len(argv):
                return False
            scripts.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith("-e") and len(arg) > 2:
            scripts.append(arg[2:])
            index += 1
            continue
        if arg in {"-f", "--file"} or arg.startswith("--file="):
            return False
        if arg.startswith("-"):
            return False
        if not scripts and not first_positional_script_seen:
            scripts.append(arg)
            first_positional_script_seen = True
        index += 1

    return quiet and bool(scripts) and all(
        _SED_PRINT_ONLY_SCRIPT_RE.match(script) for script in scripts
    )


# ---------------------------------------------------------------------------
# Semantic classification with confidence
# ---------------------------------------------------------------------------

def _classify_bash_command_semantics(command: str) -> str:
    """Backward-compatible wrapper returning category only."""
    return _classify_bash_command_semantics_v2(command).category


def _classify_bash_command_semantics_v2(command: str) -> _ClassifyResult:
    """Classify a bash command into a semantic category with confidence.

    Returns a _ClassifyResult(category, confidence, danger_detail).
    """
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex or not analysis.commands:
        return _ClassifyResult("unknown", _Confidence.LOW)

    # Check for sudo escalation — always at least MEDIUM risk
    sudo_present = _has_sudo_escalation(command)

    # Check argument-level danger patterns
    args_dangerous, args_danger_detail = _classify_bash_args_for_danger(command)
    if args_dangerous:
        return _ClassifyResult("destructive", _Confidence.HIGH, args_danger_detail)

    for segment in analysis.commands:
        exe = segment.executable
        if not exe:
            continue

        # --- git -----------------------------------------------------------
        if exe == "git":
            argv = segment.argv
            if len(argv) < 2:
                return _ClassifyResult("git", _Confidence.HIGH)
            sub = argv[1]
            combined = " ".join(str(a) for a in argv[1:3]) if len(argv) >= 3 else sub
            if combined in _GIT_DESTRUCTIVE_SUBCOMMANDS:
                return _ClassifyResult("destructive", _Confidence.HIGH)
            if sub in _GIT_DESTRUCTIVE_SUBCOMMANDS:
                return _ClassifyResult("destructive", _Confidence.HIGH)
            if sub in _GIT_READ_SUBCOMMANDS:
                return _ClassifyResult("git", _Confidence.HIGH)
            if sub == "push" or sub == "commit" or sub == "merge":
                return _ClassifyResult("edit", _Confidence.HIGH)
            return _ClassifyResult("unknown", _Confidence.LOW)

        # --- Always-destructive commands -----------------------------------
        if exe in _BASH_DESTRUCTIVE_COMMANDS:
            return _ClassifyResult("destructive", _Confidence.HIGH)

        # ``sed`` is normally an editing command, but subagents commonly use
        # ``sed -n '201,450p' file`` as a pure file-read primitive.
        if exe == "sed" and _is_read_only_sed_segment(
            segment,
            has_output_redirection=analysis.has_output_redirection,
        ):
            return _ClassifyResult("read", _Confidence.HIGH)

        # --- Edit commands -------------------------------------------------
        if exe in _BASH_EDIT_COMMANDS:
            conf = _Confidence.MEDIUM if sudo_present else _Confidence.HIGH
            return _ClassifyResult("edit", conf)

        # --- List commands -------------------------------------------------
        if exe in _BASH_LIST_COMMANDS:
            return _ClassifyResult("list", _Confidence.HIGH)

        # --- Read / search / neutral commands ------------------------------
        if (
            exe in _BASH_READ_COMMANDS
            or exe in _BASH_SEARCH_COMMANDS
            or exe in _BASH_SEMANTIC_NEUTRAL_COMMANDS
        ):
            return _ClassifyResult("read", _Confidence.HIGH)

        # --- Package managers ----------------------------------------------
        if exe in _PACKAGE_MANAGER_COMMANDS:
            return _ClassifyResult("edit", _Confidence.MEDIUM)

        # --- Build tools ---------------------------------------------------
        if exe in _BUILD_TOOL_COMMANDS:
            return _ClassifyResult("edit", _Confidence.MEDIUM)

        # --- Other VCS commands (svn, hg, etc.) ----------------------------
        if exe in _VCS_COMMANDS:
            argv = segment.argv
            if len(argv) >= 2 and argv[1] in _VCS_READ_SUBCOMMANDS:
                return _ClassifyResult("read", _Confidence.MEDIUM)
            return _ClassifyResult("edit", _Confidence.MEDIUM)

        # --- sudo wrapping any command -------------------------------------
        if exe == "sudo":
            return _ClassifyResult("edit", _Confidence.MEDIUM)

    # If sudo was detected at the command level but we didn't match a known
    # executable, still return with reduced confidence
    if sudo_present:
        return _ClassifyResult("unknown", _Confidence.MEDIUM)

    return _ClassifyResult("unknown", _Confidence.LOW)


_AUTO_ALLOW_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "Glob",
        "Grep",
        "ListMcpResourcesTool",
        "Read",
        "ReadMcpResourceTool",
        "SendUserMessage",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
    }
)

_CONDITIONAL_AUTO_ALLOW_MAP: dict[
    str,
    tuple[bool, ...] | None,
] = {}


def _check_conditional_auto_allow(
    tool_name: str,
    tool_input: Mapping[str, Any],
    context: ToolPermissionContext,
) -> bool:
    if tool_name == "LSP":
        return True
    if tool_name == "NotebookEdit":
        return _tool_path_within_working_dirs(tool_input, context)
    # CtxInspect: auto-allow only when context window inspection is read-only
    # (always true for local/context window inspection)
    if tool_name == "CtxInspect":
        return True
    # Snip: auto-allow only when snip path is within working dirs
    if tool_name == "Snip":
        return _tool_path_within_working_dirs(tool_input, context)
    return False


def _mode_prefix(mode: str) -> str:
    if mode == "bubble":
        return "Subagent bubble mode"
    return "Auto mode"


def _approval_message(mode: str, tool_name: str) -> str:
    prefix = _mode_prefix(mode)
    if mode == "bubble":
        return (
            f"{prefix} requires parent-session approval before using {tool_name}."
        )
    return f"{prefix} requires manual approval before using {tool_name}."


def _approval_reason(mode: str, tool_name: str) -> OtherDecisionReason:
    prefix = _mode_prefix(mode)
    return OtherDecisionReason(
        reason=f"{prefix} requires manual approval for {tool_name}"
    )


def _tool_path_within_working_dirs(
    tool_input: Mapping[str, Any],
    context: ToolPermissionContext,
) -> bool:
    for key in ("file_path", "filePath", "path"):
        candidate = tool_input.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return path_in_allowed_working_path(candidate, context)
    return True


def _dangerous_bash_decision(
    command: str,
    *,
    mode: str,
) -> PermissionDecision | None:
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex or not analysis.commands:
        return None

    for segment in analysis.commands:
        executable = segment.executable
        if executable in {"rm", "rmdir"}:
            targets = [token for token in segment.argv[1:] if token and not token.startswith("-")]
            if any(
                is_dangerous_removal_path(os.path.abspath(os.path.expanduser(target)))
                for target in targets
            ):
                prefix = _mode_prefix(mode)
                return PermissionDenyDecision(
                    message=(
                        f"{prefix} denied Bash because the command appears destructive: "
                        f"{command}"
                    ),
                    decision_reason=OtherDecisionReason(
                        reason="Destructive Bash command denied by automated classifier"
                    ),
                )

        if executable in {"mkfs", "poweroff", "reboot", "shutdown", "halt"}:
            prefix = _mode_prefix(mode)
            return PermissionDenyDecision(
                message=(
                    f"{prefix} denied Bash because the command appears destructive: "
                    f"{command}"
                ),
                decision_reason=OtherDecisionReason(
                    reason="Destructive Bash command denied by automated classifier"
                ),
            )

    return None


def _split_simple_bash_parts(command: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    for token in command.split():
        if token in {"&&", "||", "|", ";", ">", ">>", ">&", "2>", "&>"}:
            if current:
                parts.append(" ".join(current))
                current = []
            parts.append(token)
            continue
        if token.startswith(">") or token.startswith("2>"):
            if current:
                parts.append(" ".join(current))
                current = []
            parts.append(token)
            continue
        current.append(token)
    if current:
        parts.append(" ".join(current))
    return parts


def _is_read_only_bash_command(command: str) -> bool:
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex:
        return False

    if not analysis.commands:
        return False
    if analysis.has_output_redirection:
        return False

    has_non_neutral_command = False

    for segment in analysis.commands:
        base_command = segment.executable
        if not base_command:
            continue
        if base_command in _BASH_SEMANTIC_NEUTRAL_COMMANDS:
            continue

        has_non_neutral_command = True
        if (
            base_command not in _BASH_SEARCH_COMMANDS
            and base_command not in _BASH_READ_COMMANDS
            and base_command not in _BASH_LIST_COMMANDS
        ):
            return False

    return has_non_neutral_command


def classify_automated_permission_decision(
    tool_name: str,
    tool_input: Mapping[str, Any],
    context: ToolPermissionContext,
    tool_result: PermissionDecision | PermissionPassthrough,
) -> PermissionDecision | None:
    """Classify a tool request for ``auto``/``bubble`` permission modes.

    ``None`` means the caller should keep the existing pipeline behavior.
    """

    if context.mode not in _AUTOMATED_PERMISSION_MODES:
        return None

    if isinstance(tool_result, (PermissionAllowDecision, PermissionDenyDecision)):
        return tool_result

    if isinstance(tool_result, PermissionAskDecision):
        if tool_name in {"Edit", "Write", "MultEdit", "NotebookEdit"}:
            return PermissionAskDecision(
                message=_approval_message(context.mode, tool_name),
                decision_reason=_approval_reason(context.mode, tool_name),
            )
        return tool_result

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return PermissionAskDecision(
                message=_approval_message(context.mode, tool_name),
                decision_reason=_approval_reason(context.mode, tool_name),
            )
        dangerous = _dangerous_bash_decision(command, mode=context.mode)
        if dangerous is not None:
            return dangerous
        if bool(tool_input.get("dangerously_disable_sandbox")):
            return PermissionAskDecision(
                message=_approval_message(context.mode, tool_name),
                decision_reason=_approval_reason(context.mode, tool_name),
            )

        result = _classify_bash_command_semantics_v2(command)
        category = result.category
        confidence = result.confidence
        danger_detail = result.danger_detail

        # Use enhanced security analysis to catch high-risk commands
        enhanced = analyze_bash_security(command)
        if enhanced.max_risk_score >= 8:
            prefix = _mode_prefix(context.mode)
            return PermissionDenyDecision(
                message=(
                    f"{prefix} denied Bash due to high security risk score "
                    f"(max_risk_score={enhanced.max_risk_score}): {command}"
                ),
                decision_reason=OtherDecisionReason(
                    reason="Bash command denied by enhanced security analysis"
                ),
            )

        if category in ("git", "read", "list") and confidence == _Confidence.HIGH:
            return PermissionAllowDecision(
                decision_reason=ModeDecisionReason(mode=context.mode),
            )
        if category == "destructive" and confidence == _Confidence.HIGH:
            prefix = _mode_prefix(context.mode)
            detail_msg = f" ({danger_detail})" if danger_detail else ""
            return PermissionDenyDecision(
                message=(
                    f"{prefix} denied Bash because the command is "
                    f"classified as destructive{detail_msg}: {command}"
                ),
                decision_reason=OtherDecisionReason(
                    reason="Destructive Bash command denied by semantic classifier"
                ),
            )

        return PermissionAskDecision(
            message=_approval_message(context.mode, tool_name),
            decision_reason=_approval_reason(context.mode, tool_name),
        )

    if tool_name in _AUTO_ALLOW_TOOLS and _tool_path_within_working_dirs(
        tool_input, context
    ):
        return PermissionAllowDecision(
            decision_reason=ModeDecisionReason(mode=context.mode),
        )

    if _check_conditional_auto_allow(tool_name, tool_input, context):
        return PermissionAllowDecision(
            decision_reason=ModeDecisionReason(mode=context.mode),
        )

    return PermissionAskDecision(
        message=_approval_message(context.mode, tool_name),
        decision_reason=_approval_reason(context.mode, tool_name),
    )
