from __future__ import annotations

import os
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
    EditResult,
    FILE_NOT_FOUND_CWD_NOTE,
    FILE_UNEXPECTEDLY_MODIFIED_ERROR,
    Hunk,
    LEFT_DOUBLE_CURLY_QUOTE,
    LEFT_SINGLE_CURLY_QUOTE,
    MAX_EDIT_FILE_SIZE,
    RIGHT_DOUBLE_CURLY_QUOTE,
    RIGHT_SINGLE_CURLY_QUOTE,
    ReadFileState,
    ValidationResult,
)


def expand_path(file_path: str) -> str:
    expanded = expand_tilde(file_path).strip()
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return expanded


def is_unc_path(file_path: str) -> bool:
    raw_path = expand_tilde(file_path).strip()
    return raw_path.startswith("\\\\") or raw_path.startswith("//")


def _get_file_modification_time_ms(path: str) -> int:
    return int(os.path.getmtime(path) * 1000)


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def normalize_quotes(value: str) -> str:
    return (
        value.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def find_actual_string(file_content: str, search_string: str) -> Optional[str]:
    if search_string in file_content:
        return search_string

    normalized_search = normalize_quotes(search_string)
    normalized_file = normalize_quotes(file_content)
    search_index = normalized_file.find(normalized_search)
    if search_index == -1:
        return None
    return file_content[search_index : search_index + len(search_string)]


def _is_opening_context(chars: List[str], index: int) -> bool:
    if index == 0:
        return True
    return chars[index - 1] in (
        " ",
        "\t",
        "\n",
        "\r",
        "(",
        "[",
        "{",
        "\u2014",
        "\u2013",
    )


def _apply_curly_double_quotes(value: str) -> str:
    chars = list(value)
    result: List[str] = []
    for index, char in enumerate(chars):
        if char == '"':
            result.append(
                LEFT_DOUBLE_CURLY_QUOTE
                if _is_opening_context(chars, index)
                else RIGHT_DOUBLE_CURLY_QUOTE
            )
        else:
            result.append(char)
    return "".join(result)


def _apply_curly_single_quotes(value: str) -> str:
    chars = list(value)
    result: List[str] = []
    for index, char in enumerate(chars):
        if char == "'":
            previous = chars[index - 1] if index > 0 else None
            following = chars[index + 1] if index < len(chars) - 1 else None
            if (
                previous is not None
                and following is not None
                and previous.isalpha()
                and following.isalpha()
            ):
                result.append(RIGHT_SINGLE_CURLY_QUOTE)
            else:
                result.append(
                    LEFT_SINGLE_CURLY_QUOTE
                    if _is_opening_context(chars, index)
                    else RIGHT_SINGLE_CURLY_QUOTE
                )
        else:
            result.append(char)
    return "".join(result)


def preserve_quote_style(
    old_string: str, actual_old_string: str, new_string: str
) -> str:
    if old_string == actual_old_string:
        return new_string

    has_double_quotes = (
        LEFT_DOUBLE_CURLY_QUOTE in actual_old_string
        or RIGHT_DOUBLE_CURLY_QUOTE in actual_old_string
    )
    has_single_quotes = (
        LEFT_SINGLE_CURLY_QUOTE in actual_old_string
        or RIGHT_SINGLE_CURLY_QUOTE in actual_old_string
    )
    if not has_double_quotes and not has_single_quotes:
        return new_string

    result = new_string
    if has_double_quotes:
        result = _apply_curly_double_quotes(result)
    if has_single_quotes:
        result = _apply_curly_single_quotes(result)
    return result


def apply_edit_to_file(
    original_content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    if new_string != "":
        if replace_all:
            return original_content.replace(old_string, new_string)
        return original_content.replace(old_string, new_string, 1)

    strip_trailing_newline = (
        not old_string.endswith("\n") and (old_string + "\n") in original_content
    )
    target = old_string + "\n" if strip_trailing_newline else old_string
    if replace_all:
        return original_content.replace(target, new_string)
    return original_content.replace(target, new_string, 1)


def _compute_patch_for_edit(
    file_contents: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Tuple[Tuple[Hunk, ...], str]:
    updated = apply_edit_to_file(file_contents, old_string, new_string, replace_all)
    if updated == file_contents:
        raise ValueError(
            "Original and edited file match exactly. Failed to apply edit."
        )

    old_lines = file_contents.splitlines()
    new_lines = updated.splitlines()
    lines: List[str] = []
    for line in old_lines:
        lines.append("-" + line)
    for line in new_lines:
        lines.append("+" + line)

    if not lines:
        return (), updated

    return (
        (
            Hunk(
                old_start=1,
                old_lines=len(old_lines),
                new_start=1,
                new_lines=len(new_lines),
                lines=tuple(lines),
            ),
        ),
        updated,
    )


def _read_file_metadata(file_path: str) -> Tuple[str, bool, str, str]:
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return "", False, "utf-8", "LF"

    decoded, encoding = decode_text_bytes(raw)
    if "\r\n" in decoded:
        line_endings = "CRLF"
    elif "\r" in decoded:
        line_endings = "CR"
    else:
        line_endings = "LF"

    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, True, encoding, line_endings


def _has_full_file_snapshot(read_state: Optional[ReadFileState]) -> bool:
    if read_state is None or read_state.is_partial_view:
        return False
    return (read_state.offset in (None, 1)) and read_state.limit is None


def _restore_line_endings(content: str, line_endings: str) -> str:
    if line_endings == "CRLF":
        return content.replace("\n", "\r\n")
    if line_endings == "CR":
        return content.replace("\n", "\r")
    return content


def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return "{}B".format(size_bytes)
    if size_bytes < 1024 * 1024:
        return "{:.1f}KB".format(size_bytes / 1024)
    if size_bytes < 1024 * 1024 * 1024:
        return "{:.1f}MB".format(size_bytes / (1024 * 1024))
    return "{:.1f}GB".format(size_bytes / (1024 * 1024 * 1024))


def validate_edit_input(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    permission_context: ToolPermissionContext,
    read_file_state: Optional[Dict[str, ReadFileState]] = None,
    project_root: str | None = None,
    config_home: str | None = None,
) -> ValidationResult:
    full_file_path = expand_path(file_path)

    if old_string == new_string:
        return ValidationResult(
            result=False,
            behavior="ask",
            message="No changes to make: old_string and new_string are exactly the same.",
            error_code=1,
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
            behavior="ask",
            message="File is in a directory that is denied by your permission settings.",
            error_code=2,
        )

    if is_unc_path(file_path):
        return ValidationResult(result=True)

    team_memory_error = check_team_mem_secrets(
        full_file_path,
        new_string,
        project_root=project_root,
        config_home=config_home,
    )
    if team_memory_error is not None:
        return ValidationResult(
            result=False,
            behavior="ask",
            message=team_memory_error,
            error_code=0,
        )

    try:
        file_size = os.path.getsize(full_file_path)
        if file_size > MAX_EDIT_FILE_SIZE:
            return ValidationResult(
                result=False,
                behavior="ask",
                message=(
                    "File is too large to edit ({}). Maximum editable file size is {}.".format(
                        _format_file_size(file_size),
                        _format_file_size(MAX_EDIT_FILE_SIZE),
                    )
                ),
                error_code=10,
            )
    except OSError:
        pass

    file_content, file_exists, _, _ = _read_file_metadata(full_file_path)
    if not file_exists:
        if old_string == "":
            try:
                validate_settings_like_file_content(full_file_path, new_string)
            except ValueError as exc:
                return ValidationResult(
                    result=False,
                    behavior="ask",
                    message=str(exc),
                    error_code=11,
                )
            return ValidationResult(result=True)
        return ValidationResult(
            result=False,
            behavior="ask",
            message="File does not exist. {} {}.".format(
                FILE_NOT_FOUND_CWD_NOTE, os.getcwd()
            ),
            error_code=4,
        )

    if old_string == "":
        if file_content.strip() != "":
            return ValidationResult(
                result=False,
                behavior="ask",
                message="Cannot create new file - file already exists.",
                error_code=3,
            )
        try:
            validate_settings_like_file_content(full_file_path, new_string)
        except ValueError as exc:
            return ValidationResult(
                result=False,
                behavior="ask",
                message=str(exc),
                error_code=11,
            )
        return ValidationResult(result=True)

    if full_file_path.endswith(".ipynb"):
        return ValidationResult(
            result=False,
            behavior="ask",
            message="File is a Jupyter Notebook. Use NotebookEditTool to edit this file.",
            error_code=5,
        )

    read_state = (
        None if read_file_state is None else read_file_state.get(full_file_path)
    )
    if read_state is None or read_state.is_partial_view:
        return ValidationResult(
            result=False,
            behavior="ask",
            message="File has not been read yet. Read it first before writing to it.",
            error_code=6,
        )

    if _get_file_modification_time_ms(full_file_path) > read_state.timestamp:
        if not (_has_full_file_snapshot(read_state) and file_content == read_state.content):
            return ValidationResult(
                result=False,
                behavior="ask",
                message=(
                    "File has been modified since read, either by the user or by a linter. "
                    "Read it again before attempting to write it."
                ),
                error_code=7,
            )

    actual_old_string = find_actual_string(file_content, old_string)
    if actual_old_string is None:
        return ValidationResult(
            result=False,
            behavior="ask",
            message="String to replace not found in file.\nString: {}".format(
                old_string
            ),
            error_code=8,
        )

    match_count = file_content.count(actual_old_string)
    if match_count > 1 and not replace_all:
        return ValidationResult(
            result=False,
            behavior="ask",
            message=(
                "Found {} matches of the string to replace, but replace_all is false. "
                "To replace all occurrences, set replace_all to true. To replace only one occurrence, "
                "please provide more context to uniquely identify the instance.\nString: {}"
            ).format(match_count, old_string),
            error_code=9,
            metadata={
                "isFilePathAbsolute": os.path.isabs(expand_tilde(file_path).strip()),
                "actualOldString": actual_old_string,
            },
        )

    actual_new_string = preserve_quote_style(old_string, actual_old_string, new_string)
    candidate_content = apply_edit_to_file(
        file_content,
        actual_old_string,
        actual_new_string,
        replace_all,
    )
    try:
        validate_settings_like_file_content(full_file_path, candidate_content)
    except ValueError as exc:
        return ValidationResult(
            result=False,
            behavior="ask",
            message=str(exc),
            error_code=11,
        )

    return ValidationResult(result=True)


def check_edit_permission(
    file_path: str,
    permission_context: ToolPermissionContext,
) -> PermissionDecision:
    full_path = expand_path(file_path)
    return check_write_permission_for_tool(
        FILE_EDIT_TOOL_NAME,
        full_path,
        permission_context,
    )


def file_edit(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    permission_context: Optional[ToolPermissionContext] = None,
    read_file_state: Optional[Dict[str, ReadFileState]] = None,
    encoding: str = "utf-8",
    project_root: str | None = None,
    config_home: str | None = None,
) -> EditResult:
    del permission_context
    full_file_path = expand_path(file_path)
    _ensure_parent_dir(full_file_path)

    team_memory_error = check_team_mem_secrets(
        full_file_path,
        new_string,
        project_root=project_root,
        config_home=config_home,
    )
    if team_memory_error is not None:
        raise ValueError(team_memory_error)

    original_content, file_exists, detected_encoding, line_endings = (
        _read_file_metadata(full_file_path)
    )
    if file_exists:
        last_write_time = _get_file_modification_time_ms(full_file_path)
        last_read = (
            None if read_file_state is None else read_file_state.get(full_file_path)
        )
        if last_read is None or last_write_time > last_read.timestamp:
            content_unchanged = (
                _has_full_file_snapshot(last_read)
                and last_read is not None
                and original_content == last_read.content
            )
            if not content_unchanged:
                raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)

    actual_old_string = find_actual_string(original_content, old_string) or old_string
    actual_new_string = preserve_quote_style(old_string, actual_old_string, new_string)
    patch_hunks, updated_content = _compute_patch_for_edit(
        original_content,
        actual_old_string,
        actual_new_string,
        replace_all,
    )
    validate_settings_like_file_content(full_file_path, updated_content)

    output_encoding = detected_encoding if file_exists else encoding
    rendered_content = _restore_line_endings(updated_content, line_endings)
    with open(full_file_path, "w", encoding=output_encoding, newline="") as handle:
        handle.write(rendered_content)
        handle.flush()
        os.fsync(handle.fileno())

    if read_file_state is not None:
        read_file_state[full_file_path] = ReadFileState(
            file_path=full_file_path,
            timestamp=_get_file_modification_time_ms(full_file_path),
            content=updated_content,
            offset=None,
            limit=None,
            is_partial_view=False,
        )

    result = EditResult(
        result_type="edit",
        file_path=file_path,
        content=updated_content,
        old_string=actual_old_string,
        new_string=new_string,
        original_file=original_content if file_exists else None,
        replace_all=replace_all,
        structured_patch=patch_hunks,
    )

    # Notify LSP server of file save (non-blocking)
    threading.Thread(
        target=notify_persistent_lsp_document_saved,
        args=(full_file_path, updated_content),
        daemon=True,
    ).start()

    return result
