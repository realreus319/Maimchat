"""Dynamic MCP header helper execution."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Mapping

from .types import McpHTTPServerConfig, McpSSEServerConfig, McpWebSocketServerConfig

RemoteHeaderConfig = McpSSEServerConfig | McpHTTPServerConfig | McpWebSocketServerConfig


async def get_mcp_headers_from_helper(
    server_name: str,
    config: RemoteHeaderConfig,
    *,
    cwd: str | None = None,
    timeout_s: float = 10.0,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return dynamic headers from ``headers_helper`` or ``None`` on failure."""

    helper = getattr(config, "headers_helper", None)
    if not isinstance(helper, str) or not helper.strip():
        return None

    env = dict(os.environ)
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})
    env["CLAUDE_CODE_MCP_SERVER_NAME"] = server_name
    env["CLAUDE_CODE_MCP_SERVER_URL"] = getattr(config, "url", "")

    try:
        proc = await asyncio.create_subprocess_shell(
            helper,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return None

    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        return None

    if proc.returncode not in (0, None) or not stdout:
        return None

    payload = stdout.decode("utf-8", errors="replace").strip()
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    headers: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        headers[key] = value
    return headers


async def get_mcp_server_headers(
    server_name: str,
    config: RemoteHeaderConfig,
    *,
    cwd: str | None = None,
    timeout_s: float = 10.0,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return static and dynamic headers, with helper output taking precedence."""

    static_headers = dict(config.headers or {})
    dynamic_headers = await get_mcp_headers_from_helper(
        server_name,
        config,
        cwd=cwd,
        timeout_s=timeout_s,
        extra_env=extra_env,
    )
    if dynamic_headers:
        static_headers.update(dynamic_headers)
    return static_headers
