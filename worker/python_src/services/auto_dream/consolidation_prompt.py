from __future__ import annotations

from pathlib import Path
from typing import Sequence


ENTRYPOINT_NAME = "CLAUDE.md"
MAX_ENTRYPOINT_LINES = 200


def build_consolidation_prompt(
    *,
    memory_dir: str,
    transcript_dir: str,
    sessions: Sequence[str],
) -> str:
    session_lines = "\n".join(f"- {session}" for session in sessions)
    entrypoint = str(Path(memory_dir) / ENTRYPOINT_NAME)
    return (
        "You are consolidating Claude Code auto memory.\n\n"
        f"Memory root: {memory_dir}\n"
        f"Memory entrypoint: {entrypoint}\n"
        f"Transcript directory: {transcript_dir}\n\n"
        "Review the following session transcripts and update memory files with "
        "durable facts, project decisions, user preferences, and recurring "
        "workflow details. Do not copy transient logs or secrets.\n\n"
        f"Sessions touched since the last consolidation:\n{session_lines}\n\n"
        "Rules:\n"
        f"- Keep {ENTRYPOINT_NAME} under {MAX_ENTRYPOINT_LINES} lines.\n"
        "- Prefer concise Markdown bullets and links to deeper files.\n"
        "- Preserve existing memory structure when it is still correct.\n"
        "- If a directory already exists, add focused files instead of merging "
        "unrelated topics.\n"
        "- Only write under the memory root.\n"
    )
