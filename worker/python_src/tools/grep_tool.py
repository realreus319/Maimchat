from __future__ import annotations

import os
import re
import shutil
import subprocess

import fnmatch

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional


GREP_TOOL_NAME = "Grep"
DEFAULT_HEAD_LIMIT = 250
_VCS_DIRECTORIES = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")


def _to_relative_path(filepath: str, base: Optional[str] = None) -> str:
    cwd = base or os.getcwd()
    if filepath == cwd:
        return "."
    if filepath.startswith(cwd + os.sep):
        return filepath[len(cwd) + 1 :]
    return filepath


def _expand_path(path: str) -> str:
    if path.startswith("~"):
        return os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(os.getcwd(), path))


def _parse_glob_patterns(raw_glob: Optional[str]) -> List[str]:
    if not raw_glob:
        return []

    glob_patterns: List[str] = []
    for raw_pattern in raw_glob.split():
        if "{" in raw_pattern and "}" in raw_pattern:
            glob_patterns.append(raw_pattern)
            continue
        glob_patterns.extend(pattern for pattern in raw_pattern.split(",") if pattern)
    return glob_patterns


def _expand_fallback_glob_pattern(pattern: str) -> List[str]:
    if "{" not in pattern or "}" not in pattern:
        return [pattern]

    brace_start = pattern.find("{")
    brace_end = pattern.find("}", brace_start + 1)
    if brace_start < 0 or brace_end < 0 or brace_end < brace_start:
        return [pattern]

    prefix = pattern[:brace_start]
    suffix = pattern[brace_end + 1 :]
    options = [
        option for option in pattern[brace_start + 1 : brace_end].split(",") if option
    ]
    if not options:
        return [pattern]
    return [prefix + option + suffix for option in options]


def _normalize_exclude_glob_patterns(raw_glob: Optional[str]) -> List[str]:
    normalized: List[str] = []
    for pattern in _parse_glob_patterns(raw_glob):
        if not pattern:
            continue
        normalized.append(pattern if pattern.startswith("!") else f"!{pattern}")
    return normalized


def _matches_glob_pattern(
    filepath: str,
    search_path: str,
    glob_patterns: List[str],
) -> bool:
    if not glob_patterns:
        return True

    basename = os.path.basename(filepath)
    relative_path = os.path.relpath(filepath, search_path).replace(os.sep, "/")

    has_positive = any(not pattern.startswith("!") for pattern in glob_patterns)
    matched = not has_positive
    for raw_pattern in glob_patterns:
        is_negated = raw_pattern.startswith("!")
        pattern_body = raw_pattern[1:] if is_negated else raw_pattern
        expanded_patterns = _expand_fallback_glob_pattern(pattern_body)
        if any(
            fnmatch.fnmatch(basename, pattern)
            or fnmatch.fnmatch(relative_path, pattern)
            for pattern in expanded_patterns
        ):
            matched = not is_negated
    return matched


def _iter_search_files(search_path: str):
    if os.path.isfile(search_path):
        yield search_path
        return

    for root, dirs, files in os.walk(search_path):
        dirs[:] = [d for d in dirs if d not in _VCS_DIRECTORIES]
        for fname in files:
            yield os.path.join(root, fname)


def _path_mtime(filepath: str) -> float:
    try:
        return os.path.getmtime(filepath)
    except (OSError, FileNotFoundError):
        return 0.0


def _sort_match_paths(filepaths: List[str]) -> List[str]:
    if os.environ.get("NODE_ENV") == "test":
        return sorted(filepaths)

    return sorted(filepaths, key=lambda path: (-_path_mtime(path), path))


def _apply_head_limit(
    items: list,
    limit: Optional[int],
    offset: int = 0,
) -> tuple:
    if limit == 0:
        return items[offset:], None
    effective = limit if limit is not None else DEFAULT_HEAD_LIMIT
    sliced = items[offset : offset + effective]
    was_truncated = len(items) - offset > effective
    return sliced, effective if was_truncated else None


@dataclass(frozen=True)
class _ContentRecord:
    path: str
    line_number: Optional[str]
    marker: str
    body: str
    is_match: bool


def _parse_content_record(line: str) -> Optional[_ContentRecord]:
    if not line or line == "--":
        return None

    numbered_match = re.match(r"^(.*?)([:-])(\d+)([:-])(.*)$", line)
    if numbered_match is not None:
        raw_path, _, line_number, marker, body = numbered_match.groups()
        return _ContentRecord(
            path=_to_relative_path(raw_path),
            line_number=line_number,
            marker=marker,
            body=body,
            is_match=marker == ":",
        )

    bare_match = re.match(r"^(.*?)([:-])(.*)$", line)
    if bare_match is None:
        return None
    raw_path, marker, body = bare_match.groups()
    return _ContentRecord(
        path=_to_relative_path(raw_path),
        line_number=None,
        marker=marker,
        body=body,
        is_match=True,
    )


def _format_content_lines(
    raw_lines: List[str],
    limit: Optional[int],
    offset: int,
) -> tuple[str, int, Optional[int]]:
    limited, applied = _apply_head_limit(raw_lines, limit, offset)
    records = [
        record
        for line in limited
        if (record := _parse_content_record(line)) is not None
    ]
    if not records:
        return "", 0, applied

    line_number_width = max(
        (len(record.line_number) for record in records if record.line_number is not None),
        default=0,
    )
    rendered: List[str] = []
    current_path: Optional[str] = None
    for record in records:
        if record.path != current_path:
            if rendered:
                rendered.append("")
            rendered.append(record.path)
            current_path = record.path
        if record.line_number is None:
            rendered.append(record.body)
            continue
        prefix = f"{record.line_number.rjust(line_number_width)}{record.marker}"
        rendered.append(f"{prefix} {record.body}".rstrip())

    remaining_records = [
        record
        for line in raw_lines[offset + len(limited) :]
        if (record := _parse_content_record(line)) is not None
    ]
    additional_matches = sum(
        1 for record in remaining_records if record.is_match or record.line_number is None
    )
    shown_paths = {record.path for record in records}
    additional_paths = {
        record.path for record in remaining_records if record.path not in shown_paths
    }
    if additional_matches:
        rendered.append("")
        suffix = "" if additional_matches == 1 else "es"
        rendered.append(f"Truncated - {additional_matches} additional match{suffix}")
    if additional_paths:
        suffix = "" if len(additional_paths) == 1 else "s"
        rendered.append(
            f"Additional matches in {len(additional_paths)} other file{suffix}"
        )

    return "\n".join(rendered), len(rendered), applied


@dataclass(frozen=True)
class GrepInput:
    pattern: str
    path: Optional[str] = None
    glob: Optional[str] = None
    exclude: Optional[str] = None
    output_mode: str = "files_with_matches"
    context_before: Optional[int] = None
    context_after: Optional[int] = None
    context_c: Optional[int] = None
    context: Optional[int] = None
    show_line_numbers: Optional[bool] = None
    case_insensitive: bool = False
    type: Optional[str] = None
    head_limit: Optional[int] = None
    offset: Optional[int] = None
    multiline: bool = False


@dataclass(frozen=True)
class GrepOutput:
    filenames: List[str]
    mode: Optional[str] = None
    num_files: Optional[int] = None
    content: Optional[str] = None
    num_lines: Optional[int] = None
    num_matches: Optional[int] = None
    applied_limit: Optional[int] = None
    applied_offset: Optional[int] = None


def _find_rg_binary() -> Optional[str]:
    which = shutil.which("rg")
    if which:
        return which
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    vendor_rg = os.path.join(
        repo,
        "claude-code-sourcemap-main",
        "package",
        "vendor",
        "ripgrep",
        "x64-linux",
        "rg",
    )
    if os.path.isfile(vendor_rg) and os.access(vendor_rg, os.X_OK):
        return vendor_rg
    return None


class GrepTool:
    name: str = GREP_TOOL_NAME

    @staticmethod
    def is_concurrency_safe(_input: Mapping[str, Any]) -> bool:
        return True

    @staticmethod
    def is_read_only() -> bool:
        return True

    @staticmethod
    def validate_input(raw_input: Mapping[str, Any]) -> tuple:
        pattern = raw_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return (False, dict(raw_input))
        path = raw_input.get("path")
        if path is not None and isinstance(path, str):
            absolute_path = _expand_path(path)
            if not os.path.exists(absolute_path):
                return (False, dict(raw_input))
        return (True, dict(raw_input))

    @staticmethod
    def interrupt_behavior() -> str:
        return "cancel"

    @staticmethod
    def get_path(input: GrepInput) -> str:
        return _expand_path(input.path) if input.path else os.getcwd()

    @staticmethod
    def check_permissions(
        input: GrepInput,
        permission_context: Any,
    ) -> str:
        from ..utils.permissions.filesystem import check_read_permission_for_tool

        search_path = GrepTool.get_path(input)
        decision = check_read_permission_for_tool(
            GREP_TOOL_NAME,
            search_path,
            permission_context,
        )
        if decision is not None:
            behavior = getattr(decision, "behavior", None)
            if behavior == "allow":
                return "allow"
            if behavior == "deny":
                return "deny"
            return "ask"
        return "ask"

    @staticmethod
    def _build_rg_args(input: GrepInput) -> list:
        args = ["--hidden", "--max-columns", "500"]
        for d in _VCS_DIRECTORIES:
            args.extend(["--glob", f"!{d}"])
        if input.multiline:
            args.extend(["-U", "--multiline-dotall"])
        if input.case_insensitive:
            args.append("-i")
        mode = input.output_mode
        if mode == "files_with_matches":
            args.append("-l")
        elif mode == "count":
            args.append("-c")
        if input.show_line_numbers is not False and mode == "content":
            args.append("-n")
        if mode == "content":
            if input.context is not None:
                args.extend(["-C", str(input.context)])
            elif input.context_c is not None:
                args.extend(["-C", str(input.context_c)])
            else:
                if input.context_before is not None:
                    args.extend(["-B", str(input.context_before)])
                if input.context_after is not None:
                    args.extend(["-A", str(input.context_after)])
        pattern = input.pattern
        if pattern.startswith("-"):
            args.extend(["-e", pattern])
        else:
            args.append(pattern)
        if input.type:
            args.extend(["--type", input.type])
        if input.glob:
            for gp in _parse_glob_patterns(input.glob):
                if gp:
                    args.extend(["--glob", gp])
        if input.exclude:
            for gp in _normalize_exclude_glob_patterns(input.exclude):
                args.extend(["--glob", gp])
        return args

    @classmethod
    def call(cls, input: GrepInput) -> GrepOutput:
        absolute_path = cls.get_path(input)
        rg = _find_rg_binary()
        mode = input.output_mode or "files_with_matches"

        if rg is None:
            return cls._fallback_grep(input, absolute_path)

        args = GrepTool._build_rg_args(input)
        try:
            result = subprocess.run(
                [rg] + args + [absolute_path],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return GrepOutput(
                mode=mode,
                num_files=0,
                filenames=[],
                content="ripgrep timed out",
            )
        except FileNotFoundError:
            return GrepOutput(
                mode=mode,
                num_files=0,
                filenames=[],
                content="ripgrep binary not found",
            )

        raw_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        offset = input.offset or 0

        if mode == "content":
            content, line_count, applied = _format_content_lines(
                raw_lines,
                input.head_limit,
                offset,
            )
            return GrepOutput(
                mode="content",
                num_files=0,
                filenames=[],
                content=content,
                num_lines=line_count,
                applied_limit=applied,
                applied_offset=offset if offset > 0 else None,
            )

        if mode == "count":
            limited, applied = _apply_head_limit(raw_lines, input.head_limit, offset)
            final_count: List[str] = []
            total = 0
            fc = 0
            for line in limited:
                ci = line.rfind(":")
                if ci > 0:
                    fp = line[:ci]
                    cs = line[ci + 1 :]
                    try:
                        c = int(cs)
                        total += c
                        fc += 1
                        final_count.append(_to_relative_path(fp) + ":" + cs)
                    except ValueError:
                        final_count.append(line)
                else:
                    final_count.append(line)
            return GrepOutput(
                mode="count",
                num_files=fc,
                filenames=[],
                content="\n".join(final_count),
                num_matches=total,
                applied_limit=applied,
                applied_offset=offset if offset > 0 else None,
            )

        # files_with_matches (default)
        try:
            stat_pairs = []
            for f in raw_lines:
                try:
                    stat_pairs.append((f, os.path.getmtime(f)))
                except (OSError, FileNotFoundError):
                    stat_pairs.append((f, 0.0))
        except Exception:
            stat_pairs = [(f, 0.0) for f in raw_lines]

        if os.environ.get("NODE_ENV") == "test":
            sorted_matches = sorted(raw_lines)
        else:
            indexed = sorted(
                range(len(stat_pairs)),
                key=lambda i: (-stat_pairs[i][1], stat_pairs[i][0]),
            )
            sorted_matches = [stat_pairs[i][0] for i in indexed]

        limited, applied = _apply_head_limit(sorted_matches, input.head_limit, offset)
        relative = [_to_relative_path(m) for m in limited]
        return GrepOutput(
            mode="files_with_matches",
            filenames=relative,
            num_files=len(relative),
            applied_limit=applied,
            applied_offset=offset if offset > 0 else None,
        )

    @classmethod
    def _fallback_grep(cls, input: GrepInput, search_path: str) -> GrepOutput:
        flags = re.IGNORECASE if input.case_insensitive else 0
        try:
            compiled = re.compile(input.pattern, flags)
        except re.error:
            return GrepOutput(
                mode=input.output_mode,
                filenames=[],
                content="Invalid regex pattern",
            )

        glob_patterns = _parse_glob_patterns(input.glob)
        glob_patterns.extend(_normalize_exclude_glob_patterns(input.exclude))
        mode = input.output_mode or "files_with_matches"
        offset = input.offset or 0

        matches_by_path: dict[str, List[str]] = {}
        for full_path in _iter_search_files(search_path):
            if not _matches_glob_pattern(full_path, search_path, glob_patterns):
                continue

            try:
                with open(full_path, errors="ignore") as handle:
                    matched_lines: List[str] = []
                    for line_number, line in enumerate(handle, start=1):
                        if not compiled.search(line):
                            continue
                        matched_lines.append(
                            "{}:{}:{}".format(
                                _to_relative_path(full_path),
                                line_number,
                                line.rstrip("\n"),
                            )
                        )
            except (OSError, PermissionError):
                continue

            if matched_lines:
                matches_by_path[full_path] = matched_lines

        sorted_paths = _sort_match_paths(list(matches_by_path))

        if mode == "content":
            content_lines = [
                line for path in sorted_paths for line in matches_by_path[path]
            ]
            content, line_count, applied = _format_content_lines(
                content_lines,
                input.head_limit,
                offset,
            )
            return GrepOutput(
                mode="content",
                num_files=0,
                filenames=[],
                content=content,
                num_lines=line_count,
                applied_limit=applied,
                applied_offset=offset if offset > 0 else None,
            )

        if mode == "count":
            count_entries = [
                (_to_relative_path(path), len(matches_by_path[path]))
                for path in sorted_paths
            ]
            limited_entries, applied = _apply_head_limit(
                count_entries,
                input.head_limit,
                offset,
            )
            total = sum(count for _, count in limited_entries)
            return GrepOutput(
                mode="count",
                num_files=len(limited_entries),
                filenames=[],
                content="\n".join(
                    "{}:{}".format(path, count) for path, count in limited_entries
                ),
                num_matches=total,
                applied_limit=applied,
                applied_offset=offset if offset > 0 else None,
            )

        # files_with_matches (default)
        relative = [_to_relative_path(path) for path in sorted_paths]
        limited, applied = _apply_head_limit(relative, input.head_limit, offset)
        return GrepOutput(
            mode="files_with_matches",
            filenames=limited,
            num_files=len(limited),
            applied_limit=applied,
            applied_offset=offset if offset > 0 else None,
        )

    @staticmethod
    def map_result(output: GrepOutput) -> str:
        mode = output.mode or "files_with_matches"
        if mode == "content":
            limit_info = ""
            if output.applied_limit is not None:
                limit_info = f"limit: {output.applied_limit}"
            if output.applied_offset:
                limit_info += f", offset: {output.applied_offset}"
            content = output.content or "No matches found"
            if limit_info:
                content += f"\n\n[Showing results with pagination = {limit_info}]"
            return content

        if mode == "count":
            mc = output.num_matches or 0
            fc = output.num_files or 0
            occ = "occurrence" if mc == 1 else "occurrences"
            ff = "file" if fc == 1 else "files"
            summary = f"\n\nFound {mc} total {occ} across {fc} {ff}."
            return (output.content or "No matches found") + summary

        # files_with_matches
        if not output.filenames:
            return "No files found"
        fc = output.num_files or 0
        ff = "file" if fc == 1 else "files"
        lines = [f"Found {fc} {ff}"]
        lines.extend(output.filenames)
        return "\n".join(lines)
