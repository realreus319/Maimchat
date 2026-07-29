"""Tool registry — tool surface with gate classifications and schema signatures.

Python port of src/tools.ts getAllBaseTools() and src/Tool.ts lookup helpers.

Every tool from the TypeScript registry is represented here as a
``ToolDefinition`` with its primary name, aliases, gate type,
gate expression, and linux exposure classification.  The data is
derived mechanically from:

  - src/tools.ts  (the getAllBaseTools registry)
  - src/Tool.ts  (the Tool type, toolMatchesName, findToolByName)
  - scripts/verify/check_feature_matrix.py  (gate + exposure classification)

No tool execution logic lives here — only the surface metadata needed
for feature-matrix parity verification and tool-surface filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

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
# Schema field types
# ---------------------------------------------------------------------------

SCHEMA_STRING = "string"
SCHEMA_NUMBER = "number"
SCHEMA_BOOLEAN = "boolean"
SCHEMA_ARRAY = "array"
SCHEMA_OBJECT = "object"
SCHEMA_ENUM = "enum"

SCHEMA_ANY = "any"
DEFAULT_MAX_RESULT_SIZE_CHARS = 100_000
_SCHEMA_DEFAULT_MISSING = object()


@dataclass(frozen=True)
class SchemaField:
    """A single field in a tool's input or output schema."""

    name: str
    field_type: str
    description: str = ""
    required: bool = True
    enum_values: Tuple[str, ...] = ()
    items_type: Optional[str] = None
    properties: Optional[Tuple[SchemaField, ...]] = None
    default: Any = _SCHEMA_DEFAULT_MISSING


@dataclass(frozen=True)
class ToolSchema:
    """Schema definition for a tool's input or output.

    Mirrors the Zod schema shape from the TypeScript tool definitions.
    Fields are ordered alphabetically by name within each schema.
    """

    fields: Tuple[SchemaField, ...] = ()

    def field_names(self) -> Set[str]:
        return {f.name for f in self.fields}

    def required_fields(self) -> Tuple[SchemaField, ...]:
        return tuple(f for f in self.fields if f.required)

    def optional_fields(self) -> Tuple[SchemaField, ...]:
        return tuple(f for f in self.fields if not f.required)

    def field_name_list(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields)


@dataclass(frozen=True)
class ToolDefinition:
    """Immutable metadata for a single tool surface entry.

    Mirrors the shape from src/Tool.ts Tool type but including
    name, aliases, gate/exposure metadata, and schema signatures.
    """

    name: str
    description: str
    gate_type: str
    gate_expression: str
    linux_exposure: str
    aliases: Tuple[str, ...] = ()
    input_schema: Optional[ToolSchema] = None
    # Raw JSON Schema passthrough. When set, it is emitted verbatim as the tool's
    # ``input_schema`` payload instead of deriving one from ``input_schema``.
    # Used for dynamically discovered tools (e.g. MCP servers) whose schema is
    # already a JSON Schema and cannot be losslessly modelled as a ``ToolSchema``.
    raw_input_schema: Optional[Mapping[str, Any]] = None
    output_schema: Optional[ToolSchema] = None
    is_read_only: bool = False
    is_destructive: bool = False
    should_defer: bool = False
    always_load: bool = False
    strict: bool = False
    search_hint: str = ""

    max_result_size_chars: int = DEFAULT_MAX_RESULT_SIZE_CHARS
    source_module: str = ""  # e.g. "BashTool", "AgentTool"

    subsystem: str = "tools"


# ---------------------------------------------------------------------------
# Tool aliases — mirror TS tool alias definitions exactly
# ---------------------------------------------------------------------------

TOOL_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "Agent": ("Task",),
    "TaskStop": ("KillShell",),
    "TaskOutput": ("AgentOutputTool", "BashOutputTool"),
    "SendUserMessage": ("Brief",),
}

# ---------------------------------------------------------------------------
# Tool gate expressions — mirror check_feature_matrix.py TOOL_GATE_EXPRESSIONS
# ---------------------------------------------------------------------------

TOOL_GATE_EXPRESSIONS: Mapping[str, Tuple[str, str]] = {
    "REPLTool": (GATE_BUILD_USER_TYPE, "USER_TYPE == 'ant'"),
    "SuggestBackgroundPRTool": (
        GATE_BUILD_USER_TYPE,
        "USER_TYPE == 'ant'",
    ),
    "ConfigTool": (GATE_BUILD_USER_TYPE, "USER_TYPE == 'ant'"),
    "TungstenTool": (GATE_BUILD_USER_TYPE, "USER_TYPE == 'ant'"),
    "SleepTool": (GATE_FEATURE_FLAG, "PROACTIVE || KAIROS"),
    "CronCreateTool": (GATE_FEATURE_FLAG, "AGENT_TRIGGERS"),
    "CronDeleteTool": (GATE_FEATURE_FLAG, "AGENT_TRIGGERS"),
    "CronListTool": (GATE_FEATURE_FLAG, "AGENT_TRIGGERS"),
    "RemoteTriggerTool": (GATE_FEATURE_FLAG, "AGENT_TRIGGERS_REMOTE"),
    "MonitorTool": (GATE_FEATURE_FLAG, "MONITOR_TOOL"),
    "SendUserFileTool": (GATE_FEATURE_FLAG, "KAIROS"),
    "PushNotificationTool": (
        GATE_FEATURE_FLAG,
        "KAIROS || KAIROS_PUSH_NOTIFICATION",
    ),
    "SubscribePRTool": (GATE_FEATURE_FLAG, "KAIROS_GITHUB_WEBHOOKS"),
    "VerifyPlanExecutionTool": (
        GATE_ENV_VAR,
        "CLAUDE_CODE_VERIFY_PLAN == 'true'",
    ),
    "OverflowTestTool": (GATE_FEATURE_FLAG, "OVERFLOW_TEST_TOOL"),
    "CtxInspectTool": (GATE_FEATURE_FLAG, "CONTEXT_COLLAPSE"),
    "TerminalCaptureTool": (GATE_FEATURE_FLAG, "TERMINAL_PANEL"),
    "WebBrowserTool": (GATE_FEATURE_FLAG, "WEB_BROWSER_TOOL"),
    "PowerShellTool": (
        GATE_PLATFORM,
        "windows && isPowerShellToolEnabled()",
    ),
    "ListPeersTool": (GATE_FEATURE_FLAG, "UDS_INBOX"),
    "WorkflowTool": (GATE_FEATURE_FLAG, "WORKFLOW_SCRIPTS"),
    "TaskCreateTool": (GATE_RUNTIME_CHECK, "isTodoV2Enabled()"),
    "TaskGetTool": (GATE_RUNTIME_CHECK, "isTodoV2Enabled()"),
    "TaskUpdateTool": (GATE_RUNTIME_CHECK, "isTodoV2Enabled()"),
    "TaskListTool": (GATE_RUNTIME_CHECK, "isTodoV2Enabled()"),
    "LSPTool": (GATE_ENV_VAR, "ENABLE_LSP_TOOL"),
    "EnterWorktreeTool": (GATE_RUNTIME_CHECK, "isWorktreeModeEnabled()"),
    "ExitWorktreeTool": (GATE_RUNTIME_CHECK, "isWorktreeModeEnabled()"),
    "TeamCreateTool": (GATE_RUNTIME_CHECK, "isAgentSwarmsEnabled()"),
    "TeamDeleteTool": (GATE_RUNTIME_CHECK, "isAgentSwarmsEnabled()"),
    "SnipTool": (GATE_FEATURE_FLAG, "HISTORY_SNIP"),
    "TestingPermissionTool": (GATE_TEST_ENV, "NODE_ENV == 'test'"),
    "ToolSearchTool": (
        GATE_RUNTIME_CHECK,
        "isToolSearchEnabledOptimistic()",
    ),
}


# ---------------------------------------------------------------------------
# Internal-only tools — excluded from external builds, only for
# USER_TYPE === 'ant' or gated by test env
# ---------------------------------------------------------------------------

INTERNAL_ONLY_TOOLS: FrozenSet[str] = frozenset(
    {
        "ConfigTool",
        "TungstenTool",
        "REPLTool",
        "SuggestBackgroundPRTool",
        "TestingPermissionTool",
    }
)


# ---------------------------------------------------------------------------
# Schema definitions — input schemas for each tool
# Extracted from src/tools/*/prompt.ts and tool .ts files
# ---------------------------------------------------------------------------


# Helper to construct SchemaField tuples more concisely
def _f(
    name: str,
    field_type: str = SCHEMA_STRING,
    description: str = "",
    required: bool = True,
    enum_values: Tuple[str, ...] = (),
    items_type: Optional[str] = None,
    properties: Optional[Tuple[SchemaField, ...]] = None,
    default: Any = _SCHEMA_DEFAULT_MISSING,
) -> SchemaField:
    return SchemaField(
        name=name,
        field_type=field_type,
        description=description,
        required=required,
        enum_values=enum_values,
        items_type=items_type,
        properties=properties,
        default=default,
    )


def _schema(*fields: SchemaField) -> ToolSchema:
    return ToolSchema(fields=fields)


# --- Individual tool schemas ---

_AGENT_INPUT = _schema(
    _f("description", description="A short description of the task"),
    _f("prompt", description="The task instructions"),
    _f(
        "subagent_type",
        required=False,
        description="Type of subagent to spawn",
    ),
    _f("model", required=False, description="Model alias or full model ID"),
    _f(
        "run_in_background",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Run as a background task",
    ),
    _f("name", required=False, description="Display name for the agent"),
    _f("team_name", required=False, description="Team name for coordination"),
    _f(
        "mode",
        required=False,
        description="Agent mode (e.g. 'fork', 'broadcast')",
    ),
    _f(
        "isolation",
        required=False,
        description="Isolation policy for the subagent",
    ),
    _f(
        "cwd",
        required=False,
        description="Working directory for the subagent",
    ),
    _f(
        "max_tokens",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Maximum output token budget for the subagent",
    ),
)

_AGENT_OUTPUT = _schema(
    _f("status", description="completed or async_launched"),
    _f("prompt", description="The prompt used"),
    _f("agentId", required=False, description="ID of the spawned agent"),
    _f("agentType", required=False, description="Type of the spawned agent"),
    _f("description", required=False, description="Description of the task"),
    _f("taskId", required=False, description="Task ID for the spawned agent"),
    _f("outputFile", required=False, description="Output file path"),
    _f("error", required=False, description="Agent execution error"),
    _f("cwd", required=False, description="Working directory used by the agent"),
    _f("model", required=False, description="Model used by the agent"),
    _f(
        "maxTokens",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Maximum token budget used by the agent",
    ),
    _f(
        "canReadOutputFile",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the output file can be read",
    ),
)

_BASH_INPUT = _schema(
    _f("command", description="The bash command to run"),
    _f(
        "timeout",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Timeout in milliseconds",
    ),
    _f("description", required=False, description="Short description of the command"),
    _f(
        "run_in_background",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Run in background",
    ),
    _f(
        "dangerouslyDisableSandbox",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Disable sandbox for this command",
    ),
)

_BASH_OUTPUT = _schema(
    _f(
        "result_type",
        required=False,
        description="Structured Bash result type such as text or image",
    ),
    _f(
        "file_path",
        required=False,
        description="Detected image file path when Bash output resolves to a local image",
    ),
    _f("stdout", required=False, description="Standard output"),
    _f("stderr", required=False, description="Standard error output"),
    _f(
        "task_id",
        required=False,
        description="Background task ID when the command is launched asynchronously",
    ),
    _f(
        "task_type",
        required=False,
        description="Background task type when the command is launched asynchronously",
    ),
    _f(
        "status",
        required=False,
        description="Execution status or background launch status",
    ),
    _f(
        "description",
        required=False,
        description="Human-readable description of the command or task",
    ),
    _f(
        "output_file",
        required=False,
        description="Path to the background task output log file",
    ),
    _f(
        "backgroundTaskId",
        required=False,
        description="Stable identifier for a background bash task",
    ),
    _f(
        "backgroundedByUser",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the user explicitly requested background execution",
    ),
    _f(
        "assistantAutoBackgrounded",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the assistant automatically backgrounded the command",
    ),
    _f(
        "persistedOutputPath",
        required=False,
        description="Persisted log path for background task output",
    ),
    _f(
        "persistedOutputSize",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Persisted output size in bytes when available",
    ),
    _f(
        "exit_code",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Process exit code for foreground execution",
    ),
    _f(
        "duration_ms",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Foreground execution duration in milliseconds",
    ),
    _f(
        "rawOutputPath",
        required=False,
        description="Path to raw output file",
    ),
    _f(
        "interrupted",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the command was interrupted",
    ),
    _f(
        "timed_out",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the foreground command timed out",
    ),
    _f("base64_data", required=False, description="Base64-encoded image output"),
    _f("media_type", required=False, description="MIME type for image output"),
    _f(
        "dimensions",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Detected image dimensions",
        properties=(
            _f(
                "original_width",
                field_type=SCHEMA_NUMBER,
                required=False,
                description="Original image width in pixels",
            ),
            _f(
                "original_height",
                field_type=SCHEMA_NUMBER,
                required=False,
                description="Original image height in pixels",
            ),
            _f(
                "display_width",
                field_type=SCHEMA_NUMBER,
                required=False,
                description="Suggested display width in pixels",
            ),
            _f(
                "display_height",
                field_type=SCHEMA_NUMBER,
                required=False,
                description="Suggested display height in pixels",
            ),
        ),
    ),
    _f(
        "original_size",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Original image file size in bytes",
    ),
    _f(
        "semantic_success",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the command should be treated as semantically successful",
    ),
    _f(
        "semantic_status",
        required=False,
        description="Semantic status label for expected non-zero exits",
    ),
    _f(
        "semantic_message",
        required=False,
        description="Human-readable semantic interpretation of the result",
    ),
    _f(
        "claude_code_hints",
        field_type=SCHEMA_ARRAY,
        required=False,
        items_type=SCHEMA_STRING,
        description="Extracted Claude Code hint messages from command output",
    ),
    _f(
        "git_operation",
        required=False,
        description="Detected git operation name",
    ),
    _f(
        "git_branch",
        required=False,
        description="Detected git branch associated with the command output",
    ),
    _f(
        "git_commit_shas",
        field_type=SCHEMA_ARRAY,
        required=False,
        items_type=SCHEMA_STRING,
        description="Detected git commit SHAs mentioned by the command output",
    ),
    _f(
        "git_pr_urls",
        field_type=SCHEMA_ARRAY,
        required=False,
        items_type=SCHEMA_STRING,
        description="Detected pull request URLs mentioned by the command output",
    ),
    _f(
        "git_index_lock_error",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether a git index.lock error was detected",
    ),
    _f(
        "code_indexing_tool",
        required=False,
        description="Detected code indexing tool name when one was invoked",
    ),
    _f(
        "sleep_pattern_detected",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the command matches a foreground sleep pattern",
    ),
    _f(
        "backgrounding_suggestion",
        required=False,
        description="Suggested mitigation when a foreground sleep pattern is detected",
    ),
)

_GLOB_INPUT = _schema(
    _f("pattern", description="Glob pattern to match files"),
    _f("path", required=False, description="Base directory to search from"),
)

_GLOB_OUTPUT = _schema(
    _f(
        "durationMs",
        field_type=SCHEMA_NUMBER,
        description="Duration of the glob operation",
    ),
    _f("numFiles", field_type=SCHEMA_NUMBER, description="Number of files matched"),
    _f("filenames", field_type=SCHEMA_ARRAY, items_type=SCHEMA_STRING),
    _f("truncated", field_type=SCHEMA_BOOLEAN),
)

_GREP_INPUT = _schema(
    _f("pattern", description="The regex pattern to search for"),
    _f("path", required=False, description="File or directory path to search"),
    _f("glob", required=False, description="Glob pattern for file filtering"),
    _f("exclude", required=False, description="Glob pattern for file exclusion"),
    _f(
        "output_mode",
        required=False,
        description="Output mode: content, files_with_matches, count",
    ),
    _f(
        "-B",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Lines before match",
    ),
    _f(
        "-A",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Lines after match",
    ),
    _f(
        "-C",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Context lines around match",
    ),
    _f(
        "context",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Context lines (alias for -C)",
    ),
    _f(
        "-n",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Include line numbers",
    ),
    _f(
        "-i",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Case insensitive search",
    ),
    _f("type", required=False, description="File type filter"),
    _f(
        "head_limit",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Max results to return",
    ),
    _f(
        "offset",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Offset for paginated results",
    ),
    _f(
        "multiline",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Multiline mode for pattern matching",
    ),
)

_GREP_OUTPUT = _schema(
    _f("mode", required=False, description="Output mode used"),
    _f("numFiles", field_type=SCHEMA_NUMBER, required=False),
    _f("filenames", field_type=SCHEMA_ARRAY, items_type=SCHEMA_STRING, required=False),
    _f("content", required=False, description="Matched content"),
    _f("numLines", field_type=SCHEMA_NUMBER, required=False),
)

_READ_INPUT = _schema(
    _f("file_path", description="Absolute or relative file path"),
    _f(
        "offset",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Line offset to start reading from",
    ),
    _f(
        "limit",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Maximum number of lines to read",
    ),
    _f(
        "pages",
        required=False,
        description="Page range for PDF files such as 1-5 or 3",
    ),
    _f(
        "line_numbers",
        required=False,
        description="Show line numbers prefix",
    ),
    _f(
        "show_whitespace",
        required=False,
        description="Show whitespace characters",
    ),
    _f(
        "context",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Context lines around the target",
    ),
)

_READ_OUTPUT = _schema(
    _f("result_type", description="Read result variant"),
    _f("file_path", description="Path of the file that was read"),
    _f(
        "session_file_type",
        required=False,
        description="Session file classification for Claude-managed memory or transcript files",
    ),
    _f("content", required=False, description="File content"),
    _f(
        "total_lines",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Total number of lines in the file",
    ),
    _f(
        "offset",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Starting line number for text reads",
    ),
    _f(
        "limit",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Requested line limit for text reads",
    ),
    _f(
        "total_bytes",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Original file size in bytes",
    ),
    _f(
        "read_bytes",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Number of bytes returned to the caller",
    ),
    _f("base64_data", required=False, description="Base64-encoded file content"),
    _f("media_type", required=False, description="MIME type for binary reads"),
    _f(
        "dimensions",
        field_type=SCHEMA_OBJECT,
        required=False,
        properties=(
            _f("original_width", field_type=SCHEMA_NUMBER, required=False),
            _f("original_height", field_type=SCHEMA_NUMBER, required=False),
            _f("display_width", field_type=SCHEMA_NUMBER, required=False),
            _f("display_height", field_type=SCHEMA_NUMBER, required=False),
        ),
    ),
    _f(
        "cells",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_ANY,
        required=False,
        description="Notebook cells returned for notebook reads",
    ),
    _f(
        "cell_count",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Number of notebook cells returned",
    ),
    _f(
        "original_size",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Original binary file size in bytes",
    ),
    _f(
        "page_count",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Total number of pages in the PDF",
    ),
    _f("pages", required=False, description="Requested PDF page range"),
    _f(
        "output_dir",
        required=False,
        description="Directory containing extracted PDF page images",
    ),
    _f(
        "count",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Number of extracted PDF pages",
    ),
)

_EDIT_INPUT = _schema(
    _f("file_path", description="Absolute or relative file path"),
    _f("old_string", description="Text to find and replace"),
    _f("new_string", description="Replacement text"),
    _f(
        "replace_all",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Replace all occurrences",
    ),
)

_EDIT_OUTPUT = _schema(
    _f("result_type", description="Whether the edit created or updated the file"),
    _f("file_path", description="Path of the edited file"),
    _f("old_string", required=False, description="Original string that was replaced"),
    _f("new_string", required=False, description="Replacement string that was written"),
    _f("content", required=False, description="Updated file content"),
    _f(
        "original_file",
        required=False,
        description="Previous file contents before the edit was applied",
    ),
    _f(
        "replace_all",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether all matches were replaced",
    ),
    _f(
        "structured_patch",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_OBJECT,
        required=False,
        description="Structured diff hunks for the edit",
    ),
    _f(
        "gitDiff",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Single-file git diff summary when available",
        properties=(
            _f("filename"),
            _f(
                "status",
                field_type=SCHEMA_ENUM,
                enum_values=("modified", "added"),
            ),
            _f("additions", field_type=SCHEMA_NUMBER),
            _f("deletions", field_type=SCHEMA_NUMBER),
            _f("changes", field_type=SCHEMA_NUMBER),
            _f("patch"),
            _f("repository", required=False),
        ),
    ),
)

_WRITE_INPUT = _schema(
    _f("file_path", description="Absolute or relative file path"),
    _f("content", description="File content to write"),
)

_WRITE_OUTPUT = _schema(
    _f("result_type", description="Whether the write created or updated the file"),
    _f("file_path", description="Path of the written file"),
    _f("content", required=False, description="Written file content"),
    _f(
        "original_file",
        required=False,
        description="Previous file contents when updating an existing file",
    ),
    _f(
        "structured_patch",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_OBJECT,
        required=False,
        description="Structured diff hunks for the write",
    ),
    _f(
        "gitDiff",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Single-file git diff summary when available",
        properties=(
            _f("filename"),
            _f(
                "status",
                field_type=SCHEMA_ENUM,
                enum_values=("modified", "added"),
            ),
            _f("additions", field_type=SCHEMA_NUMBER),
            _f("deletions", field_type=SCHEMA_NUMBER),
            _f("changes", field_type=SCHEMA_NUMBER),
            _f("patch"),
            _f("repository", required=False),
        ),
    ),
)

_NOTEBOOK_EDIT_INPUT = _schema(
    _f(
        "notebook_path",
        description="Absolute or relative path to the Jupyter notebook",
    ),
    _f("cell_id", required=False, description="Cell ID to edit"),
    _f(
        "new_source",
        description="New cell source content",
    ),
    _f(
        "cell_type",
        required=False,
        description="Cell type (code, markdown, raw)",
    ),
    _f(
        "edit_mode",
        required=False,
        description="Edit mode (replace, insert, delete)",
    ),
)

_NOTEBOOK_EDIT_OUTPUT = _schema(
    _f(
        "success",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the edit succeeded",
    ),
    _f(
        "error",
        required=False,
        description="Error message if failed",
    ),
)

_WEB_FETCH_INPUT = _schema(
    _f("url", description="URL to fetch"),
    _f("prompt", description="What to extract from the page"),
)

_WEB_FETCH_OUTPUT = _schema(
    _f(
        "bytes",
        field_type=SCHEMA_NUMBER,
        description="Size of the fetched content in bytes",
    ),
    _f(
        "code",
        field_type=SCHEMA_NUMBER,
        description="HTTP response status code",
    ),
    _f(
        "codeText",
        required=False,
        description="HTTP response status text",
    ),
    _f(
        "contentType",
        required=False,
        description="Fetched response content type",
    ),
    _f(
        "durationMs",
        field_type=SCHEMA_NUMBER,
        description="Duration of the fetch",
    ),
    _f("url", description="The URL that was fetched"),
    _f("result", description="The extracted content"),
    _f(
        "persistedPath",
        required=False,
        description="Persisted path for downloaded binary content",
    ),
    _f(
        "persistedSize",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Persisted binary content size in bytes",
    ),
)

_WEB_SEARCH_INPUT = _schema(
    _f("query", description="Search query"),
    _f(
        "allowed_domains",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
        description="Domains to include in results",
    ),
    _f(
        "blocked_domains",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
        description="Domains to exclude from results",
    ),
    _f(
        "offset",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Zero-based result offset",
    ),
    _f(
        "max_results",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Maximum number of results to return",
    ),
)

_WEB_SEARCH_OUTPUT = _schema(
    _f("query", description="The search query"),
    _f("results", field_type=SCHEMA_ARRAY, items_type=SCHEMA_OBJECT),
    _f(
        "durationSeconds",
        field_type=SCHEMA_NUMBER,
        required=False,
    ),
    _f(
        "offset",
        field_type=SCHEMA_NUMBER,
        required=False,
    ),
    _f(
        "maxResults",
        field_type=SCHEMA_NUMBER,
        required=False,
    ),
    _f(
        "totalResults",
        field_type=SCHEMA_NUMBER,
        required=False,
    ),
)

_TODO_WRITE_INPUT = _schema(
    _f(
        "todos",
        field_type=SCHEMA_ARRAY,
        description="Array of todo items to write",
        items_type=SCHEMA_OBJECT,
    ),
)

_TODO_WRITE_OUTPUT = _schema(
    _f("oldTodos", field_type=SCHEMA_ARRAY, items_type=SCHEMA_OBJECT),
    _f("newTodos", field_type=SCHEMA_ARRAY, items_type=SCHEMA_OBJECT),
)

_TASK_OUTPUT_INPUT = _schema(
    _f("task_id", description="The ID of the background task"),
    _f(
        "block",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether to wait for task completion; defaults to true",
        default=True,
    ),
    _f(
        "timeout",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Max wait time in ms; defaults to 30000 and caps at 600000",
        default=30_000,
    ),
)

_TASK_OUTPUT_OUTPUT = _schema(
    _f("retrieval_status", description="Status of the retrieval"),
    _f(
        "task",
        field_type=SCHEMA_OBJECT,
        description="The task data",
        properties=(
            _f("task_id", description="The background task identifier"),
            _f("task_type", description="The background task type"),
            _f("status", description="The current task lifecycle state"),
            _f(
                "description",
                required=False,
                description="Human-readable task description",
            ),
            _f(
                "output",
                required=False,
                description="Collected task output",
            ),
            _f(
                "exit_code",
                field_type=SCHEMA_ANY,
                required=False,
                description="Process exit code when available",
            ),
            _f(
                "error",
                field_type=SCHEMA_ANY,
                required=False,
                description="Terminal error message when present",
            ),
            _f(
                "is_backgrounded",
                field_type=SCHEMA_BOOLEAN,
                required=False,
                description="Whether the task is running in the background",
            ),
            _f(
                "backgroundTaskId",
                required=False,
                description="Background task identifier for bash tasks",
            ),
            _f(
                "backgroundedByUser",
                field_type=SCHEMA_BOOLEAN,
                required=False,
                description="Whether the bash task was backgrounded explicitly",
            ),
            _f(
                "assistantAutoBackgrounded",
                field_type=SCHEMA_BOOLEAN,
                required=False,
                description="Whether the assistant auto-backgrounded the task",
            ),
            _f(
                "output_file",
                required=False,
                description="Persisted task output log path",
            ),
            _f(
                "persistedOutputPath",
                required=False,
                description="Persisted task output log path",
            ),
            _f(
                "persistedOutputSize",
                field_type=SCHEMA_NUMBER,
                required=False,
                description="Persisted task output size in bytes",
            ),
        ),
    ),
)

_TASK_STOP_INPUT = _schema(
    _f(
        "task_id",
        required=False,
        description="The ID of the background task to stop",
    ),
    _f(
        "shell_id",
        required=False,
        description="Deprecated: use task_id instead",
    ),
)

_TASK_STOP_OUTPUT = _schema(
    _f("message", description="Status message about the operation"),
    _f("task_id", description="The ID of the task that was stopped"),
    _f("task_type", description="The type of the task that was stopped"),
    _f("command", required=False, description="The command of the stopped task"),
)

_ASK_USER_QUESTION_INPUT = _schema(
    _f("question", required=False, description="Question to ask the user"),
    _f(
        "questions",
        field_type=SCHEMA_ARRAY,
        required=False,
        description="Question list for multi-question prompts",
        items_type=SCHEMA_OBJECT,
    ),
    _f(
        "options",
        field_type=SCHEMA_ARRAY,
        required=False,
        description="Available options for the user",
        items_type=SCHEMA_OBJECT,
    ),
    _f(
        "annotations",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Annotations metadata for the question",
    ),
)

_ASK_USER_QUESTION_OUTPUT = _schema(
    _f("selected", required=False, description="The selected option"),
    _f("question", required=False, description="The question that was asked"),
    _f(
        "questions",
        field_type=SCHEMA_ARRAY,
        required=False,
        description="Normalized questions and answers",
        items_type=SCHEMA_OBJECT,
    ),
    _f(
        "answers",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Map of question IDs to answers",
    ),
)

_SKILL_INPUT = _schema(
    _f("skill", description="Skill name to invoke"),
    _f("args", required=False, description="Arguments for the skill"),
)

_SKILL_OUTPUT = _schema(
    _f("result", description="Result of the skill execution"),
    _f("error", required=False, description="Error message if failed"),
)

_ENTER_PLAN_MODE_INPUT = _schema()

_ENTER_PLAN_MODE_OUTPUT = _schema(
    _f("message", description="Confirmation message"),
)

_EXIT_PLAN_MODE_INPUT = _schema(
    _f(
        "allowedPrompts",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
        description="Prompts allowed in plan mode",
    ),
)

_EXIT_PLAN_MODE_OUTPUT = _schema(
    _f("plan", description="The plan content"),
    _f(
        "warnings",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
    ),
)

_SEND_MESSAGE_INPUT = _schema(
    _f("to", description="Recipient name"),
    _f("summary", required=False, description="Message summary"),
    _f("message", description="Message content"),
)

_BRIEF_INPUT = _schema(
    _f("message", description="The message for the user"),
    _f(
        "attachments",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
        description="Optional file paths to attach",
    ),
    _f(
        "status",
        description="normal or proactive",
        enum_values=("normal", "proactive"),
    ),
)

_CONFIG_INPUT = _schema(
    _f(
        "action",
        required=False,
        description="Action to perform: list, delete, describe, or get/set (default)",
        enum_values=("list", "delete", "describe"),
    ),
    _f("setting", required=False, description="Setting name to read, modify, or delete"),
    _f("value", required=False, description="New value for the setting (for set action)"),
)

_LIST_MCP_RESOURCES_INPUT = _schema(
    _f(
        "server",
        required=False,
        description="MCP server name to list resources from",
    ),
)

_READ_MCP_RESOURCE_INPUT = _schema(
    _f("server", description="MCP server name"),
    _f("uri", description="Resource URI"),
)

_TOOL_SEARCH_INPUT = _schema(
    _f("query", description="Search query for finding tools"),
    _f(
        "max_results",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Maximum number of results",
    ),
)

_TOOL_SEARCH_OUTPUT = _schema(
    _f("matches", field_type=SCHEMA_ARRAY, items_type=SCHEMA_STRING),
    _f("query", description="The search query"),
    _f(
        "total_deferred_tools",
        field_type=SCHEMA_NUMBER,
    ),
    _f(
        "pending_mcp_servers",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
    ),
)

_TASK_CREATE_INPUT = _schema(
    _f("subject", description="Task subject/title"),
    _f("description", required=False, description="Task description"),
    _f("activeForm", required=False, description="Active form description"),
    _f(
        "metadata",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Additional task metadata",
    ),
)

_TASK_CREATE_OUTPUT = _schema(
    _f("task", field_type=SCHEMA_OBJECT, description="Created task (id, subject)"),
)

_TASK_GET_INPUT = _schema(
    _f("taskId", description="Task ID to retrieve"),
)

_TASK_GET_OUTPUT = _schema(
    _f("task", field_type=SCHEMA_OBJECT, required=False, description="Task data"),
)

_TASK_UPDATE_INPUT = _schema(
    _f("taskId", description="Task ID to update"),
    _f("subject", required=False, description="Updated subject"),
    _f("description", required=False, description="Updated description"),
    _f("activeForm", required=False, description="Updated active form"),
    _f("status", required=False, description="Updated status"),
    _f(
        "addBlocks",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
    ),
    _f(
        "addBlockedBy",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_STRING,
        required=False,
    ),
    _f("owner", required=False, description="Task owner"),
    _f(
        "metadata",
        field_type=SCHEMA_OBJECT,
        required=False,
        description="Additional task metadata",
    ),
)

_TASK_UPDATE_OUTPUT = _schema(
    _f(
        "success",
        field_type=SCHEMA_BOOLEAN,
        required=False,
    ),
    _f("taskId", required=False),
    _f("error", required=False),
)

_TASK_LIST_INPUT = _schema()

_TASK_LIST_OUTPUT = _schema(
    _f("tasks", field_type=SCHEMA_ARRAY, items_type=SCHEMA_OBJECT),
)

_ENTER_WORKTREE_INPUT = _schema(
    _f("name", required=False, description="Name for the worktree"),
)

_ENTER_WORKTREE_OUTPUT = _schema(
    _f("worktreePath", description="Path to the worktree"),
    _f("message", required=False, description="Status message"),
)

_EXIT_WORKTREE_INPUT = _schema(
    _f("action", description="Action to take"),
    _f(
        "discard_changes",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether to discard uncommitted changes",
    ),
)

_EXIT_WORKTREE_OUTPUT = _schema(
    _f("action", description="Action taken"),
    _f("originalCwd", description="Original working directory"),
    _f("message", required=False, description="Status message"),
)

_TEAM_CREATE_INPUT = _schema(
    _f("team_name", required=False, description="Name for the team"),
    _f("model", required=False, description="Model for team agents"),
    _f(
        "agents",
        field_type=SCHEMA_ARRAY,
        items_type=SCHEMA_OBJECT,
        required=False,
        description="Agent definitions for the team",
    ),
)

_TEAM_DELETE_INPUT = _schema(
    _f("team_name", description="Name of the team to delete"),
)

_CRON_CREATE_INPUT = _schema(
    _f("cron", description="Cron schedule expression"),
    _f("prompt", description="Prompt to execute on schedule"),
    _f(
        "recurring",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether the recurring",
    ),
    _f(
        "durable",
        field_type=SCHEMA_BOOLEAN,
        required=False,
        description="Whether durable across restarts",
    ),
)

_CRON_DELETE_INPUT = _schema(
    _f("id", description="Cron job ID to delete"),
)

_CRON_LIST_INPUT = _schema()

_SLEEP_INPUT = _schema(
    _f(
        "duration",
        field_type=SCHEMA_NUMBER,
        required=False,
        description="Duration to sleep in seconds",
    ),
)

_CTX_INSPECT_INPUT = _schema(
    _f(
        "focus",
        required=False,
        description="Focus area to inspect (e.g., 'memory', 'tokens', 'messages')",
    ),
)

_LSP_INPUT = _schema(
    _f("operation", description="LSP operation to perform"),
    _f("filePath", description="File path"),
    _f("line", field_type=SCHEMA_NUMBER, description="Line number (1-based)"),
    _f("character", field_type=SCHEMA_NUMBER, description="Character offset (0-based)"),
)

# ---------------------------------------------------------------------------
# Tool definitions registry — the master list of all tools
# Each entry maps one TS tool class to its definition metadata.
# ---------------------------------------------------------------------------

_REGISTRY: List[ToolDefinition] = [
    # --- linux_required tools ---
    ToolDefinition(
        name="Agent",
        description="Spawn a subagent to perform a task",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("Task",),
        input_schema=_AGENT_INPUT,
        output_schema=_AGENT_OUTPUT,
        should_defer=True,
        source_module="AgentTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskOutput",
        description="Retrieve output from a background task",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("AgentOutputTool", "BashOutputTool"),
        input_schema=_TASK_OUTPUT_INPUT,
        output_schema=_TASK_OUTPUT_OUTPUT,
        should_defer=True,
        source_module="TaskOutputTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Bash",
        description="Execute a bash command",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_BASH_INPUT,
        output_schema=_BASH_OUTPUT,
        max_result_size_chars=30_000,
        source_module="BashTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Glob",
        description="Match files using a glob pattern",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_GLOB_INPUT,
        output_schema=_GLOB_OUTPUT,
        source_module="GlobTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Grep",
        description="Search file contents using regex",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_GREP_INPUT,
        output_schema=_GREP_OUTPUT,
        max_result_size_chars=20_000,
        source_module="GrepTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ExitPlanMode",
        description="Exit plan mode and resume normal tool use",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_EXIT_PLAN_MODE_INPUT,
        output_schema=_EXIT_PLAN_MODE_OUTPUT,
        source_module="ExitPlanModeV2Tool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Read",
        description="Read file contents",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_READ_INPUT,
        output_schema=_READ_OUTPUT,
        max_result_size_chars=0,
        source_module="FileReadTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Edit",
        description="Search and replace in a file",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_EDIT_INPUT,
        output_schema=_EDIT_OUTPUT,
        strict=True,
        source_module="FileEditTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Write",
        description="Write content to a file",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_WRITE_INPUT,
        output_schema=_WRITE_OUTPUT,
        strict=True,
        source_module="FileWriteTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="NotebookEdit",
        description="Edit Jupyter notebook cells",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_NOTEBOOK_EDIT_INPUT,
        output_schema=_NOTEBOOK_EDIT_OUTPUT,
        source_module="NotebookEditTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="WebFetch",
        description=(
            "Fetch a specific URL over plain HTTP and extract its content (NO JavaScript, no "
            "cookies/login, and this device's direct egress cannot reach many sites). UNLESS you "
            "were given an explicit, direct URL, PREFER the browser tools (mcp__browser__*): they "
            "drive a real Playwright/WebView browser that runs JS, handles anti-bot and dynamic "
            "pages, and reaches sites plain HTTP cannot. Use WebFetch mainly to pull a known direct "
            "URL when a simple static fetch is enough. If the browser tools are unavailable, use "
            "WebSearch (Bing) to find URLs, then WebFetch them."
        ),
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_WEB_FETCH_INPUT,
        output_schema=_WEB_FETCH_OUTPUT,
        should_defer=True,
        source_module="WebFetchTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TodoWrite",
        description="Write or update the todo list",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_TODO_WRITE_INPUT,
        output_schema=_TODO_WRITE_OUTPUT,
        strict=True,
        source_module="TodoWriteTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="WebSearch",
        description=(
            "Search the web (Bing) and return result URLs + snippets. After searching, PREFER "
            "opening the result pages with the browser tools (mcp__browser__*, a real "
            "Playwright/WebView browser) rather than WebFetch — the browser runs JS and reaches "
            "sites this device's plain HTTP egress cannot."
        ),
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_WEB_SEARCH_INPUT,
        output_schema=_WEB_SEARCH_OUTPUT,
        should_defer=True,
        source_module="WebSearchTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskStop",
        description="Stop a running background task",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("KillShell",),
        input_schema=_TASK_STOP_INPUT,
        output_schema=_TASK_STOP_OUTPUT,
        should_defer=True,
        source_module="TaskStopTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="AskUserQuestion",
        description="Ask the user a question with options",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_ASK_USER_QUESTION_INPUT,
        output_schema=_ASK_USER_QUESTION_OUTPUT,
        source_module="AskUserQuestionTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Skill",
        description="Invoke a skill",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_SKILL_INPUT,
        output_schema=_SKILL_OUTPUT,
        source_module="SkillTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="EnterPlanMode",
        description="Enter plan mode for planning without tool execution",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_ENTER_PLAN_MODE_INPUT,
        output_schema=_ENTER_PLAN_MODE_OUTPUT,
        source_module="EnterPlanModeTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ListMcpResourcesTool",
        description="List available MCP server resources",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_LIST_MCP_RESOURCES_INPUT,
        source_module="ListMcpResourcesTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ReadMcpResourceTool",
        description="Read a specific MCP resource",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_READ_MCP_RESOURCE_INPUT,
        source_module="ReadMcpResourceTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="SendMessage",
        description="Send a message to a teammate",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        input_schema=_SEND_MESSAGE_INPUT,
        source_module="SendMessageTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="SendUserMessage",
        description="Send a brief message to the user",
        gate_type=GATE_ALWAYS,
        gate_expression="always exposed by getAllBaseTools()",
        linux_exposure=EXPOSURE_LINUX_REQUIRED,
        aliases=("Brief",),
        input_schema=_BRIEF_INPUT,
        source_module="BriefTool",
        subsystem="tools",
    ),
    # --- ant_only tools ---
    ToolDefinition(
        name="Config",
        description="Read or modify configuration settings",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        input_schema=_CONFIG_INPUT,
        source_module="ConfigTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Tungsten",
        description="Tungsten tool for internal use",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        source_module="TungstenTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="REPL",
        description="REPL mode tool for interactive shell",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        source_module="REPLTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="SuggestBackgroundPR",
        description="Suggest a background PR",
        gate_type=GATE_BUILD_USER_TYPE,
        gate_expression="USER_TYPE == 'ant'",
        linux_exposure=EXPOSURE_ANT_ONLY,
        source_module="SuggestBackgroundPRTool",
        subsystem="tools",
    ),
    # --- linux_deferred tools ---
    ToolDefinition(
        name="CronCreate",
        description="Create a cron schedule",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="AGENT_TRIGGERS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_CRON_CREATE_INPUT,
        source_module="CronCreateTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="CronDelete",
        description="Delete a cron schedule",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="AGENT_TRIGGERS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_CRON_DELETE_INPUT,
        source_module="CronDeleteTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="CronList",
        description="List cron schedules",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="AGENT_TRIGGERS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_CRON_LIST_INPUT,
        source_module="CronListTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="CtxInspect",
        description="Inspect context window usage",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="CONTEXT_COLLAPSE",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_CTX_INSPECT_INPUT,
        source_module="CtxInspectTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="EnterWorktree",
        description="Enter a worktree for isolated work",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isWorktreeModeEnabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_ENTER_WORKTREE_INPUT,
        output_schema=_ENTER_WORKTREE_OUTPUT,
        source_module="EnterWorktreeTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ExitWorktree",
        description="Exit the current worktree",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isWorktreeModeEnabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_EXIT_WORKTREE_INPUT,
        output_schema=_EXIT_WORKTREE_OUTPUT,
        source_module="ExitWorktreeTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="LSP",
        description="LSP operations (goto definition, references, etc.)",
        gate_type=GATE_ENV_VAR,
        gate_expression="ENABLE_LSP_TOOL",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_LSP_INPUT,
        source_module="LSPTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ListPeers",
        description="List connected peers",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="UDS_INBOX",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="ListPeersTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Monitor",
        description="Monitor tool for background monitoring",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="MONITOR_TOOL",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="MonitorTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="OverflowTest",
        description="Test tool for overflow scenarios",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="OVERFLOW_TEST_TOOL",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="OverflowTestTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="PushNotification",
        description="Send push notifications",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS || KAIROS_PUSH_NOTIFICATION",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="PushNotificationTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="RemoteTrigger",
        description="Remote trigger for external events",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="AGENT_TRIGGERS_REMOTE",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="RemoteTriggerTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="SendUserFile",
        description="Send a file to the user",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="SendUserFileTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Sleep",
        description="Sleep for a specified duration",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="PROACTIVE || KAIROS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_SLEEP_INPUT,
        source_module="SleepTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Snip",
        description="Snip message history",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="HISTORY_SNIP",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="SnipTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="SubscribePR",
        description="Subscribe to PR notifications",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="KAIROS_GITHUB_WEBHOOKS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="SubscribePRTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskCreate",
        description="Create a new task",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isTodoV2Enabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TASK_CREATE_INPUT,
        output_schema=_TASK_CREATE_OUTPUT,
        source_module="TaskCreateTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskGet",
        description="Get a task by ID",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isTodoV2Enabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TASK_GET_INPUT,
        output_schema=_TASK_GET_OUTPUT,
        source_module="TaskGetTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskList",
        description="List all tasks",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isTodoV2Enabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TASK_LIST_INPUT,
        output_schema=_TASK_LIST_OUTPUT,
        source_module="TaskListTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TaskUpdate",
        description="Update an existing task",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isTodoV2Enabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TASK_UPDATE_INPUT,
        output_schema=_TASK_UPDATE_OUTPUT,
        source_module="TaskUpdateTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TeamCreate",
        description="Create a team of agents",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isAgentSwarmsEnabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TEAM_CREATE_INPUT,
        source_module="TeamCreateTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TeamDelete",
        description="Delete a team of agents",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isAgentSwarmsEnabled()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TEAM_DELETE_INPUT,
        source_module="TeamDeleteTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="TerminalCapture",
        description="Capture terminal content",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="TERMINAL_PANEL",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="TerminalCaptureTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="ToolSearch",
        description="Search for available tools by keyword",
        gate_type=GATE_RUNTIME_CHECK,
        gate_expression="isToolSearchEnabledOptimistic()",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        input_schema=_TOOL_SEARCH_INPUT,
        output_schema=_TOOL_SEARCH_OUTPUT,
        should_defer=True,
        source_module="ToolSearchTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="VerifyPlanExecution",
        description="Verify plan execution against specification",
        gate_type=GATE_ENV_VAR,
        gate_expression="CLAUDE_CODE_VERIFY_PLAN == 'true'",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="VerifyPlanExecutionTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="WebBrowser",
        description="Interact with web pages",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="WEB_BROWSER_TOOL",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="WebBrowserTool",
        subsystem="tools",
    ),
    ToolDefinition(
        name="Workflow",
        description="Execute a workflow script",
        gate_type=GATE_FEATURE_FLAG,
        gate_expression="WORKFLOW_SCRIPTS",
        linux_exposure=EXPOSURE_LINUX_DEFERRED,
        source_module="WorkflowTool",
        subsystem="tools",
    ),
    # --- platform_specific tools ---
    ToolDefinition(
        name="PowerShell",
        description="Execute PowerShell commands",
        gate_type=GATE_PLATFORM,
        gate_expression="windows && isPowerShellToolEnabled()",
        linux_exposure=EXPOSURE_PLATFORM_SPECIFIC,
        max_result_size_chars=30_000,
        source_module="PowerShellTool",
        subsystem="tools",
    ),
    # --- test_only tools ---
    ToolDefinition(
        name="TestingPermission",
        description="Test tool for permission scenarios",
        gate_type=GATE_TEST_ENV,
        gate_expression="NODE_ENV == 'test'",
        linux_exposure=EXPOSURE_TEST_ONLY,
        source_module="TestingPermissionTool",
        subsystem="tools",
    ),
]


# ---------------------------------------------------------------------------
# Derived lookups — built from _REGISTRY
# ---------------------------------------------------------------------------


def _build_registry() -> Dict[str, ToolDefinition]:
    """Build primary_name -> ToolDefinition mapping from the registry list."""
    mapping: Dict[str, ToolDefinition] = {}
    for tool in _REGISTRY:
        mapping[tool.name] = tool
    return mapping


def _build_source_module_index() -> Dict[str, ToolDefinition]:
    """Build source_module_name -> ToolDefinition mapping from the registry list."""
    mapping: Dict[str, ToolDefinition] = {}
    for tool in _REGISTRY:
        mapping[tool.source_module] = tool
    return mapping


BUILTIN_TOOL_REGISTRY: Dict[str, ToolDefinition] = _build_registry()

SOURCE_MODULE_INDEX: Dict[str, ToolDefinition] = _build_source_module_index()

LINUX_REQUIRED_TOOLS: Dict[str, ToolDefinition] = {
    name: tool
    for name, tool in BUILTIN_TOOL_REGISTRY.items()
    if tool.linux_exposure == EXPOSURE_LINUX_REQUIRED
}

LINUX_DEFERRED_TOOLS: Dict[str, ToolDefinition] = {
    name: tool
    for name, tool in BUILTIN_TOOL_REGISTRY.items()
    if tool.linux_exposure == EXPOSURE_LINUX_DEFERRED
}


def _build_alias_registry() -> Dict[str, str]:
    """Build alias -> primary name mapping from all tool definitions."""
    aliases: Dict[str, str] = {}
    for tool in _REGISTRY:
        for alias in tool.aliases:
            aliases[alias] = tool.name
    return aliases


_ALIAS_TO_PRIMARY: Dict[str, str] = _build_alias_registry()


# ---------------------------------------------------------------------------
# Public query API — mirrors src/Tool.ts toolMatchesName/findToolByName
# ---------------------------------------------------------------------------


def get_all_tool_names() -> Set[str]:
    """Return the set of all primary tool names."""
    return set(BUILTIN_TOOL_REGISTRY)


def get_all_source_module_names() -> Set[str]:
    """Return the set of all source module names (e.g. 'AgentTool', 'BashTool')."""
    return set(SOURCE_MODULE_INDEX)


def get_linux_required_names() -> Set[str]:
    """Return the set of linux_required primary tool names."""
    return set(LINUX_REQUIRED_TOOLS)


def get_linux_deferred_names() -> Set[str]:
    """Return the set of linux_deferred primary tool names."""
    return set(LINUX_DEFERRED_TOOLS)


def get_linux_required_tools() -> List[ToolDefinition]:
    """Return linux_required tool definitions as a sorted list."""
    return sorted(LINUX_REQUIRED_TOOLS.values(), key=lambda t: t.name)


def get_linux_deferred_tools() -> List[ToolDefinition]:
    """Return linux_deferred tool definitions as a sorted list."""
    return sorted(LINUX_DEFERRED_TOOLS.values(), key=lambda t: t.name)


def tool_matches_name(tool: ToolDefinition, name: str) -> bool:
    """Check if a tool matches the given name (primary name or alias).

    Mirrors src/Tool.ts toolMatchesName().
    """
    if tool.name == name:
        return True
    return name in tool.aliases


def find_tool_by_name(name: str) -> Optional[ToolDefinition]:
    """Find a tool by name (primary or alias).

    Mirrors src/Tool.ts findToolByName().
    """
    tool = BUILTIN_TOOL_REGISTRY.get(name)
    if tool is not None:
        return tool
    primary = _ALIAS_TO_PRIMARY.get(name)
    if primary is not None:
        return BUILTIN_TOOL_REGISTRY.get(primary)
    return None


def find_tool_by_source_module(source_module: str) -> Optional[ToolDefinition]:
    """Find a tool by its source module name (e.g. 'AgentTool', 'BashTool')."""
    return SOURCE_MODULE_INDEX.get(source_module)


def has_tool(name: str) -> bool:
    """Check if a tool exists by name (primary or alias)."""
    return find_tool_by_name(name) is not None


def classify_tool(name: str) -> Tuple[str, str, str]:
    """Return (gate_type, gate_expression, linux_exposure) for a tool.

    Accepts either primary name or alias.  Returns defaults for unknown tools.
    """
    tool = find_tool_by_name(name)
    if tool is None:
        return (
            GATE_ALWAYS,
            "always exposed by getAllBaseTools registry",
            EXPOSURE_LINUX_REQUIRED,
        )
    return (tool.gate_type, tool.gate_expression, tool.linux_exposure)


def get_registered_aliases() -> Mapping[str, Tuple[str, ...]]:
    """Return primary_name -> aliases mapping for tools that have aliases."""
    return {tool.name: tool.aliases for tool in _REGISTRY if tool.aliases}


def is_internal_only(name: str) -> bool:
    """Check if a tool (by source_module name or primary name) is internal-only."""
    tool = find_tool_by_name(name)
    if tool is not None:
        return (
            tool.source_module in INTERNAL_ONLY_TOOLS
            or tool.name in INTERNAL_ONLY_TOOLS
        )
    return name in INTERNAL_ONLY_TOOLS


def get_tools_by_gate(gate_type: str) -> List[ToolDefinition]:
    """Return all tools matching a specific gate type."""
    return [t for t in _REGISTRY if t.gate_type == gate_type]


def get_tools_by_exposure(exposure: str) -> List[ToolDefinition]:
    """Return all tools matching a specific linux exposure classification."""
    return [t for t in _REGISTRY if t.linux_exposure == exposure]
