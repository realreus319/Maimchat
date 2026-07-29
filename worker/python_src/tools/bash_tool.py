"""Bash tool — execute shell commands with sandbox and permission integration.

Python port of src/tools/BashTool/BashTool.tsx.

This module implements the core Bash tool contract: input validation,
permission checking, sandbox integration, command execution with timeout,
and source-backed error/result semantics.  The TS source has extensive
UI/React/progress/background-task machinery that is out of scope for
this port slice; the parity contract here covers:
  - isSearchOrReadBashCommand classification
  - isReadOnly detection
  - validateInput
  - checkPermissions (via bashPermissions)
  - call (sync subprocess execution)
  - mapToolResultToToolResultBlockParam output formatting
  - sandbox integration via accepted Task 18 adapter
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..utils.sandbox.sandbox_adapter import (
    annotate_stderr_with_sandbox_failures,
    is_sandboxing_enabled,
    should_use_sandbox,
)
from ..utils.bash_ast import parse_bash_for_security
from ..utils.permissions.shell_rule_matching import (
    match_command_against_rule,
)
from .file_read import _detect_image_dimensions, _detect_image_media_type
from .shared import IMAGE_EXTENSIONS

BASH_TOOL_NAME = "Bash"
DEFAULT_TIMEOUT_MS = 120_000
MAX_OUTPUT_LENGTH = 30_000
_MAX_BASH_IMAGE_DISPLAY_DIMENSION = 1024
_CLAUDE_CODE_HINT_RE = re.compile(
    r"<claude-code-hint(?P<attrs>[^>]*)>(?P<body>.*?)</claude-code-hint>|<claude-code-hint(?P<self_attrs>[^>]*)/>",
    re.IGNORECASE | re.DOTALL,
)
_CLAUDE_CODE_HINT_ATTR_RE = re.compile(
    r"""(?:message|text)\s*=\s*(['"])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)
_GIT_COMMIT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_GIT_PR_URL_RE = re.compile(r"https?://[^\s)]+/pull/\d+[^\s)]*", re.IGNORECASE)
_GIT_BRANCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bHEAD -> (?P<branch>[\w./-]+)\b"),
    re.compile(r"\bOn branch (?P<branch>[\w./-]+)\b"),
    re.compile(r"-> (?P<branch>[\w./-]+)(?:\s|$)"),
)
_CODE_INDEXING_TOOLS: frozenset[str] = frozenset(
    {
        "ast-grep",
        "codeql",
        "cscope",
        "ctags",
        "global",
        "gtags",
        "sg",
        "sourcegraph",
        "universal-ctags",
        "zoekt",
    }
)
_AUTO_BACKGROUND_BASE_COMMANDS: frozenset[str] = frozenset(
    {
        "cargo",
        "docker",
        "docker-compose",
        "jest",
        "jupyter",
        "make",
        "pnpm",
        "pytest",
        "terraform",
        "uvicorn",
        "vite",
        "webpack",
        "yarn",
    }
)
_AUTO_BACKGROUND_NPM_ACTIONS: frozenset[str] = frozenset(
    {"build", "dev", "serve", "start", "test", "watch"}
)


# ---------------------------------------------------------------------------
# Command classification sets — mirror TS source exactly
# ---------------------------------------------------------------------------

_BASH_SEARCH_COMMANDS: frozenset = frozenset(
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

_BASH_READ_COMMANDS: frozenset = frozenset(
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
    }
)

_BASH_LIST_COMMANDS: frozenset = frozenset({"ls", "tree", "du"})

_BASH_SEMANTIC_NEUTRAL_COMMANDS: frozenset = frozenset(
    {
        "echo",
        "printf",
        "true",
        "false",
        ":",
    }
)

_BASH_SILENT_COMMANDS: frozenset = frozenset(
    {
        "mv",
        "cp",
        "rm",
        "mkdir",
        "rmdir",
        "chmod",
        "chown",
        "chgrp",
        "touch",
        "ln",
        "cd",
        "export",
        "unset",
        "wait",
    }
)


# ---------------------------------------------------------------------------
# Command classification — mirrors isSearchOrReadBashCommand from TS
# ---------------------------------------------------------------------------


_BASH_CONTROL_OPERATORS = {"&&", "||", "|", ";", "(", ")"}
_BASH_OUTPUT_REDIRECT_OPERATORS = {">", ">>", ">&", "2>", "2>>", "&>"}
_BASH_INPUT_REDIRECT_OPERATORS = {"<", "<<", "<<<"}
_BASH_REDIRECT_OPERATORS = (
    _BASH_OUTPUT_REDIRECT_OPERATORS | _BASH_INPUT_REDIRECT_OPERATORS
)


def _split_simple_parts(command: str) -> List[str]:
    """Split a command into parts by common shell operators."""
    if "$(" in command or "`" in command:
        raise ValueError("command substitutions are not classified as read-only")

    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""

    parts: List[str] = []
    current: List[str] = []
    for tok in _coalesce_shell_tokens(list(lexer)):
        if tok in _BASH_CONTROL_OPERATORS or tok in _BASH_REDIRECT_OPERATORS:
            if current:
                parts.append(" ".join(current))
                current = []
            parts.append(tok)
        else:
            current.append(tok)
    if current:
        parts.append(" ".join(current))
    return parts


def _coalesce_shell_tokens(tokens: List[str]) -> List[str]:
    merged: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        next_token = tokens[i + 1] if i + 1 < len(tokens) else None
        if token.isdigit() and next_token in {">", ">>", "<", "<<"}:
            merged.append(f"{token}{next_token}")
            i += 2
            continue
        if token in {">", "<"} and next_token == token:
            merged.append(token * 2)
            i += 2
            continue
        if token == ">" and next_token == "&":
            merged.append(">&")
            i += 2
            continue
        if token == "&" and next_token == ">":
            merged.append("&>")
            i += 2
            continue
        merged.append(token)
        i += 1
    return merged


def is_search_or_read_bash_command(command: str) -> Dict[str, bool]:
    """Check if a bash command is a search or read operation.

    Mirrors ``isSearchOrReadBashCommand`` from the TS source.  For pipelines,
    ALL parts must be search/read commands for the whole command to be
    classified as collapsible.
    """
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex:
        return {"isSearch": False, "isRead": False, "isList": False}

    if not analysis.commands:
        return {"isSearch": False, "isRead": False, "isList": False}
    if analysis.has_output_redirection:
        return {"isSearch": False, "isRead": False, "isList": False}

    has_search = False
    has_read = False
    has_list = False
    has_non_neutral_command = False

    for segment in analysis.commands:
        base_command = segment.executable
        if not base_command:
            continue
        if base_command in _BASH_SEMANTIC_NEUTRAL_COMMANDS:
            continue
        has_non_neutral_command = True
        is_part_search = base_command in _BASH_SEARCH_COMMANDS
        is_part_read = base_command in _BASH_READ_COMMANDS
        is_part_list = base_command in _BASH_LIST_COMMANDS
        if not is_part_search and not is_part_read and not is_part_list:
            return {"isSearch": False, "isRead": False, "isList": False}
        if is_part_search:
            has_search = True
        if is_part_read:
            has_read = True
        if is_part_list:
            has_list = True

    if not has_non_neutral_command:
        return {"isSearch": False, "isRead": False, "isList": False}

    return {"isSearch": has_search, "isRead": has_read, "isList": has_list}


def is_silent_bash_command(command: str) -> bool:
    """Check if a bash command produces no stdout on success."""
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex:
        return False
    if not analysis.commands:
        return False
    if analysis.has_output_redirection:
        return False
    has_non_fallback = False
    last_operator: str | None = None
    for index, segment in enumerate(analysis.commands):
        if index > 0:
            last_operator = analysis.operators[index - 1] if index - 1 < len(analysis.operators) else None
        base_command = segment.executable
        if not base_command:
            continue
        if last_operator == "||" and base_command in _BASH_SEMANTIC_NEUTRAL_COMMANDS:
            continue
        has_non_fallback = True
        if base_command not in _BASH_SILENT_COMMANDS:
            return False
    return has_non_fallback


# ---------------------------------------------------------------------------
# Input / Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BashInput:
    command: str
    timeout: Optional[int] = None
    description: Optional[str] = None
    run_in_background: bool = False
    dangerously_disable_sandbox: bool = False
    cwd: Optional[str] = None


@dataclass(frozen=True)
class BashOutput:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    interrupted: bool = False
    timed_out: bool = False
    sandbox_denied: bool = False
    no_output_expected: bool = False
    semantic_success: bool = True
    semantic_status: Optional[str] = None
    semantic_message: Optional[str] = None
    claude_code_hints: Tuple[str, ...] = ()
    git_operation: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit_shas: Tuple[str, ...] = ()
    git_pr_urls: Tuple[str, ...] = ()
    git_index_lock_error: bool = False
    code_indexing_tool: Optional[str] = None
    sleep_pattern_detected: bool = False
    backgrounding_suggestion: Optional[str] = None
    result_type: str = "text"
    file_path: Optional[str] = None
    base64_data: Optional[str] = None
    media_type: Optional[str] = None
    dimensions: Optional[Dict[str, int]] = None
    original_size: int = 0


# ---------------------------------------------------------------------------
# ShellError — mirrors src/utils/errors.ts ShellError
# ---------------------------------------------------------------------------


class ShellError(Exception):
    """Error raised when a shell command fails.

    Mirrors the ShellError from src/utils/errors.ts.  Preserves stdout,
    stderr, exit code, and interrupted state from the failed command.
    """

    def __init__(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        interrupted: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.interrupted = interrupted
        super().__init__(stderr or f"Exit code {exit_code}")


# ---------------------------------------------------------------------------
# BashTool class
# ---------------------------------------------------------------------------


class BashTool:
    """Bash tool implementation — mirrors src/tools/BashTool/BashTool.tsx."""

    name: str = BASH_TOOL_NAME

    @staticmethod
    def is_concurrency_safe(raw_input: Mapping[str, Any]) -> bool:
        return BashTool.is_read_only(raw_input)

    @staticmethod
    def is_read_only(raw_input: Mapping[str, Any]) -> bool:
        cmd = raw_input.get("command", "")
        if not isinstance(cmd, str):
            return False
        classification = is_search_or_read_bash_command(cmd)
        return (
            classification["isSearch"]
            or classification["isRead"]
            or classification["isList"]
        )

    @staticmethod
    def validate_input(raw_input: Mapping[str, Any]) -> Tuple[bool, Mapping[str, Any]]:
        cmd = raw_input.get("command")
        if not isinstance(cmd, str) or not cmd:
            return (False, dict(raw_input))
        timeout = raw_input.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)):
                return (False, dict(raw_input))
            if timeout <= 0:
                return (False, dict(raw_input))
        return (True, dict(raw_input))

    @staticmethod
    def interrupt_behavior() -> str:
        return "cancel"

    @staticmethod
    def resolve_timeout(input: BashInput) -> int:
        return input.timeout if input.timeout is not None else DEFAULT_TIMEOUT_MS

    @staticmethod
    def is_failure(output: BashOutput) -> bool:
        return bool(
            not output.semantic_success
            or output.timed_out
            or output.interrupted
            or output.sandbox_denied
        )

    @staticmethod
    def check_permissions(
        input: BashInput,
        permission_context: Any,
    ) -> str:
        if permission_context is None:
            return "ask"
        deny_rules = getattr(permission_context, "deny_rules", [])
        if deny_rules:
            for rule in deny_rules:
                if match_command_against_rule(input.command, rule):
                    return "deny"
        allow_rules = getattr(permission_context, "allow_rules", [])
        for pattern in allow_rules:
            if match_command_against_rule(input.command, pattern):
                return "allow"
        return "ask"

    @classmethod
    def finalize_output(
        cls,
        *,
        command: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_ms: float,
        interrupted: bool = False,
        timed_out: bool = False,
        sandbox_denied: bool = False,
        use_sandbox: bool = False,
        no_output_expected: bool | None = None,
        cwd: str | None = None,
    ) -> BashOutput:
        stdout = (stdout or "").strip()
        stderr = (stderr or "").strip()
        if use_sandbox and stderr:
            stderr = annotate_stderr_with_sandbox_failures(command, stderr)
        stdout, xml_hints = _extract_claude_code_hints(stdout)
        pattern_hints = _generate_hints_from_output(stdout, stderr)
        claude_code_hints = xml_hints + pattern_hints
        image_output = (
            _detect_image_output(stdout, cwd=cwd or os.getcwd())
            if exit_code == 0 and not stderr and not interrupted and not timed_out and not sandbox_denied
            else {}
        )
        semantic_status, semantic_message, semantic_success = _interpret_command_result(
            command=command,
            exit_code=exit_code,
            stderr=stderr,
            interrupted=interrupted,
            timed_out=timed_out,
            sandbox_denied=sandbox_denied,
        )
        git_metadata = _detect_git_operation(command, stdout, stderr)
        sleep_pattern_detected = _detect_sleep_pattern(command)
        backgrounding_suggestion = None
        if sleep_pattern_detected and not interrupted and not timed_out:
            backgrounding_suggestion = (
                "Prefer background execution or a readiness check instead of a foreground sleep command."
            )
        return BashOutput(
            result_type=image_output.get("result_type", "text"),
            file_path=image_output.get("file_path"),
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            interrupted=interrupted,
            timed_out=timed_out,
            sandbox_denied=sandbox_denied,
            no_output_expected=(
                is_silent_bash_command(command)
                if no_output_expected is None
                else no_output_expected
            ),
            semantic_success=semantic_success,
            semantic_status=semantic_status,
            semantic_message=semantic_message,
            claude_code_hints=claude_code_hints,
            git_operation=git_metadata.get("operation"),
            git_branch=git_metadata.get("branch"),
            git_commit_shas=git_metadata.get("commit_shas", ()),
            git_pr_urls=git_metadata.get("pr_urls", ()),
            git_index_lock_error=bool(git_metadata.get("index_lock_error")),
            code_indexing_tool=_detect_code_indexing_tool(command),
            sleep_pattern_detected=sleep_pattern_detected,
            backgrounding_suggestion=backgrounding_suggestion,
            base64_data=image_output.get("base64_data"),
            media_type=image_output.get("media_type"),
            dimensions=image_output.get("dimensions"),
            original_size=image_output.get("original_size", 0),
        )

    @classmethod
    def call(cls, input: BashInput) -> BashOutput:
        start = time.monotonic()
        timeout_ms = cls.resolve_timeout(input)
        command = input.command

        # Sandbox integration
        use_sandbox = not input.dangerously_disable_sandbox and should_use_sandbox(
            command
        )

        if use_sandbox and is_sandboxing_enabled():
            # Wrap command with sandbox — the accepted sandbox adapter's
            # wrap_with_sandbox is async but we run synchronously here;
            # fall back to direct execution when sandbox is available
            # but we can't await (matches TS behavior where sandbox
            # wrapping happens before exec).
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                try:
                    command = loop.run_until_complete(_wrap_sandbox_command(command))
                finally:
                    loop.close()
            except Exception as exc:
                return cls.finalize_output(
                    command=input.command,
                    stdout="",
                    stderr="Sandbox error: {}".format(exc),
                    exit_code=1,
                    duration_ms=(time.monotonic() - start) * 1000,
                    sandbox_denied=True,
                    cwd=input.cwd or os.getcwd(),
                )

        if input.run_in_background:
            return BashOutput(
                stdout="",
                stderr="",
                exit_code=0,
                duration_ms=0.0,
                semantic_success=True,
            )

        # Execute command — mirrors src/utils/Shell.ts exec()
        try:
            env = dict(os.environ)
            env["CI"] = "1"
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000.0,
                env=env,
                cwd=input.cwd or os.getcwd(),
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode if proc.returncode is not None else 0
            timed_out = False
            interrupted = False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            exit_code = -1
            timed_out = True
            interrupted = False
        except Exception as exc:
            return cls.finalize_output(
                command=input.command,
                stdout="",
                stderr="Process error: {}".format(exc),
                exit_code=1,
                duration_ms=(time.monotonic() - start) * 1000,
                cwd=input.cwd or os.getcwd(),
            )

        duration_ms = (time.monotonic() - start) * 1000

        # Detect interruption (SIGINT)
        if exit_code == -signal.SIGINT:
            interrupted = True
            exit_code = 130
        elif exit_code < 0:
            interrupted = True

        return cls.finalize_output(
            command=input.command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            interrupted=interrupted,
            timed_out=timed_out,
            use_sandbox=use_sandbox,
            cwd=input.cwd or os.getcwd(),
        )

    @staticmethod
    def map_result(output: BashOutput) -> str:
        """Map output to a tool-result content string.

        Mirrors mapToolResultToToolResultBlockParam from the TS source.
        """
        parts: List[str] = []

        if output.stdout and output.result_type != "image":
            processed = output.stdout
            # Strip leading whitespace/newlines (matches TS behavior)
            while processed and processed[0] in ("\n", "\r", " ", "\t"):
                if processed[0] in ("\n", "\r"):
                    processed = processed[1:]
                else:
                    break
            processed = processed.rstrip()
            parts.append(processed)

        error_message = output.stderr.strip() if output.stderr else ""
        if output.interrupted:
            if error_message:
                error_message += "\n"
            error_message += "<error>Command was aborted before completion</error>"

        if error_message:
            parts.append(error_message)

        content = "\n".join(parts)
        if not content:
            if output.result_type == "image":
                if output.file_path:
                    return f"Image output: {output.file_path}"
                return "Image output generated."
            if output.semantic_message:
                return output.semantic_message
            if output.no_output_expected:
                return "Done"
            if not output.semantic_success and output.exit_code != 0:
                return f"Exit code {output.exit_code}"
            return "(No output)"
        return content


def _extract_claude_code_hints(text: str) -> tuple[str, tuple[str, ...]]:
    if not text:
        return "", ()
    hints: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        attrs = match.group("attrs") or match.group("self_attrs") or ""
        body = (match.group("body") or "").strip()
        hint = body
        if not hint:
            attr_match = _CLAUDE_CODE_HINT_ATTR_RE.search(attrs)
            if attr_match is not None:
                hint = attr_match.group(2).strip()
        if hint:
            hints.append(hint)
        return ""

    stripped = _CLAUDE_CODE_HINT_RE.sub(_replace, text)
    return stripped.strip(), tuple(hints)


# Pattern-based hint rules: (compiled_regex, hint_message)
_OUTPUT_HINT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"command not found", re.IGNORECASE),
        "Did you install this command? Check your PATH or use a package manager to install it.",
    ),
    (
        re.compile(r"permission denied", re.IGNORECASE),
        "Check file permissions with 'ls -la'. You may need to use 'sudo' or change file ownership.",
    ),
    (
        re.compile(r"no such file or directory", re.IGNORECASE),
        "Verify the file path is correct. The file may have been moved, renamed, or deleted.",
    ),
    (
        re.compile(r"connection refused", re.IGNORECASE),
        "Is the server running? Check if the service is active and the port is correct.",
    ),
    (
        re.compile(r"connection timeout", re.IGNORECASE),
        "Check network connectivity. The server may be unreachable or behind a firewall.",
    ),
    (
        re.compile(r"resource temporarily unavailable", re.IGNORECASE),
        "The system is under load. Try again in a moment or close other resource-heavy processes.",
    ),
    (
        re.compile(r"out of memory", re.IGNORECASE),
        "System is low on memory. Close other applications or increase available RAM.",
    ),
    (
        re.compile(r"killed\s*$", re.IGNORECASE | re.MULTILINE),
        "Process was killed (likely OOM). Check system resources or try a lighter-weight alternative.",
    ),
    (
        re.compile(r"address already in use", re.IGNORECASE),
        "Another process is using this port. Find and stop it with 'lsof -i :PORT' or use a different port.",
    ),
    (
        re.compile(r"unable to access.*network", re.IGNORECASE),
        "Check your network connection. Proxy or firewall settings may be blocking access.",
    ),
)


def _generate_hints_from_output(stdout: str, stderr: str) -> tuple[str, ...]:
    combined = f"{stdout}\n{stderr}"
    hints: list[str] = []
    for pattern, hint in _OUTPUT_HINT_PATTERNS:
        if pattern.search(combined):
            hints.append(hint)
    return tuple(hints)


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.strip().split()


def _primary_command(command: str) -> str:
    analysis = parse_bash_for_security(command)
    if not analysis.is_too_complex:
        for segment in analysis.commands:
            if segment.executable:
                return segment.executable
    for part in _split_simple_parts(command):
        if part in {">", ">>", ">&", "2>", "&>", "||", "&&", "|", ";"}:
            continue
        tokens = _command_tokens(part)
        if tokens:
            return tokens[0]
    return ""


def _interpret_command_result(
    *,
    command: str,
    exit_code: int,
    stderr: str,
    interrupted: bool,
    timed_out: bool,
    sandbox_denied: bool,
) -> tuple[str | None, str | None, bool]:
    if timed_out:
        return None, None, False
    if sandbox_denied:
        return None, None, False
    # Check for SIGINT (exit code 130 = 128 + 2 for SIGINT)
    if exit_code == 130 or (interrupted and exit_code == -signal.SIGINT):
        return (
            "sigint",
            "Command was interrupted (SIGINT).",
            False,
        )
    if interrupted:
        return None, None, False
    if exit_code == 0:
        return None, None, True
    if exit_code == 126:
        return (
            "not_executable",
            "Command found but is not executable.",
            False,
        )
    if exit_code == 127:
        return ("command_not_found", "Command not found.", False)
    if stderr.strip():
        return None, None, False

    base_command = _primary_command(command)
    if base_command in _BASH_SEARCH_COMMANDS and exit_code == 1:
        return ("no_matches", "No matches found.", True)
    if base_command in {"diff", "cmp"} and exit_code == 1:
        return ("differences_found", "Differences found.", True)
    if base_command in {"test", "["} and exit_code == 1:
        return ("condition_false", "Condition evaluated to false.", True)
    return None, None, False


# Git global option patterns: -C <path>, --git-dir=<path>, --work-tree=<path>
_GIT_GLOBAL_OPTION_RE = re.compile(
    r"""\b-C\s+(\S+)|\b--git-dir[= ](\S+)|\b--work-tree[= ](\S+)"""
)


def _detect_git_operation(
    command: str,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    tokens = _command_tokens(command)
    if len(tokens) < 2:
        return {}

    base = os.path.basename(tokens[0])

    # Detect GitHub CLI (gh pr ...)
    if base == "gh":
        operation = tokens[1] if len(tokens) > 1 else None
        if operation == "pr":
            # gh pr create, gh pr view, gh pr merge, etc.
            pr_action = tokens[2] if len(tokens) > 2 else None
            if pr_action in {"create", "view", "merge", "close", "reopen", "edit", "checkout"}:
                combined = "\n".join(part for part in (stdout, stderr) if part)
                pr_urls = tuple(dict.fromkeys(_GIT_PR_URL_RE.findall(combined)))
                return {
                    "operation": f"gh pr {pr_action}",
                    "pr_urls": pr_urls,
                }
            elif pr_action is not None:
                combined = "\n".join(part for part in (stdout, stderr) if part)
                pr_urls = tuple(dict.fromkeys(_GIT_PR_URL_RE.findall(combined)))
                return {
                    "operation": f"gh pr {pr_action}",
                    "pr_urls": pr_urls,
                }
        return {}

    # Detect git commands
    if base != "git":
        return {}

    # Parse git command with global options
    # Global options (-C, --git-dir, --work-tree) appear before the subcommand
    operation = None
    global_options: list[str] = []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in ("-C", "--git-dir", "--work-tree") and i + 1 < len(tokens):
            global_options.append(f"{token} {tokens[i + 1]}")
            i += 2
        elif token.startswith("--git-dir=") or token.startswith("--work-tree="):
            global_options.append(token)
            i += 1
        elif token == "--":
            i += 1
            break
        elif not token.startswith("-"):
            operation = token
            break
        else:
            i += 1

    if operation is None:
        return {}

    # Expand operation set to include more git operations
    all_git_operations = {
        "commit", "push", "cherry-pick", "merge", "rebase",
        "fetch", "pull", "clone", "init", "add", "status",
        "branch", "checkout", "switch", "restore", "reset",
        "stash", "tag", "log", "diff", "show", "blame",
        "rebase", "am", "bisect", "archive", "bundle",
    }

    if operation not in all_git_operations:
        return {}

    # Check for destructive operations
    is_destructive = False
    if operation == "push":
        # Check for --force or -f flags
        full_command_lower = command.lower()
        if "--force" in full_command_lower or "-f" in tokens[2:] if len(tokens) > 2 else False:
            is_destructive = True
            operation = "push --force"

    combined = "\n".join(part for part in (stdout, stderr) if part)
    branch = None
    for pattern in _GIT_BRANCH_PATTERNS:
        match = pattern.search(combined)
        if match is not None:
            branch = match.group("branch")
            break
    commit_shas = tuple(dict.fromkeys(_GIT_COMMIT_SHA_RE.findall(combined)))
    pr_urls = tuple(dict.fromkeys(_GIT_PR_URL_RE.findall(combined)))
    index_lock_error = ".git/index.lock" in combined

    result: dict[str, Any] = {
        "operation": operation,
        "branch": branch,
        "commit_shas": commit_shas,
        "pr_urls": pr_urls,
        "index_lock_error": index_lock_error,
    }

    # Add global options if detected
    if global_options:
        result["git_global_options"] = global_options

    if is_destructive:
        result["is_destructive"] = True

    return result


def _detect_code_indexing_tool(command: str) -> str | None:
    for token in _command_tokens(command):
        base = os.path.basename(token)
        if base in _CODE_INDEXING_TOOLS:
            return base
    return None


def _detect_sleep_pattern(command: str) -> bool:
    tokens = _command_tokens(command)
    if not tokens:
        return False
    return os.path.basename(tokens[0]) == "sleep"


def should_auto_background_bash_command(command: str) -> bool:
    """Conservatively classify long-running commands for auto-backgrounding."""
    tokens = _command_tokens(command)
    if not tokens or _detect_sleep_pattern(command):
        return False

    base = os.path.basename(tokens[0]).lower()
    lowered_tokens = tuple(token.lower() for token in tokens[1:])
    normalized = " ".join(lowered_tokens)

    if base in _AUTO_BACKGROUND_BASE_COMMANDS:
        return True
    if base in {"npm", "npx"} and any(
        token in _AUTO_BACKGROUND_NPM_ACTIONS for token in lowered_tokens
    ):
        return True
    if base == "tail" and any(token == "-f" or token.startswith("-f") for token in lowered_tokens):
        return True
    if base in {"python", "python3"} and any(
        marker in normalized
        for marker in (
            "http.server",
            "jupyter",
            "runserver",
            "streamlit",
            "uvicorn",
        )
    ):
        return True
    if base in {"flask", "gunicorn", "streamlit"}:
        return True
    if "manage.py" in lowered_tokens and "runserver" in lowered_tokens:
        return True
    return False


def _detect_image_output(stdout: str, *, cwd: str) -> dict[str, Any]:
    candidate = _normalize_image_output_path(stdout, cwd=cwd)
    if candidate is None:
        return {}
    try:
        with open(candidate, "rb") as handle:
            raw_bytes = handle.read()
    except OSError:
        return {}
    if not raw_bytes:
        return {}
    media_type = _detect_image_media_type(raw_bytes, os.path.splitext(candidate)[1])
    if not media_type.startswith("image/"):
        return {}
    dimensions = _resize_image_dimensions(_detect_image_dimensions(raw_bytes, media_type))
    return {
        "result_type": "image",
        "file_path": candidate,
        "base64_data": base64.b64encode(raw_bytes).decode("ascii"),
        "media_type": media_type,
        "dimensions": dimensions,
        "original_size": len(raw_bytes),
    }


def _normalize_image_output_path(stdout: str, *, cwd: str) -> str | None:
    stripped = stdout.strip()
    if not stripped:
        return None
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    candidate = lines[0]
    if candidate.startswith(("http://", "https://", "data:")):
        return None
    resolved = candidate
    if not os.path.isabs(resolved):
        resolved = os.path.abspath(os.path.join(cwd, resolved))
    if not os.path.isfile(resolved):
        return None
    extension = os.path.splitext(resolved)[1].lower().lstrip(".")
    if extension not in IMAGE_EXTENSIONS:
        return None
    return resolved


def _resize_image_dimensions(
    dimensions: dict[str, int] | None,
) -> dict[str, int] | None:
    if not isinstance(dimensions, dict):
        return dimensions
    width = dimensions.get("original_width")
    height = dimensions.get("original_height")
    if not isinstance(width, int) or not isinstance(height, int):
        return dimensions
    if width <= _MAX_BASH_IMAGE_DISPLAY_DIMENSION and height <= _MAX_BASH_IMAGE_DISPLAY_DIMENSION:
        return dimensions
    scale = min(
        _MAX_BASH_IMAGE_DISPLAY_DIMENSION / max(width, 1),
        _MAX_BASH_IMAGE_DISPLAY_DIMENSION / max(height, 1),
    )
    display_width = max(1, int(width * scale))
    display_height = max(1, int(height * scale))
    return {
        **dimensions,
        "display_width": display_width,
        "display_height": display_height,
    }


async def _wrap_sandbox_command(command: str) -> str:
    """Async wrapper for sandbox command wrapping."""
    from ..utils.sandbox.sandbox_adapter import wrap_with_sandbox

    return await wrap_with_sandbox(command)
