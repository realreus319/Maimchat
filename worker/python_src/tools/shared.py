"""Shared types and constants for file tools.

Extracted from TS source:
  src/tools/FileReadTool/FileReadTool.ts (constants)
  src/tools/FileEditTool/constants.ts
  src/tools/FileEditTool/types.ts (hunk/patch schemas)
  src/tools/FileWriteTool/FileWriteTool.ts (schemas)

These types are used across all three file tool modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# Device files that would hang the process: infinite output or blocking input.
# Checked by path only (no I/O). Safe devices like /dev/null are intentionally
# omitted.
BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    }
)

BINARY_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".obj",
        ".o",
        ".pyc",
        ".class",
        ".jar",
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".7z",
        ".woff",
        ".woff2",
        ".ico",
    }
)

IMAGE_EXTENSIONS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
    }
)

FILE_NOT_FOUND_CWD_NOTE = "Current working directory: "

FILE_UNCHANGED_STUB = (
    "<system-reminder>File unchanged since last read</system-reminder>"
)

FILE_UNEXPECTEDLY_MODIFIED_ERROR = (
    "File has been unexpectedly modified. "
    "Read it again before attempting to write to it."
)

MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB


# Curly-quote constants used by quote normalization in FileEditTool
LEFT_SINGLE_CURLY_QUOTE = "\u2018"
RIGHT_SINGLE_CURLY_QUOTE = "\u2019"
LEFT_DOUBLE_CURLY_QUOTE = "\u201c"
RIGHT_DOUBLE_CURLY_QUOTE = "\u201d"


@dataclass(frozen=True)
class ReadFileState:
    """Tracks file read state for stale-write prevention."""

    file_path: str
    timestamp: int
    content: str = ""
    offset: Optional[int] = None
    limit: Optional[int] = None
    is_partial_view: bool = False


@dataclass(frozen=True)
class ReadResult:
    """Result of a file read operation."""

    result_type: str  # "text", "image", "notebook", "pdf", or "parts"
    file_path: str
    session_file_type: Optional[str] = None
    content: str = ""
    lines: List[str] = field(default_factory=list)
    total_lines: int = 0
    offset: int = 1
    limit: Optional[int] = None
    total_bytes: int = 0
    read_bytes: int = 0
    mtime_ms: float = 0.0
    base64_data: Optional[str] = None
    media_type: Optional[str] = None
    dimensions: Optional[Dict[str, int]] = None
    cells: Optional[List[Dict[str, Any]]] = None
    original_size: int = 0
    page_count: Optional[int] = None
    pages: Optional[str] = None
    output_dir: Optional[str] = None
    count: Optional[int] = None


@dataclass(frozen=True)
class WriteResult:
    """Result of a file write operation."""

    result_type: str  # "create" or "update"
    file_path: str
    content: str
    original_file: Optional[str] = None
    structured_patch: Tuple[Hunk, ...] = ()  # forward reference resolved at runtime
    git_diff: Optional["GitDiff"] = None


@dataclass(frozen=True)
class EditResult:
    """Result of a file edit operation."""

    result_type: str  # "edit"
    file_path: str
    content: str
    old_string: str
    new_string: str
    original_file: Optional[str] = None
    replace_all: bool = False
    structured_patch: Tuple[Hunk, ...] = ()
    git_diff: Optional["GitDiff"] = None


@dataclass(frozen=True)
class Hunk:
    """A single hunk in a structured patch."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: Tuple[str, ...]


@dataclass(frozen=True)
class GitDiff:
    """Single-file git diff payload exposed to tool consumers."""

    filename: str
    status: str  # "modified" or "added"
    additions: int
    deletions: int
    changes: int
    patch: str
    repository: Optional[str] = None


@dataclass(frozen=True)
class PatchResult:
    """Patch computation result."""

    hunks: Tuple[Hunk, ...]
    updated_file: str


@dataclass
class ValidationResult:
    """Input validation result."""

    result: bool
    message: str = ""
    error_code: int = 0
    behavior: str = "deny"
    metadata: Optional[Dict[str, Any]] = None
