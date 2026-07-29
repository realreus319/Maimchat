from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Optional

from .error_log_sink import DiagnosticContext, initialize_error_log_sink


@dataclass(frozen=True)
class StructuredErrorDisplay:
    title: str
    code: str
    exception_type: str
    detail: str
    detail_language: str | None = None
    stage: str | None = None
    log_path: str | None = None


def _compact_error_detail(detail: str) -> str:
    return " ".join(segment for segment in detail.strip().splitlines() if segment).strip()


def _looks_like_diff(detail: str) -> bool:
    lines = [line.rstrip("\n") for line in detail.strip().splitlines() if line.strip()]
    if not lines:
        return False
    if any(line.startswith("diff --git ") for line in lines):
        return True
    has_hunk = any(line.startswith("@@") for line in lines)
    has_headers = any(line.startswith("--- ") for line in lines) and any(
        line.startswith("+++ ") for line in lines
    )
    change_lines = sum(
        1
        for line in lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    return bool(has_hunk and (has_headers or change_lines))


def _looks_like_json(detail: str) -> bool:
    stripped = detail.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        parsed = json.loads(stripped)
    except Exception:
        return False
    return isinstance(parsed, (dict, list))


def _infer_error_detail_language(detail: str) -> str | None:
    stripped = detail.strip()
    if not stripped:
        return None
    if _looks_like_diff(stripped):
        return "diff"
    if _looks_like_json(stripped):
        return "json"
    if "Traceback (most recent call last):" in stripped:
        return "python"
    if re.search(r'^\s*File ".*", line \d+, in ', stripped, re.MULTILINE):
        return "python"
    if re.search(r"^(?:bash|sh|zsh):", stripped, re.MULTILINE):
        return "bash"
    if stripped.startswith("<") and re.search(r"</?[a-zA-Z][^>]*>", stripped):
        return "xml"
    return None


def build_structured_error_display(
    error: BaseException,
    *,
    title: str,
    code: str | None = None,
    detail: str | None = None,
    stage: str | None = None,
    config_home: str | None = None,
    diagnostic_context: Optional[DiagnosticContext] = None,
    log_error: bool = False,
) -> StructuredErrorDisplay:
    resolved_detail = (detail or str(error) or error.__class__.__name__).strip()
    resolved_code = (code or error.__class__.__name__).strip() or "runtime_error"
    log_path: str | None = None
    if log_error:
        try:
            sink = initialize_error_log_sink(
                config_home=config_home,
                context=diagnostic_context,
            )
            log_path = sink.log_error(error)
        except Exception:
            log_path = None
    return StructuredErrorDisplay(
        title=title.strip() or "Runtime Error",
        code=resolved_code,
        exception_type=error.__class__.__name__,
        detail=resolved_detail,
        detail_language=_infer_error_detail_language(resolved_detail),
        stage=stage.strip() if isinstance(stage, str) and stage.strip() else None,
        log_path=log_path,
    )


def format_structured_error_markdown(display: StructuredErrorDisplay) -> str:
    lines = [f"### {display.title}"]
    lines.append(f"- Code: `{display.code}`")
    lines.append(f"- Type: `{display.exception_type}`")
    if display.stage:
        lines.append(f"- Stage: `{display.stage}`")
    if display.log_path:
        lines.append(f"- Error Log: `{display.log_path}`")
    lines.append("- Details:")
    lines.append(f"```{display.detail_language or 'text'}")
    lines.append(display.detail)
    lines.append("```")
    return "\n".join(lines)


def format_compact_structured_error_markdown(display: StructuredErrorDisplay) -> str:
    lines = [f"### {display.title}"]
    lines.append(f"- Code: `{display.code}`")
    lines.append(f"- Type: `{display.exception_type}`")
    if display.stage:
        lines.append(f"- Stage: `{display.stage}`")
    if display.log_path:
        lines.append(f"- Error Log: `{display.log_path}`")
    detail_prefix = "- Details"
    if display.detail_language and display.detail_language != "text":
        detail_prefix = f"- Details ({display.detail_language})"
    lines.append(f"{detail_prefix}: `{_compact_error_detail(display.detail)}`")
    return "\n".join(lines)


def format_structured_error_text(display: StructuredErrorDisplay) -> str:
    lines = [f"Error: {display.title}"]
    lines.append(f"Code: {display.code}")
    lines.append(f"Type: {display.exception_type}")
    if display.stage:
        lines.append(f"Stage: {display.stage}")
    if display.log_path:
        lines.append(f"Error Log: {display.log_path}")
    if display.detail_language and display.detail_language != "text":
        lines.append(f"Detail Format: {display.detail_language}")
    lines.append("Details:")
    lines.append(display.detail)
    return "\n".join(lines)


def format_compact_structured_error_text(display: StructuredErrorDisplay) -> str:
    lines = [f"Error: {display.title}"]
    lines.append(f"Type: {display.exception_type} | Code: {display.code}")
    if display.stage:
        lines.append(f"Stage: {display.stage}")
    if display.log_path:
        lines.append(f"Error Log: {display.log_path}")
    lines.append(f"Details: {_compact_error_detail(display.detail)}")
    return "\n".join(lines)


__all__ = [
    "StructuredErrorDisplay",
    "build_structured_error_display",
    "format_compact_structured_error_markdown",
    "format_compact_structured_error_text",
    "format_structured_error_markdown",
    "format_structured_error_text",
]
