from __future__ import annotations

import os
import subprocess
import threading
from typing import Dict, List, Optional, Tuple

from ..types.permissions import (
    PermissionBehavior,
    PermissionDecision,
    ToolPermissionContext,
)
from ..utils.permissions.filesystem import (
    FILE_EDIT_TOOL_NAME,
    check_write_permission_for_tool,
    matching_rule_for_input,
)
from ..utils.permissions.path_validation import expand_tilde
from ..utils.text_encoding import decode_text_bytes
from ..services.team_memory_sync.team_mem_secret_guard import check_team_mem_secrets
from .config_file_validation import validate_settings_like_file_content
from .lsp_tool import notify_persistent_lsp_document_saved
from .shared import (
    FILE_UNEXPECTEDLY_MODIFIED_ERROR,
    GitDiff,
    Hunk,
    ReadFileState,
    ValidationResult,
    WriteResult,
)


_GIT_TIMEOUT_SECONDS = 5.0


def _is_env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_git_diff_disabled() -> bool:
    val = os.environ.get("CLAUDE_CODE_GIT_DIFF")
    if val is None:
        return False
    return not _is_env_truthy(val)


def expand_path(file_path: str) -> str:
    expanded = expand_tilde(file_path).strip()
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return expanded


def is_unc_path(file_path: str) -> bool:
    raw_path = expand_tilde(file_path).strip()
    return raw_path.startswith("\\\\") or raw_path.startswith("//")


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _read_existing_text(path: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(path, "rb") as handle:
            raw_bytes = handle.read()
    except FileNotFoundError:
        return None, None

    content, encoding = decode_text_bytes(raw_bytes)
    content = content.replace("\r\n", "\n")
    return content, encoding


def _compute_simple_patch(old_content: str, new_content: str) -> Tuple[Hunk, ...]:
    old_lines = tuple(old_content.splitlines())
    new_lines = tuple(new_content.splitlines())
    lines: List[str] = []

    for line in old_lines:
        lines.append("-" + line)
    for line in new_lines:
        lines.append("+" + line)

    if not lines:
        return ()

    return (
        Hunk(
            old_start=1,
            old_lines=len(old_lines),
            new_start=1,
            new_lines=len(new_lines),
            lines=tuple(lines),
        ),
    )


def _run_git_command(
    args: Tuple[str, ...],
    *,
    cwd: str,
) -> Optional[subprocess.CompletedProcess[str]]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, subprocess.TimeoutExpired):
        return None


def _find_git_root(file_path: str) -> Optional[str]:
    cwd = os.path.dirname(file_path) or file_path
    probe = _run_git_command(("git", "rev-parse", "--show-toplevel"), cwd=cwd)
    if probe is None or probe.returncode != 0:
        return None
    git_root = probe.stdout.strip()
    return git_root or None


def _git_relative_path(git_root: str, file_path: str) -> str:
    return os.path.relpath(file_path, git_root).replace(os.sep, "/")


def _parse_raw_diff_to_git_diff(
    filename: str,
    raw_diff: str,
    *,
    status: str,
) -> Optional[GitDiff]:
    patch_lines: List[str] = []
    additions = 0
    deletions = 0
    in_hunks = False

    for line in raw_diff.splitlines():
        if line.startswith("@@"):
            in_hunks = True
        if not in_hunks:
            continue
        patch_lines.append(line)
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    patch = "\n".join(patch_lines).strip()
    if not patch:
        return None

    return GitDiff(
        filename=filename,
        status=status,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
        repository=None,
    )


def _render_added_file_patch(content: str) -> GitDiff:
    added_lines = content.splitlines()
    if len(added_lines) == 1:
        header = "@@ -0,0 +1 @@"
    else:
        header = f"@@ -0,0 +1,{len(added_lines)} @@"
    patch_lines = [header]
    patch_lines.extend(f"+{line}" for line in added_lines)
    return GitDiff(
        filename="",
        status="added",
        additions=len(added_lines),
        deletions=0,
        changes=len(added_lines),
        patch="\n".join(patch_lines),
        repository=None,
    )


def _compute_git_diff(file_path: str, content: str) -> Optional[GitDiff]:
    git_root = _find_git_root(file_path)
    if git_root is None:
        return None

    git_path = _git_relative_path(git_root, file_path)
    tracked_probe = _run_git_command(
        ("git", "--no-optional-locks", "ls-files", "--error-unmatch", git_path),
        cwd=git_root,
    )
    if tracked_probe is not None and tracked_probe.returncode == 0:
        diff_probe = _run_git_command(
            ("git", "--no-optional-locks", "diff", "HEAD", "--", git_path),
            cwd=git_root,
        )
        if diff_probe is None or diff_probe.returncode != 0 or not diff_probe.stdout.strip():
            return None
        parsed = _parse_raw_diff_to_git_diff(git_path, diff_probe.stdout, status="modified")
        if parsed is None:
            return None
        return GitDiff(
            filename=parsed.filename,
            status=parsed.status,
            additions=parsed.additions,
            deletions=parsed.deletions,
            changes=parsed.changes,
            patch=parsed.patch,
            repository=parsed.repository,
        )

    synthetic = _render_added_file_patch(content)
    return GitDiff(
        filename=git_path,
        status=synthetic.status,
        additions=synthetic.additions,
        deletions=synthetic.deletions,
        changes=synthetic.changes,
        patch=synthetic.patch,
        repository=synthetic.repository,
    )


def _has_full_file_snapshot(read_state: Optional[ReadFileState]) -> bool:
    if read_state is None or read_state.is_partial_view:
        return False
    return (read_state.offset in (None, 1)) and read_state.limit is None


def validate_write_input(
    file_path: str,
    content: str,
    *,
    permission_context: ToolPermissionContext,
    read_file_state: Optional[Dict[str, ReadFileState]] = None,
    project_root: str | None = None,
    config_home: str | None = None,
) -> ValidationResult:
    full_file_path = expand_path(file_path)

    team_memory_error = check_team_mem_secrets(
        full_file_path,
        content,
        project_root=project_root,
        config_home=config_home,
    )
    if team_memory_error is not None:
        return ValidationResult(
            result=False,
            message=team_memory_error,
            error_code=0,
        )

    deny_rule = matching_rule_for_input(
        full_file_path,
        permission_context,
        "edit",
        PermissionBehavior.DENY,
    )
    if deny_rule is not None:
        return ValidationResult(
            result=False,
            message="File is in a directory that is denied by your permission settings.",
            error_code=1,
        )

    if is_unc_path(file_path):
        return ValidationResult(result=True)

    try:
        validate_settings_like_file_content(full_file_path, content)
    except ValueError as exc:
        return ValidationResult(
            result=False,
            message=str(exc),
            error_code=4,
        )

    try:
        file_mtime_ms = int(os.stat(full_file_path).st_mtime * 1000)
    except FileNotFoundError:
        return ValidationResult(result=True)

    if read_file_state is None:
        return ValidationResult(
            result=False,
            message="File has not been read yet. Read it first before writing to it.",
            error_code=2,
        )

    read_state = read_file_state.get(full_file_path)
    if read_state is None or read_state.is_partial_view:
        return ValidationResult(
            result=False,
            message="File has not been read yet. Read it first before writing to it.",
            error_code=2,
        )

    if file_mtime_ms > read_state.timestamp:
        return ValidationResult(
            result=False,
            message=(
                "File has been modified since read, either by the user or by a linter. "
                "Read it again before attempting to write it."
            ),
            error_code=3,
        )

    return ValidationResult(result=True)


def check_write_permission(
    file_path: str,
    permission_context: ToolPermissionContext,
) -> PermissionDecision:
    full_path = expand_path(file_path)
    return check_write_permission_for_tool(
        FILE_EDIT_TOOL_NAME,
        full_path,
        permission_context,
    )


def file_write(
    file_path: str,
    content: str,
    *,
    permission_context: Optional[ToolPermissionContext] = None,
    read_file_state: Optional[Dict[str, ReadFileState]] = None,
    encoding: str = "utf-8",
    project_root: str | None = None,
    config_home: str | None = None,
) -> WriteResult:
    del permission_context
    full_file_path = expand_path(file_path)
    _ensure_parent_dir(full_file_path)

    team_memory_error = check_team_mem_secrets(
        full_file_path,
        content,
        project_root=project_root,
        config_home=config_home,
    )
    if team_memory_error is not None:
        raise ValueError(team_memory_error)

    original_file, existing_encoding = _read_existing_text(full_file_path)
    if original_file is not None:
        last_write_time = int(os.path.getmtime(full_file_path) * 1000)
        last_read = (
            None if read_file_state is None else read_file_state.get(full_file_path)
        )
        if last_read is None or last_write_time > last_read.timestamp:
            content_unchanged = (
                _has_full_file_snapshot(last_read)
                and last_read is not None
                and last_read.content == original_file
            )
            if not content_unchanged:
                raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)

    validate_settings_like_file_content(full_file_path, content)

    target_encoding = existing_encoding or encoding
    with open(full_file_path, "w", encoding=target_encoding, newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

    mtime_ms = int(os.path.getmtime(full_file_path) * 1000)
    if read_file_state is not None:
        read_file_state[full_file_path] = ReadFileState(
            file_path=full_file_path,
            timestamp=mtime_ms,
            content=content,
            offset=None,
            limit=None,
            is_partial_view=False,
        )

    patch: Tuple[Hunk, ...] = ()
    if original_file is not None and original_file != content:
        patch = _compute_simple_patch(original_file, content)
    git_diff = _compute_git_diff(full_file_path, content) if not _is_git_diff_disabled() else None

    result = WriteResult(
        result_type="create" if original_file is None else "update",
        file_path=file_path,
        content=content,
        original_file=original_file,
        structured_patch=patch,
        git_diff=git_diff,
    )

    # Notify LSP server of file save (non-blocking)
    threading.Thread(
        target=notify_persistent_lsp_document_saved,
        args=(full_file_path, content),
        daemon=True,
    ).start()

    return result
