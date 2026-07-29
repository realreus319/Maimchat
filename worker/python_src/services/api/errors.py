"""Central API error classification and diagnostics."""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from typing import Mapping
from urllib.error import HTTPError, URLError


PROMPT_TOO_LONG_ERROR_MESSAGE = "Prompt is too long for the selected model's context window."
API_ERROR_MESSAGE_PREFIX = "API request failed"
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
AUTH_STATUS_CODES = frozenset({401, 403})
CLIENT_STATUS_CODES = frozenset({400, 404, 413, 422})


@dataclass(frozen=True)
class APIErrorClassification:
    category: str
    retryable: bool
    status_code: int | None = None
    detail: str | None = None
    hint: str | None = None


def classify_api_error(exc: BaseException) -> APIErrorClassification:
    if isinstance(exc, HTTPError):
        body_text = _read_http_body(exc)
        detail = extract_error_detail(body_text) or str(exc.reason or "")
        return APIErrorClassification(
            category=classify_http_status(exc.code, detail=detail),
            retryable=exc.code in RETRYABLE_STATUS_CODES,
            status_code=exc.code,
            detail=detail,
            hint=_http_hint(exc.code, detail),
        )

    if isinstance(exc, URLError):
        reason = exc.reason
        detail = extract_connection_error_details(reason)
        return APIErrorClassification(
            category=classify_connection_error(reason),
            retryable=True,
            detail=detail,
            hint=get_ssl_error_hint(reason),
        )

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return APIErrorClassification(
            category="connection_timeout",
            retryable=True,
            detail=str(exc),
            hint="Check network connectivity or increase request timeout.",
        )

    return APIErrorClassification(
        category="unknown_error",
        retryable=False,
        detail=str(exc) or exc.__class__.__name__,
    )


def classify_http_status(status_code: int, *, detail: str | None = None) -> str:
    normalized = (detail or "").casefold()
    if status_code == 400 and "prompt" in normalized and "long" in normalized:
        return "prompt_too_long"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_denied"
    if status_code == 404:
        return "not_found"
    if status_code == 408:
        return "request_timeout"
    if status_code == 409:
        return "conflict_retry"
    if status_code == 425:
        return "too_early"
    if status_code == 413:
        return "prompt_too_long"
    if status_code == 429:
        if "quota window" in normalized:
            return "quota_window"
        if any(marker in normalized for marker in ("quota", "credit", "billing")):
            return "quota_exceeded"
        return "rate_limited"
    if status_code == 529:
        return "upstream_overloaded"
    if 500 <= status_code <= 599:
        return "upstream_server_error"
    if status_code in CLIENT_STATUS_CODES:
        return "client_error"
    return f"http_{status_code}"


def classify_connection_error(reason: object) -> str:
    if isinstance(reason, ssl.SSLError):
        return "ssl_error"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "connection_timeout"
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    normalized = str(reason).casefold()
    if "timed out" in normalized or "timeout" in normalized:
        return "connection_timeout"
    if "name or service not known" in normalized or "nodename nor servname" in normalized:
        return "dns_error"
    if "certificate" in normalized or "ssl" in normalized:
        return "ssl_error"
    if "refused" in normalized:
        return "connection_refused"
    return "transport_error"


def extract_connection_error_details(reason: object) -> str:
    if isinstance(reason, OSError) and getattr(reason, "errno", None) is not None:
        return f"{reason.__class__.__name__} errno={reason.errno}: {reason}"
    return str(reason)


def get_ssl_error_hint(reason: object) -> str | None:
    if not isinstance(reason, ssl.SSLError) and "ssl" not in str(reason).casefold() and "certificate" not in str(reason).casefold():
        return None
    return (
        "TLS validation failed. Verify the system CA bundle, proxy TLS interception, "
        "or custom CA configuration."
    )


def extract_error_detail(body_text: str) -> str | None:
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
                message = value.get("message") or value.get("type")
                if isinstance(message, str) and message.strip():
                    return message.strip()
    return trimmed


def format_api_error(exc: BaseException, *, prefix: str = API_ERROR_MESSAGE_PREFIX) -> str:
    classification = classify_api_error(exc)
    pieces = [f"{prefix}: {classification.detail or exc}"]
    context = [f"category={classification.category}"]
    if classification.status_code is not None:
        context.append(f"status={classification.status_code}")
    context.append(f"retryable={'true' if classification.retryable else 'false'}")
    pieces.append("[" + ", ".join(context) + "]")
    if classification.hint:
        pieces.append(classification.hint)
    return " ".join(pieces)


def _read_http_body(exc: HTTPError) -> str:
    cached = getattr(exc, "_claude_code_body_cache", None)
    if isinstance(cached, str):
        return cached
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    setattr(exc, "_claude_code_body_cache", body)
    return body


def _http_hint(status_code: int, detail: str | None) -> str | None:
    category = classify_http_status(status_code, detail=detail)
    if category == "prompt_too_long":
        return PROMPT_TOO_LONG_ERROR_MESSAGE
    if category == "authentication_error":
        return "Check API credentials and authentication environment variables."
    if category == "permission_denied":
        return "Check account permissions, model access, or organization policy."
    if category in {"rate_limited", "quota_exceeded"}:
        return "Retry after the quota window or reduce request concurrency."
    return None


# TS-compatible aliases.
classifyAPIError = classify_api_error
extractConnectionErrorDetails = extract_connection_error_details
getSSLErrorHint = get_ssl_error_hint
