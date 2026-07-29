from __future__ import annotations

import base64
import time
from html.parser import HTMLParser
from typing import Any, Sequence
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from .anthropic_sidequery import send_anthropic_message
from ..services.api.with_retry import RetrySettings


_WEB_SEARCH_BETA_HEADER = "web-search-2025-03-05"
_DEFAULT_SAFE_SEARCH = "active"
_DEFAULT_RESULT_FIELDS = ("snippet", "title", "link")
_SERVER_SEARCH_TIMEOUT_SECONDS = 10.0
_FALLBACK_SEARCH_TIMEOUT_SECONDS = 15.0
_SERVER_SEARCH_RETRY_SETTINGS = RetrySettings(
    max_attempts=1,
    max_elapsed_seconds=_SERVER_SEARCH_TIMEOUT_SECONDS,
)
_RECOVERABLE_WEB_SEARCH_ERRORS = (RuntimeError, TimeoutError, OSError)
_SAFE_SEARCH_TO_DDG_PARAM = {
    "active": "1",
    "moderate": "-1",
    "off": "-2",
}


class _BingHTMLParser(HTMLParser):
    # Parses Bing (cn.bing.com) organic results: <li class="b_algo"> with the title link in
    # <h2><a href> and the snippet in a <p>. Bing China is reachable behind the Great Firewall,
    # unlike DuckDuckGo (the previous fallback) which is blocked.
    def __init__(self) -> None:
        super().__init__()
        self._results: list[dict[str, str]] = []
        self._current_link: dict[str, str] | None = None
        self._in_h2 = False
        self._collect_title = False
        self._collect_snippet = False

    @property
    def results(self) -> list[dict[str, str]]:
        self._finalize_current_link()
        return self._results

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = attr_map.get("class", "") or ""
        if tag == "li" and "b_algo" in classes:
            self._finalize_current_link()
            self._current_link = {"title": "", "url": "", "snippet": ""}
            return
        if self._current_link is None:
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._current_link["url"]:
            href = attr_map.get("href") or ""
            if href.startswith("http") or href.startswith("//"):
                self._current_link["url"] = _unwrap_bing_url(href)
                self._collect_title = True
        elif tag == "p":
            self._collect_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_h2 = False
        elif tag == "a" and self._collect_title:
            self._collect_title = False
        elif tag == "p" and self._collect_snippet:
            self._collect_snippet = False
        elif tag == "li" and self._current_link is not None:
            self._finalize_current_link()

    def handle_data(self, data: str) -> None:
        if self._current_link is None:
            return
        if self._collect_title:
            self._current_link["title"] += data
        elif self._collect_snippet:
            snippet = self._current_link["snippet"]
            snippet += (" " if snippet else "") + data
            self._current_link["snippet"] = snippet

    def _finalize_current_link(self) -> None:
        if self._current_link is None:
            return
        if self._current_link["title"] and self._current_link["url"]:
            self._results.append(self._current_link)
        self._current_link = None
        self._in_h2 = False
        self._collect_title = False
        self._collect_snippet = False


def web_search(
    query: str,
    *,
    allowed_domains: Sequence[str] | None = None,
    blocked_domains: Sequence[str] | None = None,
    offset: int = 0,
    max_results: int | None = None,
    safe_search: str = _DEFAULT_SAFE_SEARCH,
    fields: Sequence[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if not isinstance(query, str) or len(query.strip()) < 2:
        raise ValueError("Expected query to contain at least 2 characters")
    if allowed_domains and blocked_domains:
        raise ValueError("allowed_domains and blocked_domains cannot be used together")
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if max_results is not None and (
        not isinstance(max_results, int) or max_results < 1
    ):
        raise ValueError("max_results must be a positive integer")

    normalized_query = query.strip()
    normalized_safe_search = _normalize_safe_search(safe_search)
    normalized_fields = _normalize_result_fields(fields)
    allowed = {_normalize_domain(domain) for domain in allowed_domains or ()}
    blocked = {_normalize_domain(domain) for domain in blocked_domains or ()}

    started = time.monotonic()
    try:
        response = _perform_web_search_request(
            query=normalized_query,
            allowed_domains=tuple(allowed_domains or ()),
            blocked_domains=tuple(blocked_domains or ()),
            safe_search=normalized_safe_search,
            fields=normalized_fields,
            model=model,
        )
        results, commentary = _parse_server_search_response(
            response,
            allowed=allowed,
            blocked=blocked,
        )
    except _RECOVERABLE_WEB_SEARCH_ERRORS:
        html = _fetch_search_html(normalized_query)
        parser = _BingHTMLParser()
        parser.feed(html)
        commentary = ""
        results = []
        for item in parser.results:
            host = _normalize_domain(urlparse(item["url"]).hostname or "")
            if allowed and host not in allowed:
                continue
            if blocked and host in blocked:
                continue
            results.append(
                {
                    "title": _collapse_ws(item["title"]),
                    "url": item["url"],
                    "snippet": _collapse_ws(item["snippet"]),
                }
            )

    total_results = len(results)
    result_window = _paginate_results(
        results,
        offset=offset,
        max_results=max_results,
    )
    payload = {
        "query": normalized_query,
        "results": result_window,
        "durationSeconds": round(time.monotonic() - started, 3),
        "offset": offset,
        "totalResults": total_results,
    }
    if max_results is not None:
        payload["maxResults"] = max_results
    if commentary:
        payload["commentary"] = commentary
    return payload


def _perform_web_search_request(
    *,
    query: str,
    allowed_domains: Sequence[str],
    blocked_domains: Sequence[str],
    safe_search: str,
    fields: Sequence[str],
    model: str | None,
) -> dict[str, Any]:
    tool_schema: dict[str, Any] = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 8,
    }
    if allowed_domains:
        tool_schema["allowed_domains"] = list(allowed_domains)
    if blocked_domains:
        tool_schema["blocked_domains"] = list(blocked_domains)
    return send_anthropic_message(
        user_prompt=_build_web_search_user_prompt(
            query,
            safe_search=safe_search,
            fields=fields,
        ),
        system_prompt="You are an assistant for performing a web search tool use",
        model=model,
        tools=(tool_schema,),
        tool_choice={"type": "tool", "name": "web_search"},
        beta_headers=(_WEB_SEARCH_BETA_HEADER,),
        max_tokens=1536,
        request_timeout_seconds=_SERVER_SEARCH_TIMEOUT_SECONDS,
        retry_settings=_SERVER_SEARCH_RETRY_SETTINGS,
        persistent_retries=False,
    )


def _parse_server_search_response(
    payload: dict[str, Any],
    *,
    allowed: set[str],
    blocked: set[str],
) -> tuple[list[dict[str, str]], str]:
    content = payload.get("content")
    if not isinstance(content, list):
        raise RuntimeError("Web search response did not include content blocks")

    results: list[dict[str, str]] = []
    commentary_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                commentary_parts.append(text.strip())
            continue
        if block_type != "web_search_tool_result":
            continue
        hits = block.get("content")
        if not isinstance(hits, list):
            error_code = ""
            if isinstance(hits, dict):
                raw_error = hits.get("error_code")
                if isinstance(raw_error, str) and raw_error.strip():
                    error_code = raw_error.strip()
            if error_code:
                commentary_parts.append(f"Web search error: {error_code}")
            continue
        for item in hits:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(url, str) or not url.strip():
                continue
            raw_snippet = item.get("snippet")
            if not isinstance(raw_snippet, str):
                raw_snippet = item.get("description")
            host = _normalize_domain(urlparse(url).hostname or "")
            if allowed and host not in allowed:
                continue
            if blocked and host in blocked:
                continue
            results.append(
                {
                    "title": _collapse_ws(title),
                    "url": url,
                    "snippet": (
                        _collapse_ws(raw_snippet)
                        if isinstance(raw_snippet, str) and raw_snippet.strip()
                        else ""
                    ),
                }
            )
    return results, "\n\n".join(commentary_parts).strip()


def _fetch_search_html(query: str) -> str:
    # cn.bing.com (Bing China) is reachable behind the Great Firewall; DuckDuckGo (the previous
    # fallback) is blocked. `ensure_redirect=1` keeps result links direct where possible.
    url = "https://cn.bing.com/search?" + urlencode(
        {
            "q": query,
            "setlang": "zh-CN",
            "ensure_redirect": "1",
        }
    )
    request = Request(
        url,
        headers={
            # A real browser UA — Bing returns a stripped/JS layout to unknown agents.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with urlopen(request, timeout=_FALLBACK_SEARCH_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _unwrap_bing_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    # Bing sometimes wraps the real target in a /ck/a?...&u=<a1+base64url> redirect link.
    if "bing.com/ck/a" in url:
        u = parse_qs(urlparse(url).query).get("u")
        if u:
            raw = u[0]
            if raw.startswith("a1"):
                raw = raw[2:]
            padded = raw + "=" * (-len(raw) % 4)
            try:
                return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")
            except Exception:
                return url
    return url


def _normalize_domain(domain: str) -> str:
    normalized = domain.strip().lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _normalize_safe_search(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("safe_search must be a string")
    normalized = value.strip().lower()
    if normalized not in _SAFE_SEARCH_TO_DDG_PARAM:
        allowed = ", ".join(sorted(_SAFE_SEARCH_TO_DDG_PARAM))
        raise ValueError(f"safe_search must be one of: {allowed}")
    return normalized


def _normalize_result_fields(fields: Sequence[str] | None) -> tuple[str, ...]:
    if fields is None:
        return _DEFAULT_RESULT_FIELDS
    normalized: list[str] = []
    for item in fields:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("fields must be an array of non-empty strings")
        value = item.strip()
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("fields must include at least one entry")
    return tuple(normalized)


def _paginate_results(
    results: Sequence[dict[str, str]],
    *,
    offset: int,
    max_results: int | None,
) -> list[dict[str, str]]:
    if max_results is None:
        return list(results[offset:])
    return list(results[offset : offset + max_results])


def _build_web_search_user_prompt(
    query: str,
    *,
    safe_search: str,
    fields: Sequence[str],
) -> str:
    joined_fields = ",".join(fields)
    return (
        "Perform a web search for the query: "
        f"{query}. Use safe search mode {safe_search}. "
        f"Prefer result fields: {joined_fields}."
    )


def _collapse_ws(value: str) -> str:
    return " ".join(value.split())


__all__ = ["web_search"]
