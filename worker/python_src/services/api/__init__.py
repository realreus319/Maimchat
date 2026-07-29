"""API service helpers for model/network integrations."""

from .errors import (
    API_ERROR_MESSAGE_PREFIX,
    PROMPT_TOO_LONG_ERROR_MESSAGE,
    APIErrorClassification,
    classify_api_error,
    extract_connection_error_details,
    format_api_error,
    get_ssl_error_hint,
)

__all__ = [
    "API_ERROR_MESSAGE_PREFIX",
    "APIErrorClassification",
    "PROMPT_TOO_LONG_ERROR_MESSAGE",
    "classify_api_error",
    "extract_connection_error_details",
    "format_api_error",
    "get_ssl_error_hint",
]
