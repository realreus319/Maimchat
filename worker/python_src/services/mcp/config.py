"""MCP configuration parsing and environment-variable expansion.

Python port of src/services/mcp/config.ts — covers config loading from
JSON files, env-var expansion, server-signature computation, and
parse-time validation.  Enterprise policy filtering and plugin dedup
are out of scope for this task (they belong to higher-level orchestration).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .normalization import normalize_name_for_mcp
from .types import (
    ConfigScope,
    McpHTTPServerConfig,
    McpJsonConfig,
    McpSSEServerConfig,
    McpServerConfig,
    McpStdioServerConfig,
    McpWebSocketServerConfig,
    ScopedMcpServerConfig,
    ValidationError,
    parse_server_config,
)


# ---------------------------------------------------------------------------
# Env-var expansion (port of src/services/mcp/envExpansion.ts)
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def expand_env_vars_in_string(value: str) -> Tuple[str, List[str]]:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in *value*.

    Returns ``(expanded_string, missing_vars)`` matching the TS contract.
    """
    missing: List[str] = []

    def _replace(match: re.Match) -> str:
        var_content = match.group(1)
        parts = var_content.split(":-", 1)
        var_name = parts[0]
        default = parts[1] if len(parts) == 2 else None
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        missing.append(var_name)
        return match.group(0)

    expanded = _ENV_VAR_RE.sub(_replace, value)
    return expanded, missing


def expand_env_vars(config: McpServerConfig) -> Tuple[McpServerConfig, List[str]]:
    """Expand env vars in a server config.  Returns (expanded, missing_vars)."""
    all_missing: List[str] = []

    if isinstance(config, McpStdioServerConfig):
        cmd, m = expand_env_vars_in_string(config.command)
        all_missing.extend(m)
        args: List[str] = []
        for a in config.args:
            ea, m = expand_env_vars_in_string(a)
            all_missing.extend(m)
            args.append(ea)
        env: Optional[Dict[str, str]] = None
        if config.env:
            env = {}
            for k, v in config.env.items():
                ev, m = expand_env_vars_in_string(v)
                all_missing.extend(m)
                env[k] = ev
        return McpStdioServerConfig(
            command=cmd,
            args=args,
            env=env,
            type="stdio",
        ), list(set(all_missing))

    if isinstance(
        config, (McpSSEServerConfig, McpHTTPServerConfig, McpWebSocketServerConfig)
    ):
        assert hasattr(config, "url")
        url, m = expand_env_vars_in_string(getattr(config, "url"))
        all_missing.extend(m)
        headers: Optional[Dict[str, str]] = None
        raw_headers = getattr(config, "headers", None)
        if raw_headers:
            headers = {}
            for k, v in raw_headers.items():
                ev, m = expand_env_vars_in_string(v)
                all_missing.extend(m)
                headers[k] = ev
        cls = type(config)
        kwargs: Dict[str, Any] = {"url": url, "headers": headers, "type": config.type}
        raw_helper = getattr(config, "headers_helper", None)
        if raw_helper:
            h, m = expand_env_vars_in_string(raw_helper)
            all_missing.extend(m)
            kwargs["headers_helper"] = h
        if isinstance(config, (McpSSEServerConfig, McpHTTPServerConfig)):
            kwargs["oauth"] = getattr(config, "oauth", None)
        return cls(**kwargs), list(set(all_missing))

    return config, []


# ---------------------------------------------------------------------------
# Server signature (port of getMcpServerSignature)
# ---------------------------------------------------------------------------

_CCR_PROXY_MARKERS = [
    "/v2/session_ingress/shttp/mcp/",
    "/v2/ccr-sessions/",
]


def unwrap_ccr_proxy_url(url: str) -> str:
    """If *url* is a CCR proxy URL, return the original vendor URL."""
    if not any(marker in url for marker in _CCR_PROXY_MARKERS):
        return url
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        original = parse_qs(parsed.query).get("mcp_url")
        return original[0] if original else url
    except Exception:
        return url


def get_server_command_array(config: McpServerConfig) -> Optional[List[str]]:
    """Return ``[command, *args]`` for stdio servers, else ``None``."""
    if config.type not in (None, "stdio"):
        return None
    assert isinstance(config, McpStdioServerConfig)
    return [config.command, *config.args]


def get_server_url(config: McpServerConfig) -> Optional[str]:
    """Return the URL for remote servers, else ``None``."""
    return getattr(config, "url", None)


def get_mcp_server_signature(config: McpServerConfig) -> Optional[str]:
    """Compute a dedup signature.  Returns ``None`` for SDK servers."""
    cmd = get_server_command_array(config)
    if cmd:
        return f"stdio:{json.dumps(cmd)}"
    url = get_server_url(config)
    if url:
        return f"url:{unwrap_ccr_proxy_url(url)}"
    return None


# ---------------------------------------------------------------------------
# Config parsing (ports of parseMcpConfig / parseMcpConfigFromFilePath)
# ---------------------------------------------------------------------------


def parse_mcp_config(
    config_object: Any,
    *,
    expand_vars: bool = True,
    scope: ConfigScope,
    file_path: Optional[str] = None,
) -> Tuple[Optional[McpJsonConfig], List[ValidationError]]:
    """Validate an MCP config object and optionally expand env vars.

    Mirrors ``parseMcpConfig`` from the TS source.
    """
    if not isinstance(config_object, dict):
        return None, [
            ValidationError(
                path="",
                message="MCP config must be a JSON object",
                file=file_path,
                scope=scope,
            )
        ]

    raw_servers = config_object.get("mcpServers")
    if raw_servers is None:
        return None, [
            ValidationError(
                path="mcpServers",
                message="Missing required field: mcpServers",
                file=file_path,
                scope=scope,
            )
        ]

    if not isinstance(raw_servers, dict):
        return None, [
            ValidationError(
                path="mcpServers",
                message="mcpServers must be an object",
                file=file_path,
                scope=scope,
            )
        ]

    errors: List[ValidationError] = []
    validated: Dict[str, McpServerConfig] = {}

    for name, raw_config in raw_servers.items():
        if not isinstance(raw_config, dict):
            errors.append(
                ValidationError(
                    path=f"mcpServers.{name}",
                    message="Server config must be an object",
                    file=file_path,
                    scope=scope,
                    severity="fatal",
                )
            )
            continue

        try:
            parsed = parse_server_config(raw_config)
        except ValueError as exc:
            errors.append(
                ValidationError(
                    path=f"mcpServers.{name}",
                    message=f"Does not adhere to MCP server configuration schema: {exc}",
                    file=file_path,
                    scope=scope,
                    severity="fatal",
                )
            )
            continue

        config_to_check = parsed
        if expand_vars:
            expanded, missing = expand_env_vars(parsed)
            if missing:
                errors.append(
                    ValidationError(
                        path=f"mcpServers.{name}",
                        message=f"Missing environment variables: {', '.join(missing)}",
                        file=file_path,
                        scope=scope,
                        severity="warning",
                    )
                )
            config_to_check = expanded

        validated[name] = config_to_check

    if not validated and errors:
        return None, errors

    return McpJsonConfig(mcp_servers=validated), errors


def parse_mcp_config_from_file(
    file_path: str,
    *,
    expand_vars: bool = True,
    scope: ConfigScope,
) -> Tuple[Optional[McpJsonConfig], List[ValidationError]]:
    """Read, parse, and validate an MCP config file.

    Mirrors ``parseMcpConfigFromFilePath`` from the TS source.
    """
    try:
        with open(file_path) as f:
            content = f.read()
    except FileNotFoundError:
        return None, [
            ValidationError(
                path="",
                message=f"MCP config file not found: {file_path}",
                file=file_path,
                scope=scope,
            )
        ]
    except OSError as exc:
        return None, [
            ValidationError(
                path="",
                message=f"Failed to read file: {exc}",
                file=file_path,
                scope=scope,
            )
        ]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None, [
            ValidationError(
                path="",
                message="MCP config is not a valid JSON",
                file=file_path,
                scope=scope,
            )
        ]

    return parse_mcp_config(
        data, expand_vars=expand_vars, scope=scope, file_path=file_path
    )


# ---------------------------------------------------------------------------
# Add-scope helper (mirrors addScopeToServers)
# ---------------------------------------------------------------------------


def add_scope_to_servers(
    servers: Dict[str, McpServerConfig],
    scope: ConfigScope,
) -> Dict[str, ScopedMcpServerConfig]:
    """Annotate each server config with its source scope."""
    result: Dict[str, ScopedMcpServerConfig] = {}
    for name, config in servers.items():
        result[name] = ScopedMcpServerConfig(config=config, scope=scope)
    return result


# ---------------------------------------------------------------------------
# Project .mcp.json loading / approval filtering / signature dedupe
# ---------------------------------------------------------------------------

_MCP_JSON_FILENAME = ".mcp.json"


def get_project_mcp_configs(
    cwd: str | None,
    *,
    expand_vars: bool = True,
) -> Tuple[Dict[str, ScopedMcpServerConfig], List[ValidationError]]:
    """Load and merge ``.mcp.json`` files from parent directories down to *cwd*.

    Files closer to the working directory override parent definitions.
    Missing files are ignored; malformed files are reported in ``errors``.
    """

    if not cwd:
        return {}, []

    start_dir = os.path.abspath(cwd)
    directories: List[str] = []
    current_dir = start_dir
    while current_dir != os.path.dirname(current_dir):
        directories.append(current_dir)
        current_dir = os.path.dirname(current_dir)
    if not directories:
        directories.append(start_dir)

    merged: Dict[str, ScopedMcpServerConfig] = {}
    errors: List[ValidationError] = []

    for directory in reversed(directories):
        config, parse_errors = parse_mcp_config_from_file(
            os.path.join(directory, _MCP_JSON_FILENAME),
            expand_vars=expand_vars,
            scope="project",
        )
        if config is None:
            errors.extend(
                error
                for error in parse_errors
                if not error.message.startswith("MCP config file not found")
            )
            continue
        if config.mcp_servers:
            merged.update(add_scope_to_servers(config.mcp_servers, "project"))
        errors.extend(parse_errors)

    return merged, errors


def get_project_mcp_server_status(
    server_name: str,
    settings: Mapping[str, Any] | None,
) -> str:
    """Return ``approved``, ``rejected`` or ``pending`` for a project MCP server."""

    normalized_name = normalize_name_for_mcp(server_name)
    settings_obj = settings or {}

    disabled = settings_obj.get("disabledMcpjsonServers")
    if isinstance(disabled, list):
        for candidate in disabled:
            if isinstance(candidate, str) and normalize_name_for_mcp(candidate) == normalized_name:
                return "rejected"

    enabled = settings_obj.get("enabledMcpjsonServers")
    if isinstance(enabled, list):
        for candidate in enabled:
            if isinstance(candidate, str) and normalize_name_for_mcp(candidate) == normalized_name:
                return "approved"

    if settings_obj.get("enableAllProjectMcpServers") is True:
        return "approved"

    return "pending"


def is_mcp_server_disabled(
    server_name: str,
    settings: Mapping[str, Any] | None,
) -> bool:
    """Return whether a project-scoped MCP server is explicitly disabled."""

    return get_project_mcp_server_status(server_name, settings) == "rejected"


def filter_project_mcp_servers(
    servers: Mapping[str, ScopedMcpServerConfig],
    settings: Mapping[str, Any] | None,
) -> Dict[str, ScopedMcpServerConfig]:
    """Keep only approved project-scoped ``.mcp.json`` servers."""

    return {
        name: scoped
        for name, scoped in servers.items()
        if get_project_mcp_server_status(name, settings) == "approved"
    }


def dedupe_scoped_mcp_servers(
    servers: Mapping[str, ScopedMcpServerConfig],
) -> Dict[str, ScopedMcpServerConfig]:
    """Deduplicate servers by content signature, keeping later entries.

    This mirrors the TS behavior of preferring the most-local/manual definition
    when multiple scopes point at the same underlying command or URL.
    """

    items = list(servers.items())
    keep_names: set[str] = set()
    seen_signatures: set[str] = set()

    for name, scoped in reversed(items):
        signature = get_mcp_server_signature(scoped.config)
        if not signature:
            keep_names.add(name)
            continue
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        keep_names.add(name)

    return {name: scoped for name, scoped in items if name in keep_names}
