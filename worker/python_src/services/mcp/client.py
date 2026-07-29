from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import replace
import inspect
import json
import os
import subprocess
import tempfile
import time
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .config import (
    expand_env_vars_in_string,
    get_mcp_server_signature,
    unwrap_ccr_proxy_url,
)
from .headers_helper import get_mcp_server_headers
from .remote_transport import RemoteMCPJsonRpcClient
from .types import (
    ConfigScope,
    ConnectedMCPServer,
    DisabledMCPServer,
    FailedMCPServer,
    MCPServerConnection,
    McpHTTPServerConfig,
    McpSSEServerConfig,
    McpSdkServerConfig,
    McpServerConfig,
    McpStdioServerConfig,
    McpWebSocketServerConfig,
    NeedsAuthMCPServer,
    PendingMCPServer,
    ScopedMcpServerConfig,
    parse_server_config,
)


DEFAULT_MCP_TOOL_TIMEOUT_MS = 100_000_000
MCP_REQUEST_TIMEOUT_MS = 60_000
MAX_MCP_DESCRIPTION_LENGTH = 2048
STDIO_STARTUP_PROBE_S = 0.1
STDIO_SHUTDOWN_TIMEOUT_S = 2.0
STDIO_FORCE_KILL_TIMEOUT_S = 0.25
DEFAULT_MCP_RESOURCE_CACHE_TTL_SECONDS = 300
DEFAULT_MCP_RESOURCE_CACHE_MAX_ENTRIES = 128


def get_connection_timeout_ms() -> int:
    return int(os.environ.get("MCP_TIMEOUT", "") or 30000)


def get_mcp_tool_timeout_ms() -> int:
    return int(os.environ.get("MCP_TOOL_TIMEOUT", "") or DEFAULT_MCP_TOOL_TIMEOUT_MS)


def get_mcp_server_connection_batch_size() -> int:
    return int(os.environ.get("MCP_SERVER_CONNECTION_BATCH_SIZE", "") or 3)


def get_remote_mcp_server_connection_batch_size() -> int:
    return int(os.environ.get("MCP_REMOTE_SERVER_CONNECTION_BATCH_SIZE", "") or 20)


def get_mcp_resource_cache_ttl_seconds() -> int:
    return int(
        os.environ.get("MCP_RESOURCE_CACHE_TTL_SECONDS", "")
        or DEFAULT_MCP_RESOURCE_CACHE_TTL_SECONDS
    )


def get_mcp_resource_cache_max_entries() -> int:
    return int(
        os.environ.get("MCP_RESOURCE_CACHE_MAX_ENTRIES", "")
        or DEFAULT_MCP_RESOURCE_CACHE_MAX_ENTRIES
    )


def _is_local_mcp_server(config: ScopedMcpServerConfig) -> bool:
    server_type = config.config.type if hasattr(config.config, "type") else None
    return server_type in (None, "stdio", "sdk")


_connection_cache: Dict[str, MCPServerConnection] = {}
_tool_cache: "OrderedDict[str, Any]" = OrderedDict()
_resource_cache: "OrderedDict[str, Any]" = OrderedDict()
_VSCODE_SDK_NAME = "claude-vscode"


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _require_optional_positive_int(
    value: object,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return value


def _cache_namespace(owner_id: Optional[str]) -> str:
    if owner_id is None or not owner_id.strip():
        return "shared"
    return owner_id.strip()


def get_server_cache_key(
    name: str,
    config: McpServerConfig,
    owner_id: Optional[str] = None,
) -> str:
    return f"{_cache_namespace(owner_id)}::{name}-{_config_fingerprint(config)}"


def _resource_cache_key(name: str, owner_id: Optional[str]) -> str:
    return f"{_cache_namespace(owner_id)}::{name}"


def _tool_cache_key(name: str, owner_id: Optional[str]) -> str:
    return f"{_cache_namespace(owner_id)}::{name}"


def _tool_cache_payload(
    cached: Any,
) -> Tuple[float | None, List[Dict[str, Any]]] | None:
    if isinstance(cached, list):
        return None, [dict(tool) for tool in cached if isinstance(tool, Mapping)]
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and isinstance(cached[0], (int, float))
        and isinstance(cached[1], list)
    ):
        return (
            float(cached[0]),
            [dict(tool) for tool in cached[1] if isinstance(tool, Mapping)],
        )
    return None


def _prune_tool_cache(now: float | None = None) -> None:
    if now is None:
        now = time.monotonic()

    ttl_seconds = max(get_mcp_resource_cache_ttl_seconds(), 0)
    if ttl_seconds == 0:
        _tool_cache.clear()
        return

    invalid_keys: List[str] = []
    for key, cached in list(_tool_cache.items()):
        payload = _tool_cache_payload(cached)
        if payload is None:
            invalid_keys.append(key)
    for key in invalid_keys:
        _tool_cache.pop(key, None)

    max_entries = max(get_mcp_resource_cache_max_entries(), 0)
    if max_entries == 0:
        _tool_cache.clear()
        return
    while len(_tool_cache) > max_entries:
        _tool_cache.popitem(last=False)


def _read_cached_tools(
    cache_key: str,
    *,
    allow_stale: bool = False,
) -> List[Dict[str, Any]] | None:
    now = time.monotonic()
    _prune_tool_cache(now)
    ttl_seconds = max(get_mcp_resource_cache_ttl_seconds(), 0)
    if ttl_seconds == 0:
        return None

    cached = _tool_cache.get(cache_key)
    if cached is None:
        return None

    payload = _tool_cache_payload(cached)
    if payload is None:
        _tool_cache.pop(cache_key, None)
        return None

    timestamp, tools = payload
    if timestamp is None:
        timestamp = now
    elif not allow_stale and now - timestamp >= ttl_seconds:
        return None
    _tool_cache.move_to_end(cache_key)
    _tool_cache[cache_key] = (timestamp, [dict(tool) for tool in tools])
    return [dict(tool) for tool in tools]


def _write_cached_tools(
    cache_key: str,
    tools: Sequence[Mapping[str, Any]],
) -> None:
    _tool_cache.pop(cache_key, None)
    _tool_cache[cache_key] = (
        time.monotonic(),
        [dict(tool) for tool in tools if isinstance(tool, Mapping)],
    )
    _prune_tool_cache()


def _resource_cache_payload(
    cached: Any,
) -> Tuple[float | None, List[Dict[str, Any]]] | None:
    if isinstance(cached, list):
        return None, [dict(resource) for resource in cached if isinstance(resource, Mapping)]
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and isinstance(cached[0], (int, float))
        and isinstance(cached[1], list)
    ):
        return (
            float(cached[0]),
            [dict(resource) for resource in cached[1] if isinstance(resource, Mapping)],
        )
    return None


def _prune_resource_cache(now: float | None = None) -> None:
    if now is None:
        now = time.monotonic()

    ttl_seconds = max(get_mcp_resource_cache_ttl_seconds(), 0)
    if ttl_seconds == 0:
        _resource_cache.clear()
        return

    invalid_keys: List[str] = []
    for key, cached in list(_resource_cache.items()):
        payload = _resource_cache_payload(cached)
        if payload is None:
            invalid_keys.append(key)
    for key in invalid_keys:
        _resource_cache.pop(key, None)

    max_entries = max(get_mcp_resource_cache_max_entries(), 0)
    if max_entries == 0:
        _resource_cache.clear()
        return
    while len(_resource_cache) > max_entries:
        _resource_cache.popitem(last=False)


def _read_cached_resources(
    cache_key: str,
    *,
    allow_stale: bool = False,
) -> List[Dict[str, Any]] | None:
    now = time.monotonic()
    _prune_resource_cache(now)
    ttl_seconds = max(get_mcp_resource_cache_ttl_seconds(), 0)
    if ttl_seconds == 0:
        return None

    cached = _resource_cache.get(cache_key)
    if cached is None:
        return None

    payload = _resource_cache_payload(cached)
    if payload is None:
        _resource_cache.pop(cache_key, None)
        return None

    timestamp, resources = payload
    if timestamp is None:
        timestamp = now
    elif not allow_stale and now - timestamp >= ttl_seconds:
        return None
    _resource_cache.move_to_end(cache_key)
    _resource_cache[cache_key] = (timestamp, [dict(resource) for resource in resources])
    return [dict(resource) for resource in resources]


def _write_cached_resources(
    cache_key: str,
    resources: Sequence[Mapping[str, Any]],
) -> None:
    _resource_cache.pop(cache_key, None)
    _resource_cache[cache_key] = (
        time.monotonic(),
        [dict(resource) for resource in resources if isinstance(resource, Mapping)],
    )
    _prune_resource_cache()


def _config_fingerprint(config: McpServerConfig) -> str:
    parts = [config.type or "stdio"]
    if isinstance(config, McpStdioServerConfig):
        parts.append(config.command)
        parts.extend(config.args)
    elif hasattr(config, "url"):
        parts.append(getattr(config, "url", ""))
    return "|".join(parts)


def _iter_vscode_notification_targets(
    owner_id: Optional[str],
) -> Tuple[ConnectedMCPServer, ...]:
    owned: List[ConnectedMCPServer] = []
    shared: List[ConnectedMCPServer] = []

    for candidate in _connection_cache.values():
        if not isinstance(candidate, ConnectedMCPServer):
            continue
        if candidate.type != "connected":
            continue

        config = getattr(candidate, "config", None)
        config_type = getattr(config, "type", None)
        config_name = getattr(config, "name", None)
        if config_type != "sdk":
            continue
        if config_name != _VSCODE_SDK_NAME and candidate.name != _VSCODE_SDK_NAME:
            continue
        if not callable(getattr(candidate.client, "notification", None)):
            continue

        if owner_id and getattr(candidate, "owner_id", None) == owner_id:
            owned.append(candidate)
        elif getattr(candidate, "owner_id", None) is None:
            shared.append(candidate)

    if owner_id and owned:
        return tuple(owned)
    if shared:
        return tuple(shared)
    if owner_id:
        return tuple(owned)
    return ()


async def notify_vscode_file_updated(
    file_path: str,
    old_content: Optional[str],
    new_content: Optional[str],
    *,
    owner_id: Optional[str] = None,
) -> None:
    """Best-effort file_updated notification for a connected claude-vscode SDK server."""

    payload = {
        "method": "file_updated",
        "params": {
            "filePath": file_path,
            "oldContent": old_content,
            "newContent": new_content,
        },
    }

    for target in _iter_vscode_notification_targets(owner_id):
        notification = getattr(target.client, "notification", None)
        if not callable(notification):
            continue
        try:
            result = notification(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            continue


def clear_connection_cache(
    name: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> None:
    namespace = _cache_namespace(owner_id)
    if name is None and owner_id is None:
        for conn in list(_connection_cache.values()):
            _best_effort_cleanup_connection(conn)
        _connection_cache.clear()
        _tool_cache.clear()
        _resource_cache.clear()
        return

    keys_to_remove = []
    for key in _connection_cache:
        if owner_id is not None and not key.startswith(f"{namespace}::"):
            continue
        if name is not None and f"::{name}-" not in key:
            continue
        keys_to_remove.append(key)
    for key in keys_to_remove:
        conn = _connection_cache.pop(key)
        _best_effort_cleanup_connection(conn)

    tool_keys = []
    for key in _tool_cache:
        if owner_id is not None and not key.startswith(f"{namespace}::"):
            continue
        if name is not None and key != _tool_cache_key(name, owner_id):
            continue
        tool_keys.append(key)
    for key in tool_keys:
        _tool_cache.pop(key, None)

    resource_keys = []
    for key in _resource_cache:
        if owner_id is not None and not key.startswith(f"{namespace}::"):
            continue
        if name is not None and key != _resource_cache_key(name, owner_id):
            continue
        resource_keys.append(key)
    for key in resource_keys:
        _resource_cache.pop(key, None)


async def connect_to_server(
    name: str,
    config: McpServerConfig,
    scope: ConfigScope = "user",
    owner_id: Optional[str] = None,
) -> MCPServerConnection:
    validated_name = _require_non_empty_string(name, "name")
    cache_key = get_server_cache_key(validated_name, config, owner_id)
    if cache_key in _connection_cache:
        return _connection_cache[cache_key]

    timeout_ms = get_connection_timeout_ms()

    if isinstance(config, McpStdioServerConfig):
        result = await _connect_stdio(
            validated_name,
            config,
            scope,
            timeout_ms,
            owner_id,
        )
    elif config.type in ("sse", "http", "ws", "sse-ide", "ws-ide", "claudeai-proxy"):
        result = await _connect_remote(
            validated_name,
            config,
            scope,
            timeout_ms,
            owner_id,
        )
    elif config.type == "sdk":
        result = ConnectedMCPServer(
            name=validated_name,
            config=config,
            scope=scope,
            owner_id=owner_id,
        )
    else:
        result = FailedMCPServer(
            name=validated_name,
            config=config,
            scope=scope,
            owner_id=owner_id,
            error=f"Unsupported server type: {config.type}",
        )

    _connection_cache[cache_key] = result
    return result


async def _connect_stdio(
    name: str,
    config: McpStdioServerConfig,
    scope: ConfigScope,
    timeout_ms: int,
    owner_id: Optional[str],
) -> MCPServerConnection:
    cmd = os.environ.get("CLAUDE_CODE_SHELL_PREFIX") or config.command
    if os.environ.get("CLAUDE_CODE_SHELL_PREFIX"):
        args_list = [" ".join([config.command, *config.args])]
    else:
        args_list = list(config.args)

    env = dict(os.environ)
    if config.env:
        env.update(config.env)

    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args_list,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return FailedMCPServer(
            name=name,
            config=config,
            scope=scope,
            owner_id=owner_id,
            error=str(exc),
        )

    startup_probe_s = max(
        min(timeout_ms / 1000.0, STDIO_STARTUP_PROBE_S),
        0.01,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=startup_probe_s)
    except asyncio.TimeoutError:
        return ConnectedMCPServer(
            name=name,
            config=config,
            scope=scope,
            owner_id=owner_id,
            server_info={"name": name, "version": "unknown"},
            transport_process=proc,
        )

    stderr_output = ""
    if proc.stderr is not None:
        try:
            stderr_output = await _read_stream_snippet(proc.stderr)
        except Exception:
            stderr_output = ""

    error_detail = stderr_output or (
        f"Server exited before connection established "
        f"(exit code {proc.returncode})"
    )
    return FailedMCPServer(
        name=name,
        config=config,
        scope=scope,
        owner_id=owner_id,
        error=error_detail,
    )


async def _connect_remote(
    name: str,
    config: McpServerConfig,
    scope: ConfigScope,
    timeout_ms: int,
    owner_id: Optional[str],
) -> MCPServerConnection:
    url = getattr(config, "url", None) or ""
    if not url:
        return FailedMCPServer(
            name=name,
            config=config,
            scope=scope,
            owner_id=owner_id,
            error=f"Remote server '{name}' missing URL",
        )

    resolved_config = config
    if isinstance(
        config,
        (McpSSEServerConfig, McpHTTPServerConfig, McpWebSocketServerConfig),
    ):
        headers = await get_mcp_server_headers(
            name,
            config,
            timeout_s=max(timeout_ms / 1000.0, 0.01),
        )
        resolved_config = replace(config, headers=headers or None)

    client_impl = None
    if getattr(resolved_config, "type", None) in {"http", "sse"}:
        client_impl = RemoteMCPJsonRpcClient(
            name=name,
            url=url,
            headers=getattr(resolved_config, "headers", None),
            transport_type=getattr(resolved_config, "type", "http") or "http",
            timeout_s=max(timeout_ms / 1000.0, 0.01),
        )
    else:
        await asyncio.sleep(0)

    return ConnectedMCPServer(
        name=name,
        config=resolved_config,
        scope=scope,
        owner_id=owner_id,
        server_info={"name": name, "version": "unknown"},
        client=client_impl,
    )


async def disconnect_server(
    name: str,
    config: McpServerConfig,
    scope: ConfigScope = "user",
    owner_id: Optional[str] = None,
) -> None:
    validated_name = _require_non_empty_string(name, "name")
    cache_key = get_server_cache_key(validated_name, config, owner_id)
    conn = _connection_cache.pop(cache_key, None)
    _tool_cache.pop(_tool_cache_key(validated_name, owner_id), None)
    _resource_cache.pop(_resource_cache_key(validated_name, owner_id), None)

    if conn is None or conn.type != "connected":
        return

    close = getattr(conn.client, "close", None)
    if callable(close):
        close()

    if isinstance(config, McpStdioServerConfig):
        await _cleanup_stdio(conn)


async def _cleanup_stdio(connection: ConnectedMCPServer) -> None:
    proc = connection.transport_process
    close = getattr(connection.client, "close", None)
    if callable(close):
        close()
    if proc is None:
        return

    _close_process_stdin(proc)

    if proc.returncode is not None:
        return

    terminated = _terminate_process_gracefully(proc)
    if not terminated:
        _terminate_process(proc)

    if await _wait_for_process_exit(proc, STDIO_SHUTDOWN_TIMEOUT_S):
        return

    _terminate_process(proc)
    await _wait_for_process_exit(proc, STDIO_FORCE_KILL_TIMEOUT_S)


async def _wait_for_process_exit(
    proc: asyncio.subprocess.Process,
    timeout_s: float,
) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        return False


async def _read_stream_snippet(stream: asyncio.StreamReader) -> str:
    chunk = await asyncio.wait_for(stream.read(4096), timeout=0.1)
    return chunk.decode("utf-8", errors="replace").strip()


def _close_process_stdin(proc: asyncio.subprocess.Process) -> None:
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except Exception:
        pass


def _terminate_process_gracefully(proc: asyncio.subprocess.Process) -> bool:
    if proc.returncode is not None:
        return True

    terminate = getattr(proc, "terminate", None)
    if terminate is None:
        return False

    try:
        terminate()
        return True
    except ProcessLookupError:
        return True


def _best_effort_cleanup_connection(connection: MCPServerConnection) -> None:
    if not isinstance(connection, ConnectedMCPServer):
        return

    proc = connection.transport_process
    if proc is None:
        return

    _close_process_stdin(proc)
    _terminate_process_gracefully(proc)


def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def ensure_connected_client(
    client: ConnectedMCPServer,
) -> ConnectedMCPServer:
    if client.config.type == "sdk":
        return client

    cache_key = get_server_cache_key(client.name, client.config, client.owner_id)
    cached = _connection_cache.get(cache_key)
    if isinstance(cached, ConnectedMCPServer):
        return cached

    result = await connect_to_server(
        client.name,
        client.config,
        client.scope,
        owner_id=client.owner_id,
    )
    if not isinstance(result, ConnectedMCPServer):
        raise RuntimeError(f"MCP server '{client.name}' is not connected")
    return result


def _get_available_server_names(
    clients: Iterable[MCPServerConnection],
) -> List[str]:
    return sorted({client.name for client in clients})


def _format_missing_server_message(
    server: str,
    clients: Iterable[MCPServerConnection],
) -> str:
    available = _get_available_server_names(clients)
    return f'Server "{server}" not found. Available servers: {", ".join(available)}'


def _find_server(
    clients: Sequence[MCPServerConnection],
    server: str,
) -> MCPServerConnection:
    for client in clients:
        if client.name == server:
            return client
    raise ValueError(_format_missing_server_message(server, clients))


def _get_capabilities(client: ConnectedMCPServer) -> Mapping[str, Any]:
    capabilities = client.capabilities
    if isinstance(capabilities, Mapping):
        return capabilities
    return {}


def _has_resource_capability(client: ConnectedMCPServer) -> bool:
    capabilities = _get_capabilities(client)
    if client.capabilities is None:
        return True
    return "resources" in capabilities


def _has_tool_capability(client: ConnectedMCPServer) -> bool:
    capabilities = _get_capabilities(client)
    if client.capabilities is None:
        return True
    return "tools" in capabilities


async def fetch_tools_for_client(
    client: MCPServerConnection,
) -> List[Dict[str, Any]]:
    if not isinstance(client, ConnectedMCPServer):
        return []

    connected = await ensure_connected_client(client)
    if not _has_tool_capability(connected):
        return []

    if connected.tools is not None:
        return [dict(tool) for tool in connected.tools if isinstance(tool, Mapping)]

    cache_key = _tool_cache_key(connected.name, connected.owner_id)
    cached = _read_cached_tools(cache_key)
    if cached is not None:
        return cached
    stale_cached = _read_cached_tools(cache_key, allow_stale=True)

    try:
        result = await _invoke_request_method(connected, {"method": "tools/list"})
    except Exception:
        return stale_cached or []

    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        return stale_cached or []

    normalized = [dict(tool) for tool in raw_tools if isinstance(tool, Mapping)]
    _write_cached_tools(cache_key, normalized)
    return [dict(tool) for tool in normalized]


def _normalize_server_resource(
    client: ConnectedMCPServer,
    resource: Mapping[str, Any],
) -> Dict[str, Any]:
    normalized = dict(resource)
    normalized["server"] = client.name
    return normalized


async def _invoke_request_method(
    connected: ConnectedMCPServer,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    client_impl = connected.client
    if client_impl is None:
        raise RuntimeError(f'Server "{connected.name}" is not connected')

    method = getattr(client_impl, "request", None)
    if method is None:
        raise RuntimeError(f'Server "{connected.name}" does not support resources')

    result = method(payload)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, Mapping):
        return dict(result)
    raise RuntimeError(f'Server "{connected.name}" returned invalid MCP response')


async def fetch_resources_for_client(
    client: MCPServerConnection,
) -> List[Dict[str, Any]]:
    if not isinstance(client, ConnectedMCPServer):
        return []

    connected = await ensure_connected_client(client)
    if not _has_resource_capability(connected):
        return []

    if connected.resources is not None:
        return [
            _normalize_server_resource(connected, resource)
            for resource in connected.resources
        ]

    cache_key = _resource_cache_key(connected.name, connected.owner_id)
    cached = _read_cached_resources(cache_key)
    if cached is not None:
        return cached
    stale_cached = _read_cached_resources(cache_key, allow_stale=True)

    try:
        result = await _invoke_request_method(connected, {"method": "resources/list"})
    except Exception:
        return stale_cached or []

    raw_resources = result.get("resources")
    if not isinstance(raw_resources, list):
        return stale_cached or []

    normalized = [
        _normalize_server_resource(connected, resource)
        for resource in raw_resources
        if isinstance(resource, Mapping)
    ]
    _write_cached_resources(cache_key, normalized)
    return normalized


async def list_mcp_resources(
    clients: Sequence[MCPServerConnection],
    server: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if server is not None:
        target = _find_server(clients, _require_non_empty_string(server, "server"))
        return await fetch_resources_for_client(target)

    resources: List[Dict[str, Any]] = []
    for client in clients:
        resources.extend(await fetch_resources_for_client(client))
    return resources


def _persist_blob_content(content: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(content)
    blob = normalized.pop("blob", None)
    if blob is None or normalized.get("blobSavedTo"):
        return normalized

    if isinstance(blob, bytes):
        raw_bytes = blob
    elif isinstance(blob, str):
        raw_bytes = base64.b64decode(blob)
    else:
        raise RuntimeError("Unsupported MCP blob content")

    suffix = ""
    mime_type = normalized.get("mimeType")
    if isinstance(mime_type, str) and "/" in mime_type:
        suffix = "." + mime_type.split("/", 1)[1]

    fd, path = tempfile.mkstemp(prefix="mcp-resource-", suffix=suffix)
    os.close(fd)
    with open(path, "wb") as handle:
        handle.write(raw_bytes)
    normalized["blobSavedTo"] = path
    return normalized


async def read_mcp_resource(
    clients: Sequence[MCPServerConnection],
    server: str,
    uri: str,
) -> Dict[str, Any]:
    validated_server = _require_non_empty_string(server, "server")
    validated_uri = _require_non_empty_string(uri, "uri")
    target = _find_server(clients, validated_server)
    if not isinstance(target, ConnectedMCPServer):
        raise RuntimeError(f'Server "{validated_server}" is not connected')

    connected = await ensure_connected_client(target)
    if not _has_resource_capability(connected):
        raise RuntimeError(f'Server "{validated_server}" does not support resources')

    result = await _invoke_request_method(
        connected,
        {"method": "resources/read", "params": {"uri": validated_uri}},
    )
    raw_contents = result.get("contents")
    if not isinstance(raw_contents, list):
        raise RuntimeError(
            f'Server "{validated_server}" returned invalid resource contents'
        )

    return {
        "contents": [
            _persist_blob_content(content)
            for content in raw_contents
            if isinstance(content, Mapping)
        ]
    }


async def _invoke_tool_method(
    method: Callable[..., Any],
    payload: Mapping[str, Any],
    timeout_ms: Optional[int],
    signal: Any,
    on_progress: Any,
) -> Any:
    kwargs: Dict[str, Any] = {}
    parameter_names = set(inspect.signature(method).parameters)
    if timeout_ms is not None:
        if "timeout_ms" in parameter_names:
            kwargs["timeout_ms"] = timeout_ms
        if "timeout" in parameter_names:
            kwargs["timeout"] = timeout_ms
    if signal is not None and "signal" in parameter_names:
        kwargs["signal"] = signal
    if on_progress is not None:
        if "on_progress" in parameter_names:
            kwargs["on_progress"] = on_progress
        if "onProgress" in parameter_names:
            kwargs["onProgress"] = on_progress

    result = method(payload, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def call_mcp_tool(
    client: ConnectedMCPServer,
    tool: str,
    arguments: Mapping[str, Any],
    meta: Optional[Mapping[str, Any]] = None,
    timeout_ms: Optional[int] = None,
    signal: Any = None,
    on_progress: Any = None,
) -> Dict[str, Any]:
    validated_tool = _require_non_empty_string(tool, "tool")
    validated_arguments = dict(_require_mapping(arguments, "arguments"))
    validated_meta = dict(_require_mapping(meta, "meta")) if meta is not None else None
    validated_timeout_ms = _require_optional_positive_int(timeout_ms, "timeout_ms")
    if validated_timeout_ms is None:
        validated_timeout_ms = get_mcp_tool_timeout_ms()
    connected = await ensure_connected_client(client)
    client_impl = connected.client
    if client_impl is None:
        raise RuntimeError(f"MCP server '{connected.name}' is not connected")

    method = getattr(client_impl, "call_tool", None)
    if method is None:
        method = getattr(client_impl, "callTool", None)
    if method is None:
        raise RuntimeError(f"MCP server '{connected.name}' does not expose tool calls")

    payload: Dict[str, Any] = {"name": validated_tool, "arguments": validated_arguments}
    if validated_meta is not None:
        payload["_meta"] = validated_meta

    result = await _invoke_tool_method(
        method,
        payload,
        validated_timeout_ms,
        signal,
        on_progress,
    )
    if isinstance(result, Mapping):
        return dict(result)
    return {"content": result}


def map_mcp_tool_result_to_tool_result_block(
    content: Any,
    tool_use_id: str,
) -> Dict[str, Any]:
    if isinstance(content, (dict, list)):
        rendered = json.dumps(content, sort_keys=True)
    else:
        rendered = content
    return {
        "tool_use_id": tool_use_id,
        "type": "tool_result",
        "content": rendered,
    }


def are_mcp_configs_equal(
    a: ScopedMcpServerConfig,
    b: ScopedMcpServerConfig,
) -> bool:
    if a.config.type != b.config.type:
        return False
    return _config_fingerprint(a.config) == _config_fingerprint(b.config)


async def connect_servers_batched(
    configs: Dict[str, ScopedMcpServerConfig],
    disabled_names: Sequence[str] = (),
    owner_id: Optional[str] = None,
) -> List[MCPServerConnection]:
    disabled_set = set(disabled_names)
    results: List[MCPServerConnection] = []

    local_entries: List[Tuple[str, ScopedMcpServerConfig]] = []
    remote_entries: List[Tuple[str, ScopedMcpServerConfig]] = []

    for srv_name, srv_config in configs.items():
        if srv_name in disabled_set:
            results.append(
                DisabledMCPServer(
                    name=srv_name,
                    config=srv_config.config,
                    scope=srv_config.scope,
                    owner_id=owner_id,
                )
            )
            continue
        if _is_local_mcp_server(srv_config):
            local_entries.append((srv_name, srv_config))
        else:
            remote_entries.append((srv_name, srv_config))

    local_sem = asyncio.Semaphore(get_mcp_server_connection_batch_size())
    remote_sem = asyncio.Semaphore(get_remote_mcp_server_connection_batch_size())

    async def _connect_with_sem(
        entry: Tuple[str, ScopedMcpServerConfig],
        sem: asyncio.Semaphore,
    ) -> MCPServerConnection:
        async with sem:
            srv_name, srv_config = entry
            return await connect_to_server(
                srv_name,
                srv_config.config,
                srv_config.scope,
                owner_id=owner_id,
            )

    tasks = []
    for entry in local_entries:
        tasks.append(_connect_with_sem(entry, local_sem))
    for entry in remote_entries:
        tasks.append(_connect_with_sem(entry, remote_sem))

    if tasks:
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        for br in batch_results:
            if isinstance(br, BaseException):
                results.append(
                    FailedMCPServer(
                        name="unknown",
                        config=McpStdioServerConfig(command="__error__"),
                        scope="user",
                        owner_id=owner_id,
                        error=str(br),
                    )
                )
            elif isinstance(
                br,
                (
                    ConnectedMCPServer,
                    FailedMCPServer,
                    NeedsAuthMCPServer,
                    PendingMCPServer,
                    DisabledMCPServer,
                ),
            ):
                results.append(br)

    return results


def validate_mcp_client_contract() -> Tuple[bool, Tuple[str, ...]]:
    errors: List[str] = []

    valid_configs = [
        ({"command": "npx", "args": ["-y", "foo"]}, "stdio"),
        ({"type": "stdio", "command": "node", "args": ["server.js"]}, "stdio"),
        ({"type": "sse", "url": "http://localhost:8080/sse"}, "sse"),
        ({"type": "http", "url": "http://localhost:8080/mcp"}, "http"),
        ({"type": "ws", "url": "ws://localhost:8080/ws"}, "ws"),
        ({"type": "sdk", "name": "claude-vscode"}, "sdk"),
        (
            {
                "type": "sse-ide",
                "url": "http://localhost:7777/sse",
                "ideName": "vscode",
            },
            "sse-ide",
        ),
        (
            {"type": "ws-ide", "url": "ws://localhost:7777/ws", "ideName": "vscode"},
            "ws-ide",
        ),
        (
            {
                "type": "claudeai-proxy",
                "url": "https://mcp.example.com",
                "id": "srv_123",
            },
            "claudeai-proxy",
        ),
    ]
    for raw, expected_type in valid_configs:
        try:
            parsed = parse_server_config(raw)
            if parsed.type != expected_type:
                errors.append(
                    f"parse_server_config({raw}) produced type={parsed.type!r}, "
                    f"expected {expected_type!r}"
                )
        except Exception as exc:
            errors.append(f"parse_server_config({raw}) raised {exc}")

    invalid_configs = [
        ({}, "missing command and type"),
        ({"type": "stdio"}, "missing command"),
        ({"type": "sse"}, "missing url"),
        ({"type": "http"}, "missing url"),
        ({"type": "ws"}, "missing url"),
        ({"type": "sdk"}, "missing name"),
        ({"type": "sse-ide", "url": "http://x"}, "missing ideName"),
        ({"type": "ws-ide", "url": "ws://x"}, "missing ideName"),
        ({"type": "claudeai-proxy", "url": "http://x"}, "missing id"),
        ({"type": "claudeai-proxy", "id": "x"}, "missing url"),
        ({"type": "nonexistent"}, "unknown type"),
        ({"command": ""}, "empty command"),
    ]
    for raw, desc in invalid_configs:
        try:
            parse_server_config(raw)
            errors.append(f"parse_server_config({raw}) should have raised for {desc}")
        except ValueError:
            pass
        except Exception as exc:
            errors.append(
                f"parse_server_config({raw}) raised unexpected "
                f"{type(exc).__name__}: {exc}"
            )

    os.environ["_MCP_TEST_VAR"] = "hello"
    try:
        expanded, missing = expand_env_vars_in_string("${_MCP_TEST_VAR} world")
        if expanded != "hello world":
            errors.append(f"env expansion failed: got {expanded!r}")
        if missing:
            errors.append(f"env expansion reported missing: {missing}")
    finally:
        del os.environ["_MCP_TEST_VAR"]

    expanded, missing = expand_env_vars_in_string("${_MCP_MISSING_VAR:-fallback}")
    if expanded != "fallback":
        errors.append(f"default expansion failed: got {expanded!r}")

    expanded, missing = expand_env_vars_in_string("${_MCP_REALLY_MISSING}")
    if "_MCP_REALLY_MISSING" not in missing:
        errors.append(f"missing var not reported: {missing}")

    stdio_cfg = McpStdioServerConfig(command="npx", args=["-y", "foo"])
    sig = get_mcp_server_signature(stdio_cfg)
    if not sig or not sig.startswith("stdio:"):
        errors.append(f"stdio signature wrong: {sig!r}")

    http_cfg = McpHTTPServerConfig(url="http://localhost:8080")
    sig = get_mcp_server_signature(http_cfg)
    if not sig or not sig.startswith("url:"):
        errors.append(f"http signature wrong: {sig!r}")

    sdk_cfg = McpSdkServerConfig(name="test")
    sig = get_mcp_server_signature(sdk_cfg)
    if sig is not None:
        errors.append(f"sdk signature should be None: {sig!r}")

    proxy_url = (
        "https://example.com/v2/session_ingress/shttp/mcp/x"
        "?mcp_url=https%3A%2F%2Freal.server.com"
    )
    unwrapped = unwrap_ccr_proxy_url(proxy_url)
    if unwrapped != "https://real.server.com":
        errors.append(f"CCR unwrap failed: got {unwrapped!r}")

    plain_url = "https://direct.server.com/mcp"
    if unwrap_ccr_proxy_url(plain_url) != plain_url:
        errors.append("CCR unwrap changed a non-proxy URL")

    a = ScopedMcpServerConfig(config=stdio_cfg, scope="user")
    b = ScopedMcpServerConfig(config=stdio_cfg, scope="project")
    if not are_mcp_configs_equal(a, b):
        errors.append("identical configs with different scopes should be equal")

    c = ScopedMcpServerConfig(
        config=McpStdioServerConfig(command="different"),
        scope="user",
    )
    if are_mcp_configs_equal(a, c):
        errors.append("different configs should not be equal")

    key_a = get_server_cache_key("test", stdio_cfg)
    key_b = get_server_cache_key("test", stdio_cfg)
    if key_a != key_b:
        errors.append("cache key not deterministic")

    key_c = get_server_cache_key("other", stdio_cfg)
    if key_a == key_c:
        errors.append("different names produced same cache key")

    async def _test_batched() -> None:
        batch_configs = {
            "srv1": ScopedMcpServerConfig(
                config=McpSdkServerConfig(name="srv1"),
                scope="user",
            ),
            "srv2": ScopedMcpServerConfig(
                config=McpSdkServerConfig(name="srv2"),
                scope="user",
            ),
        }
        batch_results = await connect_servers_batched(
            batch_configs, disabled_names=["srv2"]
        )
        types = {r.name: r.type for r in batch_results}
        if types.get("srv2") != "disabled":
            errors.append(f"srv2 should be disabled, got {types.get('srv2')}")
        if "srv1" not in types:
            errors.append(f"srv1 missing from results: {types}")

    asyncio.run(_test_batched())

    return (len(errors) == 0, tuple(errors))


def validate_mcp_resources_contract() -> Tuple[bool, Tuple[str, ...]]:
    errors: List[str] = []

    class _RequestClient:
        def __init__(self) -> None:
            self.requests: List[Dict[str, Any]] = []

        async def request(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
            self.requests.append(dict(payload))
            method = payload.get("method")
            if method == "resources/list":
                return {
                    "resources": [
                        {
                            "uri": "file:///alpha.txt",
                            "name": "alpha",
                            "mimeType": "text/plain",
                            "description": "Alpha file",
                        }
                    ]
                }
            if method == "resources/read":
                return {
                    "contents": [
                        {
                            "uri": payload["params"]["uri"],
                            "mimeType": "text/plain",
                            "text": "hello",
                        }
                    ]
                }
            raise AssertionError("unexpected request")

    class _ToolClient:
        def __init__(self) -> None:
            self.payloads: List[Dict[str, Any]] = []

        async def call_tool(
            self,
            payload: Mapping[str, Any],
            timeout_ms: Optional[int] = None,
        ) -> Dict[str, Any]:
            record = dict(payload)
            record["timeout_ms"] = timeout_ms
            self.payloads.append(record)
            return {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"arguments": payload.get("arguments")},
                "_meta": payload.get("_meta"),
            }

    async def _raise_missing_resource(
        payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        raise RuntimeError("Resource not found: file:///missing")

    async def _run() -> None:
        request_client = _RequestClient()
        connected = ConnectedMCPServer(
            name="demo",
            config=McpSdkServerConfig(name="demo"),
            scope="user",
            client=request_client,
            capabilities={"resources": {}},
        )
        listed = await list_mcp_resources([connected])
        if listed != [
            {
                "uri": "file:///alpha.txt",
                "name": "alpha",
                "mimeType": "text/plain",
                "description": "Alpha file",
                "server": "demo",
            }
        ]:
            errors.append(f"list_mcp_resources mismatch: {listed!r}")

        read_result = await read_mcp_resource([connected], "demo", "file:///alpha.txt")
        if read_result != {
            "contents": [
                {
                    "uri": "file:///alpha.txt",
                    "mimeType": "text/plain",
                    "text": "hello",
                }
            ]
        }:
            errors.append(f"read_mcp_resource mismatch: {read_result!r}")

        try:
            await list_mcp_resources([connected], server="missing")
            errors.append("missing server should raise during list")
        except ValueError as exc:
            expected = 'Server "missing" not found. Available servers: demo'
            if str(exc) != expected:
                errors.append(f"missing server message mismatch: {exc!s}")

        missing_resource = ConnectedMCPServer(
            name="missing-resource",
            config=McpSdkServerConfig(name="missing-resource"),
            scope="user",
            client=_RequestClient(),
            capabilities={"resources": {}},
        )
        missing_resource.client.request = _raise_missing_resource  # type: ignore[attr-defined]
        try:
            await read_mcp_resource(
                [missing_resource],
                "missing-resource",
                "file:///missing",
            )
            errors.append("missing URI should raise during read")
        except RuntimeError as exc:
            if str(exc) != "Resource not found: file:///missing":
                errors.append(f"missing URI message mismatch: {exc!s}")

        tool_client = _ToolClient()
        tool_server = ConnectedMCPServer(
            name="tool-demo",
            config=McpSdkServerConfig(name="tool-demo"),
            scope="user",
            client=tool_client,
        )
        tool_response = await call_mcp_tool(
            tool_server,
            tool="demo-tool",
            arguments={"query": {"nested": True}},
            meta={"trace": {"id": 7}},
            timeout_ms=123,
        )
        if tool_client.payloads != [
            {
                "name": "demo-tool",
                "arguments": {"query": {"nested": True}},
                "_meta": {"trace": {"id": 7}},
                "timeout_ms": 123,
            }
        ]:
            errors.append(f"tool payload was reshaped: {tool_client.payloads!r}")
        if tool_response != {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"arguments": {"query": {"nested": True}}},
            "_meta": {"trace": {"id": 7}},
        }:
            errors.append(f"tool response was reshaped: {tool_response!r}")

    asyncio.run(_run())
    return (len(errors) == 0, tuple(errors))
