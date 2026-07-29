"""Command registration, gating, and surface exposure.

Python port of src/commands.ts command-surface slice.
Scope: exposure only — no execution, TUI, skill/plugin loading, or tool registry.
"""

from .registry import (
    BUILTIN_COMMAND_REGISTRY,
    COMMAND_ALIASES,
    COMMAND_GATE_DEFINITIONS,
    GATE_ALWAYS,
    GATE_BUILD_USER_TYPE,
    GATE_ENV_VAR,
    GATE_FEATURE_FLAG,
    GATE_RUNTIME_CHECK,
    INTERNAL_ONLY_COMMANDS,
    LINUX_DEFERRED_COMMANDS,
    LINUX_REQUIRED_COMMANDS,
    CommandDefinition,
    classify_command,
    get_all_command_names,
    get_linux_required_commands,
    get_linux_required_names,
    get_registered_aliases,
    is_internal_only,
)

__all__ = [
    "BUILTIN_COMMAND_REGISTRY",
    "COMMAND_ALIASES",
    "COMMAND_GATE_DEFINITIONS",
    "GATE_ALWAYS",
    "GATE_BUILD_USER_TYPE",
    "GATE_ENV_VAR",
    "GATE_FEATURE_FLAG",
    "GATE_RUNTIME_CHECK",
    "INTERNAL_ONLY_COMMANDS",
    "LINUX_DEFERRED_COMMANDS",
    "LINUX_REQUIRED_COMMANDS",
    "CommandDefinition",
    "classify_command",
    "get_all_command_names",
    "get_linux_required_commands",
    "get_linux_required_names",
    "get_registered_aliases",
    "is_internal_only",
]
