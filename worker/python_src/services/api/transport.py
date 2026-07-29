from __future__ import annotations

import io
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen as _stdlib_urlopen

import urllib3
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from ...utils.tls import urllib3_ssl_pool_kwargs_from_env


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

_pool_lock = threading.Lock()
_direct_pool: urllib3.PoolManager | None = None
_proxy_pools: dict[str, urllib3.ProxyManager] = {}
_http2_attempted = False
_http2_enabled = False


@dataclass(frozen=True)
class ApiTransportStatus:
    connection_pool_enabled: bool
    http2_requested: bool
    http2_enabled: bool
    direct_pool_created: bool
    proxy_pool_count: int


class PooledHTTPResponse:
    """urllib.response-compatible wrapper over urllib3's pooled response."""

    def __init__(self, response: urllib3.HTTPResponse) -> None:
        self._response = response
        self.status = response.status
        self.code = response.status
        self.reason = response.reason
        self.headers = response.headers

    def __enter__(self) -> "PooledHTTPResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def __iter__(self) -> Iterable[bytes]:
        pending = b""
        for chunk in self._response.stream(amt=65536, decode_content=True):
            if not chunk:
                continue
            pending += chunk
            while True:
                newline_index = pending.find(b"\n")
                if newline_index < 0:
                    break
                line = pending[: newline_index + 1]
                pending = pending[newline_index + 1 :]
                yield line
        if pending:
            yield pending

    def read(self, amt: int | None = None) -> bytes:
        return self._response.read(amt=amt, decode_content=True)

    def close(self) -> None:
        self._response.release_conn()

    def getcode(self) -> int:
        return self.status

    def info(self) -> Any:
        return self.headers


def pooled_urlopen(
    request: Request,
    timeout: float | None = None,
    *,
    preload_content: bool = False,
) -> PooledHTTPResponse:
    """Open a urllib Request through a process-wide keep-alive pool.

    The return value and raised exceptions intentionally follow
    urllib.request.urlopen closely so existing retry classification keeps
    working while the socket lifecycle is handled by urllib3 PoolManager.
    """

    if not api_connection_pool_enabled():
        return _stdlib_urlopen(request, timeout=timeout)  # type: ignore[return-value]

    url = request.full_url
    method = request.get_method()
    body = request.data
    headers = dict(request.header_items())
    headers["x-client-request-id"] = str(uuid.uuid4())
    manager = _get_pool_for_url(url)
    request_timeout = (
        urllib3.Timeout.from_float(timeout) if timeout is not None else None
    )
    try:
        response = manager.request(
            method,
            url,
            body=body,
            headers=headers,
            timeout=request_timeout,
            preload_content=preload_content,
            retries=False,
        )
    except Urllib3HTTPError as exc:
        raise URLError(exc) from exc
    except OSError as exc:
        raise URLError(exc) from exc

    if response.status >= 400:
        body_bytes = response.read(decode_content=True)
        response.release_conn()
        raise HTTPError(
            url,
            response.status,
            response.reason or "",
            response.headers,
            io.BytesIO(body_bytes),
        )

    return PooledHTTPResponse(response)


def preconnect_anthropic_api(
    base_url: str,
    *,
    timeout_seconds: float = 10.0,
    background: bool = True,
) -> None:
    """Warm the direct Anthropic API pool with a best-effort HEAD request."""

    if not api_connection_pool_enabled() or _should_skip_preconnect(base_url):
        return

    def _run() -> None:
        request = Request(base_url, method="HEAD")
        try:
            with pooled_urlopen(request, timeout=timeout_seconds):
                pass
        except Exception:
            return

    if background:
        thread = threading.Thread(
            target=_run,
            name="anthropic-api-preconnect",
            daemon=True,
        )
        thread.start()
        return
    _run()


def api_transport_status() -> ApiTransportStatus:
    return ApiTransportStatus(
        connection_pool_enabled=api_connection_pool_enabled(),
        http2_requested=_http2_requested(),
        http2_enabled=_http2_enabled,
        direct_pool_created=_direct_pool is not None,
        proxy_pool_count=len(_proxy_pools),
    )


def close_api_transport() -> None:
    global _direct_pool
    with _pool_lock:
        if _direct_pool is not None:
            _direct_pool.clear()
        for pool in _proxy_pools.values():
            pool.clear()
        _direct_pool = None
        _proxy_pools.clear()


def reset_api_transport_for_testing() -> None:
    global _http2_attempted, _http2_enabled
    close_api_transport()
    _http2_attempted = False
    _http2_enabled = False


def api_connection_pool_enabled() -> bool:
    value = os.environ.get("CLAUDE_CODE_DISABLE_API_CONNECTION_POOL")
    return not _env_truthy(value)


def _get_pool_for_url(url: str) -> urllib3.PoolManager | urllib3.ProxyManager:
    proxy_url = _get_proxy_url()
    if proxy_url and not should_bypass_proxy(url):
        return _get_proxy_pool(proxy_url)
    return _get_direct_pool()


def _get_direct_pool() -> urllib3.PoolManager:
    global _direct_pool
    with _pool_lock:
        if _direct_pool is None:
            _maybe_enable_http2()
            _direct_pool = urllib3.PoolManager(**_pool_kwargs())
        return _direct_pool


def _get_proxy_pool(proxy_url: str) -> urllib3.ProxyManager:
    with _pool_lock:
        pool = _proxy_pools.get(proxy_url)
        if pool is None:
            _maybe_enable_http2()
            pool = urllib3.ProxyManager(proxy_url, **_pool_kwargs())
            _proxy_pools[proxy_url] = pool
        return pool


def _pool_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "num_pools": _parse_positive_int(
            os.environ.get("CLAUDE_CODE_API_POOL_NUM_POOLS"),
            default=10,
        ),
        "maxsize": _parse_positive_int(
            os.environ.get("CLAUDE_CODE_API_POOL_MAXSIZE"),
            default=10,
        ),
        "block": _env_bool(
            os.environ.get("CLAUDE_CODE_API_POOL_BLOCK"),
            default=False,
        ),
    }
    kwargs.update(urllib3_ssl_pool_kwargs_from_env())
    return kwargs


def _maybe_enable_http2() -> None:
    global _http2_attempted, _http2_enabled
    if _http2_attempted:
        return
    _http2_attempted = True
    if not _http2_requested():
        return
    try:
        from urllib3.http2 import inject_into_urllib3

        inject_into_urllib3()
    except Exception:
        _http2_enabled = False
        return
    _http2_enabled = True


def _http2_requested() -> bool:
    disabled = os.environ.get("CLAUDE_CODE_DISABLE_HTTP2")
    if _env_truthy(disabled):
        return False
    value = os.environ.get("CLAUDE_CODE_HTTP2")
    if value is None or not value.strip():
        return True
    return _env_bool(value, default=True)


def _get_proxy_url() -> str | None:
    return (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
    )


def _get_no_proxy() -> str | None:
    return os.environ.get("no_proxy") or os.environ.get("NO_PROXY")


def should_bypass_proxy(url: str, no_proxy: str | None = None) -> bool:
    no_proxy_value = _get_no_proxy() if no_proxy is None else no_proxy
    if not no_proxy_value:
        return False
    if no_proxy_value.strip() == "*":
        return True

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    host_with_port = f"{hostname}:{port}"

    for raw_pattern in no_proxy_value.replace(",", " ").split():
        pattern = raw_pattern.strip().lower()
        if not pattern:
            continue
        if ":" in pattern:
            if host_with_port == pattern:
                return True
            continue
        if pattern.startswith("."):
            suffix = pattern
            if hostname == pattern[1:] or hostname.endswith(suffix):
                return True
            continue
        if hostname == pattern:
            return True
    return False


def _should_skip_preconnect(base_url: str) -> bool:
    if _env_truthy(os.environ.get("CLAUDE_CODE_USE_BEDROCK")):
        return True
    if _env_truthy(os.environ.get("CLAUDE_CODE_USE_VERTEX")):
        return True
    if _env_truthy(os.environ.get("CLAUDE_CODE_USE_FOUNDRY")):
        return True
    if os.environ.get("ANTHROPIC_UNIX_SOCKET"):
        return True
    if os.environ.get("CLAUDE_CODE_CLIENT_CERT") or os.environ.get(
        "CLAUDE_CODE_CLIENT_KEY"
    ):
        return True
    return bool(_get_proxy_url() and not should_bypass_proxy(base_url))


def _env_truthy(value: str | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _env_bool(value: str | None, *, default: bool) -> bool:
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return default


def _parse_positive_int(value: str | None, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = [
    "ApiTransportStatus",
    "PooledHTTPResponse",
    "api_connection_pool_enabled",
    "api_transport_status",
    "close_api_transport",
    "pooled_urlopen",
    "preconnect_anthropic_api",
    "reset_api_transport_for_testing",
    "should_bypass_proxy",
]
