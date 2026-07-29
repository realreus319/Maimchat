"""Full CLI startup — command parsing, flag validation, and dispatch.

Python port of src/main.tsx (startup boot sequence only).

Boot order (mirrors main.tsx main() function):
  1. Security env: NoDefaultCurrentDirectoryInExePath
  2. Initialize warning handler
  3. Signal handlers (SIGINT)
  4. Parse CLI args to determine interactive vs non-interactive
  5. Classify entrypoint (cli / sdk-cli / mcp / github-action)
  6. Determine client type from env vars
  7. Eager-load settings flags
  8. Run commander-style argument parsing and dispatch
"""

from __future__ import annotations

import json
import os
import signal
import sys
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .bootstrap import (
    getInitialMainLoopModel,
    setClientType,
    setFlagSettingsPath,
    setInitialEffortValue,
    setInitialMainLoopModel,
    setInlinePlugins,
    setIsInteractive,
    setIsNonInteractiveSession,
    setMainThreadAgentType,
    setQuestionPreviewFormat,
    setSdkBetas,
    setSessionId,
    setSessionPersistenceDisabled,
    setSessionSource,
)
from .utils.agents import (
    AgentDefinition,
    build_agent_registry,
    delete_custom_agent,
    format_agents_listing,
    get_custom_agent,
    parse_agents_json,
    upsert_custom_agent,
)
from .utils.mcp_cli import (
    format_project_mcp_listing,
    get_project_mcp_file_path,
    get_project_mcp_server_details,
    list_project_mcp_servers,
    remove_project_mcp_server,
    reset_project_mcp_choices,
    upsert_project_mcp_server,
)
from .utils.plugin_registry import (
    format_plugin_listing,
    install_plugin_from_directory,
    list_installed_plugins,
    set_installed_plugin_enabled,
    uninstall_installed_plugin,
    update_installed_plugin,
    validate_plugin_directory,
)
from .utils.settings import get_initial_settings as _get_initial_settings
from .utils.system_prompt import (
    build_effective_system_prompt,
    merge_system_prompt_segments,
)

VALID_PERMISSION_MODES = (
    "default",
    "plan",
    "auto",
    "bypassPermissions",
    "acceptEdits",
    "dontAsk",
)
VALID_OUTPUT_FORMATS = ("text", "json", "stream-json")
VALID_INPUT_FORMATS = ("text", "stream-json")
VALID_EFFORT_LEVELS = ("low", "medium", "high", "max")

_VERSION = os.environ.get("CLAUDE_CODE_VERSION", "0.0.0-python-port")
_LOCAL_SUBCOMMANDS = frozenset({"agents", "doctor", "mcp", "plugin", "task"})


def _apply_settings_to_runtime(settings: dict) -> None:
    env_settings = settings.get("env")
    if isinstance(env_settings, dict):
        for key, value in env_settings.items():
            if isinstance(key, str) and isinstance(value, str):
                os.environ[key] = value

    default_model = settings.get("defaultModel")
    if not isinstance(default_model, str) or not default_model.strip():
        default_model = os.environ.get("ANTHROPIC_DEFAULT_MODEL")

    if (
        isinstance(default_model, str)
        and default_model.strip()
        and getInitialMainLoopModel() is None
    ):
        setInitialMainLoopModel(default_model)


class BootError(Exception):
    pass


class StartupProfiler:
    """Minimal profiler that records named checkpoints in order."""

    def __init__(self) -> None:
        self.checkpoints: List[Tuple[str, float]] = []
        self._enabled = os.environ.get("CLAUDE_CODE_PROFILE") is not None

    def checkpoint(self, name: str) -> None:
        if self._enabled:
            import time

            self.checkpoints.append((name, time.monotonic()))


_profiler = StartupProfiler()


def is_env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes")


def _reset_cursor() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def _initialize_entrypoint(is_non_interactive: bool) -> None:
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return

    cli_args = sys.argv[1:]
    mcp_index = -1
    try:
        mcp_index = cli_args.index("mcp")
    except ValueError:
        pass

    if (
        mcp_index != -1
        and mcp_index + 1 < len(cli_args)
        and cli_args[mcp_index + 1] == "serve"
    ):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "mcp"
        return

    if is_env_truthy(os.environ.get("CLAUDE_CODE_ACTION")):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-code-github-action"
        return

    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli" if is_non_interactive else "cli"


def _determine_client_type() -> str:
    if is_env_truthy(os.environ.get("GITHUB_ACTIONS")):
        return "github-action"
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    entrypoint_map = {
        "sdk-ts": "sdk-typescript",
        "sdk-py": "sdk-python",
        "sdk-cli": "sdk-cli",
        "claude-vscode": "claude-vscode",
        "local-agent": "local-agent",
        "claude-desktop": "claude-desktop",
    }
    if entrypoint in entrypoint_map:
        return entrypoint_map[entrypoint]

    has_session_ingress_token = bool(
        os.environ.get("CLAUDE_CODE_SESSION_ACCESS_TOKEN")
        or os.environ.get("CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR")
    )
    if entrypoint == "remote" or has_session_ingress_token:
        return "remote"

    return "cli"


def _eager_parse_cli_flag(flag: str, args: List[str]) -> Optional[str]:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"{flag}="):
            return a[len(flag) + 1 :]
    return None


def _eager_load_settings(args: List[str]) -> None:
    settings_file = _eager_parse_cli_flag("--settings", args)
    if settings_file:
        trimmed = settings_file.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            import json

            try:
                json.loads(trimmed)
            except json.JSONDecodeError:
                raise BootError("Error: Invalid JSON provided to --settings")
            import tempfile

            fd, path = tempfile.mkstemp(suffix=".json", prefix="claude-settings-")
            with os.fdopen(fd, "w") as f:
                f.write(trimmed)
            setFlagSettingsPath(path)
        else:
            resolved = os.path.abspath(trimmed)
            if not os.path.isfile(resolved):
                raise BootError(f"Error: Settings file not found: {resolved}")
            setFlagSettingsPath(resolved)

    setting_sources = _eager_parse_cli_flag("--setting-sources", args)
    if setting_sources is not None:
        from .bootstrap import setAllowedSettingSources

        setAllowedSettingSources([s.strip() for s in setting_sources.split(",")])


def _validate_model_option(model: Optional[str]) -> Optional[str]:
    return model


def _validate_effort_option(effort: Optional[str]) -> Optional[str]:
    if effort is None:
        return None
    value = effort.lower()
    if value not in VALID_EFFORT_LEVELS:
        raise BootError(
            f"error: option '--effort <level>' argument '{effort}' is invalid. "
            f"Allowed choices: {', '.join(VALID_EFFORT_LEVELS)}"
        )
    return value


def _validate_permission_mode(mode: Optional[str]) -> Optional[str]:
    if mode is None:
        return None
    if mode not in VALID_PERMISSION_MODES:
        raise BootError(
            f"error: option '--permission-mode <mode>' argument '{mode}' is invalid. "
            f"Allowed choices: {', '.join(VALID_PERMISSION_MODES)}"
        )
    return mode


def _effective_permission_mode(opts: ParsedOptions) -> str:
    if opts.dangerously_skip_permissions:
        return "bypassPermissions"
    return opts.permission_mode or "default"


def _validate_output_format(fmt: Optional[str]) -> Optional[str]:
    if fmt is None:
        return None
    if fmt not in VALID_OUTPUT_FORMATS:
        raise BootError(
            f"error: option '--output-format <format>' argument '{fmt}' is invalid. "
            f"Allowed choices: {', '.join(VALID_OUTPUT_FORMATS)}"
        )
    return fmt


def _validate_input_format(fmt: Optional[str]) -> Optional[str]:
    if fmt is None:
        return None
    if fmt not in VALID_INPUT_FORMATS:
        raise BootError(
            f"error: option '--input-format <format>' argument '{fmt}' is invalid. "
            f"Allowed choices: {', '.join(VALID_INPUT_FORMATS)}"
        )
    return fmt


def _validate_session_id(session_id: Optional[str]) -> Optional[str]:
    if session_id is None:
        return None
    import re

    if not re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        session_id,
        re.IGNORECASE,
    ):
        raise BootError("Error: Invalid session ID. Must be a valid UUID.")
    return session_id


def _validate_budget(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        amount = float(value)
    except (ValueError, TypeError):
        raise BootError("error: option '--max-budget-usd' must be a number")
    if amount <= 0:
        raise BootError("--max-budget-usd must be a positive number greater than 0")
    return amount


class ParsedOptions:
    """Holds all parsed CLI options from the commander-style argument parser."""

    def __init__(self) -> None:
        self.prompt: Optional[str] = None
        self.print_mode: bool = False
        self.bare: bool = False
        self.debug: bool = False
        self.debug_to_stderr: bool = False
        self.debug_file: Optional[str] = None
        self.verbose: bool = False
        self.output_format: Optional[str] = None
        self.input_format: Optional[str] = None
        self.json_schema: Optional[str] = None
        self.model: Optional[str] = None
        self.effort: Optional[str] = None
        self.agent: Optional[str] = None
        self.permission_mode: Optional[str] = None
        self.system_prompt: Optional[str] = None
        self.system_prompt_file: Optional[str] = None
        self.append_system_prompt: Optional[str] = None
        self.append_system_prompt_file: Optional[str] = None
        self.dump_system_prompt: bool = False
        self.continue_session: bool = False
        self.resume: Optional[str] = None
        self.session_id: Optional[str] = None
        self.max_budget_usd: Optional[float] = None
        self.settings: Optional[str] = None
        self.add_dir: List[str] = []
        self.mcp_config: List[str] = []
        self.allowed_tools: List[str] = []
        self.disallowed_tools: List[str] = []
        self.betas: List[str] = []
        self.brief: bool = False
        self.init: bool = False
        self.init_only: bool = False
        self.ide: bool = False
        self.strict_mcp_config: bool = False
        self.disable_slash_commands: bool = False
        self.no_session_persistence: bool = False
        self.name: Optional[str] = None
        self.agents: tuple[AgentDefinition, ...] | None = None
        self.plugin_dir: List[str] = []
        self.chrome: Optional[bool] = None
        self.dangerously_skip_permissions: bool = False
        self.allow_dangerously_skip_permissions: bool = False
        self.help_requested: bool = False
        self.version_requested: bool = False
        self.fallback_model: Optional[str] = None
        self.workload: Optional[str] = None
        self.setting_sources: Optional[str] = None
        self.file_specs: List[str] = []
        self.unknown_args: List[str] = []


HELP_TEXT = (
    "Usage: claude [options] [command] [prompt]\n"
    "\n"
    "Claude Code - starts an interactive session by default, "
    "use -p/--print for non-interactive output\n"
    "\n"
    "Commands:\n"
    "  agents [options]            List configured agents\n"
    "  doctor                     Run local environment diagnostics\n"
    "  mcp <subcommand> ...       Manage local project MCP servers\n"
    "  plugin <subcommand> ...    Manage local plugins\n"
    "  task <subcommand> ...      Manage persistent local tasks\n"
    "\n"
    "Arguments:\n"
    "  prompt                      Your prompt\n"
    "\n"
    "Options:\n"
    "  -h, --help                  Display help for command\n"
    "  -v, -V, --version           Display version\n"
    "  -p, --print                 Print response and exit\n"
    "  -d, --debug                 Enable debug mode\n"
    "  --bare                      Minimal mode\n"
    "  --model <model>             Model for the current session\n"
    "  --effort <level>            Effort level (low, medium, high, max)\n"
    "  --agent <agent>             Agent for the current session. Overrides the 'agent' setting.\n"
    "  --permission-mode <mode>    Permission mode\n"
    "  --output-format <format>    Output format (text, json, stream-json)\n"
    "  --input-format <format>     Input format (text, stream-json)\n"
    "  --system-prompt <prompt>    System prompt\n"
    "  --dump-system-prompt        Print the effective system prompt and exit\n"
    "  --append-system-prompt      Append to system prompt\n"
    "  -c, --continue              Continue most recent conversation\n"
    "  -r, --resume [id]           Resume conversation\n"
    "  --session-id <uuid>         Session ID\n"
    "  --max-budget-usd <amount>   Budget limit\n"
    "  --settings <file>           Settings file or JSON\n"
    "  --add-dir <dirs>            Additional directories\n"
    "  --mcp-config <configs>      MCP server configs\n"
    "  --allowed-tools <tools>     Allowed tools\n"
    "  --disallowed-tools <tools>  Disallowed tools\n"
    "  --betas <betas>             Beta headers\n"
    "  --brief                     Enable SendUserMessage tool\n"
    "  --ide                       Connect to IDE\n"
    "  --chrome                    Enable Chrome integration\n"
    "  --no-chrome                 Disable Chrome integration\n"
    "  --name <name>               Session display name\n"
    "  --agents <json>             JSON object defining custom agents\n"
    "  --plugin-dir <path>         Plugin directory (repeatable)\n"
    "  --fallback-model <model>    Fallback model\n"
    "  --settings <file>           Settings file\n"
    "  --setting-sources <srcs>    Setting sources\n"
    "  --strict-mcp-config         Only use --mcp-config\n"
    "  --disable-slash-commands    Disable skills\n"
    "  --no-session-persistence    No session saving\n"
    "  --file <specs>              File resources to download\n"
    "  --allow-dangerously-skip-permissions  Enable bypass option\n"
    "  --dangerously-skip-permissions       Bypass all permissions\n"
)

AGENTS_HELP_TEXT = (
    "Usage: claude agents <subcommand> [options]\n"
    "\n"
    "Subcommands:\n"
    "  list                        List configured agents\n"
    "  get <name>                  Show one custom agent\n"
    "  set <name>                  Create or update a custom agent\n"
    "  delete <name>               Delete a custom agent\n"
    "\n"
    "Examples:\n"
    "  claude agents\n"
    "  claude agents get reviewer\n"
    "  claude agents set reviewer --description \"Reviews code\" --prompt \"You are a code reviewer\"\n"
    "  claude agents delete reviewer\n"
)

MCP_HELP_TEXT = (
    "Usage: claude mcp <subcommand> [options]\n"
    "\n"
    "Subcommands:\n"
    "  list                        List project MCP servers\n"
    "  get <name>                  Show one project MCP server\n"
    "  add-json <name> <json>      Add or replace a project MCP server from JSON\n"
    "  remove <name>               Remove one project MCP server\n"
    "  reset-project-choices       Clear local MCP enable/disable selections\n"
    "  serve                       Start stdio MCP server mode\n"
    "\n"
    "Examples:\n"
    "  claude mcp list\n"
    "  claude mcp get filesystem\n"
    "  claude mcp add-json filesystem '{\"command\":\"uvx\",\"args\":[\"mcp-server\"]}'\n"
    "  claude mcp remove filesystem\n"
    "  claude mcp reset-project-choices\n"
)

PLUGIN_HELP_TEXT = (
    "Usage: claude plugin <subcommand> [options]\n"
    "\n"
    "Subcommands:\n"
    "  list [--json]               List installed local plugins\n"
    "  validate <path>             Validate a local plugin directory\n"
    "  install <path>              Install a local plugin directory\n"
    "  uninstall <name>            Remove an installed local plugin\n"
    "  enable <name>               Enable an installed local plugin\n"
    "  disable <name>              Disable an installed local plugin\n"
    "  update <name>               Reinstall an installed plugin from sourcePath\n"
    "\n"
    "Examples:\n"
    "  claude plugin list\n"
    "  claude plugin validate ./plugins/demo\n"
    "  claude plugin install ./plugins/demo\n"
    "  claude plugin disable demo-plugin\n"
)

TASK_HELP_TEXT = (
    "Usage: claude task <subcommand> [options]\n"
    "\n"
    "Subcommands:\n"
    "  list                       List local tasks\n"
    "  get <task-id>              Show one local task\n"
    "  create <subject>           Create a local task\n"
    "  update <task-id>           Update a local task\n"
    "\n"
    "Examples:\n"
    "  claude task create \"Review API\" --description \"investigate retry path\"\n"
    "  claude task list\n"
    "  claude task get 3\n"
    "  claude task update 3 --status in_progress --owner alice\n"
)


def _parse_commander_args(args: List[str]) -> Tuple[ParsedOptions, List[str]]:
    """Parse argv into structured options, mirroring Commander.js behavior.

    Returns (parsed_options, remaining_positional_args).
    Writes help/version text to stdout and raises SystemExit(0) for those flags.
    Raises BootError for validation failures.
    """
    opts = ParsedOptions()
    positional: List[str] = []
    i = 0

    while i < len(args):
        arg = args[i]

        if arg in ("--help", "-h"):
            opts.help_requested = True
            i += 1
        elif arg in ("--version", "-v", "-V"):
            opts.version_requested = True
            i += 1
        elif arg in ("-p", "--print"):
            opts.print_mode = True
            i += 1
        elif arg == "--bare":
            opts.bare = True
            os.environ["CLAUDE_CODE_SIMPLE"] = "1"
            i += 1
        elif arg in ("-d", "--debug"):
            opts.debug = True
            i += 1
        elif arg == "--debug-to-stderr":
            opts.debug_to_stderr = True
            i += 1
        elif arg == "--debug-file":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--debug-file' argument missing")
            opts.debug_file = args[i]
            opts.debug = True
            i += 1
        elif arg == "--verbose":
            opts.verbose = True
            i += 1
        elif arg == "--output-format":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--output-format' argument missing")
            opts.output_format = _validate_output_format(args[i])
            i += 1
        elif arg.startswith("--output-format="):
            opts.output_format = _validate_output_format(arg.split("=", 1)[1])
            i += 1
        elif arg == "--input-format":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--input-format' argument missing")
            opts.input_format = _validate_input_format(args[i])
            i += 1
        elif arg.startswith("--input-format="):
            opts.input_format = _validate_input_format(arg.split("=", 1)[1])
            i += 1
        elif arg == "--json-schema":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--json-schema' argument missing")
            opts.json_schema = args[i]
            i += 1
        elif arg == "--model":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--model' argument missing")
            opts.model = args[i]
            i += 1
        elif arg.startswith("--model="):
            opts.model = arg.split("=", 1)[1]
            i += 1
        elif arg == "--effort":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--effort' argument missing")
            opts.effort = _validate_effort_option(args[i])
            i += 1
        elif arg.startswith("--effort="):
            opts.effort = _validate_effort_option(arg.split("=", 1)[1])
            i += 1
        elif arg == "--agent":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--agent' argument missing")
            opts.agent = args[i]
            i += 1
        elif arg.startswith("--agent="):
            opts.agent = arg.split("=", 1)[1]
            i += 1
        elif arg == "--permission-mode":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--permission-mode' argument missing")
            opts.permission_mode = _validate_permission_mode(args[i])
            i += 1
        elif arg.startswith("--permission-mode="):
            opts.permission_mode = _validate_permission_mode(arg.split("=", 1)[1])
            i += 1
        elif arg == "--system-prompt":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--system-prompt' argument missing")
            opts.system_prompt = args[i]
            i += 1
        elif arg == "--system-prompt-file":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--system-prompt-file' argument missing")
            opts.system_prompt_file = args[i]
            i += 1
        elif arg == "--dump-system-prompt":
            opts.dump_system_prompt = True
            i += 1
        elif arg == "--append-system-prompt":
            i += 1
            if i >= len(args):
                raise BootError(
                    "error: option '--append-system-prompt' argument missing"
                )
            opts.append_system_prompt = args[i]
            i += 1
        elif arg == "--append-system-prompt-file":
            i += 1
            if i >= len(args):
                raise BootError(
                    "error: option '--append-system-prompt-file' argument missing"
                )
            opts.append_system_prompt_file = args[i]
            i += 1
        elif arg in ("-c", "--continue"):
            opts.continue_session = True
            i += 1
        elif arg in ("-r", "--resume"):
            opts.resume = (
                args[i + 1]
                if i + 1 < len(args) and not args[i + 1].startswith("-")
                else ""
            )
            if opts.resume == "":
                opts.resume = "__picker__"
            else:
                i += 1
            i += 1
        elif arg == "--session-id":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--session-id' argument missing")
            opts.session_id = _validate_session_id(args[i])
            i += 1
        elif arg.startswith("--session-id="):
            opts.session_id = _validate_session_id(arg.split("=", 1)[1])
            i += 1
        elif arg == "--max-budget-usd":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--max-budget-usd' argument missing")
            opts.max_budget_usd = _validate_budget(args[i])
            i += 1
        elif arg.startswith("--max-budget-usd="):
            opts.max_budget_usd = _validate_budget(arg.split("=", 1)[1])
            i += 1
        elif arg == "--settings":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--settings' argument missing")
            opts.settings = args[i]
            i += 1
        elif arg.startswith("--settings="):
            opts.settings = arg.split("=", 1)[1]
            i += 1
        elif arg == "--add-dir":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--add-dir' argument missing")
            opts.add_dir.append(args[i])
            i += 1
        elif arg.startswith("--add-dir="):
            opts.add_dir.extend(a.strip() for a in arg.split("=", 1)[1].split(","))
            i += 1
        elif arg == "--mcp-config":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--mcp-config' argument missing")
            opts.mcp_config.append(args[i])
            i += 1
        elif arg == "--allowed-tools":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--allowed-tools' argument missing")
            opts.allowed_tools.append(args[i])
            i += 1
        elif arg == "--disallowed-tools":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--disallowed-tools' argument missing")
            opts.disallowed_tools.append(args[i])
            i += 1
        elif arg == "--betas":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--betas' argument missing")
            opts.betas.append(args[i])
            i += 1
        elif arg == "--brief":
            opts.brief = True
            i += 1
        elif arg == "--init":
            opts.init = True
            i += 1
        elif arg == "--init-only":
            opts.init_only = True
            i += 1
        elif arg == "--ide":
            opts.ide = True
            i += 1
        elif arg == "--strict-mcp-config":
            opts.strict_mcp_config = True
            i += 1
        elif arg == "--disable-slash-commands":
            opts.disable_slash_commands = True
            i += 1
        elif arg == "--no-session-persistence":
            opts.no_session_persistence = True
            i += 1
        elif arg == "--name":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--name' argument missing")
            opts.name = args[i]
            i += 1
        elif arg.startswith("--name="):
            opts.name = arg.split("=", 1)[1]
            i += 1
        elif arg == "--agents":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--agents' argument missing")
            try:
                opts.agents = parse_agents_json(
                    args[i],
                    source_name="--agents",
                    source_type="inline",
                )
            except ValueError as exc:
                raise BootError(f"Error: {exc}") from exc
            i += 1
        elif arg.startswith("--agents="):
            try:
                opts.agents = parse_agents_json(
                    arg.split("=", 1)[1],
                    source_name="--agents",
                    source_type="inline",
                )
            except ValueError as exc:
                raise BootError(f"Error: {exc}") from exc
            i += 1
        elif arg == "--plugin-dir":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--plugin-dir' argument missing")
            opts.plugin_dir.append(args[i])
            i += 1
        elif arg == "--chrome":
            opts.chrome = True
            i += 1
        elif arg == "--no-chrome":
            opts.chrome = False
            i += 1
        elif arg == "--allow-dangerously-skip-permissions":
            opts.allow_dangerously_skip_permissions = True
            i += 1
        elif arg == "--dangerously-skip-permissions":
            opts.dangerously_skip_permissions = True
            i += 1
        elif arg == "--fallback-model":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--fallback-model' argument missing")
            opts.fallback_model = args[i]
            i += 1
        elif arg.startswith("--fallback-model="):
            opts.fallback_model = arg.split("=", 1)[1]
            i += 1
        elif arg == "--workload":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--workload' argument missing")
            opts.workload = args[i]
            i += 1
        elif arg == "--setting-sources":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--setting-sources' argument missing")
            opts.setting_sources = args[i]
            i += 1
        elif arg == "--file":
            i += 1
            if i >= len(args):
                raise BootError("error: option '--file' argument missing")
            opts.file_specs.append(args[i])
            i += 1
        elif arg.startswith("-"):
            raise BootError(f"error: unknown option '{arg}'")
        else:
            positional.append(arg)
            i += 1
            if len(positional) == 1 and arg in _LOCAL_SUBCOMMANDS:
                positional.extend(args[i:])
                break

    if positional:
        opts.prompt = positional[0]

    return opts, positional


def _task_usage_error(message: str) -> BootError:
    return BootError(f"{message}\n\n{TASK_HELP_TEXT}")


def _agents_usage_error(message: str) -> BootError:
    return BootError(f"{message}\n\n{AGENTS_HELP_TEXT}")


def _mcp_usage_error(message: str) -> BootError:
    return BootError(f"{message}\n\n{MCP_HELP_TEXT}")


def _plugin_usage_error(message: str) -> BootError:
    return BootError(f"{message}\n\n{PLUGIN_HELP_TEXT}")


def _parse_task_metadata(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootError(f"Error: Invalid JSON for --metadata: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise BootError("Error: --metadata must decode to a JSON object")
    return payload


def _split_task_ids(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _consume_flag(
    items: list[str],
    index: int,
    name: str,
    *,
    usage_error: Callable[[str], BootError],
) -> tuple[str, int]:
    arg = items[index]
    if arg == name:
        next_index = index + 1
        if next_index >= len(items):
            raise usage_error(f"Error: option '{name}' argument missing")
        return items[next_index], next_index + 1
    if arg.startswith(f"{name}="):
        return arg.split("=", 1)[1], index + 1
    raise AssertionError(f"Unsupported flag pattern for {name}: {arg}")


def _parse_agents_tools_arg(value: str) -> tuple[str, ...]:
    tools: list[str] = []
    for item in value.replace(",", " ").split():
        normalized = item.strip()
        if normalized and normalized not in tools:
            tools.append(normalized)
    return tuple(tools)


def _format_plugin_payload(
    plugin_payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "description": plugin_payload.get("description") or "",
        "enabled": bool(plugin_payload.get("enabled")),
        "installPath": plugin_payload.get("installPath"),
        "name": plugin_payload.get("name"),
        "pluginId": plugin_payload.get("pluginId"),
        "source": plugin_payload.get("source"),
        "sourcePath": plugin_payload.get("sourcePath"),
        "version": plugin_payload.get("version"),
    }


def _plugin_record_payload(name: str) -> dict[str, object]:
    plugins_payload = list_installed_plugins()
    raw_plugins = plugins_payload.get("plugins")
    if isinstance(raw_plugins, Sequence) and not isinstance(
        raw_plugins,
        (str, bytes, bytearray),
    ):
        for plugin in raw_plugins:
            if isinstance(plugin, Mapping) and plugin.get("name") == name:
                return _format_plugin_payload(plugin)
    raise ValueError("Plugin not found after operation: {}".format(name))


def _handle_plugin_command(args: Sequence[str]) -> int:
    if not args:
        print(format_plugin_listing(list_installed_plugins()), end="")
        return 0

    if args[0] in {"-h", "--help", "help"}:
        print(PLUGIN_HELP_TEXT)
        return 0

    subcommand = args[0]
    subargs = list(args[1:])
    json_output = False

    def _require_single_operand(label: str) -> str:
        operands = [arg for arg in subargs if not arg.startswith("-")]
        if len(operands) != 1:
            raise _plugin_usage_error(f"Error: {subcommand} requires exactly one {label}")
        return operands[0]

    try:
        if subcommand == "list":
            for arg in subargs:
                if arg == "--json":
                    json_output = True
                    continue
                raise _plugin_usage_error(f"Error: unknown option '{arg}'")
            payload = list_installed_plugins()
            if json_output:
                _print_json(payload)
            else:
                print(format_plugin_listing(payload), end="")
            return 0

        if subcommand == "validate":
            plugin_path = _require_single_operand("plugin path")
            plugin = validate_plugin_directory(plugin_path)
            _print_json(
                {
                    "plugin": {
                        "description": plugin.manifest.get("description", ""),
                        "enabled": True,
                        "installPath": os.path.abspath(plugin.path),
                        "name": plugin.name,
                        "pluginId": "{}@local".format(plugin.name),
                        "source": "local",
                        "sourcePath": os.path.abspath(plugin.path),
                        "version": plugin.manifest.get("version"),
                    },
                    "valid": True,
                }
            )
            return 0

        if subcommand == "install":
            plugin_path = _require_single_operand("plugin path")
            record = install_plugin_from_directory(plugin_path)
            _print_json(
                {
                    "plugin": _plugin_record_payload(record.name),
                    "success": True,
                }
            )
            return 0

        if subcommand == "uninstall":
            plugin_name = _require_single_operand("plugin name")
            record = uninstall_installed_plugin(plugin_name)
            _print_json(
                {
                    "plugin": record.to_public_payload(enabled=False),
                    "success": True,
                }
            )
            return 0

        if subcommand == "enable":
            plugin_name = _require_single_operand("plugin name")
            record = set_installed_plugin_enabled(plugin_name, True)
            _print_json(
                {
                    "plugin": _plugin_record_payload(record.name),
                    "success": True,
                }
            )
            return 0

        if subcommand == "disable":
            plugin_name = _require_single_operand("plugin name")
            record = set_installed_plugin_enabled(plugin_name, False)
            _print_json(
                {
                    "plugin": _plugin_record_payload(record.name),
                    "success": True,
                }
            )
            return 0

        if subcommand == "update":
            plugin_name = _require_single_operand("plugin name")
            record = update_installed_plugin(plugin_name)
            _print_json(
                {
                    "plugin": _plugin_record_payload(record.name),
                    "success": True,
                }
            )
            return 0

        raise _plugin_usage_error(f"Error: unknown plugin subcommand '{subcommand}'")
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


def _handle_agents_command(args: Sequence[str]) -> int:
    if not args:
        print(format_agents_listing(build_agent_registry()), end="")
        return 0

    if args[0] in {"-h", "--help", "help"}:
        print(AGENTS_HELP_TEXT)
        return 0

    subcommand = args[0]
    subargs = list(args[1:])
    json_output = False

    try:
        if subcommand == "list":
            for arg in subargs:
                if arg == "--json":
                    json_output = True
                    continue
                raise _agents_usage_error(f"Error: unknown option '{arg}'")
            registry = build_agent_registry()
            if json_output:
                _print_json(
                    {
                        "agents": [
                            agent.to_public_payload()
                            for agent in registry.all_agents
                        ]
                    }
                )
            else:
                print(format_agents_listing(registry), end="")
            return 0

        if subcommand == "get":
            agent_name: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _agents_usage_error(f"Error: unknown option '{arg}'")
                if agent_name is not None:
                    raise _agents_usage_error("Error: get accepts exactly one agent name")
                agent_name = arg
                i += 1
            if agent_name is None:
                raise _agents_usage_error("Error: get requires an agent name")
            agent = get_custom_agent(agent_name)
            if agent is None:
                sys.stderr.write(f"Error: Agent not found: {agent_name}\n")
                return 1
            _print_json({"agent": agent.to_public_payload()})
            return 0

        if subcommand == "set":
            agent_name: str | None = None
            description: str | None = None
            prompt: str | None = None
            model: str | None = None
            color: str | None = None
            agent_type: str | None = None
            tools: tuple[str, ...] | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    i += 1
                    continue
                if arg.startswith("--description"):
                    description, i = _consume_flag(
                        subargs,
                        i,
                        "--description",
                        usage_error=_agents_usage_error,
                    )
                    continue
                if arg.startswith("--prompt"):
                    prompt, i = _consume_flag(
                        subargs,
                        i,
                        "--prompt",
                        usage_error=_agents_usage_error,
                    )
                    continue
                if arg.startswith("--model"):
                    model, i = _consume_flag(
                        subargs,
                        i,
                        "--model",
                        usage_error=_agents_usage_error,
                    )
                    continue
                if arg.startswith("--color"):
                    color, i = _consume_flag(
                        subargs,
                        i,
                        "--color",
                        usage_error=_agents_usage_error,
                    )
                    continue
                if arg.startswith("--type"):
                    agent_type, i = _consume_flag(
                        subargs,
                        i,
                        "--type",
                        usage_error=_agents_usage_error,
                    )
                    continue
                if arg.startswith("--tools"):
                    raw_tools, i = _consume_flag(
                        subargs,
                        i,
                        "--tools",
                        usage_error=_agents_usage_error,
                    )
                    tools = _parse_agents_tools_arg(raw_tools)
                    continue
                if arg.startswith("-"):
                    raise _agents_usage_error(f"Error: unknown option '{arg}'")
                if agent_name is not None:
                    raise _agents_usage_error("Error: set accepts exactly one agent name")
                agent_name = arg
                i += 1
            if agent_name is None:
                raise _agents_usage_error("Error: set requires an agent name")
            if all(
                value is None
                for value in (description, prompt, model, color, agent_type)
            ) and tools is None:
                raise _agents_usage_error(
                    "Error: set requires at least one of --description, --prompt, --model, --color, --type, or --tools"
                )
            agent = upsert_custom_agent(
                agent_name,
                description=description,
                prompt=prompt,
                model=model,
                color=color,
                tools=tools,
                agent_type=agent_type,
            )
            _print_json({"success": True, "agent": agent.to_public_payload()})
            return 0

        if subcommand == "delete":
            agent_name: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    json_output = True
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _agents_usage_error(f"Error: unknown option '{arg}'")
                if agent_name is not None:
                    raise _agents_usage_error(
                        "Error: delete accepts exactly one agent name"
                    )
                agent_name = arg
                i += 1
            if agent_name is None:
                raise _agents_usage_error("Error: delete requires an agent name")
            if not delete_custom_agent(agent_name):
                sys.stderr.write(f"Error: Agent not found: {agent_name}\n")
                return 1
            _print_json({"success": True, "agentName": agent_name})
            return 0
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    raise _agents_usage_error(f"Error: unknown agents subcommand '{subcommand}'")


def _handle_task_command(args: Sequence[str]) -> int:
    from .utils.tasks import (
        TASK_COMPLETED,
        TASK_DELETED,
        TASK_IN_PROGRESS,
        TASK_PENDING,
        create_task,
        get_task,
        is_todo_v2_enabled,
        list_tasks,
        to_list_task,
        to_public_task,
        update_task,
    )

    if not is_todo_v2_enabled():
        sys.stderr.write("Error: Task V2 tools are disabled.\n")
        return 1

    if not args or args[0] in {"-h", "--help", "help"}:
        print(TASK_HELP_TEXT)
        return 0

    subcommand = args[0]
    subargs = list(args[1:])

    def _consume_flag(
        items: list[str],
        index: int,
        name: str,
    ) -> tuple[str, int]:
        arg = items[index]
        if arg == name:
            next_index = index + 1
            if next_index >= len(items):
                raise _task_usage_error(f"Error: option '{name}' argument missing")
            return items[next_index], next_index + 1
        if arg.startswith(f"{name}="):
            return arg.split("=", 1)[1], index + 1
        raise AssertionError(f"Unsupported flag pattern for {name}: {arg}")

    try:
        if subcommand == "list":
            for arg in subargs:
                if arg == "--json":
                    continue
                raise _task_usage_error(f"Error: unknown option '{arg}'")
            tasks = list_tasks()
            completed_ids = {
                task.id for task in tasks if task.status == TASK_COMPLETED
            }
            public_tasks = []
            for task in tasks:
                public_task = to_list_task(task, completed_ids)
                if public_task is not None:
                    public_tasks.append(public_task)
            _print_json({"tasks": public_tasks})
            return 0

        if subcommand == "get":
            task_id: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _task_usage_error(f"Error: unknown option '{arg}'")
                if task_id is not None:
                    raise _task_usage_error("Error: get accepts exactly one task ID")
                task_id = arg
                i += 1
            if task_id is None:
                raise _task_usage_error("Error: get requires a task ID")
            task = get_task(task_id)
            if task is None:
                sys.stderr.write(f"Error: Task not found: {task_id}\n")
                return 1
            _print_json({"task": to_public_task(task)})
            return 0

        if subcommand == "create":
            subject: str | None = None
            description = ""
            active_form: str | None = None
            metadata: dict[str, Any] | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    i += 1
                    continue
                if arg in {"--description", "--active-form", "--metadata"} or any(
                    arg.startswith(prefix)
                    for prefix in (
                        "--description=",
                        "--active-form=",
                        "--metadata=",
                    )
                ):
                    if arg.startswith("--description"):
                        description, i = _consume_flag(subargs, i, "--description")
                    elif arg.startswith("--active-form"):
                        active_form, i = _consume_flag(subargs, i, "--active-form")
                    else:
                        metadata_raw, i = _consume_flag(subargs, i, "--metadata")
                        metadata = _parse_task_metadata(metadata_raw)
                    continue
                if arg.startswith("-"):
                    raise _task_usage_error(f"Error: unknown option '{arg}'")
                if subject is not None:
                    raise _task_usage_error(
                        "Error: create accepts exactly one positional subject"
                    )
                subject = arg
                i += 1
            if subject is None:
                raise _task_usage_error("Error: create requires a subject")
            task = create_task(
                subject=subject,
                description=description,
                active_form=active_form,
                metadata=metadata or {},
            )
            _print_json({"task": to_public_task(task)})
            return 0

        if subcommand == "update":
            task_id: str | None = None
            subject: str | None = None
            description: str | None = None
            active_form: str | None = None
            owner: str | None = None
            status: str | None = None
            metadata: dict[str, Any] | None = None
            add_blocks: list[str] | None = None
            add_blocked_by: list[str] | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    i += 1
                    continue
                if arg in {
                    "--subject",
                    "--description",
                    "--active-form",
                    "--owner",
                    "--status",
                    "--metadata",
                    "--add-blocks",
                    "--add-blocked-by",
                } or any(
                    arg.startswith(prefix)
                    for prefix in (
                        "--subject=",
                        "--description=",
                        "--active-form=",
                        "--owner=",
                        "--status=",
                        "--metadata=",
                        "--add-blocks=",
                        "--add-blocked-by=",
                    )
                ):
                    if arg.startswith("--subject"):
                        subject, i = _consume_flag(subargs, i, "--subject")
                    elif arg.startswith("--description"):
                        description, i = _consume_flag(subargs, i, "--description")
                    elif arg.startswith("--active-form"):
                        active_form, i = _consume_flag(subargs, i, "--active-form")
                    elif arg.startswith("--owner"):
                        owner, i = _consume_flag(subargs, i, "--owner")
                    elif arg.startswith("--status"):
                        status, i = _consume_flag(subargs, i, "--status")
                    elif arg.startswith("--metadata"):
                        metadata_raw, i = _consume_flag(subargs, i, "--metadata")
                        metadata = _parse_task_metadata(metadata_raw)
                    elif arg.startswith("--add-blocks"):
                        raw_blocks, i = _consume_flag(subargs, i, "--add-blocks")
                        add_blocks = _split_task_ids(raw_blocks)
                    else:
                        raw_blocked_by, i = _consume_flag(
                            subargs,
                            i,
                            "--add-blocked-by",
                        )
                        add_blocked_by = _split_task_ids(raw_blocked_by)
                    continue
                if arg.startswith("-"):
                    raise _task_usage_error(f"Error: unknown option '{arg}'")
                if task_id is not None:
                    raise _task_usage_error(
                        "Error: update accepts exactly one positional task ID"
                    )
                task_id = arg
                i += 1
            if task_id is None:
                raise _task_usage_error("Error: update requires a task ID")
            if status is not None and status not in {
                TASK_PENDING,
                TASK_IN_PROGRESS,
                TASK_COMPLETED,
                TASK_DELETED,
            }:
                sys.stderr.write(f"Error: Invalid status: {status}\n")
                return 1
            task, updated_fields, error = update_task(
                task_id=task_id,
                subject=subject,
                description=description,
                active_form=active_form,
                owner=owner,
                status=status,
                metadata=metadata,
                add_blocks=add_blocks,
                add_blocked_by=add_blocked_by,
            )
            if task is None and status == TASK_DELETED and error is None:
                _print_json(
                    {
                        "success": True,
                        "taskId": task_id,
                        "updatedFields": updated_fields,
                    }
                )
                return 0
            if task is None:
                sys.stderr.write(f"Error: {error or f'Task not found: {task_id}'}\n")
                return 1
            _print_json(
                {
                    "success": True,
                    "taskId": task_id,
                    "updatedFields": updated_fields,
                    "task": to_public_task(task),
                }
            )
            return 0
    except BootError:
        raise
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    raise _task_usage_error(f"Error: unknown task subcommand '{subcommand}'")


def _parse_mcp_json_payload(raw: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootError(f"Error: Invalid JSON for MCP server config: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise BootError("Error: MCP server config JSON must decode to an object")
    return payload


def _handle_mcp_command(args: Sequence[str]) -> int:
    if not args or args[0] in {"-h", "--help", "help"}:
        print(MCP_HELP_TEXT)
        return 0

    subcommand = args[0]
    subargs = list(args[1:])
    json_output = False

    try:
        if subcommand == "list":
            for arg in subargs:
                if arg == "--json":
                    json_output = True
                    continue
                raise _mcp_usage_error(f"Error: unknown option '{arg}'")
            payload = list_project_mcp_servers()
            if json_output:
                _print_json(payload)
            else:
                print(format_project_mcp_listing(payload))
            return 0

        if subcommand == "get":
            server_name: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    json_output = True
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _mcp_usage_error(f"Error: unknown option '{arg}'")
                if server_name is not None:
                    raise _mcp_usage_error("Error: get accepts exactly one server name")
                server_name = arg
                i += 1
            if server_name is None:
                raise _mcp_usage_error("Error: get requires a server name")
            server = get_project_mcp_server_details(server_name)
            if server is None:
                sys.stderr.write(f"Error: MCP server not found: {server_name}\n")
                return 1
            _print_json({"server": server})
            return 0

        if subcommand == "add-json":
            server_name: str | None = None
            config_json: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    json_output = True
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _mcp_usage_error(f"Error: unknown option '{arg}'")
                if server_name is None:
                    server_name = arg
                elif config_json is None:
                    config_json = arg
                else:
                    raise _mcp_usage_error(
                        "Error: add-json accepts exactly one name and one JSON payload"
                    )
                i += 1
            if server_name is None or config_json is None:
                raise _mcp_usage_error("Error: add-json requires a name and JSON payload")
            server = upsert_project_mcp_server(
                server_name,
                _parse_mcp_json_payload(config_json),
            )
            _print_json(
                {
                    "success": True,
                    "mcpFile": get_project_mcp_file_path(),
                    "server": server,
                }
            )
            return 0

        if subcommand == "remove":
            server_name: str | None = None
            i = 0
            while i < len(subargs):
                arg = subargs[i]
                if arg == "--json":
                    json_output = True
                    i += 1
                    continue
                if arg.startswith("-"):
                    raise _mcp_usage_error(f"Error: unknown option '{arg}'")
                if server_name is not None:
                    raise _mcp_usage_error(
                        "Error: remove accepts exactly one server name"
                    )
                server_name = arg
                i += 1
            if server_name is None:
                raise _mcp_usage_error("Error: remove requires a server name")
            server = remove_project_mcp_server(server_name)
            if server is None:
                sys.stderr.write(f"Error: MCP server not found: {server_name}\n")
                return 1
            _print_json(
                {
                    "success": True,
                    "mcpFile": get_project_mcp_file_path(),
                    "server": server,
                }
            )
            return 0

        if subcommand == "reset-project-choices":
            for arg in subargs:
                if arg == "--json":
                    json_output = True
                    continue
                raise _mcp_usage_error(f"Error: unknown option '{arg}'")
            _print_json(
                {
                    "success": True,
                    "localSettings": reset_project_mcp_choices(),
                }
            )
            return 0

        if subcommand == "serve":
            debug = False
            verbose = False
            for arg in subargs:
                if arg == "--debug":
                    debug = True
                    continue
                if arg == "--verbose":
                    verbose = True
                    continue
                raise _mcp_usage_error(f"Error: unknown option '{arg}'")
            from .services.mcp.server import run_mcp_stdio_server

            return run_mcp_stdio_server(
                version=_VERSION,
                debug=debug,
                verbose=verbose,
            )
    except BootError:
        raise
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    raise _mcp_usage_error(f"Error: unknown mcp subcommand '{subcommand}'")


def main(argv: list[str] | None = None) -> int:
    """Run the full CLI startup sequence and return exit code."""
    _profiler.checkpoint("main_function_start")

    # Security: prevent current-directory command lookup on Windows
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"

    _profiler.checkpoint("main_security_env_set")

    # Signal handlers
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
    except (OSError, ValueError):
        pass

    _profiler.checkpoint("main_signals_installed")

    args = (argv or sys.argv)[1:]

    # Determine interactive mode
    has_print_flag = "-p" in args or "--print" in args
    has_init_only_flag = "--init-only" in args
    has_sdk_url = any(a.startswith("--sdk-url") for a in args)
    is_non_interactive = has_print_flag or has_init_only_flag or has_sdk_url

    setIsInteractive(not is_non_interactive)
    setIsNonInteractiveSession(is_non_interactive)

    _initialize_entrypoint(is_non_interactive)
    client_type = _determine_client_type()
    setClientType(client_type)

    _profiler.checkpoint("main_client_type_determined")

    preview_format = os.environ.get("CLAUDE_CODE_QUESTION_PREVIEW_FORMAT")
    if preview_format in ("markdown", "html"):
        setQuestionPreviewFormat(preview_format)
    elif not client_type.startswith("sdk-") and client_type not in (
        "claude-desktop",
        "local-agent",
        "remote",
    ):
        setQuestionPreviewFormat("markdown")

    if os.environ.get("CLAUDE_CODE_ENVIRONMENT_KIND") == "bridge":
        setSessionSource("remote-control")

    _profiler.checkpoint("main_before_eager_settings")

    _eager_load_settings(args)

    _profiler.checkpoint("main_before_run")
    return _run(args)


def _run(args: List[str]) -> int:
    _profiler.checkpoint("run_function_start")

    try:
        opts, positional = _parse_commander_args(args)
    except BootError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    setInlinePlugins(
        [
            os.path.abspath(plugin_dir)
            for plugin_dir in opts.plugin_dir
            if isinstance(plugin_dir, str) and plugin_dir.strip()
        ]
    )

    if positional and positional[0] == "doctor":
        if len(positional) > 1:
            sys.stderr.write("Error: doctor does not accept additional arguments.\n")
            return 1
        from .local_tool_executor import LocalToolExecutor
        from .repl_runtime import build_doctor_report
        from .state.app_state_store import get_default_app_state

        executor = LocalToolExecutor(app_state=get_default_app_state())
        print(
            build_doctor_report(
                persist_sessions=not opts.no_session_persistence,
                tool_count=len(executor.get_tool_schemas()),
            )
        )
        return 0

    if positional and positional[0] == "agents":
        try:
            return _handle_agents_command(positional[1:])
        except BootError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    if positional and positional[0] == "mcp":
        try:
            return _handle_mcp_command(positional[1:])
        except BootError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    if positional and positional[0] == "plugin":
        try:
            return _handle_plugin_command(positional[1:])
        except BootError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    if positional and positional[0] == "task":
        try:
            return _handle_task_command(positional[1:])
        except BootError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    # Handle --help
    if opts.help_requested:
        print(HELP_TEXT)
        return 0

    # Handle --version (falls through from cli.py if not caught there)
    if opts.version_requested:
        print(f"{_VERSION} (Claude Code)")
        return 0

    # --bare sets SIMPLE
    if opts.bare:
        os.environ["CLAUDE_CODE_SIMPLE"] = "1"

    # Validate conflicting options
    if opts.system_prompt and opts.system_prompt_file:
        sys.stderr.write(
            "Error: Cannot use both --system-prompt and --system-prompt-file. "
            "Please use only one.\n"
        )
        return 1

    if opts.append_system_prompt and opts.append_system_prompt_file:
        sys.stderr.write(
            "Error: Cannot use both --append-system-prompt and "
            "--append-system-prompt-file. Please use only one.\n"
        )
        return 1

    # Load system prompt from file
    if opts.system_prompt_file:
        try:
            with open(os.path.abspath(opts.system_prompt_file)) as f:
                opts.system_prompt = f.read()
        except FileNotFoundError:
            sys.stderr.write(
                f"Error: System prompt file not found: "
                f"{os.path.abspath(opts.system_prompt_file)}\n"
            )
            return 1

    if opts.append_system_prompt_file:
        try:
            with open(os.path.abspath(opts.append_system_prompt_file)) as f:
                opts.append_system_prompt = f.read()
        except FileNotFoundError:
            sys.stderr.write(
                f"Error: Append system prompt file not found: "
                f"{os.path.abspath(opts.append_system_prompt_file)}\n"
            )
            return 1

    # Validate session ID + continue/resume conflict
    if opts.session_id and (opts.continue_session or opts.resume is not None):
        sys.stderr.write(
            "Error: --session-id can only be used with --continue or --resume "
            "if --fork-session is also specified.\n"
        )
        return 1

    # Validate fallback model != main model
    if opts.fallback_model and opts.model and opts.fallback_model == opts.model:
        sys.stderr.write(
            "Error: Fallback model cannot be the same as the main model. "
            "Please specify a different model for --fallback-model.\n"
        )
        return 1

    # --print --input-format stream-json enables the PERSISTENT multi-turn loop:
    # stdin is consumed line-by-line inside _run_print_mode_persistent, so we do
    # NOT drain it here into a single one-shot prompt.
    is_persistent_stream = opts.print_mode and (opts.input_format or "text") == "stream-json"

    # --print mode requires non-interactive handling
    prompt = ""
    if opts.print_mode and not is_persistent_stream:
        _profiler.checkpoint("run_print_mode")
        try:
            prompt = _resolve_non_interactive_prompt(opts)
        except BootError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

    # Store model override in bootstrap state
    if opts.model:
        setInitialMainLoopModel(opts.model)
    setInitialEffortValue(opts.effort)
    setSdkBetas(list(opts.betas))
    setSessionPersistenceDisabled(opts.no_session_persistence)
    setSessionId(opts.session_id)

    _profiler.checkpoint("run_after_parse")

    # Load merged settings from all sources in precedence order.
    # This mirrors main.tsx preAction → init() → getInitialSettings().
    from .utils.settings import reset_settings_cache as _do_reset_cache

    flag_path = None
    flag_inline = None
    if opts.settings:
        trimmed = opts.settings.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            import json

            try:
                flag_inline = json.loads(trimmed)
            except json.JSONDecodeError:
                pass
        else:
            flag_path = os.path.abspath(trimmed)

    _do_reset_cache()
    settings = _get_initial_settings(flag_path=flag_path, flag_inline=flag_inline)
    _apply_settings_to_runtime(settings)
    if opts.fallback_model:
        os.environ["CLAUDE_CODE_FALLBACK_MODEL"] = opts.fallback_model.strip()

    selected_agent_name = opts.agent
    if selected_agent_name is None:
        configured_agent = settings.get("agent")
        if isinstance(configured_agent, str) and configured_agent.strip():
            selected_agent_name = configured_agent.strip()

    setMainThreadAgentType(None)
    selected_agent = None
    try:
        agent_registry = build_agent_registry(inline_agents=opts.agents)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    if selected_agent_name is not None:
        selected_agent = agent_registry.resolve(selected_agent_name)
        if selected_agent is None:
            sys.stderr.write(f"Error: Unknown agent: {selected_agent_name}\n")
            return 1
        setMainThreadAgentType(selected_agent.resolved_agent_type)
        if (
            not opts.model
            and isinstance(selected_agent.model, str)
            and selected_agent.model.strip()
            and selected_agent.display_model != "inherit"
        ):
            setInitialMainLoopModel(selected_agent.model.strip())

    effective_system_prompt = build_effective_system_prompt(
        agent_system_prompt=selected_agent.prompt if selected_agent is not None else None,
        custom_system_prompt=opts.system_prompt,
        append_system_prompt=opts.append_system_prompt,
    )

    if opts.dump_system_prompt:
        if effective_system_prompt is None:
            print("No system prompt configured.")
        else:
            print(effective_system_prompt)
        return 0

    _profiler.checkpoint("run_settings_loaded")

    if opts.print_mode:
        from .query_streaming import QueryStreamConfig

        print_mode_kwargs: dict[str, Any] = dict(
            output_format=opts.output_format or "text",
            session_id=opts.session_id,
            continue_most_recent=opts.continue_session,
            resume_session_id=(
                opts.resume if opts.resume not in (None, "__picker__") else None
            ),
            resume_latest_session=opts.resume == "__picker__",
            persist_sessions=not opts.no_session_persistence,
            display_name=opts.name,
            permission_mode=_effective_permission_mode(opts),
            stream_config=QueryStreamConfig(system_prompt=effective_system_prompt),
            **({"inline_agents": opts.agents} if opts.agents is not None else {}),
        )
        if is_persistent_stream:
            return _run_print_mode_persistent(**print_mode_kwargs)
        return _run_print_mode(prompt=prompt, **print_mode_kwargs)

    if not opts.print_mode:
        from .repl_runtime import can_start_interactive_repl, run_interactive_repl
        from .query_streaming import QueryStreamConfig

        if can_start_interactive_repl():
            repl_kwargs: dict[str, Any] = dict(
                session_id=opts.session_id,
                continue_most_recent=opts.continue_session,
                resume_session_id=(
                    opts.resume
                    if opts.resume not in (None, "__picker__")
                    else None
                ),
                resume_latest_session=opts.resume == "__picker__",
                persist_sessions=not opts.no_session_persistence,
                display_name=opts.name,
                permission_mode=_effective_permission_mode(opts),
                stream_config=QueryStreamConfig(system_prompt=effective_system_prompt),
                **({"inline_agents": opts.agents} if opts.agents is not None else {}),
            )
            return run_interactive_repl(**repl_kwargs)

        return 0

    return 0


def _stdin_is_tty() -> bool:
    isatty = getattr(sys.stdin, "isatty", None)
    return bool(callable(isatty) and isatty())


def _extract_prompt_from_text_input(raw: str) -> str:
    prompt = raw.strip()
    if prompt:
        return prompt
    raise BootError(
        "Error: No prompt provided. Pass a prompt argument or pipe input on stdin."
    )


def _coerce_stream_json_prompt(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None

    if isinstance(value, Mapping):
        for key in ("prompt", "content", "text", "input"):
            prompt = _coerce_stream_json_prompt(value.get(key))
            if prompt is not None:
                return prompt
        message_payload = value.get("message")
        if isinstance(message_payload, Mapping):
            prompt = _coerce_stream_json_prompt(message_payload)
            if prompt is not None:
                return prompt
        if value.get("type") == "user":
            prompt = _coerce_stream_json_prompt(value.get("content"))
            if prompt is not None:
                return prompt
        return None

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = [
            prompt
            for item in value
            if (prompt := _coerce_stream_json_prompt(item)) is not None
        ]
        if parts:
            return "\n".join(parts)
    return None


def _extract_prompt_from_stream_json(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        raise BootError(
            "Error: No prompt provided. Pass a prompt argument or pipe input on stdin."
        )

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        prompts: list[str] = []
        for index, line in enumerate(raw.splitlines(), start=1):
            normalized = line.strip()
            if not normalized:
                continue
            try:
                record = json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise BootError(
                    f"Error: Invalid stream-json input on line {index}: {exc.msg}"
                ) from exc
            prompt = _coerce_stream_json_prompt(record)
            if prompt is not None:
                prompts.append(prompt)
        if prompts:
            return "\n".join(prompts)
        raise BootError(
            "Error: Could not extract a prompt from --input-format stream-json input."
        )

    prompt = _coerce_stream_json_prompt(payload)
    if prompt is not None:
        return prompt
    raise BootError(
        "Error: Could not extract a prompt from --input-format stream-json input."
    )


def _resolve_non_interactive_prompt(opts: ParsedOptions) -> str:
    if isinstance(opts.prompt, str) and opts.prompt.strip():
        return opts.prompt.strip()
    if _stdin_is_tty():
        raise BootError(
            "Error: No prompt provided. Pass a prompt argument or pipe input on stdin."
        )
    raw = sys.stdin.read()
    if (opts.input_format or "text") == "stream-json":
        return _extract_prompt_from_stream_json(raw)
    return _extract_prompt_from_text_input(raw)


def _assistant_text_content(message: Any) -> str:
    from .query import AssistantMessage, TextBlock

    if not isinstance(message, AssistantMessage):
        return ""
    return "".join(
        block.text for block in message.message.content if isinstance(block, TextBlock)
    )


def _serialize_terminal_transition(terminal: Any) -> dict[str, Any] | None:
    if terminal is None:
        return None
    return {
        "reason": getattr(getattr(terminal, "reason", None), "value", None),
        "error": getattr(terminal, "error", None),
        "turnCount": getattr(terminal, "turnCount", None),
    }


def _serialize_query_output(output: Any) -> dict[str, Any]:
    from .query import Message, RequestStartEvent, StreamEvent, TombstoneMessage
    from .state.persistence import _serialize_message

    if isinstance(output, RequestStartEvent):
        return {"type": output.type}
    if isinstance(output, StreamEvent):
        payload: dict[str, Any] = {
            "type": output.type,
            "event": dict(output.event),
        }
        if output.ttftMs is not None:
            payload["ttftMs"] = output.ttftMs
        return payload
    if isinstance(output, TombstoneMessage):
        return {
            "type": output.type,
            "message": _serialize_message(output.message),
        }
    if isinstance(output, Message):
        return _serialize_message(output)
    raise BootError(f"Error: Unsupported non-interactive output: {type(output)!r}")


class _PrintModeEmitter:
    def __init__(self, *, output_format: str) -> None:
        self.output_format = output_format
        self._json_messages: list[dict[str, Any]] = []
        self._assistant_text_parts: list[str] = []
        self._printed_text = False
        self._text_ended_with_newline = True
        self._message_starts_seen = 0
        self._streamed_text_pending = False

    def consume_output(self, output: Any) -> None:
        if output is None:
            return
        if self.output_format == "stream-json":
            self._emit_json_line(_serialize_query_output(output))

        if self.output_format == "json":
            self._consume_json_output(output)
            return

        if self.output_format == "text":
            self._consume_text_output(output)

    def consume_terminal(self, terminal: Any) -> None:
        if self.output_format == "stream-json":
            self._emit_json_line(
                {"type": "terminal", **(_serialize_terminal_transition(terminal) or {})}
            )

    def finalize(
        self,
        *,
        session_id: str | None,
        cwd: str,
        terminal: Any,
    ) -> None:
        if self.output_format == "json":
            payload = {
                "session_id": session_id,
                "cwd": cwd,
                "terminal": _serialize_terminal_transition(terminal),
                "assistant_text": "".join(self._assistant_text_parts).strip() or None,
                "messages": self._json_messages,
            }
            self._emit_json_line(payload)
            return

        if self.output_format == "text" and self._printed_text and not self._text_ended_with_newline:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._text_ended_with_newline = True

    def _consume_json_output(self, output: Any) -> None:
        from .query import AssistantMessage, Message, TombstoneMessage

        if isinstance(output, Message):
            self._json_messages.append(_serialize_query_output(output))
            if isinstance(output, AssistantMessage):
                text = _assistant_text_content(output)
                if text:
                    self._assistant_text_parts.append(text)
            return
        if isinstance(output, TombstoneMessage):
            self._json_messages.append(_serialize_query_output(output))

    def _consume_text_output(self, output: Any) -> None:
        from .query import AssistantMessage, StreamEvent

        if isinstance(output, StreamEvent):
            part = dict(output.event)
            if part.get("type") == "message_start":
                if self._message_starts_seen > 0:
                    self._write_text_separator()
                self._message_starts_seen += 1
                return
            if part.get("type") != "content_block_delta":
                return
            delta = part.get("delta")
            if not isinstance(delta, Mapping) or delta.get("type") != "text_delta":
                return
            text = str(delta.get("text", ""))
            if text:
                self._write_text(text)
                self._streamed_text_pending = True
            return

        if isinstance(output, AssistantMessage):
            text = _assistant_text_content(output)
            if not text:
                self._streamed_text_pending = False
                return
            if self._streamed_text_pending:
                self._streamed_text_pending = False
                return
            if self._printed_text:
                self._write_text_separator()
            self._write_text(text)

    def _emit_json_line(self, payload: Mapping[str, Any]) -> None:
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()

    def _write_text(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self._printed_text = True
        self._text_ended_with_newline = text.endswith("\n")

    def _write_text_separator(self) -> None:
        if self._printed_text and not self._text_ended_with_newline:
            self._write_text("\n")


async def _run_print_mode_stream(
    runtime: Any,
    emitter: _PrintModeEmitter,
) -> None:
    from .query_streaming import stream_query_session

    model_adapter = runtime.model_adapter
    if model_adapter is None:
        raise RuntimeError("Model adapter was not initialized")
    config = runtime._effective_stream_config()
    # Clear the shared interrupt signal at the very start of the turn (on the
    # event loop). Any stale ``signal.set()`` scheduled between turns via
    # ``call_soon_threadsafe`` runs before this coroutine's first step, so this
    # clear cancels it — an interrupt that arrived while no turn was running is
    # effectively ignored, while a set() that lands mid-turn still aborts.
    if config.signal is not None:
        config.signal.clear()
    async for event in stream_query_session(
        runtime.session,
        model_adapter=model_adapter,
        tool_executor=runtime.tool_executor,
        config=config,
    ):
        runtime.session = event.session
        runtime._persist_session()
        emitter.consume_output(event.output)
        if event.terminal is not None:
            emitter.consume_terminal(event.terminal)


def _run_print_mode_turn(runtime: Any, emitter: _PrintModeEmitter, prompt: str) -> int:
    """Run exactly one print-mode turn and return its exit code (0 ok, 1 error).

    This is the shared per-turn machinery used by both the one-shot print path
    (:func:`_run_print_mode`) and the persistent stream-json loop
    (:func:`_run_print_mode_persistent`). It appends ``prompt`` as a new user
    message, drives a single turn through :func:`_run_print_mode_stream`, and
    always emits the per-turn ``terminal`` line via the emitter.
    """
    from .query import TerminalReason, TerminalTransition, createAssistantAPIErrorMessage, createUserMessage
    from .utils.error_display import (
        build_structured_error_display,
        format_structured_error_markdown,
    )
    from .utils.error_log_sink import DiagnosticContext

    exit_code = 0
    normalized_prompt = runtime._emit_user_prompt_submit_hook(prompt)
    runtime.session = runtime.session.startTurn()
    runtime.session = runtime.session.appendMessage(
        createUserMessage(content=normalized_prompt)
    )
    turn_start_message_count = len(runtime.session.messages)
    runtime._persist_session()
    try:
        runtime._get_runner().run(_run_print_mode_stream(runtime, emitter))
        error_message = runtime._ensure_terminal_error_message_for_turn(
            previous_message_count=turn_start_message_count,
            title="Model Error",
            code="model_request_error",
            stage="print_mode",
        )
        if error_message is not None:
            emitter.consume_output(error_message)
            emitter.consume_terminal(runtime.session.lastTerminal)
    except Exception as exc:
        display = build_structured_error_display(
            exc,
            title="Model Error",
            code="model_request_error",
            stage="print_mode",
            config_home=runtime.config_home,
            diagnostic_context=DiagnosticContext(
                session_id=runtime.session_id or "print-mode",
                cwd=runtime.cwd,
                version=runtime.version,
            ),
            log_error=True,
        )
        error_message = createAssistantAPIErrorMessage(
            content=format_structured_error_markdown(display),
            apiError="model_request_error",
            error=exc,
            errorDetails=display.detail,
        )
        runtime.session = runtime.session.appendMessage(error_message)
        runtime.session = runtime.session.finishTurn(
            TerminalTransition(
                reason=TerminalReason.MODEL_ERROR,
                error=display.detail,
            )
        )
        emitter.consume_output(error_message)
        emitter.consume_terminal(runtime.session.lastTerminal)
    terminal = runtime.session.lastTerminal
    emitter.finalize(
        session_id=runtime.session_id,
        cwd=runtime.cwd,
        terminal=terminal,
    )
    if terminal is not None and terminal.reason != TerminalReason.COMPLETED:
        exit_code = 1
    return exit_code


def _run_print_mode(
    *,
    prompt: str,
    output_format: str,
    session_id: str | None,
    continue_most_recent: bool,
    resume_session_id: str | None,
    resume_latest_session: bool,
    persist_sessions: bool,
    display_name: str | None,
    permission_mode: str,
    stream_config: Any,
    inline_agents: Sequence[AgentDefinition] | None = None,
) -> int:
    from .repl_runtime import InteractiveReplRuntime

    runtime = InteractiveReplRuntime(
        session_id=session_id,
        continue_most_recent=continue_most_recent,
        resume_session_id=resume_session_id,
        resume_latest_session=resume_latest_session,
        persist_sessions=persist_sessions,
        display_name=display_name,
        permission_mode=permission_mode,
        stream_config=stream_config,
        inline_agents=tuple(inline_agents or ()),
    )
    emitter = _PrintModeEmitter(output_format=output_format)
    try:
        exit_code = _run_print_mode_turn(runtime, emitter, prompt)
    finally:
        runtime.close()
    return exit_code


def _classify_stream_json_line(record: Any) -> tuple[str, str | None]:
    """Classify one parsed NDJSON control/user record for the persistent loop.

    Returns a ``(kind, prompt)`` tuple where ``kind`` is one of:

    - ``"user"``    — a user message; ``prompt`` is the extracted text.
    - ``"interrupt"`` — ``{"type":"control","subtype":"interrupt"}``.
    - ``"end"``     — ``{"type":"control","subtype":"end"}``.
    - ``"ignore"``  — anything unrecognized (skipped defensively).

    Accepted user shapes (all funnel through ``_coerce_stream_json_prompt``):
      ``{"type":"user","message":{"role":"user","content":"<text>"}}``
      ``{"type":"user","content":"<text>"}``
      ``{"prompt":"<text>"}``
    """
    if isinstance(record, Mapping):
        if record.get("type") == "control":
            subtype = record.get("subtype")
            if subtype == "interrupt":
                return ("interrupt", None)
            if subtype in ("end", "close", "stop"):
                return ("end", None)
            return ("ignore", None)

    prompt = _coerce_stream_json_prompt(record)
    if prompt is not None:
        return ("user", prompt)
    return ("ignore", None)


def _run_print_mode_persistent(
    *,
    output_format: str,
    session_id: str | None,
    continue_most_recent: bool,
    resume_session_id: str | None,
    resume_latest_session: bool,
    persist_sessions: bool,
    display_name: str | None,
    permission_mode: str,
    stream_config: Any,
    inline_agents: Sequence[AgentDefinition] | None = None,
) -> int:
    """Persistent multi-turn ``--print --input-format stream-json`` loop.

    Keeps the worker process alive, reading NDJSON from stdin one line at a
    time. Each ``user`` line runs one turn through the shared per-turn
    machinery; ``control/interrupt`` aborts the currently-running turn by
    setting a shared ``asyncio.Event``; ``control/end`` or stdin EOF exits.
    """
    import asyncio
    import queue as _queue
    import threading
    from dataclasses import replace as _dataclass_replace

    from .repl_runtime import InteractiveReplRuntime

    # A single shared interrupt signal wired into the stream config. Each turn
    # clears it at the start (inside _run_print_mode_stream, on the loop) and
    # stream_query_session aborts the turn/tool-calls when it becomes set.
    interrupt_signal = asyncio.Event()
    stream_config = _dataclass_replace(stream_config, signal=interrupt_signal)

    runtime = InteractiveReplRuntime(
        session_id=session_id,
        continue_most_recent=continue_most_recent,
        resume_session_id=resume_session_id,
        resume_latest_session=resume_latest_session,
        persist_sessions=persist_sessions,
        display_name=display_name,
        permission_mode=permission_mode,
        stream_config=stream_config,
        inline_agents=tuple(inline_agents or ()),
    )

    # The Runner keeps one persistent event loop across every per-turn
    # run() call, so the reader thread can schedule interrupt_signal.set()
    # onto it via call_soon_threadsafe even while a turn is in flight.
    loop = runtime._get_runner().get_loop()

    # Reader thread → main loop hand-off. User prompts flow over this queue;
    # interrupts are applied out-of-band (directly on the loop) so they land
    # mid-turn instead of waiting for the current turn's queue.get().
    message_queue: "_queue.Queue[tuple[str, str | None]]" = _queue.Queue()

    def _reader() -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception as exc:  # stdin closed/errored → treat as end
                sys.stderr.write(f"stream-json reader: stdin read failed: {exc}\n")
                message_queue.put(("end", None))
                return
            if line == "":  # EOF / closed stdin
                message_queue.put(("end", None))
                return
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                sys.stderr.write(
                    f"stream-json reader: skipping malformed line: {exc.msg}\n"
                )
                continue
            try:
                kind, prompt = _classify_stream_json_line(record)
            except Exception as exc:  # never let a bad line kill the reader
                sys.stderr.write(
                    f"stream-json reader: skipping unclassifiable line: {exc}\n"
                )
                continue
            if kind == "interrupt":
                # Set the signal on the loop so a currently-running turn aborts.
                # If no turn is running the set() is cleared at the next turn
                # start, so it is effectively ignored.
                try:
                    loop.call_soon_threadsafe(interrupt_signal.set)
                except RuntimeError:
                    pass  # loop already closed during shutdown
                continue
            if kind == "end":
                message_queue.put(("end", None))
                return
            if kind == "user" and prompt:
                message_queue.put(("user", prompt))
                continue
            # kind == "ignore" (or empty user prompt): skip defensively.
            sys.stderr.write(
                "stream-json reader: skipping unrecognized message\n"
            )

    reader_thread = threading.Thread(
        target=_reader, name="stream-json-reader", daemon=True
    )
    reader_thread.start()

    try:
        while True:
            kind, prompt = message_queue.get()
            if kind == "end":
                break
            if kind != "user" or not prompt:
                continue
            # Fresh emitter per turn so every turn emits its own stream-json
            # events and its own trailing `terminal` line (turn boundary). The
            # per-turn exit code is intentionally not propagated: an aborted or
            # errored turn must not tear down a persistent session — the driver
            # sees the turn's terminal line and keeps steering.
            emitter = _PrintModeEmitter(output_format=output_format)
            try:
                _run_print_mode_turn(runtime, emitter, prompt)
            except BaseException as exc:  # noqa: BLE001 - a per-turn failure MUST NOT kill the session
                # _run_print_mode_turn already handles model/tool errors internally; this catches
                # anything that still escapes (setup/finalize) so the persistent session survives and
                # keeps steering. Emit a raw terminal so the driver closes out this turn.
                sys.stderr.write(f"stream-json loop: turn crashed, session continues: {exc!r}\n")
                try:
                    sys.stdout.write(
                        json.dumps({"type": "terminal", "reason": "error", "error": str(exc)}) + "\n"
                    )
                    sys.stdout.flush()
                except Exception:
                    pass
    finally:
        runtime.close()
    # control/end or stdin EOF ends the loop cleanly.
    return 0


def _merge_system_prompt_segments(*segments: Optional[str]) -> Optional[str]:
    return merge_system_prompt_segments(*segments)


def _sigint_handler(signum: int, frame: object) -> None:
    args = sys.argv[1:] if sys.argv else []
    if "-p" in args or "--print" in args:
        return
    sys.exit(0)
