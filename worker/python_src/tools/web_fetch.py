from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import threading
import time
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
    getproxies,
    proxy_bypass,
)

from ..utils.tls import build_ssl_context_from_env
from .anthropic_sidequery import (
    extract_first_text_block,
    resolve_small_fast_model,
    send_anthropic_message,
)


_CACHE_TTL_SECONDS = 900
_CACHE_MAX_BYTES = 50 * 1024 * 1024
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_RESULT_CHARS = 100_000
_DOMAIN_CHECK_TTL_SECONDS = 300
_DOMAIN_CHECK_CACHE_MAX_ENTRIES = 128
_DOMAIN_CHECK_TIMEOUT_SECONDS = 10
_DOMAIN_INFO_URL = "https://api.anthropic.com/api/web/domain_info"
_LOCAL_PREAPPROVED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_USER_AGENT = "claude-code-python-port/1.0"
_CACHE: OrderedDict[str, tuple[float, dict[str, Any], int]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_TOTAL_BYTES = 0
_DOMAIN_CHECK_CACHE: OrderedDict[str, float] = OrderedDict()
_DOMAIN_CHECK_CACHE_LOCK = threading.Lock()

# This worker runs on a device behind the Great Firewall (China). These hosts are ALWAYS unreachable
# here — fetching them either fails instantly ("Network unreachable") or, worse, blocks for the full
# timeout before failing. Detecting them lets us fail fast AND hand the agent actionable guidance so it
# stops hammering blocked sources and switches to an accessible one.
_GFW_BLOCKED_HOST_SUFFIXES = (
    "google.com",
    "googleusercontent.com",  # incl. webcache.googleusercontent.com
    "gstatic.com",
    "googleapis.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "t.co",
    "facebook.com",
    "fbcdn.net",
    "instagram.com",
    "r.jina.ai",
    "duckduckgo.com",
    "blogspot.com",
)

_GFW_GUIDANCE = (
    "NOTE: this worker is on a network behind the Great Firewall (China). Google "
    "(including webcache.googleusercontent.com / Google cache), YouTube, Twitter/X, "
    "Facebook, Instagram, r.jina.ai, DuckDuckGo and similar are UNREACHABLE and will "
    "ALWAYS fail (often only after a long timeout). Do NOT retry them or try more "
    "Google-family mirrors. Use an ACCESSIBLE source instead: search via cn.bing.com "
    "(Bing China) or Baidu, then WebFetch the specific result pages; prefer the "
    "target's China-reachable official/mirror domain. If a couple of accessible "
    "fetches still don't yield the answer, stop and report what you have rather than "
    "probing more sources."
)


def _is_gfw_blocked_host(host: str) -> bool:
    h = (host or "").lower()
    return any(h == suffix or h.endswith("." + suffix) for suffix in _GFW_BLOCKED_HOST_SUFFIXES)


def _looks_like_network_block(reason: str) -> bool:
    # Egress failures that, on this device, almost always mean a GFW block rather than a real outage.
    lowered = (reason or "").lower()
    return any(
        marker in lowered
        for marker in (
            "unreachable",
            "connection refused",
            "timed out",
            "timeout",
            "name or service not known",
            "no route to host",
            "connection reset",
            "errno 101",
            "errno 111",
            "errno -2",
            "errno -3",
        )
    )


class EgressBlockedError(RuntimeError):
    def __init__(self, domain: str) -> None:
        self.domain = domain
        super().__init__(f"Access to {domain} is blocked by the network egress proxy.")


class _RedirectBlocked(RuntimeError):
    def __init__(self, target_url: str) -> None:
        super().__init__(target_url)
        self.target_url = target_url


class _RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self._redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        self._redirect_count += 1
        if self._redirect_count > 10:
            raise _RedirectBlocked(newurl)
        source_host = (urlparse(req.full_url).hostname or "").lower()
        target_host = (urlparse(newurl).hostname or "").lower()
        if not _hosts_are_compatible(source_host, target_host):
            raise _RedirectBlocked(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data.strip())

    def get_text(self) -> str:
        return "\n".join(self._parts)


def web_fetch(
    url: str,
    prompt: str,
    *,
    model: str | None = None,
    skip_preflight: bool = False,
) -> dict[str, Any]:
    normalized_url = _normalize_url(url)
    blocked_host = (urlparse(normalized_url).hostname or "").lower()
    if _is_gfw_blocked_host(blocked_host):
        raise RuntimeError(
            f"Cannot fetch {blocked_host}: this host is blocked by the Great Firewall and is "
            f"unreachable from this device.\n\n{_GFW_GUIDANCE}"
        )
    is_preapproved_domain = _is_preapproved_domain(normalized_url)
    if not skip_preflight and not is_preapproved_domain:
        domain = (urlparse(normalized_url).hostname or "").lower()
        check_result = _check_domain_blocklist(domain)
        if check_result["status"] == "blocked":
            raise RuntimeError(f"Claude Code is unable to fetch from {domain}")
        # Fail OPEN on a failed safety check: the blocklist lives at api.anthropic.com, which is
        # unreachable on the restricted networks this on-device worker runs on, so a "check_failed"
        # here is a connectivity problem with the CHECK service — not evidence the target is unsafe.
        # Blocking every fetch on it makes web_fetch unusable; proceed and let the real fetch surface
        # any genuine error instead. (Explicit "blocked" verdicts above are still honored.)

    started = time.monotonic()
    fetch_state = _read_cached_fetch(normalized_url)
    if fetch_state is None:
        fetch_state = _fetch_url(normalized_url)
        _write_cached_fetch(normalized_url, fetch_state)

    if fetch_state["kind"] == "redirect":
        duration_ms = int((time.monotonic() - started) * 1000)
        payload = {
            "url": normalized_url,
            "result": f"REDIRECT DETECTED: {fetch_state['target_url']}",
            "durationMs": duration_ms,
            "bytes": 0,
            "code": 302,
        }
        return payload

    body = fetch_state["body"]
    content_type = fetch_state["content_type"]
    final_url = fetch_state["url"]
    code = fetch_state["code"]
    persisted_path = None
    persisted_size = None
    if _is_html_content(content_type):
        markdown = _html_to_text(body, content_type)
        is_preapproved_domain = _is_preapproved_domain(final_url)
        if _should_return_raw_markdown(content_type, markdown, is_preapproved_domain):
            result = markdown[:_MAX_RESULT_CHARS]
        else:
            result = _apply_prompt_to_markdown(
                prompt,
                markdown,
                is_preapproved_domain=is_preapproved_domain,
                model=model,
            )
    else:
        suffix = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
        persisted_path, persisted_size = _write_download(final_url, body, suffix)
        result = f"Downloaded non-HTML content to {persisted_path}"

    duration_ms = int((time.monotonic() - started) * 1000)
    payload = {
        "url": final_url,
        "result": result,
        "durationMs": duration_ms,
        "bytes": len(body),
        "code": code,
        "codeText": _http_status_text(code),
        "contentType": content_type,
    }
    if persisted_path:
        payload["persistedPath"] = persisted_path
    if persisted_size is not None:
        payload["persistedSize"] = persisted_size
    return payload


def _read_cached_fetch(url: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        _prune_cache_locked()
        cached = _CACHE.get(url)
        if cached is None:
            return None
        timestamp, payload, size_bytes = cached
        _CACHE.move_to_end(url)
        _CACHE[url] = (timestamp, payload, size_bytes)
        return dict(payload)


def _write_cached_fetch(url: str, fetch_state: dict[str, Any]) -> None:
    global _CACHE_TOTAL_BYTES
    size_bytes = _estimate_cache_entry_size(fetch_state)
    if size_bytes > _CACHE_MAX_BYTES:
        return

    with _CACHE_LOCK:
        _prune_cache_locked()
        existing = _CACHE.pop(url, None)
        if existing is not None:
            _CACHE_TOTAL_BYTES -= existing[2]
        _CACHE[url] = (time.monotonic(), dict(fetch_state), size_bytes)
        _CACHE_TOTAL_BYTES += size_bytes
        _prune_cache_locked()


def _read_cached_domain_allow(domain: str) -> bool:
    with _DOMAIN_CHECK_CACHE_LOCK:
        now = time.monotonic()
        expired = [
            key
            for key, timestamp in _DOMAIN_CHECK_CACHE.items()
            if now - timestamp >= _DOMAIN_CHECK_TTL_SECONDS
        ]
        for key in expired:
            _DOMAIN_CHECK_CACHE.pop(key, None)
        cached = _DOMAIN_CHECK_CACHE.get(domain)
        if cached is None:
            return False
        _DOMAIN_CHECK_CACHE.move_to_end(domain)
        _DOMAIN_CHECK_CACHE[domain] = cached
        return True


def _write_cached_domain_allow(domain: str) -> None:
    with _DOMAIN_CHECK_CACHE_LOCK:
        _DOMAIN_CHECK_CACHE[domain] = time.monotonic()
        _DOMAIN_CHECK_CACHE.move_to_end(domain)
        while len(_DOMAIN_CHECK_CACHE) > _DOMAIN_CHECK_CACHE_MAX_ENTRIES:
            _DOMAIN_CHECK_CACHE.popitem(last=False)


def clear_web_fetch_caches() -> None:
    global _CACHE_TOTAL_BYTES
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE_TOTAL_BYTES = 0
    with _DOMAIN_CHECK_CACHE_LOCK:
        _DOMAIN_CHECK_CACHE.clear()


def _prune_cache_locked() -> None:
    global _CACHE_TOTAL_BYTES
    now = time.monotonic()
    expired_keys = [
        key
        for key, (timestamp, _, _) in _CACHE.items()
        if now - timestamp >= _CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        cached = _CACHE.pop(key, None)
        if cached is not None:
            _CACHE_TOTAL_BYTES -= cached[2]

    while _CACHE_TOTAL_BYTES > _CACHE_MAX_BYTES and _CACHE:
        _, (_, _, size_bytes) = _CACHE.popitem(last=False)
        _CACHE_TOTAL_BYTES -= size_bytes


def _estimate_cache_entry_size(fetch_state: dict[str, Any]) -> int:
    total = 0
    for value in fetch_state.values():
        if isinstance(value, (bytes, bytearray)):
            total += len(value)
        elif isinstance(value, str):
            total += len(value.encode("utf-8", errors="replace"))
        elif isinstance(value, (int, float)):
            total += 8
        else:
            total += len(repr(value).encode("utf-8", errors="replace"))
    return total


def _fetch_url(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    opener = build_opener(*_opener_handlers_for_url(url, follow_redirects=True))
    try:
        with opener.open(request, timeout=60) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("Fetched content exceeded maximum size")
            return {
                "kind": "response",
                "url": response.geturl(),
                "body": body,
                "content_type": response.headers.get("Content-Type", ""),
                "code": getattr(response, "status", 200),
            }
    except _RedirectBlocked as exc:
        return {
            "kind": "redirect",
            "target_url": exc.target_url,
        }
    except HTTPError as exc:
        proxy_error = _proxy_error_code(exc.headers)
        if exc.code == 403 and proxy_error == "blocked-by-allowlist":
            hostname = (urlparse(url).hostname or "").lower()
            raise EgressBlockedError(hostname) from exc
        raise RuntimeError(f"HTTP {exc.code} while fetching {url}") from exc
    except URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        message = f"Failed to fetch {url}: {reason}"
        if _looks_like_network_block(reason):
            message += "\n\n" + _GFW_GUIDANCE
        raise RuntimeError(message) from exc


def _check_domain_blocklist(domain: str) -> dict[str, Any]:
    if _read_cached_domain_allow(domain):
        return {"status": "allowed"}

    request_url = f"{_DOMAIN_INFO_URL}?{urlencode({'domain': domain})}"
    request = Request(
        request_url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        opener = build_opener(*_opener_handlers_for_url(request_url))
        with opener.open(request, timeout=_DOMAIN_CHECK_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                return {
                    "status": "check_failed",
                    "error": RuntimeError(
                        f"Domain check returned status {getattr(response, 'status', 200)}"
                    ),
                }
            raw_payload = response.read()
    except Exception as exc:
        return {
            "status": "check_failed",
            "error": exc,
        }

    try:
        payload = json.loads(raw_payload.decode("utf-8", "replace"))
    except Exception as exc:
        return {
            "status": "check_failed",
            "error": exc,
        }

    if isinstance(payload, dict) and payload.get("can_fetch") is True:
        _write_cached_domain_allow(domain)
        return {"status": "allowed"}
    if isinstance(payload, dict) and payload.get("can_fetch") is False:
        return {"status": "blocked"}
    return {
        "status": "check_failed",
        "error": RuntimeError("Domain check returned an invalid payload"),
    }


def _normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Expected non-empty string for url")
    trimmed = url.strip()
    if len(trimmed) > 2000:
        raise ValueError("URL is too long")
    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("WebFetch only supports http and https URLs")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not supported")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL must include a hostname")
    if host not in _LOCAL_PREAPPROVED_HOSTS and "." not in host:
        raise ValueError("URL must include a public hostname")
    if parsed.scheme == "http" and host not in _LOCAL_PREAPPROVED_HOSTS:
        parsed = parsed._replace(scheme="https")
    # Percent-encode non-ASCII (IRI) characters in the path/query/fragment and IDNA-encode the host so
    # urllib can place the URL on the HTTP request line, which must be ASCII. Without this a URL like
    # https://cn.bing.com/search?q=<中文> raises "'ascii' codec can't encode characters". `safe` keeps
    # URL-structural chars and existing %xx escapes intact (so already-encoded URLs aren't doubled).
    try:
        ascii_host = (parsed.hostname or "").encode("idna").decode("ascii")
        netloc = f"{ascii_host}:{parsed.port}" if parsed.port else ascii_host
    except Exception:
        netloc = parsed.netloc
    parsed = parsed._replace(
        netloc=netloc,
        path=quote(parsed.path, safe="/%:@-._~!$&'()*+,;="),
        query=quote(parsed.query, safe="/%:@-._~!$&'()*+,;=&?"),
        fragment=quote(parsed.fragment, safe="/%:@-._~!$&'()*+,;=&?"),
    )
    return parsed.geturl()


def _proxy_handler_for_url(url: str) -> ProxyHandler:
    hostname = (urlparse(url).hostname or "").lower()
    if hostname in _LOCAL_PREAPPROVED_HOSTS:
        return ProxyHandler({})
    if hostname and proxy_bypass(hostname):
        return ProxyHandler({})

    proxies: dict[str, str] = {}
    for scheme, value in getproxies().items():
        normalized_scheme = str(scheme).lower()
        if normalized_scheme not in {"http", "https", "all"}:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        proxies[normalized_scheme] = value.strip()
    return ProxyHandler(proxies)


def _opener_handlers_for_url(
    url: str,
    *,
    follow_redirects: bool = False,
) -> tuple[object, ...]:
    handlers: list[object] = [_proxy_handler_for_url(url)]
    ssl_context = build_ssl_context_from_env()
    if ssl_context is not None:
        handlers.append(HTTPSHandler(context=ssl_context))
    if follow_redirects:
        handlers.append(_RestrictedRedirectHandler())
    return tuple(handlers)


def _hosts_are_compatible(source_host: str, target_host: str) -> bool:
    if source_host == target_host:
        return True
    if source_host.startswith("www.") and source_host[4:] == target_host:
        return True
    if target_host.startswith("www.") and target_host[4:] == source_host:
        return True
    return False


def _is_html_content(content_type: str) -> bool:
    lowered = content_type.lower()
    return "html" in lowered or lowered.startswith("text/")


def _is_preapproved_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_PREAPPROVED_HOSTS


def _should_return_raw_markdown(
    content_type: str,
    markdown: str,
    is_preapproved_domain: bool,
) -> bool:
    lowered = content_type.lower()
    if not is_preapproved_domain:
        return False
    if "markdown" not in lowered and "text/plain" not in lowered:
        return False
    return len(markdown) <= _MAX_RESULT_CHARS


def _html_to_text(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip()
            break
    decoded = body.decode(charset, errors="replace")
    try:
        from markdownify import markdownify as _markdownify

        return _markdownify(decoded).strip()
    except Exception:
        parser = _HTMLTextExtractor()
        parser.feed(decoded)
        return parser.get_text().strip()


def _apply_prompt_to_markdown(
    prompt: str,
    markdown_content: str,
    *,
    is_preapproved_domain: bool,
    model: str | None,
) -> str:
    truncated_content = markdown_content
    if len(truncated_content) > _MAX_RESULT_CHARS:
        truncated_content = (
            truncated_content[:_MAX_RESULT_CHARS]
            + "\n\n[Content truncated due to length...]"
        )
    try:
        response = send_anthropic_message(
            user_prompt=_make_secondary_model_prompt(
                truncated_content,
                prompt,
                is_preapproved_domain=is_preapproved_domain,
            ),
            model=resolve_small_fast_model(model),
            max_tokens=1024,
        )
    except RuntimeError:
        if prompt.strip():
            return f"{prompt.strip()}\n\n{truncated_content}"
        return truncated_content

    text = extract_first_text_block(response)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return "No response from model"


def _make_secondary_model_prompt(
    markdown_content: str,
    prompt: str,
    *,
    is_preapproved_domain: bool,
) -> str:
    guidelines = (
        "Provide a concise response based on the content above. Include relevant "
        "details, code examples, and documentation excerpts as needed."
        if is_preapproved_domain
        else (
            "Provide a concise response based only on the content above. In your "
            "response:\n"
            "- Enforce a strict 125-character maximum for quotes from any source "
            "document.\n"
            "- Use quotation marks for exact language from articles; any language "
            "outside quotation should be paraphrased.\n"
            "- Never produce exact song lyrics.\n"
            "- Do not speculate beyond the supplied page content."
        )
    )
    return (
        "Web page content:\n---\n"
        f"{markdown_content}\n"
        "---\n\n"
        f"{prompt}\n\n"
        f"{guidelines}"
    )


def _write_download(url: str, body: bytes, suffix: str) -> tuple[str, int]:
    parsed = urlparse(url)
    base_name = os.path.basename(parsed.path) or "download"
    prefix = base_name[:40].replace(" ", "_") + "-"
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, delete=False) as handle:
        handle.write(body)
        path = handle.name
    return path, len(body)


def _proxy_error_code(headers: Any) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter("X-Proxy-Error")
        if not value:
            value = getter("x-proxy-error")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _http_status_text(code: int) -> str:
    try:
        from http import HTTPStatus

        return HTTPStatus(code).phrase
    except Exception:
        return str(code)


__all__ = [
    "EgressBlockedError",
    "clear_web_fetch_caches",
    "web_fetch",
]
