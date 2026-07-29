"""
Pure permission type definitions.

Port of src/types/permissions.ts. Contains only type definitions and constants
with no runtime dependencies, avoiding circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Union


# ============================================================================
# Permission Modes
# ============================================================================

EXTERNAL_PERMISSION_MODES: List[str] = [
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
]

# Runtime validation set: modes that are valid inside the app state, including
# internal-only delegation modes that are not exposed on the CLI.
INTERNAL_PERMISSION_MODES: List[str] = [
    *EXTERNAL_PERMISSION_MODES,
    "bubble",
]
PERMISSION_MODES: List[str] = INTERNAL_PERMISSION_MODES


class PermissionMode(str, Enum):
    """Exhaustive mode union for typechecking."""

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    AUTO = "auto"
    BUBBLE = "bubble"

    @classmethod
    def from_string(cls, s: str) -> "PermissionMode":
        try:
            return cls(s)
        except ValueError:
            return cls.DEFAULT


# ============================================================================
# Permission Behaviors
# ============================================================================


class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


# ============================================================================
# Permission Rules
# ============================================================================


class PermissionRuleSource(str, Enum):
    USER_SETTINGS = "userSettings"
    PROJECT_SETTINGS = "projectSettings"
    LOCAL_SETTINGS = "localSettings"
    FLAG_SETTINGS = "flagSettings"
    POLICY_SETTINGS = "policySettings"
    CLI_ARG = "cliArg"
    COMMAND = "command"
    SESSION = "session"


@dataclass(frozen=True)
class PermissionRuleValue:
    tool_name: str
    rule_content: Optional[str] = None


@dataclass(frozen=True)
class PermissionRule:
    source: PermissionRuleSource
    rule_behavior: PermissionBehavior
    rule_value: PermissionRuleValue


# ============================================================================
# Permission Updates
# ============================================================================


class PermissionUpdateDestination(str, Enum):
    USER_SETTINGS = "userSettings"
    PROJECT_SETTINGS = "projectSettings"
    LOCAL_SETTINGS = "localSettings"
    SESSION = "session"
    CLI_ARG = "cliArg"


# ============================================================================
# Permission Decisions & Results
# ============================================================================


@dataclass(frozen=True)
class RuleDecisionReason:
    type: str = "rule"
    rule: PermissionRule = field(
        default_factory=lambda: PermissionRule(
            source=PermissionRuleSource.SESSION,
            rule_behavior=PermissionBehavior.ALLOW,
            rule_value=PermissionRuleValue(tool_name=""),
        )
    )


@dataclass(frozen=True)
class ModeDecisionReason:
    type: str = "mode"
    mode: str = "default"


@dataclass(frozen=True)
class SafetyCheckDecisionReason:
    type: str = "safetyCheck"
    reason: str = ""
    classifier_approvable: bool = False


@dataclass(frozen=True)
class WorkingDirDecisionReason:
    type: str = "workingDir"
    reason: str = ""


@dataclass(frozen=True)
class OtherDecisionReason:
    type: str = "other"
    reason: str = ""


@dataclass(frozen=True)
class HookDecisionReason:
    type: str = "hook"
    hook_name: str = ""
    hook_source: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class AsyncAgentDecisionReason:
    type: str = "asyncAgent"
    reason: str = ""


@dataclass(frozen=True)
class SubcommandResultsDecisionReason:
    type: str = "subcommandResults"
    reasons: Dict[str, "PermissionResult"] = field(default_factory=dict)


PermissionDecisionReason = Union[
    RuleDecisionReason,
    ModeDecisionReason,
    SafetyCheckDecisionReason,
    WorkingDirDecisionReason,
    OtherDecisionReason,
    HookDecisionReason,
    AsyncAgentDecisionReason,
    SubcommandResultsDecisionReason,
]


@dataclass
class PermissionAllowDecision:
    behavior: str = "allow"
    updated_input: Optional[Dict] = None
    user_modified: bool = False
    decision_reason: Optional[PermissionDecisionReason] = None
    tool_use_id: Optional[str] = None


@dataclass
class PermissionAskDecision:
    behavior: str = "ask"
    message: str = ""
    updated_input: Optional[Dict] = None
    decision_reason: Optional[PermissionDecisionReason] = None
    suggestions: Optional[List] = None
    blocked_path: Optional[str] = None
    is_bash_security_check_for_misparsing: bool = False


@dataclass
class PermissionDenyDecision:
    behavior: str = "deny"
    message: str = ""
    decision_reason: Optional[PermissionDecisionReason] = None
    tool_use_id: Optional[str] = None


PermissionDecision = Union[
    PermissionAllowDecision,
    PermissionAskDecision,
    PermissionDenyDecision,
]


@dataclass
class PermissionPassthrough:
    behavior: str = "passthrough"
    message: str = ""
    decision_reason: Optional[PermissionDecisionReason] = None
    suggestions: Optional[List] = None
    blocked_path: Optional[str] = None


PermissionResult = Union[
    PermissionAllowDecision,
    PermissionAskDecision,
    PermissionDenyDecision,
    PermissionPassthrough,
]


# ============================================================================
# Tool Permission Context
# ============================================================================

ToolPermissionRulesBySource = Dict[str, List[str]]


@dataclass
class AdditionalWorkingDirectory:
    path: str
    source: str


@dataclass
class ToolPermissionContext:
    mode: str = "default"
    cwd: Optional[str] = None
    additional_working_directories: Dict[str, AdditionalWorkingDirectory] = field(
        default_factory=dict
    )
    always_allow_rules: ToolPermissionRulesBySource = field(default_factory=dict)
    always_deny_rules: ToolPermissionRulesBySource = field(default_factory=dict)
    always_ask_rules: ToolPermissionRulesBySource = field(default_factory=dict)
    is_bypass_permissions_mode_available: bool = False
    stripped_dangerous_rules: Optional[ToolPermissionRulesBySource] = None
    should_avoid_permission_prompts: bool = False
    await_automated_checks_before_dialog: bool = False
    pre_plan_mode: Optional[str] = None
