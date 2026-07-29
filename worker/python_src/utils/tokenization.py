from __future__ import annotations

import importlib
import re
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _load_tiktoken_module() -> Any | None:
    try:
        return importlib.import_module("tiktoken")
    except ImportError:
        return None


@lru_cache(maxsize=16)
def _load_token_encoding_cached(normalized_model: str) -> Any | None:
    tiktoken = _load_tiktoken_module()
    if tiktoken is None:
        return None

    if normalized_model:
        try:
            return tiktoken.encoding_for_model(normalized_model)
        except Exception:
            pass

    for encoding_name in ("o200k_base", "cl100k_base"):
        try:
            return tiktoken.get_encoding(encoding_name)
        except Exception:
            continue
    return None


def load_token_encoding(model: str | None = None) -> Any | None:
    return _load_token_encoding_cached((model or "").strip())


def count_text_tokens(text: str, *, model: str | None = None) -> int:
    if not text:
        return 0
    encoding = load_token_encoding(model)
    if encoding is not None:
        try:
            tokens = encoding.encode(text, disallowed_special=())
        except TypeError:
            tokens = encoding.encode(text)
        return len(tokens)
    return estimate_token_count_without_tiktoken(text)


_FALLBACK_TOKEN_RE = re.compile(
    r"[\u3400-\u9fff]|[A-Za-z_][A-Za-z0-9_']*|\d+(?:\.\d+)?|[^\s\w]",
    re.UNICODE,
)


def estimate_token_count_without_tiktoken(text: str) -> int:
    if not text:
        return 0
    tokens = _FALLBACK_TOKEN_RE.findall(text)
    if not tokens:
        return 0
    long_token_extra = sum(max(0, (len(token) - 12) // 8) for token in tokens)
    return max(1, len(tokens) + long_token_extra)


def clear_tokenizer_cache() -> None:
    _load_token_encoding_cached.cache_clear()
    _load_tiktoken_module.cache_clear()
