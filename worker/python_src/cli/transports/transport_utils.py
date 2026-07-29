from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from .hybrid_transport import HybridTransport
from .sse_transport import SSETransport
from .websocket_transport import WebSocketTransport


def is_env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _convert_ws_to_http(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme
    if scheme == "wss":
        scheme = "https"
    elif scheme == "ws":
        scheme = "http"
    path = parsed.path.rstrip("/") + "/worker/events/stream"
    return urlunparse(
        (scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment)
    )


def get_transport_for_url(
    url: str,
    headers: dict[str, str] | None = None,
    session_id: str | None = None,
    refresh_headers=None,
    env: dict[str, str] | None = None,
):
    del refresh_headers
    env_map = env or {}
    parsed = urlparse(url)
    if is_env_truthy(env_map.get("CLAUDE_CODE_USE_CCR_V2")):
        return SSETransport(_convert_ws_to_http(url), headers or {}, session_id)
    if parsed.scheme in {"ws", "wss"}:
        if is_env_truthy(env_map.get("CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2")):
            return HybridTransport(url, headers or {}, session_id)
        return WebSocketTransport(url, headers or {}, session_id)
    raise ValueError("Unsupported protocol: {}".format(parsed.scheme))
