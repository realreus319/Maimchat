"""Pure utility functions for MCP name normalization."""

from __future__ import annotations

import re


CLAUDEAI_SERVER_PREFIX = "claude.ai "
_INVALID_MCP_NAME_CHAR_RE = re.compile(r"[^a-zA-Z0-9_-]")
_REPEATED_UNDERSCORE_RE = re.compile(r"_+")
_EDGE_UNDERSCORE_RE = re.compile(r"^_|_$")


def normalize_name_for_mcp(name: str) -> str:
    """Normalize an MCP-visible name to ASCII word characters.

    Mirrors the TypeScript helper used for ``mcp__server__tool`` identifiers.
    """

    normalized = _INVALID_MCP_NAME_CHAR_RE.sub("_", name)
    if name.startswith(CLAUDEAI_SERVER_PREFIX):
        normalized = _REPEATED_UNDERSCORE_RE.sub("_", normalized)
        normalized = _EDGE_UNDERSCORE_RE.sub("", normalized)
    return normalized
