"""MCP (Model Context Protocol) service modules.

Python port of src/services/mcp/* — config parsing, connection lifecycle,
and client management. Resource/tool surfaces live separately.
"""

from .config import (
    dedupe_scoped_mcp_servers,
    expand_env_vars,
    expand_env_vars_in_string,
    filter_project_mcp_servers,
    get_mcp_server_signature,
    get_project_mcp_configs,
    get_project_mcp_server_status,
    is_mcp_server_disabled,
    parse_mcp_config,
    parse_mcp_config_from_file,
    unwrap_ccr_proxy_url,
)
from .headers_helper import get_mcp_headers_from_helper, get_mcp_server_headers
from .in_process_transport import InProcessTransport, create_linked_transport_pair
from .normalization import normalize_name_for_mcp
from .official_registry import (
    is_official_mcp_url,
    normalize_official_registry_url,
    prefetch_official_mcp_urls,
    reset_official_mcp_urls_for_testing,
)
from .client import clear_connection_cache

__all__ = [
    "InProcessTransport",
    "clear_connection_cache",
    "create_linked_transport_pair",
    "dedupe_scoped_mcp_servers",
    "expand_env_vars",
    "expand_env_vars_in_string",
    "filter_project_mcp_servers",
    "get_mcp_headers_from_helper",
    "get_mcp_server_headers",
    "get_mcp_server_signature",
    "get_project_mcp_configs",
    "get_project_mcp_server_status",
    "is_mcp_server_disabled",
    "is_official_mcp_url",
    "normalize_name_for_mcp",
    "normalize_official_registry_url",
    "parse_mcp_config",
    "parse_mcp_config_from_file",
    "prefetch_official_mcp_urls",
    "reset_official_mcp_urls_for_testing",
    "unwrap_ccr_proxy_url",
]
