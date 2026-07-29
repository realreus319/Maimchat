"""Local prompt suggestion helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PromptSuggestion:
    text: str
    source: str = "local"
    score: float = 0.0
    description: str | None = None


def generate_prompt_suggestions(
    history: Sequence[str],
    *,
    prefix: str = "",
    commands: Sequence[str] = (),
    limit: int = 5,
) -> tuple[PromptSuggestion, ...]:
    normalized_prefix = prefix.strip().casefold()
    candidates: list[PromptSuggestion] = []
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            continue
        if normalized_prefix and not command.casefold().startswith(normalized_prefix):
            continue
        candidates.append(PromptSuggestion(text=command, source="command", score=0.9))

    seen = {candidate.text for candidate in candidates}
    for item in reversed(history):
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if len(text) < 8 or text in seen:
            continue
        if normalized_prefix and not text.casefold().startswith(normalized_prefix):
            continue
        seen.add(text)
        candidates.append(PromptSuggestion(text=text, source="history", score=0.6))
        if len(candidates) >= max(limit, 0):
            break
    return tuple(candidates[: max(limit, 0)])


def accept_prompt_suggestion(
    suggestion: PromptSuggestion,
    *,
    current_input: str = "",
) -> str:
    if not current_input:
        return suggestion.text
    if suggestion.text.startswith(current_input):
        return suggestion.text
    return current_input.rstrip() + " " + suggestion.text


# TS-compatible aliases.
generatePromptSuggestions = generate_prompt_suggestions
acceptPromptSuggestion = accept_prompt_suggestion
