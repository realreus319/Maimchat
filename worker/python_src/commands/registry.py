"""Command registry — slash-command surface with gate classifications.

Python port of src/commands.ts COMMANDS() array.

Every command from the TypeScript registry is represented here as a
``CommandDefinition`` with its gate type, gate expression, and linux
exposure classification.  The data is derived mechanically from:

  - src/commands.ts  (the COMMANDS memoized array)
  - scripts/verify/check_feature_matrix.py  (gate + exposure classification)

No command execution logic lives here — only the surface metadata needed
for feature-matrix parity verification and command-surface filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Gate type constants — must match check_feature_matrix.py exactly
# ---------------------------------------------------------------------------

GATE_ALWAYS = "always"
GATE_FEATURE_FLAG = "feature_flag"
GATE_ENV_VAR = "env_var"
GATE_RUNTIME_CHECK = "runtime_check"
GATE_BUILD_USER_TYPE = "build_user_type"
GATE_PLATFORM = "platform"
GATE_TEST_ENV = "test_env"

# ---------------------------------------------------------------------------
# Exposure classification constants
# ---------------------------------------------------------------------------

EXPOSURE_LINUX_REQUIRED = "linux_required"
EXPOSURE_LINUX_DEFERRED = "linux_deferred"
EXPOSURE_ANT_ONLY = "ant_only"
EXPOSURE_PLATFORM_SPECIFIC = "platform_specific"
EXPOSURE_TEST_ONLY = "test_only"

# ---------------------------------------------------------------------------
# Command definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandDefinition:
    """Immutable metadata for a single slash-command surface entry."""

    name: str
    description: str
    command_type: str  # 'prompt', 'local', 'local-jsx'
    gate_type: str
    gate_expression: str
    linux_exposure: str
    aliases: Tuple[str, ...] = ()
    is_hidden: bool = False


# ---------------------------------------------------------------------------
# Internal-only commands — mirror src/commands.ts INTERNAL_ONLY_COMMANDS
# These are eliminated from the external build and only appear when
# USER_TYPE === 'ant' && !IS_DEMO.
# ---------------------------------------------------------------------------

INTERNAL_ONLY_COMMANDS: FrozenSet[str] = frozenset(
    {
        "backfillSessions",
        "breakCache",
        "bughunter",
        "commit",
        "commitPushPr",
        "ctx_viz",
        "goodClaude",
        "issue",
        "initVerifiers",
        "forceSnip",
        "mockLimits",
        "bridgeKick",
        "version",
        "ultraplan",
        "subscribePr",
        "resetLimits",
        "resetLimitsNonInteractive",
        "onboarding",
        "share",
        "summary",
        "teleport",
        "antTrace",
        "perfIssue",
        "env",
        "oauthRefresh",
        "debugToolCall",
        "agentsPlatform",
        "autofixPr",
    }
)

# ---------------------------------------------------------------------------
# Gate definitions per command — mirror check_feature_matrix.py
# COMMAND_GATE_EXPRESSIONS
# ---------------------------------------------------------------------------

COMMAND_GATE_DEFINITIONS: Mapping[str, Tuple[str, str]] = {
    "agentsPlatform": (GATE_BUILD_USER_TYPE, "USER_TYPE == 'ant'"),
    "proactive": (GATE_FEATURE_FLAG, "PROACTIVE || KAIROS"),
    "briefCommand": (GATE_FEATURE_FLAG, "KAIROS || KAIROS_BRIEF"),
    "assistantCommand": (GATE_FEATURE_FLAG, "KAIROS"),
    "bridge": (GATE_FEATURE_FLAG, "BRIDGE_MODE"),
    "remoteControlServerCommand": (GATE_FEATURE_FLAG, "DAEMON && BRIDGE_MODE"),
    "voiceCommand": (GATE_FEATURE_FLAG, "VOICE_MODE"),
    "forceSnip": (GATE_FEATURE_FLAG, "HISTORY_SNIP"),
    "workflowsCmd": (GATE_FEATURE_FLAG, "WORKFLOW_SCRIPTS"),
    "webCmd": (GATE_FEATURE_FLAG, "CCR_REMOTE_SETUP"),
    "subscribePr": (GATE_FEATURE_FLAG, "KAIROS_GITHUB_WEBHOOKS"),
    "ultraplan": (GATE_FEATURE_FLAG, "ULTRAPLAN"),
    "torch": (GATE_FEATURE_FLAG, "TORCH"),
    "peersCmd": (GATE_FEATURE_FLAG, "UDS_INBOX"),
    "forkCmd": (GATE_FEATURE_FLAG, "FORK_SUBAGENT"),
    "buddy": (GATE_FEATURE_FLAG, "BUDDY"),
    "login": (GATE_RUNTIME_CHECK, "!isUsing3PServices()"),
    "logout": (GATE_RUNTIME_CHECK, "!isUsing3PServices()"),
}

# ---------------------------------------------------------------------------
# Command aliases — extracted from src/commands.ts and src/commands/**
# These are the alternative slash-command triggers for each command.
# ---------------------------------------------------------------------------

COMMAND_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "clear": ("reset", "new"),
    "config": ("settings",),
    "desktop": ("app",),
    "exit": ("quit",),
    "feedback": ("bug",),
    "mobile": ("ios", "android"),
    "permissions": ("allowed-tools",),
    "plugin": ("plugins", "marketplace"),
    "resume": ("continue",),
    "rewind": ("checkpoint",),
    "tasks": ("bashes",),
    "session": ("remote",),
    "bridge": ("rc",),
}

# ---------------------------------------------------------------------------
# The full registry — every command from src/commands.ts COMMANDS()
#
# Fields:
#   name            — JavaScript identifier used in the COMMANDS array
#   description     — short description (matches src/commands/<name>/*.ts)
#   command_type    — TS type: 'prompt', 'local', 'local-jsx'
#   gate_type       — gate classification
#   gate_expression — gate condition text
#   linux_exposure  — exposure classification
#   aliases         — alternative slash triggers
#   is_hidden       — hidden from typeahead when not in remote mode etc.
# ---------------------------------------------------------------------------

_REGISTRY: List[CommandDefinition] = [
    # ---- LINUX_REQUIRED: always-on commands (71) ----
    CommandDefinition(
        name="addDir",
        description="Add additional directories for tool access",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="advisor",
        description="Get advice from an advisor model",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="agents",
        description="Manage custom agents",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="branch",
        description="Switch between git branches",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="btw",
        description="Quick note",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="chrome",
        description="Chrome integration",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="clear",
        description="Clear conversation history",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("reset", "new"),
    ),
    CommandDefinition(
        name="color",
        description="Change agent color",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="compact",
        description="Compact conversation context",
        command_type="local",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="config",
        description="Manage configuration settings",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("settings",),
    ),
    CommandDefinition(
        name="context",
        description="Context management",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="contextNonInteractive",
        description="Context management (non-interactive)",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="copy",
        description="Copy last message",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="cost",
        description="Show session cost",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="desktop",
        description="Desktop app management",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("app",),
    ),
    CommandDefinition(
        name="diff",
        description="Show diff of changes",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="doctor",
        description="Run diagnostics",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="effort",
        description="Set effort level",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="exit",
        description="Exit Claude Code",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("quit",),
    ),
    CommandDefinition(
        name="fast",
        description="Fast mode toggle",
        command_type="local",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="feedback",
        description="Send feedback",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("bug",),
    ),
    CommandDefinition(
        name="files",
        description="List tracked files",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="heapDump",
        description="Capture heap dump",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="help",
        description="Show help and available commands",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="hooks",
        description="Manage hooks",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="ide",
        description="IDE integration",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="init",
        description="Initialize Claude Code configuration",
        command_type="local",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="installGitHubApp",
        description="Install GitHub App",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="installSlackApp",
        description="Install Slack App",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="keybindings",
        description="Manage keybindings",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="mcp",
        description="Manage MCP servers",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="memory",
        description="Manage memory",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="mobile",
        description="Mobile QR code",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("ios", "android"),
    ),
    CommandDefinition(
        name="model",
        description="Change model",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="outputStyle",
        description="Set output style",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="passes",
        description="Manage passes",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="permissions",
        description="Manage permissions",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("allowed-tools",),
    ),
    CommandDefinition(
        name="plan",
        description="Plan mode toggle",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="plugin",
        description="Manage plugins",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("plugins", "marketplace"),
    ),
    CommandDefinition(
        name="pr_comments",
        description="View PR comments",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="privacySettings",
        description="Privacy settings",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="rateLimitOptions",
        description="Rate limit options",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="releaseNotes",
        description="Show release notes",
        command_type="local",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="reloadPlugins",
        description="Reload plugins",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="remoteEnv",
        description="Remote environment setup",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="rename",
        description="Rename current session",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="resume",
        description="Resume a conversation",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        aliases=("continue",),
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
    ),
    CommandDefinition(
        name="review",
        description="Code review",
        command_type="prompt",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="rewind",
        description="Rewind conversation to checkpoint",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("checkpoint",),
    ),
    CommandDefinition(
        name="sandboxToggle",
        description="Toggle sandbox mode",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="securityReview",
        description="Security review",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="session",
        description="Session management",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("remote",),
    ),
    CommandDefinition(
        name="skills",
        description="Manage skills",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="stats",
        description="Show statistics",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="status",
        description="Show Claude Code status",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="statusline",
        description="Toggle status line",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="stickers",
        description="Stickers",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="tag",
        description="Tag management",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="tasks",
        description="Manage background tasks",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("bashes",),
    ),
    CommandDefinition(
        name="terminalSetup",
        description="Terminal setup",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="theme",
        description="Change terminal theme",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="thinkback",
        description="Review thinking",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="thinkbackPlay",
        description="Play thinking replay",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="ultrareview",
        description="Enhanced code review",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="upgrade",
        description="Upgrade Claude Code",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="extraUsage",
        description="Extra usage information",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="extraUsageNonInteractive",
        description="Extra usage (non-interactive)",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="exportCommand",
        description="Export conversation",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="usage",
        description="Show usage information",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="usageReport",
        description="Generate usage insights report",
        command_type="prompt",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    CommandDefinition(
        name="vim",
        description="Toggle vim mode",
        command_type="local-jsx",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by COMMANDS registry",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=(),
    ),
    # ---- LINUX_DEFERRED: gated commands (14) ----
    CommandDefinition(
        name="proactive",
        description="Proactive mode",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="PROACTIVE || KAIROS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="briefCommand",
        description="Brief mode",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS || KAIROS_BRIEF",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="assistantCommand",
        description="Assistant command",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="bridge",
        description="Bridge mode",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="BRIDGE_MODE",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=("rc",),
    ),
    CommandDefinition(
        name="remoteControlServerCommand",
        description="Remote control server",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="DAEMON && BRIDGE_MODE",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="voiceCommand",
        description="Voice mode",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="VOICE_MODE",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="workflowsCmd",
        description="Workflow scripts",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="WORKFLOW_SCRIPTS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="webCmd",
        description="Web remote setup",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="CCR_REMOTE_SETUP",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="peersCmd",
        description="Peer connections",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="UDS_INBOX",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="forkCmd",
        description="Fork subagent",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="FORK_SUBAGENT",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="buddy",
        description="Buddy mode",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="BUDDY",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="torch",
        description="Torch command",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="TORCH",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="login",
        description="Log in to Claude",
        command_type="local-jsx",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="!isUsing3PServices()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    CommandDefinition(
        name="logout",
        description="Log out of Claude",
        command_type="local-jsx",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="!isUsing3PServices()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        aliases=(),
    ),
    # ---- ANT_ONLY: internal-only commands (28) ----
    CommandDefinition(
        name="agentsPlatform",
        description="Platform agents (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="antTrace",
        description="Ant trace (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="autofixPr",
        description="Auto-fix PR (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="backfillSessions",
        description="Backfill sessions (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="breakCache",
        description="Break cache (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="bridgeKick",
        description="Bridge kick (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="bughunter",
        description="Bug hunter (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="commit",
        description="Commit (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="commitPushPr",
        description="Commit, push, PR (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="ctx_viz",
        description="Context visualizer (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="debugToolCall",
        description="Debug tool call (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="env",
        description="Environment (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="forceSnip",
        description="Force snip (internal)",
        command_type="local",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="HISTORY_SNIP",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="goodClaude",
        description="Good Claude (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="initVerifiers",
        description="Initialize verifiers (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="issue",
        description="Issue (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="mockLimits",
        description="Mock limits (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="oauthRefresh",
        description="OAuth refresh (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="onboarding",
        description="Onboarding (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="perfIssue",
        description="Performance issue (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="resetLimits",
        description="Reset limits (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="resetLimitsNonInteractive",
        description="Reset limits non-interactive (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="share",
        description="Share (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="subscribePr",
        description="Subscribe PR (internal)",
        command_type="local",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS_GITHUB_WEBHOOKS",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="summary",
        description="Summary (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="teleport",
        description="Teleport (internal)",
        command_type="local-jsx",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="ultraplan",
        description="Ultraplan (internal)",
        command_type="local-jsx",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="ULTRAPLAN",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
    CommandDefinition(
        name="version",
        description="Version (internal)",
        command_type="local",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        aliases=(),
    ),
]


# ---------------------------------------------------------------------------
# Derived lookup structures
# ---------------------------------------------------------------------------


def _build_registry() -> Dict[str, CommandDefinition]:
    result: Dict[str, CommandDefinition] = {}
    for cmd in _REGISTRY:
        result[cmd.name] = cmd
    return result


BUILTIN_COMMAND_REGISTRY: Dict[str, CommandDefinition] = _build_registry()

LINUX_REQUIRED_COMMANDS: Dict[str, CommandDefinition] = {
    name: cmd
    for name, cmd in BUILTIN_COMMAND_REGISTRY.items()
    if cmd.linux_exposure == EXPOSURE_LINUX_REQUIRED
}

LINUX_DEFERRED_COMMANDS: Dict[str, CommandDefinition] = {
    name: cmd
    for name, cmd in BUILTIN_COMMAND_REGISTRY.items()
    if cmd.linux_exposure == EXPOSURE_LINUX_DEFERRED
}


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


def get_all_command_names() -> Set[str]:
    return set(BUILTIN_COMMAND_REGISTRY.keys())


def get_linux_required_names() -> Set[str]:
    return set(LINUX_REQUIRED_COMMANDS.keys())


def get_linux_required_commands() -> List[CommandDefinition]:
    return sorted(LINUX_REQUIRED_COMMANDS.values(), key=lambda c: c.name)


def get_registered_aliases() -> Dict[str, Tuple[str, ...]]:
    return {
        name: cmd.aliases
        for name, cmd in BUILTIN_COMMAND_REGISTRY.items()
        if cmd.aliases
    }


def is_internal_only(name: str) -> bool:
    return name in INTERNAL_ONLY_COMMANDS


def classify_command(name: str) -> Tuple[str, str, str]:
    gate_type, gate_expression = COMMAND_GATE_DEFINITIONS.get(
        name,
        (GATE_ALWAYS, "always exposed by COMMANDS registry"),
    )
    if name in INTERNAL_ONLY_COMMANDS:
        return gate_type, gate_expression, EXPOSURE_ANT_ONLY
    if gate_type == GATE_ALWAYS:
        return gate_type, gate_expression, EXPOSURE_LINUX_REQUIRED
    return gate_type, gate_expression, EXPOSURE_LINUX_DEFERRED


def find_command(name: str) -> Optional[CommandDefinition]:
    cmd = BUILTIN_COMMAND_REGISTRY.get(name)
    if cmd is not None:
        return cmd
    for cmd_def in BUILTIN_COMMAND_REGISTRY.values():
        if name in cmd_def.aliases:
            return cmd_def
    return None


def has_command(name: str) -> bool:
    return find_command(name) is not None
