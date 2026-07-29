from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..services.api.transport import pooled_urlopen as urlopen
from ..services.api.with_retry import (
    RetrySettings,
    build_effective_retry_settings,
    classify_retryable_error,
    format_request_failure,
    persistent_retry_enabled,
    retry_window_exhausted,
    sleep_for_retry_blocking,
)
from ..utils.config import (
    get_anthropic_api_key,
    get_anthropic_auth_token,
    get_anthropic_base_url,
    get_api_timeout_ms,
)
from ..utils.model_selection import resolve_fallback_model


_DEFAULT_SMALL_FAST_MODEL = "claude-3-5-haiku-latest"
_SIDEQUERY_RETRY_SETTINGS = RetrySettings()


def resolve_small_fast_model(preferred_model: str | None = None) -> str:
    configured = os.environ.get("ANTHROPIC_SMALL_FAST_MODEL")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if isinstance(preferred_model, str) and preferred_model.strip():
        return preferred_model.strip()
    return _DEFAULT_SMALL_FAST_MODEL


def resolve_message_model(preferred_model: str | None = None) -> str:
    if isinstance(preferred_model, str) and preferred_model.strip():
        return preferred_model.strip()
    return resolve_small_fast_model()


def send_anthropic_message(
    *,
    user_prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    tool_choice: Mapping[str, Any] | None = None,
    beta_headers: Sequence[str] | None = None,
    max_tokens: int = 1024,
    background_request: bool = False,
    request_timeout_seconds: float | None = None,
    retry_settings: RetrySettings | None = None,
    persistent_retries: bool | None = None,
) -> dict[str, Any]:
    api_key = get_anthropic_api_key()
    auth_token = get_anthropic_auth_token()
    if api_key is None and auth_token is None:
        raise RuntimeError("Anthropic credentials are required for this tool path")
    if request_timeout_seconds is not None and request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")

    primary_model = resolve_message_model(model)
    fallback_model = resolve_fallback_model(primary_model=primary_model)
    request_models = (
        (primary_model, fallback_model)
        if fallback_model is not None
        else (primary_model,)
    )
    persistent_mode = (
        persistent_retry_enabled()
        if persistent_retries is None
        else persistent_retries
    )
    effective_retry_settings = build_effective_retry_settings(
        retry_settings or _SIDEQUERY_RETRY_SETTINGS,
        persistent_mode=persistent_mode,
    )
    timeout_seconds = get_api_timeout_ms() / 1000
    if request_timeout_seconds is not None:
        timeout_seconds = min(timeout_seconds, request_timeout_seconds)
    started_at = time.monotonic()
    attempt = 0
    model_attempts_529: dict[str, int] = {candidate: 0 for candidate in request_models}
    current_model_index = 0
    while current_model_index < len(request_models):
        active_model = request_models[current_model_index]
        request_body: dict[str, Any] = {
            "model": active_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_prompt}],
            "stream": False,
        }
        if isinstance(system_prompt, str) and system_prompt.strip():
            request_body["system"] = system_prompt
        if tools:
            request_body["tools"] = list(tools)
        if tool_choice:
            request_body["tool_choice"] = dict(tool_choice)

        request = Request(
            f"{get_anthropic_base_url().rstrip('/')}/v1/messages",
            data=json.dumps(request_body).encode("utf-8"),
            headers=_build_headers(
                api_key=api_key,
                auth_token=auth_token,
                beta_headers=beta_headers,
            ),
            method="POST",
        )
        attempt += 1
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            break
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            decision = classify_retryable_error(exc)
            if decision.status_code == 529 and background_request:
                raise RuntimeError(
                    format_request_failure(
                        "Anthropic-compatible background request dropped to avoid retry amplification",
                        exc,
                        decision=decision,
                        attempt=attempt,
                        max_attempts=effective_retry_settings.max_attempts,
                        active_model=active_model,
                        request_models=request_models,
                        persistent_mode=persistent_mode,
                    )
                ) from exc
            if not decision.retryable:
                raise RuntimeError(
                    format_request_failure(
                        "Anthropic-compatible request failed",
                        exc,
                        decision=decision,
                        attempt=attempt,
                        max_attempts=effective_retry_settings.max_attempts,
                        active_model=active_model,
                        request_models=request_models,
                        persistent_mode=persistent_mode,
                    )
                ) from exc
            if decision.status_code == 529:
                model_attempts_529[active_model] = (
                    model_attempts_529.get(active_model, 0) + 1
                )
            else:
                model_attempts_529[active_model] = 0
            if (
                current_model_index == 0
                and current_model_index + 1 < len(request_models)
                and model_attempts_529.get(active_model, 0) >= 3
            ):
                current_model_index += 1
                continue
            if retry_window_exhausted(
                attempt,
                settings=effective_retry_settings,
                started_at=started_at,
                retry_after_seconds=decision.retry_after_seconds,
            ):
                raise RuntimeError(
                    format_request_failure(
                        "Anthropic-compatible request failed",
                        exc,
                        decision=decision,
                        attempt=attempt,
                        max_attempts=effective_retry_settings.max_attempts,
                        active_model=active_model,
                        request_models=request_models,
                        persistent_mode=persistent_mode,
                    )
                ) from exc
            sleep_for_retry_blocking(
                attempt,
                settings=effective_retry_settings,
                retry_after_seconds=decision.retry_after_seconds,
            )
    else:  # pragma: no cover - defensive
        raise RuntimeError("Anthropic-compatible request retry loop exhausted")

    if not isinstance(payload, dict):
        raise RuntimeError("Anthropic-compatible request returned a non-object payload")
    return payload


def extract_first_text_block(payload: Mapping[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _build_headers(
    *,
    api_key: str | None,
    auth_token: str | None,
    beta_headers: Sequence[str] | None,
) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "accept": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    joined_betas = ",".join(
        beta.strip() for beta in beta_headers or () if isinstance(beta, str) and beta.strip()
    )
    if joined_betas:
        headers["anthropic-beta"] = joined_betas
    return headers


__all__ = [
    "extract_first_text_block",
    "resolve_message_model",
    "resolve_small_fast_model",
    "send_anthropic_message",
]
