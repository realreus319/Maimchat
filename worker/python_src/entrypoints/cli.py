"""CLI entrypoint — thin bootstrap that dispatches fast paths before heavy imports.

Python port of src/entrypoints/cli.tsx.

Boot order:
  1. Side-effect env fixes (COREPACK_ENABLE_AUTO_PIN, ablation baseline)
  2. Fast-path: --version / -v / -V → print version and exit
  3. Fast-path: --help → print help and exit
  4. Load local .env files for runtime provider configuration
  5. Load profiler, check special flags
  6. Import and run the full main module

This module is the executable entry point registered in the verification
harness as the ``python-candidate`` target.
"""

from __future__ import annotations

import os
import sys
from importlib import import_module

# Side-effect 1: Disable corepack auto-pinning (mirrors cli.tsx line 5)
os.environ.setdefault("COREPACK_ENABLE_AUTO_PIN", "0")

# Side-effect 2: CCR remote heap size (mirrors cli.tsx lines 9-13)
if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
    existing = os.environ.get("NODE_OPTIONS", "")
    os.environ["NODE_OPTIONS"] = (
        f"{existing} --max-old-space-size=8192"
        if existing
        else "--max-old-space-size=8192"
    )

# Side-effect 3: Ablation baseline (mirrors cli.tsx lines 21-26)
ABLATION_KEYS = [
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_DISABLE_THINKING",
    "DISABLE_INTERLEAVED_THINKING",
    "DISABLE_COMPACT",
    "DISABLE_AUTO_COMPACT",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
]

if os.environ.get("CLAUDE_CODE_ABLATION_BASELINE"):
    for _k in ABLATION_KEYS:
        os.environ.setdefault(_k, "1")

_VERSION = os.environ.get("CLAUDE_CODE_VERSION", "0.0.0-python-port")


def _parse_args(argv: list[str] | None = None) -> list[str]:
    return (argv or sys.argv)[1:]


def _load_full_main():
    if __package__ not in (None, ""):
        from ..main import main as full_main

        return full_main

    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.dirname(os.path.dirname(_here))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    return import_module("python_src.main").main


def _load_runtime_env_files() -> None:
    if __package__ not in (None, ""):
        from ..utils.config import (
            load_claude_py_env_files,
            should_load_claude_py_env_files,
        )
    else:
        _here = os.path.dirname(os.path.abspath(__file__))
        _repo_root = os.path.dirname(os.path.dirname(_here))
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        config_module = import_module("python_src.utils.config")
        load_claude_py_env_files = config_module.load_claude_py_env_files
        should_load_claude_py_env_files = config_module.should_load_claude_py_env_files
    if should_load_claude_py_env_files():
        load_claude_py_env_files(project_root=os.getcwd())


def _format_startup_error(exc: BaseException) -> str:
    try:
        if __package__ not in (None, ""):
            from ..utils.error_display import (
                build_structured_error_display,
                format_structured_error_text,
            )
            from ..utils.error_log_sink import DiagnosticContext
        else:
            _here = os.path.dirname(os.path.abspath(__file__))
            _repo_root = os.path.dirname(os.path.dirname(_here))
            if _repo_root not in sys.path:
                sys.path.insert(0, _repo_root)
            error_display_module = import_module("python_src.utils.error_display")
            error_log_module = import_module("python_src.utils.error_log_sink")
            build_structured_error_display = error_display_module.build_structured_error_display
            format_structured_error_text = error_display_module.format_structured_error_text
            DiagnosticContext = error_log_module.DiagnosticContext
        display = build_structured_error_display(
            exc,
            title="Startup Error",
            code="startup_error",
            stage="cli_entrypoint",
            diagnostic_context=DiagnosticContext(
                session_id="startup",
                cwd=os.getcwd(),
                version=_VERSION,
            ),
            log_error=True,
        )
        return format_structured_error_text(display)
    except Exception:
        return str(exc)


def cli_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Fast-path 1: --version / -v / -V
    if len(args) == 1 and args[0] in ("--version", "-v", "-V"):
        print(f"{_VERSION} (Claude Code)")
        return 0

    # Fast-path 2: --help / -h
    if len(args) == 1 and args[0] in ("--help", "-h"):
        _print_help()
        return 0

    # Fast-path 3: --bare → set SIMPLE early (mirrors cli.tsx line 283)
    if "--bare" in args:
        os.environ["CLAUDE_CODE_SIMPLE"] = "1"

    # Redirect common update flag mistakes (mirrors cli.tsx lines 277-279)
    if len(args) == 1 and args[0] in ("--update", "--upgrade"):
        args = ["update"]

    try:
        _load_runtime_env_files()
        full_main = _load_full_main()
        synthetic_argv = (argv or sys.argv)[:1] + args
        return full_main(synthetic_argv)
    except Exception as exc:
        msg = _format_startup_error(exc)
        if msg:
            sys.stderr.write(f"{msg}\n")
        return 1


def _print_help() -> None:
    prog = os.path.basename(sys.argv[0]) if sys.argv else "claude"
    print(f"Usage: {prog} [options] [command] [prompt]")
    print()
    print("Claude Code - starts an interactive session by default,")
    print("use -p/--print for non-interactive output")
    print()
    print("Commands:")
    print("  agents [options]    List configured agents")
    print("  doctor               Run local environment diagnostics")
    print("  mcp <subcommand>     Manage local project MCP servers")
    print("  plugin <subcommand>  Manage local plugins")
    print("  task <subcommand>    Manage persistent local tasks")
    print()
    print("Options:")
    print("  -h, --help            Display help for command")
    print("  -v, -V, --version     Display version")
    print("  -p, --print           Print response and exit (non-interactive)")
    print("  -d, --debug           Enable debug mode")
    print("  --bare                Minimal mode (skip hooks, LSP, plugins)")
    print("  -c, --continue        Continue most recent conversation")
    print("  -r, --resume [id]     Resume a conversation by session ID")
    print("  --model <model>       Model for the current session")
    print("  --dump-system-prompt  Print the effective system prompt and exit")
    print("  --session-id <uuid>   Use a specific session ID")
    print("  --settings <file>     Path to settings JSON file or JSON string")
    print("  --permission-mode     Permission mode for the session")
    print("  --allowed-tools       Comma-separated list of allowed tools")
    print("  --disallowed-tools    Comma-separated list of denied tools")
    print("  --mcp-config          Load MCP servers from JSON files")
    print("  --add-dir <dirs>      Additional directories for tool access")
    print("  --agent <agent>       Agent for the current session")
    print("  --agents <json>       JSON object defining custom agents")
    print("  --plugin-dir <path>   Plugin directory (repeatable)")
    print("  --effort <level>      Effort level (low, medium, high, max)")
    print("  --output-format       Output format: text, json, stream-json")
    print()


def main() -> None:
    sys.exit(cli_main())


if __name__ == "__main__":
    main()
