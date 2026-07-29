"""Official MCP registry helpers."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping
from urllib.request import urlopen


OFFICIAL_MCP_REGISTRY_URL = (
    "https://api.anthropic.com/mcp-registry/v0/servers"
    "?version=latest&visibility=commercial"
)

_official_urls: set[str] | None = None


def normalize_official_registry_url(url: str) -> str | None:
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    normalized_path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    cleaned = parsed._replace(path=normalized_path, params="", query="", fragment="")
    return urlunparse(cleaned)


async def _fetch_official_registry_payload(timeout_s: float) -> Mapping[str, Any]:
    def _read() -> Mapping[str, Any]:
        with urlopen(OFFICIAL_MCP_REGISTRY_URL, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("Official MCP registry returned non-object payload")
        return payload

    return await asyncio.to_thread(_read)


async def prefetch_official_mcp_urls(timeout_s: float = 5.0) -> frozenset[str]:
    """Populate the in-memory official MCP URL registry."""

    global _official_urls

    if os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        return frozenset()

    try:
        payload = await _fetch_official_registry_payload(timeout_s)
    except Exception:
        return frozenset(_official_urls or ())

    urls: set[str] = set()
    servers = payload.get("servers")
    if isinstance(servers, list):
        for entry in servers:
            if not isinstance(entry, Mapping):
                continue
            server = entry.get("server")
            if not isinstance(server, Mapping):
                continue
            remotes = server.get("remotes")
            if not isinstance(remotes, list):
                continue
            for remote in remotes:
                if not isinstance(remote, Mapping):
                    continue
                normalized = normalize_official_registry_url(str(remote.get("url", "")))
                if normalized:
                    urls.add(normalized)

    _official_urls = urls
    return frozenset(urls)


def is_official_mcp_url(normalized_url: str) -> bool:
    return normalized_url in (_official_urls or set())


def reset_official_mcp_urls_for_testing() -> None:
    global _official_urls
    _official_urls = None
