from __future__ import annotations

import os
from typing import Any, Mapping

from ..bootstrap import getInitialMainLoopModel, getMainLoopModelOverride

DEFAULT_MAIN_LOOP_MODEL = "claude-sonnet-4-5"
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "max"})


def resolve_main_loop_model(
    *,
    app_state: object | None = None,
    settings: Mapping[str, Any] | None = None,
    fallback: str = DEFAULT_MAIN_LOOP_MODEL,
) -> str:
    resolved_settings = settings
    if resolved_settings is None and app_state is not None:
        candidate_settings = getattr(app_state, "settings", None)
        if isinstance(candidate_settings, Mapping):
            resolved_settings = candidate_settings

    candidates: tuple[object | None, ...] = (
        getattr(app_state, "main_loop_model_for_session", None),
        getattr(app_state, "main_loop_model", None),
        resolved_settings.get("model") if isinstance(resolved_settings, Mapping) else None,
        os.environ.get("ANTHROPIC_MODEL"),
        os.environ.get("OPENAI_MODEL"),
        os.environ.get("DEEPSEEK_MODEL"),
        resolved_settings.get("defaultModel")
        if isinstance(resolved_settings, Mapping)
        else None,
        os.environ.get("ANTHROPIC_DEFAULT_MODEL"),
        os.environ.get("OPENAI_DEFAULT_MODEL"),
        os.environ.get("DEEPSEEK_DEFAULT_MODEL"),
        getMainLoopModelOverride(),
        getInitialMainLoopModel(),
        fallback,
    )

    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip()
            if normalized:
                return normalized
    return fallback


def resolve_fallback_model(
    *,
    app_state: object | None = None,
    settings: Mapping[str, Any] | None = None,
    primary_model: str | None = None,
    fallback: str | None = None,
) -> str | None:
    resolved_settings = settings
    if resolved_settings is None and app_state is not None:
        candidate_settings = getattr(app_state, "settings", None)
        if isinstance(candidate_settings, Mapping):
            resolved_settings = candidate_settings

    candidates: tuple[object | None, ...] = (
        os.environ.get("CLAUDE_CODE_FALLBACK_MODEL"),
        getattr(app_state, "fallback_model_for_session", None),
        getattr(app_state, "fallback_model", None),
        resolved_settings.get("fallbackModel")
        if isinstance(resolved_settings, Mapping)
        else None,
        resolved_settings.get("fallback_model")
        if isinstance(resolved_settings, Mapping)
        else None,
        os.environ.get("ANTHROPIC_FALLBACK_MODEL"),
        fallback,
    )
    normalized_primary = _normalize_model_identity(primary_model)
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        normalized = candidate.strip()
        if not normalized:
            continue
        if normalized_primary and _normalize_model_identity(normalized) == normalized_primary:
            continue
        return normalized
    return None


def normalize_model_string_for_api(model: str) -> str:
    return model.replace("[1m]", "").replace("[2m]", "").replace("[1M]", "").replace("[2M]", "")


def model_supports_effort(model: str) -> bool:
    normalized = normalize_model_string_for_api(model).strip().lower()
    if not normalized:
        return False
    if _env_truthy("CLAUDE_CODE_ALWAYS_ENABLE_EFFORT"):
        return True
    if "opus-4-6" in normalized or "sonnet-4-6" in normalized:
        return True
    if any(token in normalized for token in ("haiku", "sonnet", "opus")):
        return False
    return True


def model_supports_adaptive_thinking(model: str) -> bool:
    normalized = normalize_model_string_for_api(model).strip().lower()
    if not normalized:
        return False
    if "opus-4-6" in normalized or "sonnet-4-6" in normalized:
        return True
    if any(token in normalized for token in ("haiku", "sonnet", "opus")):
        return False
    return True


def is_valid_effort_level(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _EFFORT_LEVELS


def default_thinking_enabled(*, settings: Mapping[str, Any] | None = None) -> bool:
    raw_budget = os.environ.get("MAX_THINKING_TOKENS")
    if isinstance(raw_budget, str) and raw_budget.strip():
        try:
            return int(raw_budget) > 0
        except (TypeError, ValueError):
            return True
    if isinstance(settings, Mapping) and settings.get("alwaysThinkingEnabled") is False:
        return False
    return True


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name)
    return isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_model_identity(model: str | None) -> str:
    if not isinstance(model, str):
        return ""
    return normalize_model_string_for_api(model).strip().lower()
