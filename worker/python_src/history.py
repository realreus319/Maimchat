"""Prompt history persistence via JSONL.

Python port of src/history.ts (Task 28).

Manages a JSONL file at ``~/.claude_py/history.jsonl`` where each line is a
JSON object representing a prompt-history entry.  Entries are appended
atomically and read in reverse order (newest first).

Key parity contracts with the TS source:
- ``MAX_HISTORY_ITEMS = 100`` — upper bound on yielded entries.
- ``addToHistory()`` appends to a pending buffer and asynchronously flushes.
- ``removeLastFromHistory()`` is one-shot: pops from pending buffer or
  records the flushed timestamp in a skip-set.
- ``getHistory()`` yields current-session entries first, then other sessions.
- ``clearPendingHistoryEntries()`` resets in-memory state.
- Malformed JSONL lines are silently skipped (no crash on corrupt artifacts).
"""

from __future__ import annotations

import json
import os
import hashlib
import time
from typing import Any, Dict, Generator, List, Optional, Set

from .utils.config import get_claude_config_home

MAX_HISTORY_ITEMS = 100
MAX_PASTED_CONTENT_LENGTH = 1024

_StoredPastedContent = Dict[str, Any]
_HistoryEntry = Dict[str, Any]
_LogEntry = Dict[str, Any]


def get_pasted_text_ref_num_lines(text: str) -> int:
    return text.count("\n") + text.count("\r\n") + text.count("\r")


def format_pasted_text_ref(entry_id: int, num_lines: int) -> str:
    if num_lines == 0:
        return f"[Pasted text #{entry_id}]"
    return f"[Pasted text #{entry_id} +{num_lines} lines]"


def format_image_ref(entry_id: int) -> str:
    return f"[Image #{entry_id}]"


def parse_references(
    input_text: str,
) -> List[Dict[str, Any]]:
    import re

    pattern = (
        r"\[(Pasted text|Image|\.\.\.Truncated text) #(\d+)(?: \+\d+ lines)?(\.)*\]"
    )
    matches = list(re.finditer(pattern, input_text))
    results = []
    for match in matches:
        entry_id = int(match.group(2) or "0")
        if entry_id > 0:
            results.append(
                {
                    "id": entry_id,
                    "match": match.group(0),
                    "index": match.start(),
                }
            )
    return results


def expand_pasted_text_refs(
    input_text: str,
    pasted_contents: Dict[int, _HistoryEntry],
) -> str:
    refs = parse_references(input_text)
    expanded = input_text
    for i in range(len(refs) - 1, -1, -1):
        ref = refs[i]
        content = pasted_contents.get(ref["id"])
        if content is None or content.get("type") != "text":
            continue
        expanded = (
            expanded[: ref["index"]]
            + content.get("content", "")
            + expanded[ref["index"] + len(ref["match"]) :]
        )
    return expanded


class HistoryStore:
    """Manages prompt-history persistence in a JSONL file.

    Mirrors the module-level state pattern from ``src/history.ts``:
    - ``_pending_entries`` — buffer of entries not yet flushed to disk.
    - ``_skipped_timestamps`` — timestamps of flushed entries to skip on read.
    - ``_last_added_entry`` — tracks the most recent addition for one-shot undo.
    """

    def __init__(
        self,
        config_home: str | None = None,
        project_root: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._config_home = config_home or get_claude_config_home()
        self._project_root = project_root or os.getcwd()
        self._session_id = session_id or "default-session"
        self._pending_entries: List[_LogEntry] = []
        self._skipped_timestamps: Set[int] = set()
        self._last_added_entry: Optional[_LogEntry] = None
        self._is_writing = False
        self._last_timestamp_ms = 0

    @property
    def history_path(self) -> str:
        return os.path.join(self._config_home, "history.jsonl")

    def add_to_history(
        self,
        command: str | Dict[str, Any],
    ) -> None:
        if os.environ.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY"):
            return

        entry = (
            {"display": command, "pastedContents": {}}
            if isinstance(command, str)
            else command
        )

        stored_pasted: Dict[int, _StoredPastedContent] = {}
        pasted = entry.get("pastedContents") or {}
        for raw_id, content in pasted.items():
            content_id = int(raw_id)
            if content.get("type") == "image":
                continue
            text = content.get("content", "")
            if len(text) <= MAX_PASTED_CONTENT_LENGTH:
                stored_pasted[content_id] = {
                    "id": content_id,
                    "type": content.get("type", "text"),
                    "content": text,
                    "mediaType": content.get("mediaType"),
                    "filename": content.get("filename"),
                }
            else:
                stored_pasted[content_id] = {
                    "id": content_id,
                    "type": content.get("type", "text"),
                    "contentHash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "mediaType": content.get("mediaType"),
                    "filename": content.get("filename"),
                }

        timestamp = int(time.time() * 1000)
        if timestamp <= self._last_timestamp_ms:
            timestamp = self._last_timestamp_ms + 1
        self._last_timestamp_ms = timestamp

        log_entry: _LogEntry = {
            "display": entry.get("display", ""),
            "pastedContents": stored_pasted,
            "timestamp": timestamp,
            "project": self._project_root,
            "sessionId": self._session_id,
        }

        self._pending_entries.append(log_entry)
        self._last_added_entry = log_entry

    def flush(self) -> None:
        if not self._pending_entries:
            return
        path = self.history_path
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)

        lines = []
        for entry in self._pending_entries:
            lines.append(json.dumps(entry, sort_keys=True))

        self._pending_entries.clear()

        for _attempt in range(5):
            try:
                with open(path, "a", encoding="utf-8") as f:
                    for line in lines:
                        f.write(line + "\n")
                return
            except OSError:
                time.sleep(0.5)

    def remove_last_from_history(self) -> None:
        if self._last_added_entry is None:
            return
        entry = self._last_added_entry
        self._last_added_entry = None

        try:
            idx = self._pending_entries.index(entry)
            self._pending_entries.pop(idx)
        except ValueError:
            self._skipped_timestamps.add(entry.get("timestamp", 0))

    def clear_pending_entries(self) -> None:
        self._pending_entries.clear()
        self._last_added_entry = None
        self._skipped_timestamps.clear()

    def read_log_entries_reverse(self) -> Generator[_LogEntry, None, None]:
        for entry in reversed(self._pending_entries):
            yield entry

        path = self.history_path
        try:
            with open(path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            return

        for line in reversed(all_lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            if (
                raw_entry.get("sessionId") == self._session_id
                and raw_entry.get("timestamp") in self._skipped_timestamps
            ):
                continue

            yield raw_entry

    def get_history(self) -> Generator[_HistoryEntry, None, None]:
        current_session: List[_LogEntry] = []
        other_session: List[_LogEntry] = []
        count = 0

        for entry in self.read_log_entries_reverse():
            if not entry or not isinstance(entry.get("project"), str):
                continue
            if entry["project"] != self._project_root:
                continue

            if entry.get("sessionId") == self._session_id:
                current_session.append(entry)
            else:
                other_session.append(entry)

            count += 1
            if count >= MAX_HISTORY_ITEMS:
                break

        for entry in current_session:
            yield self._log_entry_to_history_entry(entry)

        for entry in other_session:
            yield self._log_entry_to_history_entry(entry)

    def _log_entry_to_history_entry(self, entry: _LogEntry) -> _HistoryEntry:
        return {
            "display": entry.get("display", ""),
            "pastedContents": entry.get("pastedContents", {}),
        }
