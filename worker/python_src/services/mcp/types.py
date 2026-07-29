"""MCP type definitions.

Python port of src/services/mcp/types.ts.

Provides config scopes, transport literals, server config dataclasses for each
transport type (stdio / sse / http / ws / sdk / claudeai-proxy / sse-ide / ws-ide),
and connection state discriminated unions (connected / failed / needs-auth / pending / disabled).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

# ---------------------------------------------------------------------------
# Scope and transport type aliases (mirror Zod enums from TS source)
# ---------------------------------------------------------------------------

ConfigScope = Literal[
    "local",
    "user",
    "project",
    "dynamic",
    "enterprise",
    "claudeai",
    "managed",
]

Transport = Literal["stdio", "sse", "sse-ide", "http", "ws", "sdk"]

# ---------------------------------------------------------------------------
# Server config dataclasses (mirror Zod object schemas from TS source)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpStdioServerConfig:
    command: str
    args: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    type: str = "stdio"


@dataclass(frozen=True)
class McpSSEServerConfig:
    url: str
    headers: Optional[Dict[str, str]] = None
    headers_helper: Optional[str] = None
    oauth: Optional[Dict[str, Any]] = None
    type: str = "sse"


@dataclass(frozen=True)
class McpSSEIDEServerConfig:
    url: str
    ide_name: str
    ide_running_in_windows: Optional[bool] = None
    type: str = "sse-ide"


@dataclass(frozen=True)
class McpWebSocketIDEServerConfig:
    url: str
    ide_name: str
    auth_token: Optional[str] = None
    ide_running_in_windows: Optional[bool] = None
    type: str = "ws-ide"


@dataclass(frozen=True)
class McpHTTPServerConfig:
    url: str
    headers: Optional[Dict[str, str]] = None
    headers_helper: Optional[str] = None
    oauth: Optional[Dict[str, Any]] = None
    type: str = "http"


@dataclass(frozen=True)
class McpWebSocketServerConfig:
    url: str
    headers: Optional[Dict[str, str]] = None
    headers_helper: Optional[str] = None
    type: str = "ws"


@dataclass(frozen=True)
class McpSdkServerConfig:
    name: str
    type: str = "sdk"


@dataclass(frozen=True)
class McpClaudeAIProxyServerConfig:
    url: str
    id: str
    type: str = "claudeai-proxy"


McpServerConfig = Union[
    McpStdioServerConfig,
    McpSSEServerConfig,
    McpSSEIDEServerConfig,
    McpWebSocketIDEServerConfig,
    McpHTTPServerConfig,
    McpWebSocketServerConfig,
    McpSdkServerConfig,
    McpClaudeAIProxyServerConfig,
]


@dataclass(frozen=True)
class ScopedMcpServerConfig:
    config: McpServerConfig
    scope: ConfigScope
    plugin_source: Optional[str] = None


@dataclass(frozen=True)
class McpJsonConfig:
    mcp_servers: Dict[str, McpServerConfig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Connection state discriminated union (mirrors TS MCPServerConnection)
# ---------------------------------------------------------------------------


@dataclass
class ConnectedMCPServer:
    name: str
    config: McpServerConfig
    scope: ConfigScope
    owner_id: Optional[str] = None
    server_info: Optional[Dict[str, Any]] = None
    client: Optional[Any] = None
    capabilities: Optional[Dict[str, Any]] = None
    resources: Optional[List[Dict[str, Any]]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    instructions: Optional[str] = None
    transport_process: Optional[Any] = None
    type: str = "connected"


@dataclass
class FailedMCPServer:
    name: str
    config: McpServerConfig
    scope: ConfigScope
    owner_id: Optional[str] = None
    error: Optional[str] = None
    type: str = "failed"


@dataclass
class NeedsAuthMCPServer:
    name: str
    config: McpServerConfig
    scope: ConfigScope
    owner_id: Optional[str] = None
    type: str = "needs-auth"


@dataclass
class PendingMCPServer:
    name: str
    config: McpServerConfig
    scope: ConfigScope
    owner_id: Optional[str] = None
    reconnect_attempt: Optional[int] = None
    max_reconnect_attempts: Optional[int] = None
    type: str = "pending"


@dataclass
class DisabledMCPServer:
    name: str
    config: McpServerConfig
    scope: ConfigScope
    owner_id: Optional[str] = None
    type: str = "disabled"


MCPServerConnection = Union[
    ConnectedMCPServer,
    FailedMCPServer,
    NeedsAuthMCPServer,
    PendingMCPServer,
    DisabledMCPServer,
]


# ---------------------------------------------------------------------------
# Validation error type (mirrors config.ts validation error shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str
    file: Optional[str] = None
    suggestion: Optional[str] = None
    scope: Optional[ConfigScope] = None
    severity: str = "fatal"


# ---------------------------------------------------------------------------
# Config parser (mirrors Zod union schema validation)
# ---------------------------------------------------------------------------

_VALID_SERVER_TYPES = {
    "stdio",
    "sse",
    "sse-ide",
    "ws-ide",
    "http",
    "ws",
    "sdk",
    "claudeai-proxy",
}


def parse_server_config(raw: Dict[str, Any]) -> McpServerConfig:
    """Parse and validate a single MCP server config dict.

    Raises ``ValueError`` on invalid input.
    """
    server_type = raw.get("type")

    # stdio is the default when type is absent
    if server_type is None or server_type == "stdio":
        cmd = raw.get("command")
        if not isinstance(cmd, str) or not cmd:
            raise ValueError("stdio server: 'command' must be a non-empty string")
        args = raw.get("args", [])
        if not isinstance(args, list):
            raise ValueError("stdio server: 'args' must be a list")
        env = raw.get("env")
        if env is not None and not isinstance(env, dict):
            raise ValueError("stdio server: 'env' must be a dict")
        return McpStdioServerConfig(
            command=cmd,
            args=[str(a) for a in args],
            env=env if isinstance(env, dict) else None,
            type="stdio",
        )

    if server_type == "sse":
        url = _require_str_field(raw, "url", "sse server")
        return McpSSEServerConfig(
            url=url,
            headers=_opt_str_dict(raw.get("headers")),
            headers_helper=_opt_str(raw.get("headersHelper")),
            oauth=_opt_oauth(raw.get("oauth")),
            type="sse",
        )

    if server_type == "sse-ide":
        url = _require_str_field(raw, "url", "sse-ide server")
        ide_name = _require_str_field(raw, "ideName", "sse-ide server")
        return McpSSEIDEServerConfig(
            url=url,
            ide_name=ide_name,
            ide_running_in_windows=raw.get("ideRunningInWindows"),
            type="sse-ide",
        )

    if server_type == "ws-ide":
        url = _require_str_field(raw, "url", "ws-ide server")
        ide_name = _require_str_field(raw, "ideName", "ws-ide server")
        return McpWebSocketIDEServerConfig(
            url=url,
            ide_name=ide_name,
            auth_token=raw.get("authToken"),
            ide_running_in_windows=raw.get("ideRunningInWindows"),
            type="ws-ide",
        )

    if server_type == "http":
        url = _require_str_field(raw, "url", "http server")
        return McpHTTPServerConfig(
            url=url,
            headers=_opt_str_dict(raw.get("headers")),
            headers_helper=_opt_str(raw.get("headersHelper")),
            oauth=_opt_oauth(raw.get("oauth")),
            type="http",
        )

    if server_type == "ws":
        url = _require_str_field(raw, "url", "ws server")
        return McpWebSocketServerConfig(
            url=url,
            headers=_opt_str_dict(raw.get("headers")),
            headers_helper=_opt_str(raw.get("headersHelper")),
            type="ws",
        )

    if server_type == "sdk":
        sdk_name = _require_str_field(raw, "name", "sdk server")
        return McpSdkServerConfig(name=sdk_name, type="sdk")

    if server_type == "claudeai-proxy":
        url = _require_str_field(raw, "url", "claudeai-proxy server")
        server_id = _require_str_field(raw, "id", "claudeai-proxy server")
        return McpClaudeAIProxyServerConfig(
            url=url,
            id=server_id,
            type="claudeai-proxy",
        )

    raise ValueError(
        f"Invalid MCP server type: {server_type!r}. "
        f"Expected one of: {sorted(_VALID_SERVER_TYPES)}"
    )


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _require_str_field(raw: Dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: '{key}' must be a non-empty string")
    return value


def _opt_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    return None


def _opt_str_dict(value: Any) -> Optional[Dict[str, str]]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return None


def _opt_oauth(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    return None
