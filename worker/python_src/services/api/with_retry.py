from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError

from .errors import classify_api_error, extract_error_detail


RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0
    jitter_seconds: float = 0.25
    max_elapsed_seconds: float | None = None
    persistent_retry_mode: bool = False


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    retry_after_seconds: float | None = None
    status_code: int | None = None
    category: str | None = None
    detail: str | None = None


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUS_CODES


def classify_retryable_error(exc: BaseException) -> RetryDecision:
    classification = classify_api_error(exc)
    if isinstance(exc, HTTPError):
        body_text = _read_http_error_body(exc)
        retry_after_seconds = parse_retry_after_seconds(exc.headers)
        return RetryDecision(
            retryable=is_retryable_http_status(exc.code),
            retry_after_seconds=retry_after_seconds,
            status_code=exc.code,
            category=classification.category or _classify_http_error_category(
                exc.code,
                body_text=body_text,
                retry_after_seconds=retry_after_seconds,
            ),
            detail=classification.detail or extract_error_detail(body_text),
        )
    if isinstance(exc, URLError):
        return RetryDecision(
            retryable=classification.retryable,
            category=classification.category,
            detail=classification.detail,
        )
    return RetryDecision(retryable=False, category="non_retryable_error")


def persistent_retry_enabled(
    *,
    options: Mapping[str, object] | None = None,
    default: bool = False,
) -> bool:
    if isinstance(options, Mapping):
        option_value = options.get("persistent_mode")
        if isinstance(option_value, bool):
            return option_value
    env_value = os.environ.get("CLAUDE_CODE_PERSISTENT_MODE")
    if isinstance(env_value, str) and env_value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    if isinstance(env_value, str) and env_value.strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    return default


def build_effective_retry_settings(
    settings: RetrySettings,
    *,
    persistent_mode: bool,
) -> RetrySettings:
    if not persistent_mode:
        return settings
    max_attempts = _parse_positive_int(
        os.environ.get("CLAUDE_CODE_PERSISTENT_RETRY_MAX_ATTEMPTS")
    ) or max(settings.max_attempts, 10)
    max_delay_seconds = _parse_positive_float(
        os.environ.get("CLAUDE_CODE_PERSISTENT_RETRY_MAX_DELAY_SECONDS", "")
    ) or max(settings.max_delay_seconds, 60.0)
    max_elapsed_seconds = _parse_positive_float(
        os.environ.get("CLAUDE_CODE_PERSISTENT_RETRY_MAX_SECONDS", "")
    )
    if max_elapsed_seconds is None:
        max_elapsed_seconds = (
            settings.max_elapsed_seconds
            if settings.max_elapsed_seconds is not None
            else 6 * 60 * 60
        )
    return RetrySettings(
        max_attempts=max_attempts,
        base_delay_seconds=settings.base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
        jitter_seconds=settings.jitter_seconds,
        max_elapsed_seconds=max_elapsed_seconds,
        persistent_retry_mode=True,
    )


def retry_window_exhausted(
    attempt: int,
    *,
    settings: RetrySettings,
    started_at: float,
    retry_after_seconds: float | None = None,
) -> bool:
    if attempt >= settings.max_attempts:
        return True
    if settings.max_elapsed_seconds is None:
        return False
    next_delay = compute_retry_delay_seconds(
        attempt,
        settings=settings,
        retry_after_seconds=retry_after_seconds,
    )
    elapsed = max(0.0, time.monotonic() - started_at)
    return elapsed + next_delay > settings.max_elapsed_seconds


def parse_retry_after_seconds(headers: Optional[Mapping[str, object]]) -> float | None:
    if headers is None:
        return None
    raw_ms = headers.get("retry-after-ms")
    if isinstance(raw_ms, str):
        parsed_ms = _parse_positive_float(raw_ms)
        if parsed_ms is not None:
            return max(0.0, parsed_ms / 1000.0)
    raw = headers.get("retry-after")
    if not isinstance(raw, str):
        return None
    parsed_seconds = _parse_positive_float(raw)
    if parsed_seconds is not None:
        return parsed_seconds
    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def compute_retry_delay_seconds(
    attempt: int,
    *,
    settings: RetrySettings,
    retry_after_seconds: float | None = None,
) -> float:
    if retry_after_seconds is not None:
        delay = max(0.0, retry_after_seconds)
        # Persistent retry windows are allowed to honor long Retry-After values
        # (for example multi-minute or multi-hour quota recovery) instead of
        # collapsing them back to the short exponential backoff ceiling.
        if settings.max_elapsed_seconds is not None and delay > settings.max_delay_seconds:
            return delay
        return min(delay, settings.max_delay_seconds)
    exponent = max(attempt - 1, 0)
    base_delay = min(
        settings.base_delay_seconds * (2**exponent),
        settings.max_delay_seconds,
    )
    if settings.jitter_seconds <= 0:
        return base_delay
    jitter = random.uniform(0.0, settings.jitter_seconds)
    return min(base_delay + jitter, settings.max_delay_seconds)


async def sleep_for_retry(
    attempt: int,
    *,
    settings: RetrySettings,
    retry_after_seconds: float | None = None,
) -> None:
    await asyncio.sleep(
        compute_retry_delay_seconds(
            attempt,
            settings=settings,
            retry_after_seconds=retry_after_seconds,
        )
    )


def sleep_for_retry_blocking(
    attempt: int,
    *,
    settings: RetrySettings,
    retry_after_seconds: float | None = None,
) -> None:
    time.sleep(
        compute_retry_delay_seconds(
            attempt,
            settings=settings,
            retry_after_seconds=retry_after_seconds,
        )
    )


def format_request_failure(
    prefix: str,
    exc: BaseException,
    *,
    decision: RetryDecision | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
    active_model: str | None = None,
    request_models: Sequence[str] | None = None,
    persistent_mode: bool | None = None,
) -> str:
    if isinstance(exc, HTTPError):
        details = _read_http_error_body(exc) or exc.reason or "unknown HTTP error"
        message = f"{prefix} with HTTP {exc.code}: {details}"
        return _append_failure_context(
            message,
            decision=decision,
            attempt=attempt,
            max_attempts=max_attempts,
            active_model=active_model,
            request_models=request_models,
            persistent_mode=persistent_mode,
        )
    if isinstance(exc, URLError):
        message = f"{prefix}: {_stringify_url_reason(exc.reason)}"
        return _append_failure_context(
            message,
            decision=decision,
            attempt=attempt,
            max_attempts=max_attempts,
            active_model=active_model,
            request_models=request_models,
            persistent_mode=persistent_mode,
        )
    message = f"{prefix}: {exc}"
    return _append_failure_context(
        message,
        decision=decision,
        attempt=attempt,
        max_attempts=max_attempts,
        active_model=active_model,
        request_models=request_models,
        persistent_mode=persistent_mode,
    )


def _append_failure_context(
    message: str,
    *,
    decision: RetryDecision | None,
    attempt: int | None,
    max_attempts: int | None,
    active_model: str | None,
    request_models: Sequence[str] | None,
    persistent_mode: bool | None,
) -> str:
    context_parts: list[str] = []
    if isinstance(decision, RetryDecision) and isinstance(decision.category, str):
        context_parts.append(f"category={decision.category}")
    if isinstance(decision, RetryDecision) and decision.retry_after_seconds is not None:
        context_parts.append(
            f"retry_after={_format_retry_after(decision.retry_after_seconds)}"
        )
    if attempt is not None and max_attempts is not None:
        context_parts.append(f"attempt={attempt}/{max_attempts}")
    elif attempt is not None:
        context_parts.append(f"attempt={attempt}")
    if isinstance(active_model, str) and active_model.strip():
        model_context = active_model.strip()
        if request_models:
            candidate_total = len(request_models)
            try:
                candidate_index = request_models.index(active_model.strip()) + 1
            except ValueError:
                candidate_index = None
            if candidate_index is not None:
                model_context = f"{model_context} ({candidate_index}/{candidate_total})"
        context_parts.append(f"model={model_context}")
    if persistent_mode is not None:
        context_parts.append(
            f"persistent_mode={'true' if persistent_mode else 'false'}"
        )
    if not context_parts:
        return message
    return f"{message} [{', '.join(context_parts)}]"


def _read_http_error_body(exc: HTTPError) -> str:
    cached = getattr(exc, "_claude_code_body_cache", None)
    if isinstance(cached, str):
        return cached
    body_text = exc.read().decode("utf-8", "replace")
    setattr(exc, "_claude_code_body_cache", body_text)
    return body_text


def _classify_http_error_category(
    status_code: int,
    *,
    body_text: str,
    retry_after_seconds: float | None,
) -> str:
    normalized_detail = (_extract_error_detail(body_text) or body_text).lower()
    if status_code == 408:
        return "request_timeout"
    if status_code == 409:
        return "conflict_retry"
    if status_code == 425:
        return "too_early"
    if status_code == 429:
        if (
            "quota" in normalized_detail
            or "credit" in normalized_detail
            or "billing" in normalized_detail
            or (retry_after_seconds or 0.0) >= 300.0
        ):
            return "quota_window"
        return "rate_limited"
    if status_code == 529:
        return "upstream_overloaded"
    if status_code in {500, 502, 503, 504}:
        return "upstream_server_error"
    return f"http_{status_code}"


def _extract_error_detail(body_text: str) -> str | None:
    trimmed = body_text.strip()
    if not trimmed:
        return None
    try:
        payload = json.loads(trimmed)
    except (TypeError, ValueError):
        return trimmed
    if isinstance(payload, Mapping):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, Mapping):
                nested_message = value.get("message")
                if isinstance(nested_message, str) and nested_message.strip():
                    return nested_message.strip()
                nested_type = value.get("type")
                if isinstance(nested_type, str) and nested_type.strip():
                    return nested_type.strip()
    return trimmed


def _stringify_url_reason(reason: object) -> str:
    if isinstance(reason, str):
        return reason
    return str(reason)


def _format_retry_after(seconds: float) -> str:
    rounded = round(seconds, 3)
    if float(rounded).is_integer():
        return f"{int(rounded)}s"
    return f"{rounded}s"


def _parse_positive_float(raw: str) -> float | None:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _parse_positive_int(raw: str | None) -> int | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value
