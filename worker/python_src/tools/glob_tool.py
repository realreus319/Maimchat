from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional


GLOB_TOOL_NAME = "Glob"
GLOB_MAX_RESULTS = 100
_VCS_DIRECTORIES = {".git", ".svn", ".hg", ".bzr", ".jj", ".sl"}


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


def _filter_gitignored_matches(
    filepaths: List[str],
    base_path: str,
) -> List[str]:
    if not filepaths:
        return []

    candidates: list[tuple[str, str]] = []
    for filepath in filepaths:
        try:
            candidate = os.path.relpath(filepath, start=base_path)
        except ValueError:
            candidate = filepath
        candidates.append((filepath, candidate))

    try:
        completed = subprocess.run(
            ["git", "-C", base_path, "check-ignore", "--stdin"],
            input="\n".join(candidate for _, candidate in candidates),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return list(filepaths)

    if completed.returncode not in {0, 1}:
        return list(filepaths)

    ignored = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    if not ignored:
        return list(filepaths)
    return [
        filepath
        for filepath, candidate in candidates
        if candidate not in ignored
    ]


@dataclass(frozen=True)
class GlobInput:
    pattern: str
    path: Optional[str] = None


@dataclass(frozen=True)
class GlobOutput:
    filenames: List[str]
    duration_ms: float
    num_files: int
    truncated: bool


class GlobTool:
    name: str = GLOB_TOOL_NAME

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
        if path is not None:
            if not isinstance(path, str):
                return (False, dict(raw_input))
            absolute_path = _expand_path(path)
            if not os.path.exists(absolute_path):
                return (False, dict(raw_input))
            if not os.path.isdir(absolute_path):
                return (False, dict(raw_input))
        return (True, dict(raw_input))

    @staticmethod
    def interrupt_behavior() -> str:
        return "cancel"

    @staticmethod
    def get_path(input: GlobInput) -> str:
        if input.path:
            return _expand_path(input.path)
        return os.getcwd()

    @classmethod
    def call(
        cls,
        input: GlobInput,
        abort_signal: Optional[Any] = None,
        max_results: int = GLOB_MAX_RESULTS,
    ) -> GlobOutput:
        start = time.monotonic()
        base_path = cls.get_path(input)

        if abort_signal is not None and getattr(abort_signal, "aborted", False):
            return GlobOutput(
                filenames=[],
                duration_ms=(time.monotonic() - start) * 1000,
                num_files=0,
                truncated=False,
            )

        matched_files: List[str] = []
        pattern = input.pattern

        try:
            p = Path(base_path)
            for match in p.glob(pattern):
                if abort_signal is not None and getattr(abort_signal, "aborted", False):
                    break
                rel = os.path.relpath(str(match), start=base_path)
                parts = rel.split(os.sep)
                if any(part in _VCS_DIRECTORIES for part in parts):
                    continue
                if match.is_file():
                    matched_files.append(str(match))
        except (OSError, PermissionError):
            pass

        matched_files = _filter_gitignored_matches(matched_files, base_path)

        if os.environ.get("NODE_ENV") == "test":
            matched_files.sort()
        else:
            try:
                matched_files.sort(
                    key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0,
                    reverse=True,
                )
            except OSError:
                matched_files.sort()

        truncated = len(matched_files) > max_results
        result_files = matched_files[:max_results]
        filenames = [_to_relative_path(f, base_path) for f in result_files]

        return GlobOutput(
            filenames=filenames,
            duration_ms=(time.monotonic() - start) * 1000,
            num_files=len(filenames),
            truncated=truncated,
        )

    @staticmethod
    def map_result(output: GlobOutput) -> str:
        if not output.filenames:
            return "No files found"
        lines = list(output.filenames)
        if output.truncated:
            lines.append(
                "(Results are truncated. Consider using a more specific path or pattern.)"
            )
        return "\n".join(lines)
