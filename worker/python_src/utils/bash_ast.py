"""Conservative bash command analysis used by permission-sensitive paths.

The TypeScript source has a large tree-sitter-compatible parser.  The Python
port intentionally implements a smaller fail-closed analyzer: it extracts
simple command segments for rule matching and refuses to classify any syntax
that can hide execution or mutate parsing after expansion.

Enhanced analysis (BashSecurityAnalysis) provides deeper AST-level inspection
including command chaining detection, subshell recognition, IO redirection
tracking, environment variable manipulation, process substitution detection,
and per-command security scoring — all without a tree-sitter dependency.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, Sequence


CONTROL_OPERATORS = frozenset({"&&", "||", "|", ";"})
GROUPING_OPERATORS = frozenset({"(", ")"})
OUTPUT_REDIRECT_OPERATORS = frozenset({">", ">>", ">&", "2>", "2>>", "&>"})
INPUT_REDIRECT_OPERATORS = frozenset({"<"})
HEREDOC_OPERATORS = frozenset({"<<", "<<<"})
REDIRECT_OPERATORS = OUTPUT_REDIRECT_OPERATORS | INPUT_REDIRECT_OPERATORS | HEREDOC_OPERATORS
_SAFE_ASCII_WHITESPACE = frozenset({" ", "\t", "\r", "\n"})
_BRACE_EXPANSION_RE = re.compile(r"(?<!\\)\{[^{}]*(?:,|\.\.)[^{}]*\}")


@dataclass(frozen=True)
class BashCommandSegment:
    """One executable command segment from a simple bash expression."""

    argv: tuple[str, ...]
    text: str
    redirects: tuple[str, ...] = ()

    @property
    def executable(self) -> str:
        return self.argv[0] if self.argv else ""


@dataclass(frozen=True)
class BashAnalysis:
    """Result of fail-closed bash analysis."""

    original: str
    commands: tuple[BashCommandSegment, ...] = ()
    operators: tuple[str, ...] = ()
    is_too_complex: bool = False
    complex_reason: str | None = None
    tokens: tuple[str, ...] = ()
    has_output_redirection: bool = False
    has_input_redirection: bool = False

    @property
    def is_simple(self) -> bool:
        return not self.is_too_complex

    def command_texts(self) -> tuple[str, ...]:
        return tuple(command.text for command in self.commands if command.text)


def parse_bash_for_security(command: str) -> BashAnalysis:
    """Analyze *command* and extract simple command segments.

    Unsupported shell constructs are deliberately marked ``too_complex`` so
    callers can ask the user instead of auto-allowing a possibly misparsed
    command.
    """

    if not isinstance(command, str) or not command.strip():
        return BashAnalysis(original=str(command or ""), is_too_complex=True, complex_reason="empty command")

    normalized = command.strip()
    reason = _preflight_complex_reason(normalized)
    if reason is not None:
        return BashAnalysis(original=normalized, is_too_complex=True, complex_reason=reason)

    try:
        raw_tokens = _coalesce_shell_tokens(_shlex_tokens(normalized))
    except ValueError as exc:
        return BashAnalysis(original=normalized, is_too_complex=True, complex_reason=str(exc))

    commands: list[BashCommandSegment] = []
    operators: list[str] = []
    current: list[str] = []
    redirects: list[str] = []
    has_output_redirection = False
    has_input_redirection = False
    expect_redirect_target = False
    group_depth = 0

    def flush_current() -> None:
        nonlocal current, redirects
        argv = tuple(token for token in current if token)
        if argv:
            commands.append(
                BashCommandSegment(
                    argv=argv,
                    text=shlex.join(argv),
                    redirects=tuple(redirects),
                )
            )
        current = []
        redirects = []

    for token in raw_tokens:
        if token == "(":
            if current:
                return BashAnalysis(
                    original=normalized,
                    tokens=tuple(raw_tokens),
                    is_too_complex=True,
                    complex_reason="group opener after command arguments",
                )
            group_depth += 1
            continue
        if token == ")":
            if group_depth <= 0:
                return BashAnalysis(
                    original=normalized,
                    tokens=tuple(raw_tokens),
                    is_too_complex=True,
                    complex_reason="unbalanced shell grouping",
                )
            flush_current()
            group_depth -= 1
            continue
        if token in HEREDOC_OPERATORS:
            return BashAnalysis(
                original=normalized,
                tokens=tuple(raw_tokens),
                is_too_complex=True,
                complex_reason="heredoc is not auto-classified",
            )
        if expect_redirect_target:
            redirects.append(token)
            expect_redirect_target = False
            continue
        if token in CONTROL_OPERATORS:
            flush_current()
            operators.append(token)
            continue
        if token in OUTPUT_REDIRECT_OPERATORS:
            has_output_redirection = True
            redirects.append(token)
            expect_redirect_target = True
            continue
        if token in INPUT_REDIRECT_OPERATORS:
            has_input_redirection = True
            redirects.append(token)
            expect_redirect_target = True
            continue
        if token in {"&"}:
            return BashAnalysis(
                original=normalized,
                tokens=tuple(raw_tokens),
                is_too_complex=True,
                complex_reason="background execution is not auto-classified",
            )
        current.append(token)

    if expect_redirect_target:
        return BashAnalysis(
            original=normalized,
            tokens=tuple(raw_tokens),
            is_too_complex=True,
            complex_reason="missing redirection target",
        )
    if group_depth != 0:
        return BashAnalysis(
            original=normalized,
            tokens=tuple(raw_tokens),
            is_too_complex=True,
            complex_reason="unbalanced shell grouping",
        )
    flush_current()

    if not commands:
        return BashAnalysis(
            original=normalized,
            tokens=tuple(raw_tokens),
            is_too_complex=True,
            complex_reason="no executable command segment",
        )

    return BashAnalysis(
        original=normalized,
        commands=tuple(commands),
        operators=tuple(operators),
        tokens=tuple(raw_tokens),
        has_output_redirection=has_output_redirection,
        has_input_redirection=has_input_redirection,
    )


def iter_analyzable_command_texts(command: str) -> tuple[str, ...]:
    """Return subcommands for rule matching, falling back to the raw command."""

    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex:
        return (command.strip(),) if command.strip() else ()
    texts = analysis.command_texts()
    return texts or ((command.strip(),) if command.strip() else ())


def command_has_unsafe_expansion(command: str) -> bool:
    return _preflight_complex_reason(command) is not None


def _shlex_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _coalesce_shell_tokens(tokens: Sequence[str]) -> list[str]:
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        next_token = tokens[i + 1] if i + 1 < len(tokens) else None
        third_token = tokens[i + 2] if i + 2 < len(tokens) else None
        if token.isdigit() and next_token in {">", ">>", "<", "<<"}:
            merged.append(f"{token}{next_token}")
            i += 2
            continue
        if token in {">", "<"} and next_token == token and third_token == token:
            merged.append(token * 3)
            i += 3
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
        if token == "&" and next_token == "&":
            merged.append("&&")
            i += 2
            continue
        if token == "|" and next_token == "|":
            merged.append("||")
            i += 2
            continue
        merged.append(token)
        i += 1
    return merged


def _preflight_complex_reason(command: str) -> str | None:
    for ch in command:
        if ch == "\x00" or (ord(ch) < 32 and ch not in _SAFE_ASCII_WHITESPACE):
            return "control character in command"
        if ch.isspace() and ch not in _SAFE_ASCII_WHITESPACE:
            return "unicode whitespace in command"

    quote_state = _scan_quotes_and_expansions(command)
    if quote_state is not None:
        return quote_state

    if "<(" in command or ">(" in command:
        return "process substitution is not auto-classified"
    if "<<" in command:
        return "heredoc is not auto-classified"
    if _BRACE_EXPANSION_RE.search(command):
        return "brace expansion is not auto-classified"
    return None


def _scan_quotes_and_expansions(command: str) -> str | None:
    single = False
    double = False
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == "'" and not double:
            single = not single
            i += 1
            continue
        if ch == '"' and not single:
            double = not double
            i += 1
            continue
        if ch == "`" and not single:
            return "backtick command substitution is not auto-classified"
        if ch == "$" and not single:
            if nxt in {"(", "{", "["}:
                return "shell expansion is not auto-classified"
            if nxt and (nxt.isalpha() or nxt in {"_", "?", "#", "*", "@", "!", "$"}):
                return "parameter expansion is not auto-classified"
        i += 1
    if single or double:
        return "unbalanced shell quotes"
    if escaped:
        return "trailing shell escape"
    return None


def executable_names(command: str) -> tuple[str, ...]:
    analysis = parse_bash_for_security(command)
    if analysis.is_too_complex:
        return ()
    return tuple(segment.executable for segment in analysis.commands if segment.executable)


def all_executables_in(command: str, allowed: Iterable[str]) -> bool:
    allowed_set = set(allowed)
    names = executable_names(command)
    return bool(names) and all(name in allowed_set for name in names)


# ---------------------------------------------------------------------------
# Enhanced security analysis (B1-H1/H2)
# ---------------------------------------------------------------------------

_CHAINING_RE = re.compile(r"""(?:&&|\|\||[;|])""")
_SUBSHELL_RE = re.compile(r"""\$\(|`""")
_REDIRECT_RE = re.compile(r"""(?:^|\s)(?:\d*&?>{1,2}|&>{1,2}|\d*>&|<{1,2}|>{1,2})\s""")
_ENV_ASSIGN_RE = re.compile(r"""^[A-Za-z_][A-Za-z0-9_]*=\S""")
_PROCESS_SUB_RE = re.compile(r"""[<>]\(""")

_RISK_READ = 1
_RISK_LIST = 1
_RISK_SEARCH = 2
_RISK_EDIT = 5
_RISK_DESTRUCTIVE = 9
_RISK_UNKNOWN = 6
_RISK_SUDO_BONUS = 3
_RISK_CHAINING_BONUS = 1
_RISK_SUBSHELL_BONUS = 2
_RISK_REDIRECT_BONUS = 1
_RISK_ENV_BONUS = 1

_RISK_CATEGORY_MAP: dict[str, int] = {
    "read": _RISK_READ,
    "list": _RISK_LIST,
    "search": _RISK_SEARCH,
    "edit": _RISK_EDIT,
    "destructive": _RISK_DESTRUCTIVE,
    "git": _RISK_SEARCH,
    "unknown": _RISK_UNKNOWN,
}

_SECURITY_COMMAND_SETS: dict[str, frozenset[str]] = {
    "read": frozenset({
        "cat", "head", "tail", "less", "more", "wc", "stat", "file",
        "strings", "jq", "awk", "cut", "sort", "uniq", "tr",
        "hexdump", "xxd", "od", "md5sum", "sha256sum", "sha1sum",
        "b2sum", "cksum",
    }),
    "list": frozenset({"ls", "tree", "du", "df", "free", "uptime", "uname", "hostname"}),
    "search": frozenset({
        "find", "grep", "rg", "ag", "ack", "locate", "which", "whereis",
    }),
    "neutral": frozenset({
        "echo", "printf", "true", "false", ":", "date", "whoami", "id",
        "pwd", "env", "printenv",
    }),
    "edit": frozenset({
        "sed", "tee", "mv", "cp", "install", "chmod", "chown", "chgrp",
        "ln", "mkdir", "touch", "truncate",
    }),
    "destructive": frozenset({
        "rm", "rmdir", "mkfs", "poweroff", "reboot", "shutdown",
        "halt", "init", "systemctl", "killall", "pkill", "dd", "shred",
    }),
}

_SUDO_RE = re.compile(r"""^\s*sudo(\s+-[A-Za-z])?(\s+--)?\s+""")


@dataclass(frozen=True)
class CommandSecurityInfo:
    """Security classification for a single parsed command."""

    executable: str
    argv: tuple[str, ...]
    text: str
    category: str
    security_score: int

    @property
    def is_read_like(self) -> bool:
        return self.category in {"read", "list", "search", "neutral"}

    @property
    def is_destructive(self) -> bool:
        return self.category == "destructive"


@dataclass(frozen=True)
class BashSecurityAnalysis:
    """Enhanced bash security analysis with per-command scoring.

    Provides deeper AST-level inspection without tree-sitter: detects command
    chaining, subshell invocations, IO redirection, environment variable
    manipulation, and process substitution.  Each command segment receives a
    0-10 risk score based on its semantic category and structural modifiers.
    """

    original: str
    commands: tuple[CommandSecurityInfo, ...] = ()
    has_chaining: bool = False
    has_subshell: bool = False
    has_redirection: bool = False
    has_env_manipulation: bool = False
    has_process_substitution: bool = False
    is_too_complex: bool = False

    @property
    def max_risk_score(self) -> int:
        if not self.commands:
            return 10 if self.is_too_complex else 0
        return max(cmd.security_score for cmd in self.commands)

    @property
    def is_safe(self) -> bool:
        if self.is_too_complex:
            return False
        return self.max_risk_score <= _RISK_SEARCH


def _detect_structural_features(command: str) -> tuple[bool, bool, bool, bool, bool]:
    has_chaining = bool(_CHAINING_RE.search(command))
    has_subshell = bool(_SUBSHELL_RE.search(command))
    has_redirection = bool(_REDIRECT_RE.search(command))
    has_env = _detect_env_manipulation(command)
    has_proc_sub = bool(_PROCESS_SUB_RE.search(command))
    return has_chaining, has_subshell, has_redirection, has_env, has_proc_sub


def _detect_env_manipulation(command: str) -> bool:
    try:
        raw_tokens = _shlex_tokens(command)
    except (ValueError, Exception):
        tokens = command.split()
    else:
        tokens = _coalesce_shell_tokens(raw_tokens)
    for tok in tokens:
        if tok in CONTROL_OPERATORS or tok in REDIRECT_OPERATORS or tok in {"&", "(", ")"}:
            break
        if _ENV_ASSIGN_RE.match(tok):
            return True
    return False


def _classify_executable(executable: str) -> str:
    for category, cmd_set in _SECURITY_COMMAND_SETS.items():
        if executable in cmd_set:
            if category == "neutral":
                return "read"
            return category
    return "unknown"


def _compute_risk_score(category: str, *, has_sudo: bool = False) -> int:
    base = _RISK_CATEGORY_MAP.get(category, _RISK_UNKNOWN)
    if has_sudo:
        base += _RISK_SUDO_BONUS
    return min(base, 10)


def _classify_segment(segment: BashCommandSegment, command: str) -> CommandSecurityInfo:
    exe = segment.executable
    argv = segment.argv
    category = _classify_executable(exe)
    if exe == "sudo":
        inner_exe = argv[1] if len(argv) > 1 else ""
        if inner_exe:
            category = _classify_executable(inner_exe)
            score = _compute_risk_score(category, has_sudo=True)
        else:
            score = _compute_risk_score("unknown", has_sudo=True)
    elif exe == "git" and len(argv) >= 2:
        sub = argv[1]
        destructive_subs = {
            "push --force", "push -f", "reset --hard", "clean -f",
            "clean -fd", "checkout --", "rebase", "filter-branch",
            "submodule deinit",
        }
        combined = " ".join(str(a) for a in argv[1:3]) if len(argv) >= 3 else sub
        if combined in destructive_subs or sub in destructive_subs:
            category = "destructive"
        elif sub in {
            "push", "commit", "merge", "add", "stash pop", "stash drop",
            "branch -D", "branch -d", "tag -d",
        }:
            category = "edit"
        else:
            category = "git"
        score = _compute_risk_score(category)
    else:
        score = _compute_risk_score(category)
    return CommandSecurityInfo(
        executable=exe,
        argv=argv,
        text=segment.text,
        category=category,
        security_score=score,
    )


def analyze_bash_security(command: str) -> BashSecurityAnalysis:
    """Perform enhanced security analysis on *command*.

    Returns a `BashSecurityAnalysis` with per-command classification and risk
    scores, plus structural feature detection (chaining, subshell, redirection,
    env manipulation, process substitution).
    """
    if not isinstance(command, str) or not command.strip():
        return BashSecurityAnalysis(
            original=str(command or ""),
            is_too_complex=True,
        )

    normalized = command.strip()
    (
        has_chaining,
        has_subshell,
        has_redirection,
        has_env_manipulation,
        has_process_substitution,
    ) = _detect_structural_features(normalized)

    analysis = parse_bash_for_security(normalized)

    if analysis.is_too_complex:
        score = 10
        if has_subshell:
            score = 10
        elif has_chaining:
            score = min(score, 8)
        elif has_redirection:
            score = min(score, 7)
        elif has_env_manipulation:
            score = min(score, 6)
        else:
            score = min(score, 8)
        return BashSecurityAnalysis(
            original=normalized,
            has_chaining=has_chaining,
            has_subshell=has_subshell,
            has_redirection=has_redirection,
            has_env_manipulation=has_env_manipulation,
            has_process_substitution=has_process_substitution,
            is_too_complex=True,
            commands=(
                CommandSecurityInfo(
                    executable="",
                    argv=(),
                    text=normalized,
                    category="unknown",
                    security_score=score,
                ),
            ),
        )

    classified_commands: list[CommandSecurityInfo] = []
    for segment in analysis.commands:
        info = _classify_segment(segment, normalized)
        bonus = 0
        if has_chaining:
            bonus += _RISK_CHAINING_BONUS
        if has_subshell:
            bonus += _RISK_SUBSHELL_BONUS
        if has_redirection:
            bonus += _RISK_REDIRECT_BONUS
        if has_env_manipulation:
            bonus += _RISK_ENV_BONUS
        adjusted_score = min(info.security_score + bonus, 10)
        classified_commands.append(
            CommandSecurityInfo(
                executable=info.executable,
                argv=info.argv,
                text=info.text,
                category=info.category,
                security_score=adjusted_score,
            )
        )

    return BashSecurityAnalysis(
        original=normalized,
        commands=tuple(classified_commands),
        has_chaining=has_chaining,
        has_subshell=has_subshell,
        has_redirection=has_redirection,
        has_env_manipulation=has_env_manipulation,
        has_process_substitution=has_process_substitution,
    )
