"""FileReadTool -- Python port of src/tools/FileReadTool/FileReadTool.ts.

Implements file reading with permission integration and denied-path semantics.
Concurrency-safe (readonly) tool.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def _is_env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_dedup_enabled() -> bool:
    return not _is_env_truthy(os.environ.get("CLAUDE_CODE_DEDUP_KILLSWITCH"))

from ..types.permissions import (
    PermissionBehavior,
    ToolPermissionContext,
)
from ..utils.config import get_claude_config_home
from ..utils.permissions.path_validation import expand_tilde
from ..utils.permissions.filesystem import (
    FILE_READ_TOOL_NAME,
    matching_rule_for_input,
    check_read_permission_for_tool,
)
from ..utils.text_encoding import decode_text_bytes
from ..utils.tokenization import estimate_token_count_without_tiktoken, load_token_encoding
from .shared import (
    BLOCKED_DEVICE_PATHS,
    BINARY_EXTENSIONS,
    IMAGE_EXTENSIONS,
    FILE_NOT_FOUND_CWD_NOTE,
    FILE_UNCHANGED_STUB,
    ReadFileState,
    ReadResult,
    ValidationResult,
)

PDF_MAX_PAGES_PER_READ = 20
PDF_AT_MENTION_INLINE_THRESHOLD = 10
DEFAULT_MAX_OUTPUT_TOKENS = 25_000


class MaxFileReadTokenExceededError(ValueError):
    def __init__(self, token_count: int, max_tokens: int) -> None:
        super().__init__(
            "File content "
            f"({token_count} tokens) exceeds maximum allowed tokens ({max_tokens}). "
            "Use offset and limit parameters to read specific portions of the file, "
            "or search for specific content instead of reading the whole file."
        )
        self.token_count = token_count
        self.max_tokens = max_tokens


def get_file_read_max_output_tokens() -> int:
    raw_override = os.environ.get("CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS")
    if raw_override:
        try:
            parsed = int(raw_override, 10)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_MAX_OUTPUT_TOKENS


def _count_output_tokens(text: str) -> int:
    encoding = load_token_encoding()
    if encoding is not None:
        try:
            return len(encoding.encode(text, disallowed_special=()))
        except TypeError:
            return len(encoding.encode(text))
    return max(estimate_token_count_without_tiktoken(text), (len(text) + 3) // 4)


def validate_content_tokens(content: str, *, max_tokens: int | None = None) -> None:
    effective_max_tokens = max_tokens or get_file_read_max_output_tokens()
    token_count = _count_output_tokens(content)
    if token_count > effective_max_tokens:
        raise MaxFileReadTokenExceededError(token_count, effective_max_tokens)


def is_blocked_device_path(file_path: str) -> bool:
    """Check if file_path is a device file that would hang the process."""
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    if file_path.startswith("/proc/") and (
        file_path.endswith("/fd/0")
        or file_path.endswith("/fd/1")
        or file_path.endswith("/fd/2")
    ):
        return True
    return False


def has_binary_extension(file_path: str) -> bool:
    """Check if the file has a binary extension."""
    _, ext = os.path.splitext(file_path)
    return ext.lower() in BINARY_EXTENSIONS


def is_image_extension(ext: str) -> bool:
    """Check if an extension (without dot) is an image format."""
    return ext.lower().lstrip(".") in IMAGE_EXTENSIONS


def is_pdf_extension(ext: str) -> bool:
    """Check if an extension (without dot) is PDF."""
    return ext.lower().lstrip(".") == "pdf"


def expand_path(file_path: str) -> str:
    expanded = expand_tilde(file_path).strip()
    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)
    return expanded


def _to_comparable_path(path: str) -> str:
    normalized = os.path.abspath(path).replace("\\", "/")
    return normalized.lower() if os.name == "nt" else normalized


def detect_session_file_type(file_path: str) -> str | None:
    comparable_path = _to_comparable_path(file_path)
    comparable_home = _to_comparable_path(get_claude_config_home()).rstrip("/")
    if comparable_path != comparable_home and not comparable_path.startswith(
        comparable_home + "/"
    ):
        return None
    if "/session-memory/" in comparable_path and comparable_path.endswith(".md"):
        return "session_memory"
    if "/projects/" in comparable_path and comparable_path.endswith(".jsonl"):
        return "session_transcript"
    return None


def is_unc_path(file_path: str) -> bool:
    raw_path = expand_tilde(file_path).strip()
    return raw_path.startswith("\\\\") or raw_path.startswith("//")


def parse_pdf_page_range(pages: str) -> tuple[int, int] | None:
    normalized = pages.strip()
    if not normalized:
        return None
    single_match = re.fullmatch(r"(\d+)", normalized)
    if single_match:
        value = int(single_match.group(1))
        if value <= 0:
            return None
        return (value, value)
    range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", normalized)
    if range_match is None:
        return None
    first_page = int(range_match.group(1))
    last_page = int(range_match.group(2))
    if first_page <= 0 or last_page < first_page:
        return None
    return (first_page, last_page)


def validate_read_input(
    file_path: str,
    *,
    permission_context: ToolPermissionContext,
    pages: str | None = None,
) -> ValidationResult:
    """Validate read input without performing I/O.

    Checks (in order):
    1. Deny rule from permission settings
    2. UNC path (skip remaining checks)
    3. Binary extension (reject except PDF/images)
    4. Blocked device path (reject)

    Returns ValidationResult with result=True if all checks pass.
    """
    full_file_path = expand_path(file_path)

    if pages is not None:
        parsed_pages = parse_pdf_page_range(pages)
        if parsed_pages is None:
            return ValidationResult(
                result=False,
                message=(
                    f'Invalid pages parameter: "{pages}". Use formats like '
                    '"1-5", "3", or "10-20". Pages are 1-indexed.'
                ),
                error_code=7,
            )
        range_size = parsed_pages[1] - parsed_pages[0] + 1
        if range_size > PDF_MAX_PAGES_PER_READ:
            return ValidationResult(
                result=False,
                message=(
                    f'Page range "{pages}" exceeds maximum of '
                    f"{PDF_MAX_PAGES_PER_READ} pages per request. "
                    "Please use a smaller range."
                ),
                error_code=8,
            )

    # 1. Deny rule check
    deny_rule = matching_rule_for_input(
        full_file_path,
        permission_context,
        "read",
        PermissionBehavior.DENY,
    )
    if deny_rule is not None:
        return ValidationResult(
            result=False,
            message="File is in a directory that is denied by your permission settings.",
            error_code=1,
        )

    # 2. UNC path check -- skip filesystem operations
    if is_unc_path(file_path):
        return ValidationResult(result=True)

    # 3. Binary extension check -- PDF and images are excluded
    ext = os.path.splitext(full_file_path)[1].lower()
    if (
        has_binary_extension(full_file_path)
        and not is_pdf_extension(ext)
        and not is_image_extension(ext.lstrip("."))
    ):
        return ValidationResult(
            result=False,
            message=(
                f"This tool cannot read binary files. The file appears to be a "
                f"binary {ext} file. Please use appropriate tools for binary "
                f"file analysis."
            ),
            error_code=4,
        )

    # 4. Blocked device path check
    if is_blocked_device_path(full_file_path):
        return ValidationResult(
            result=False,
            message=(
                f"Cannot read '{file_path}': this device file would block "
                f"or produce infinite output."
            ),
            error_code=9,
        )

    return ValidationResult(result=True)


def check_read_permission(
    file_path: str,
    permission_context: ToolPermissionContext,
) -> object:
    """Check read permission for the given file path.

    Returns a PermissionDecision (Allow/Ask/Deny).
    """
    return check_read_permission_for_tool(
        FILE_READ_TOOL_NAME,
        file_path,
        permission_context,
    )


def file_read(
    file_path: str,
    *,
    offset: int = 1,
    limit: Optional[int] = None,
    pages: str | None = None,
    permission_context: Optional[ToolPermissionContext] = None,
    read_file_state: Optional[Dict[str, ReadFileState]] = None,
) -> ReadResult:
    """Read a file and return its content.

    Args:
        file_path: Absolute or relative path to the file.
        offset: 1-indexed line number to start reading from.
        limit: Maximum number of lines to read.
        permission_context: Permission context for stale-write tracking.
        read_file_state: Mutable dict tracking file read state.

    Returns:
        ReadResult with content, line info, and byte counts.

    Raises:
        FileNotFoundError: If the file does not exist.
        IsADirectoryError: If the path is a directory.
        OSError: If the file cannot be read.
    """
    full_file_path = expand_path(file_path)
    validation = validate_read_input(
        file_path,
        permission_context=permission_context or ToolPermissionContext(),
        pages=pages,
    )
    if not validation.result:
        raise ValueError(validation.message or "Invalid file read request")

    if not os.path.exists(full_file_path):
        raise FileNotFoundError(
            f"File does not exist. {FILE_NOT_FOUND_CWD_NOTE} {os.getcwd()}."
        )

    if os.path.isdir(full_file_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {full_file_path}")

    stat_result = os.stat(full_file_path)
    mtime_ms = stat_result.st_mtime * 1000.0
    total_bytes = stat_result.st_size
    ext = os.path.splitext(full_file_path)[1].lower()
    session_file_type = detect_session_file_type(full_file_path)

    if pages is not None and not is_pdf_extension(ext):
        raise ValueError("pages parameter is only supported for PDF files")

    last_read = None if read_file_state is None else read_file_state.get(full_file_path)
    if (
        _is_dedup_enabled()
        and not is_image_extension(ext)
        and not is_pdf_extension(ext)
        and last_read is not None
        and last_read.timestamp == int(mtime_ms)
        and (last_read.offset or 1) == offset
        and last_read.limit == limit
    ):
        return ReadResult(
            result_type="text",
            file_path=file_path,
            session_file_type=session_file_type,
            content=FILE_UNCHANGED_STUB,
            lines=[],
            total_lines=0,
            offset=offset,
            limit=limit,
            total_bytes=total_bytes,
            read_bytes=0,
            mtime_ms=mtime_ms,
        )

    if ext == ".ipynb":
        return _read_notebook(
            file_path=file_path,
            full_file_path=full_file_path,
            offset=offset,
            limit=limit,
            total_bytes=total_bytes,
            mtime_ms=mtime_ms,
            read_file_state=read_file_state,
            session_file_type=session_file_type,
        )

    # Read file content with encoding detection
    raw_bytes = b""
    with open(full_file_path, "rb") as f:
        raw_bytes = f.read()

    if is_image_extension(ext):
        return _read_image(
            file_path=file_path,
            raw_bytes=raw_bytes,
            total_bytes=total_bytes,
            mtime_ms=mtime_ms,
            file_extension=ext,
            session_file_type=session_file_type,
        )

    if is_pdf_extension(ext):
        return _read_pdf(
            file_path=file_path,
            full_file_path=full_file_path,
            raw_bytes=raw_bytes,
            total_bytes=total_bytes,
            mtime_ms=mtime_ms,
            pages=pages,
            session_file_type=session_file_type,
        )

    content, encoding = decode_text_bytes(raw_bytes)
    # Normalize CRLF to LF, matching TS behavior
    content = content.replace("\r\n", "\n")

    lines = content.split("\n")
    # Remove trailing empty element from split if file ends with newline
    if lines and lines[-1] == "":
        lines = lines[:-1]

    total_lines = len(lines)
    line_offset = max(0, offset - 1) if offset >= 1 else 0

    if limit is not None:
        selected_lines = lines[line_offset : line_offset + limit]
    else:
        selected_lines = lines[line_offset:]

    read_content = "\n".join(selected_lines) if selected_lines else ""
    validate_content_tokens(read_content)
    result_lines = list(selected_lines)
    read_bytes = len(read_content.encode(encoding)) if read_content else 0

    result = ReadResult(
        result_type="text",
        file_path=file_path,
        session_file_type=session_file_type,
        content=read_content,
        lines=result_lines,
        total_lines=total_lines,
        offset=offset,
        limit=limit,
        total_bytes=total_bytes,
        read_bytes=read_bytes,
        mtime_ms=mtime_ms,
    )

    # Update read state for stale-write prevention
    if read_file_state is not None:
        is_partial_view = limit is not None or offset > 1
        read_file_state[full_file_path] = ReadFileState(
            file_path=full_file_path,
            timestamp=int(mtime_ms),
            content=read_content,
            offset=offset,
            limit=limit,
            is_partial_view=is_partial_view,
        )

    return result


def _read_notebook(
    *,
    file_path: str,
    full_file_path: str,
    offset: int,
    limit: Optional[int],
    total_bytes: int,
    mtime_ms: float,
    read_file_state: Optional[Dict[str, ReadFileState]],
    session_file_type: str | None,
) -> ReadResult:
    with open(full_file_path, "r", encoding="utf-8") as handle:
        notebook = json.load(handle)
    raw_cells = notebook.get("cells")
    if not isinstance(raw_cells, list):
        raise ValueError("Notebook is missing a valid cells array")

    cells = [_map_notebook_cell(cell, index) for index, cell in enumerate(raw_cells)]
    serialized_cells = json.dumps(cells, ensure_ascii=False)
    validate_content_tokens(serialized_cells)
    result = ReadResult(
        result_type="notebook",
        file_path=file_path,
        session_file_type=session_file_type,
        content=serialized_cells,
        total_lines=len(cells),
        offset=offset,
        limit=limit,
        total_bytes=total_bytes,
        read_bytes=len(serialized_cells.encode("utf-8")),
        mtime_ms=mtime_ms,
        cells=cells,
        original_size=total_bytes,
    )
    if read_file_state is not None:
        read_file_state[full_file_path] = ReadFileState(
            file_path=full_file_path,
            timestamp=int(mtime_ms),
            content=serialized_cells,
            offset=offset,
            limit=limit,
            is_partial_view=False,
        )
    return result


def _map_notebook_cell(cell: Any, index: int) -> dict[str, Any]:
    if not isinstance(cell, dict):
        return {
            "id": f"cell-{index}",
            "cell_type": "unknown",
            "source": "",
            "outputs": [],
        }
    raw_outputs = cell.get("outputs")
    outputs = raw_outputs if isinstance(raw_outputs, list) else []
    return {
        "id": _notebook_cell_id(cell, index),
        "cell_type": str(cell.get("cell_type", "code")),
        "source": _normalize_notebook_source(cell.get("source")),
        "execution_count": cell.get("execution_count"),
        "outputs": outputs,
        "metadata": cell.get("metadata") if isinstance(cell.get("metadata"), dict) else {},
    }


def _notebook_cell_id(cell: dict[str, Any], index: int) -> str:
    raw_id = cell.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return f"cell-{index}"


def _normalize_notebook_source(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(str(item) for item in source)
    return ""


def _read_image(
    *,
    file_path: str,
    raw_bytes: bytes,
    total_bytes: int,
    mtime_ms: float,
    file_extension: str,
    session_file_type: str | None,
) -> ReadResult:
    media_type = _detect_image_media_type(raw_bytes, file_extension)
    return ReadResult(
        result_type="image",
        file_path=file_path,
        session_file_type=session_file_type,
        total_bytes=total_bytes,
        read_bytes=len(raw_bytes),
        mtime_ms=mtime_ms,
        base64_data=base64.b64encode(raw_bytes).decode("ascii"),
        media_type=media_type,
        dimensions=_detect_image_dimensions(raw_bytes, media_type),
        original_size=total_bytes,
    )


def _detect_image_media_type(raw_bytes: bytes, file_extension: str) -> str:
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WEBP":
        return "image/webp"
    normalized = file_extension.lower().lstrip(".")
    if normalized in {"jpg", "jpeg"}:
        return "image/jpeg"
    if normalized in IMAGE_EXTENSIONS:
        return f"image/{normalized}"
    return "application/octet-stream"


def _detect_image_dimensions(
    raw_bytes: bytes,
    media_type: str,
) -> Optional[dict[str, int]]:
    try:
        if media_type == "image/png" and len(raw_bytes) >= 24:
            width = int.from_bytes(raw_bytes[16:20], "big")
            height = int.from_bytes(raw_bytes[20:24], "big")
            return {
                "original_width": width,
                "original_height": height,
                "display_width": width,
                "display_height": height,
            }
        if media_type == "image/gif" and len(raw_bytes) >= 10:
            width = int.from_bytes(raw_bytes[6:8], "little")
            height = int.from_bytes(raw_bytes[8:10], "little")
            return {
                "original_width": width,
                "original_height": height,
                "display_width": width,
                "display_height": height,
            }
        if media_type == "image/webp":
            dimensions = _detect_webp_dimensions(raw_bytes)
            if dimensions is not None:
                width, height = dimensions
                return {
                    "original_width": width,
                    "original_height": height,
                    "display_width": width,
                    "display_height": height,
                }
        if media_type == "image/jpeg":
            dimensions = _detect_jpeg_dimensions(raw_bytes)
            if dimensions is not None:
                width, height = dimensions
                return {
                    "original_width": width,
                    "original_height": height,
                    "display_width": width,
                    "display_height": height,
                }
    except Exception:
        return None
    return None


def _detect_jpeg_dimensions(raw_bytes: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(raw_bytes):
        if raw_bytes[index] != 0xFF:
            index += 1
            continue
        marker = raw_bytes[index + 1]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            block_length = int.from_bytes(raw_bytes[index + 2 : index + 4], "big")
            if block_length < 7 or index + 9 >= len(raw_bytes):
                return None
            height = int.from_bytes(raw_bytes[index + 5 : index + 7], "big")
            width = int.from_bytes(raw_bytes[index + 7 : index + 9], "big")
            return (width, height)
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 >= len(raw_bytes):
            return None
        block_length = int.from_bytes(raw_bytes[index + 2 : index + 4], "big")
        if block_length < 2:
            return None
        index += 2 + block_length
    return None


def _detect_webp_dimensions(raw_bytes: bytes) -> tuple[int, int] | None:
    if len(raw_bytes) < 30 or not raw_bytes.startswith(b"RIFF") or raw_bytes[8:12] != b"WEBP":
        return None
    chunk_type = raw_bytes[12:16]
    if chunk_type == b"VP8X" and len(raw_bytes) >= 30:
        width = 1 + int.from_bytes(raw_bytes[24:27], "little")
        height = 1 + int.from_bytes(raw_bytes[27:30], "little")
        return (width, height)
    if chunk_type == b"VP8 " and len(raw_bytes) >= 30:
        start = raw_bytes.find(b"\x9d\x01\x2a")
        if start >= 0 and start + 7 <= len(raw_bytes):
            width = int.from_bytes(raw_bytes[start + 3 : start + 5], "little") & 0x3FFF
            height = int.from_bytes(raw_bytes[start + 5 : start + 7], "little") & 0x3FFF
            return (width, height)
    if chunk_type == b"VP8L" and len(raw_bytes) >= 25:
        bits = int.from_bytes(raw_bytes[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    return None


def _read_pdf(
    *,
    file_path: str,
    full_file_path: str,
    raw_bytes: bytes,
    total_bytes: int,
    mtime_ms: float,
    pages: str | None,
    session_file_type: str | None,
) -> ReadResult:
    page_count = _get_pdf_page_count(full_file_path)
    if pages is not None:
        first_page, last_page = parse_pdf_page_range(pages) or (1, 1)
        if page_count is not None and first_page > page_count:
            raise ValueError(
                f'Page range "{pages}" is outside this PDF. The file has {page_count} pages.'
            )
        output_dir, extracted_count = _extract_pdf_pages(
            full_file_path,
            first_page=first_page,
            last_page=last_page,
        )
        return ReadResult(
            result_type="parts",
            file_path=file_path,
            session_file_type=session_file_type,
            total_bytes=total_bytes,
            read_bytes=0,
            mtime_ms=mtime_ms,
            original_size=total_bytes,
            page_count=page_count,
            pages=pages,
            output_dir=output_dir,
            count=extracted_count,
        )

    if page_count is not None and page_count > PDF_AT_MENTION_INLINE_THRESHOLD:
        raise ValueError(
            f"This PDF has {page_count} pages, which is too many to read at once. "
            f'Use the pages parameter to read specific page ranges (e.g., pages: "1-5"). '
            f"Maximum {PDF_MAX_PAGES_PER_READ} pages per request."
        )

    return ReadResult(
        result_type="pdf",
        file_path=file_path,
        session_file_type=session_file_type,
        total_bytes=total_bytes,
        read_bytes=len(raw_bytes),
        mtime_ms=mtime_ms,
        base64_data=base64.b64encode(raw_bytes).decode("ascii"),
        media_type="application/pdf",
        original_size=total_bytes,
        page_count=page_count,
    )


def _get_pdf_page_count(full_file_path: str) -> int | None:
    try:
        completed = subprocess.run(
            ["pdfinfo", full_file_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    for line in completed.stdout.splitlines():
        if not line.startswith("Pages:"):
            continue
        raw_value = line.partition(":")[2].strip()
        if raw_value.isdigit():
            return int(raw_value)
    return None


def _extract_pdf_pages(
    full_file_path: str,
    *,
    first_page: int,
    last_page: int,
) -> tuple[str, int]:
    output_dir = tempfile.mkdtemp(prefix="cc-pdf-pages-")
    output_prefix = str(Path(output_dir) / "page")
    try:
        subprocess.run(
            [
                "pdftoppm",
                "-jpeg",
                "-f",
                str(first_page),
                "-l",
                str(last_page),
                full_file_path,
                output_prefix,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdftoppm is required to extract PDF pages") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr)
        raise RuntimeError(stderr.strip() or "Failed to extract PDF pages") from exc
    image_files = sorted(Path(output_dir).glob("page-*.jpg"))
    return (output_dir, len(image_files))
