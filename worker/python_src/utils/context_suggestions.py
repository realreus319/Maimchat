"""Context optimization suggestions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextSuggestion:
    kind: str
    message: str
    severity: str = "info"
    token_savings_estimate: int | None = None


def build_context_suggestions(
    *,
    input_tokens: int,
    max_context_tokens: int,
    message_count: int,
    file_count: int = 0,
) -> tuple[ContextSuggestion, ...]:
    if max_context_tokens <= 0:
        return ()
    ratio = max(0.0, input_tokens / max_context_tokens)
    suggestions: list[ContextSuggestion] = []
    if ratio >= 0.9:
        suggestions.append(
            ContextSuggestion(
                kind="compact",
                message="Context is almost full; run compaction before continuing.",
                severity="high",
                token_savings_estimate=max(input_tokens // 3, 1),
            )
        )
    elif ratio >= 0.72:
        suggestions.append(
            ContextSuggestion(
                kind="summarize",
                message="Context usage is high; summarize completed work or compact soon.",
                severity="medium",
                token_savings_estimate=max(input_tokens // 5, 1),
            )
        )
    if message_count >= 80:
        suggestions.append(
            ContextSuggestion(
                kind="thread_split",
                message="The session has many turns; start a focused follow-up after summarizing decisions.",
                severity="medium",
            )
        )
    if file_count >= 20:
        suggestions.append(
            ContextSuggestion(
                kind="file_focus",
                message="Many files are referenced; narrow active context to the files needed for the next step.",
                severity="low",
            )
        )
    return tuple(suggestions)


# TS-compatible alias.
buildContextSuggestions = build_context_suggestions
