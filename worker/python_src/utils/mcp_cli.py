from __future__ import annotations

import json
import os
from typing import Any, Mapping

from ..services.mcp.config import (
    dedupe_scoped_mcp_servers,
    get_mcp_server_signature,
    get_project_mcp_configs,
    get_project_mcp_server_status,
)
from ..services.mcp.normalization import normalize_name_for_mcp
from ..services.mcp.types import (
    McpClaudeAIProxyServerConfig,
    McpHTTPServerConfig,
    McpSdkServerConfig,
    McpSSEIDEServerConfig,
    McpSSEServerConfig,
    McpServerConfig,
    McpStdioServerConfig,
    McpWebSocketIDEServerConfig,
    McpWebSocketServerConfig,
    ValidationError,
    parse_server_config,
)
from .settings import get_initial_settings, update_settings_for_source

_PROJECT_MCP_FILENAME = ".mcp.json"


def get_project_mcp_file_path(cwd: str | None = None) -> str:
    root = os.path.abspath(cwd or os.getcwd())
    return os.path.join(root, _PROJECT_MCP_FILENAME)


def _project_root(cwd: str | None = None) -> str:
    return os.path.abspath(cwd or os.getcwd())


def _serialize_server_config(config: McpServerConfig) -> dict[str, Any]:
    if isinstance(config, McpStdioServerConfig):
        payload: dict[str, Any] = {
            "type": "stdio",
            "command": config.command,
            "args": list(config.args),
        }
        if config.env:
            payload["env"] = dict(config.env)
        return payload
    if isinstance(config, McpSSEServerConfig):
        payload = {"type": "sse", "url": config.url}
        if config.headers:
            payload["headers"] = dict(config.headers)
        if config.headers_helper:
            payload["headersHelper"] = config.headers_helper
        if config.oauth is not None:
            payload["oauth"] = config.oauth
        return payload
    if isinstance(config, McpHTTPServerConfig):
        payload = {"type": "http", "url": config.url}
        if config.headers:
            payload["headers"] = dict(config.headers)
        if config.headers_helper:
            payload["headersHelper"] = config.headers_helper
        if config.oauth is not None:
            payload["oauth"] = config.oauth
        return payload
    if isinstance(config, McpWebSocketServerConfig):
        payload = {"type": "ws", "url": config.url}
        if config.headers:
            payload["headers"] = dict(config.headers)
        if config.headers_helper:
            payload["headersHelper"] = config.headers_helper
        return payload
    if isinstance(config, McpSSEIDEServerConfig):
        payload = {
            "type": "sse-ide",
            "url": config.url,
            "ideName": config.ide_name,
        }
        if config.ide_running_in_windows is not None:
            payload["ideRunningInWindows"] = config.ide_running_in_windows
        return payload
    if isinstance(config, McpWebSocketIDEServerConfig):
        payload = {
            "type": "ws-ide",
            "url": config.url,
            "ideName": config.ide_name,
        }
        if config.auth_token:
            payload["authToken"] = config.auth_token
        if config.ide_running_in_windows is not None:
            payload["ideRunningInWindows"] = config.ide_running_in_windows
        return payload
    if isinstance(config, McpSdkServerConfig):
        return {"type": "sdk", "name": config.name}
    if isinstance(config, McpClaudeAIProxyServerConfig):
        return {"type": "claudeai-proxy", "url": config.url, "id": config.id}
    raise TypeError(f"Unsupported MCP server config type: {type(config)!r}")


def _serialize_validation_error(error: ValidationError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": error.path,
        "message": error.message,
        "severity": error.severity,
    }
    if error.file is not None:
        payload["file"] = error.file
    if error.scope is not None:
        payload["scope"] = error.scope
    if error.suggestion is not None:
        payload["suggestion"] = error.suggestion
    return payload


def _normalize_server_payload(
    name: str,
    config: McpServerConfig,
    *,
    settings: Mapping[str, Any] | None,
    scope: str,
) -> dict[str, Any]:
    raw = _serialize_server_config(config)
    return {
        "name": name,
        "scope": scope,
        "transport": raw.get("type", "stdio"),
        "status": get_project_mcp_server_status(name, settings),
        "signature": get_mcp_server_signature(config),
        "config": raw,
    }


def _resolve_name(
    candidates: Mapping[str, Any],
    requested_name: str,
) -> str | None:
    requested = normalize_name_for_mcp(requested_name)
    for candidate in candidates:
        if normalize_name_for_mcp(candidate) == requested:
            return candidate
    return None


def _read_project_mcp_document(cwd: str | None = None) -> dict[str, Any]:
    path = get_project_mcp_file_path(cwd)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {"mcpServers": {}}
    except OSError as exc:
        raise ValueError(f"Failed to read {path}: {exc}") from exc

    if raw.strip() == "":
        return {"mcpServers": {}}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")

    servers = payload.get("mcpServers")
    if servers is None:
        payload["mcpServers"] = {}
        return payload
    if not isinstance(servers, dict):
        raise ValueError(f"{path}.mcpServers must be a JSON object")
    return payload


def _write_project_mcp_document(payload: Mapping[str, Any], cwd: str | None = None) -> None:
    path = get_project_mcp_file_path(cwd)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def list_project_mcp_servers(
    *,
    cwd: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _project_root(cwd)
    effective_settings = dict(
        settings
        or get_initial_settings(
            config_home=None,
            project_root=root,
        )
    )
    servers, errors = get_project_mcp_configs(root)
    deduped = dedupe_scoped_mcp_servers(servers)
    items = [
        _normalize_server_payload(
            name,
            scoped.config,
            settings=effective_settings,
            scope=scoped.scope,
        )
        for name, scoped in sorted(
            deduped.items(),
            key=lambda item: normalize_name_for_mcp(item[0]),
        )
    ]
    return {
        "projectRoot": root,
        "mcpFile": get_project_mcp_file_path(root),
        "servers": items,
        "errors": [
            _serialize_validation_error(error)
            for error in errors
        ],
    }


def get_project_mcp_server_details(
    name: str,
    *,
    cwd: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    payload = list_project_mcp_servers(cwd=cwd, settings=settings)
    resolved_name = _resolve_name(
        {server["name"]: server for server in payload["servers"]},
        name,
    )
    if resolved_name is None:
        return None
    for server in payload["servers"]:
        if server["name"] == resolved_name:
            return server
    return None


def upsert_project_mcp_server(
    name: str,
    config_payload: Mapping[str, Any],
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    parsed = parse_server_config(dict(config_payload))
    document = _read_project_mcp_document(cwd)
    servers = document.setdefault("mcpServers", {})
    assert isinstance(servers, dict)
    existing_name = _resolve_name(servers, name)
    stored_name = existing_name or name
    servers[stored_name] = _serialize_server_config(parsed)
    _write_project_mcp_document(document, cwd)
    return _normalize_server_payload(
        stored_name,
        parsed,
        settings=get_initial_settings(project_root=_project_root(cwd)),
        scope="project",
    )


def remove_project_mcp_server(
    name: str,
    *,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    document = _read_project_mcp_document(cwd)
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{get_project_mcp_file_path(cwd)}.mcpServers must be a JSON object")
    resolved_name = _resolve_name(servers, name)
    if resolved_name is None:
        return None
    removed = servers.pop(resolved_name)
    if not servers:
        document["mcpServers"] = {}
    _write_project_mcp_document(document, cwd)
    parsed = parse_server_config(dict(removed))
    return _normalize_server_payload(
        resolved_name,
        parsed,
        settings=get_initial_settings(project_root=_project_root(cwd)),
        scope="project",
    )


def reset_project_mcp_choices(
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    root = _project_root(cwd)
    return update_settings_for_source(
        "localSettings",
        {
            "enabledMcpjsonServers": None,
            "disabledMcpjsonServers": None,
            "enableAllProjectMcpServers": None,
        },
        project_root=root,
    )


def format_project_mcp_listing(payload: Mapping[str, Any]) -> str:
    servers = payload.get("servers")
    errors = payload.get("errors")
    if not isinstance(servers, list):
        return "0 project MCP servers"
    lines = [f"{len(servers)} project MCP servers"]
    for server in servers:
        if not isinstance(server, Mapping):
            continue
        name = str(server.get("name", "unknown"))
        transport = str(server.get("transport", "unknown"))
        status = str(server.get("status", "unknown"))
        lines.append(f"{name} · {transport} · {status}")
    if isinstance(errors, list) and errors:
        lines.append("Warnings:")
        for error in errors:
            if not isinstance(error, Mapping):
                continue
            path = error.get("path") or "<root>"
            message = error.get("message") or "unknown error"
            lines.append(f"{path}: {message}")
    return "\n".join(lines)
