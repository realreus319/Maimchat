"""Layered system prompt builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SystemPromptLayer:
    content: str
    name: str = "custom"
    mode: str = "append"
    priority: int = 0


def as_system_prompt(content: str | Sequence[str] | None) -> tuple[str, ...]:
    if content is None:
        return ()
    if isinstance(content, str):
        return (content,) if content.strip() else ()
    return tuple(item for item in content if isinstance(item, str) and item.strip())


def merge_system_prompt_segments(*segments: str | Sequence[str] | None) -> str | None:
    normalized: list[str] = []
    for segment in segments:
        normalized.extend(as_system_prompt(segment))
    if not normalized:
        return None
    return "\n\n".join(item.strip("\n") for item in normalized if item.strip())


def build_effective_system_prompt(
    *,
    override_system_prompt: str | Sequence[str] | None = None,
    coordinator_system_prompt: str | Sequence[str] | None = None,
    agent_system_prompt: str | Sequence[str] | None = None,
    custom_system_prompt: str | Sequence[str] | None = None,
    default_system_prompt: str | Sequence[str] | None = None,
    append_system_prompt: str | Sequence[str] | None = None,
    proactive: bool = False,
    agent_prompt_mode: str | None = None,
    layers: Iterable[SystemPromptLayer] | None = None,
) -> str | None:
    if override_system_prompt is not None:
        base = merge_system_prompt_segments(override_system_prompt)
    else:
        base_segments: list[str] = []
        base_segments.extend(as_system_prompt(coordinator_system_prompt))
        if agent_system_prompt is not None:
            replace_agent = agent_prompt_mode == "replace" or (
                proactive and agent_prompt_mode not in {"append", "prepend"}
            )
            if replace_agent:
                base_segments = list(as_system_prompt(agent_system_prompt))
            elif agent_prompt_mode == "prepend":
                base_segments = [
                    *as_system_prompt(agent_system_prompt),
                    *base_segments,
                ]
            else:
                base_segments.extend(as_system_prompt(agent_system_prompt))
        base_segments.extend(as_system_prompt(custom_system_prompt))
        if not base_segments:
            base_segments.extend(as_system_prompt(default_system_prompt))
        base = merge_system_prompt_segments(base_segments)

    if layers:
        ordered_layers = sorted(layers, key=lambda layer: layer.priority)
        for layer in ordered_layers:
            if not isinstance(layer.content, str) or not layer.content.strip():
                continue
            mode = layer.mode
            if mode == "replace":
                base = layer.content
            elif mode == "prepend":
                base = merge_system_prompt_segments(layer.content, base)
            else:
                base = merge_system_prompt_segments(base, layer.content)

    return merge_system_prompt_segments(base, append_system_prompt)


# TS-compatible aliases.
asSystemPrompt = as_system_prompt
buildEffectiveSystemPrompt = build_effective_system_prompt
mergeSystemPromptSegments = merge_system_prompt_segments
