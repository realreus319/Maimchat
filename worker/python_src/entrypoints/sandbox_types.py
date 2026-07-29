"""
Sandbox configuration types.

Python port of src/entrypoints/sandboxTypes.ts.

Single source of truth for sandbox configuration schemas.  Both the SDK
and settings validation import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ============================================================================
# Network configuration
# ============================================================================


@dataclass
class SandboxNetworkConfig:
    """Network configuration for sandbox."""

    allowed_domains: Optional[List[str]] = None
    allow_managed_domains_only: Optional[bool] = None
    allow_unix_sockets: Optional[List[str]] = None
    allow_all_unix_sockets: Optional[bool] = None
    allow_local_binding: Optional[bool] = None
    http_proxy_port: Optional[int] = None
    socks_proxy_port: Optional[int] = None


# ============================================================================
# Filesystem configuration
# ============================================================================


@dataclass
class SandboxFilesystemConfig:
    """Filesystem configuration for sandbox."""

    allow_write: Optional[List[str]] = None
    deny_write: Optional[List[str]] = None
    deny_read: Optional[List[str]] = None
    allow_read: Optional[List[str]] = None
    allow_managed_read_paths_only: Optional[bool] = None


# ============================================================================
# Sandbox settings
# ============================================================================


@dataclass
class SandboxRipgrepConfig:
    """Custom ripgrep configuration for bundled ripgrep support."""

    command: str = ""
    args: Optional[List[str]] = None
    argv0: Optional[str] = None


@dataclass
class SandboxSettings:
    """Top-level sandbox settings.

    Corresponds to SandboxSettingsSchema in the TypeScript source.
    """

    enabled: Optional[bool] = None
    fail_if_unavailable: Optional[bool] = None
    enabled_platforms: Optional[List[str]] = None
    auto_allow_bash_if_sandboxed: Optional[bool] = None
    allow_unsandboxed_commands: Optional[bool] = None
    network: Optional[SandboxNetworkConfig] = None
    filesystem: Optional[SandboxFilesystemConfig] = None
    ignore_violations: Optional[Dict[str, List[str]]] = None
    enable_weaker_nested_sandbox: Optional[bool] = None
    enable_weaker_network_isolation: Optional[bool] = None
    excluded_commands: Optional[List[str]] = None
    ripgrep: Optional[SandboxRipgrepConfig] = None


# ============================================================================
# Runtime config (produced by convert_to_sandbox_runtime_config)
# ============================================================================


@dataclass
class SandboxRuntimeNetworkConfig:
    """Network portion of SandboxRuntimeConfig."""

    allowed_domains: List[str] = field(default_factory=list)
    denied_domains: List[str] = field(default_factory=list)
    allow_unix_sockets: Optional[List[str]] = None
    allow_all_unix_sockets: Optional[bool] = None
    allow_local_binding: Optional[bool] = None
    http_proxy_port: Optional[int] = None
    socks_proxy_port: Optional[int] = None


@dataclass
class SandboxRuntimeFilesystemConfig:
    """Filesystem portion of SandboxRuntimeConfig."""

    deny_read: List[str] = field(default_factory=list)
    allow_read: List[str] = field(default_factory=list)
    allow_write: List[str] = field(default_factory=list)
    deny_write: List[str] = field(default_factory=list)


@dataclass
class SandboxRuntimeConfig:
    """Full runtime config passed to the sandbox manager."""

    network: SandboxRuntimeNetworkConfig = field(
        default_factory=SandboxRuntimeNetworkConfig
    )
    filesystem: SandboxRuntimeFilesystemConfig = field(
        default_factory=SandboxRuntimeFilesystemConfig
    )
    ignore_violations: Optional[Dict[str, List[str]]] = None
    enable_weaker_nested_sandbox: Optional[bool] = None
    enable_weaker_network_isolation: Optional[bool] = None
    ripgrep: Optional[SandboxRipgrepConfig] = None


# ============================================================================
# Dependency check result
# ============================================================================


@dataclass
class SandboxDependencyCheck:
    """Result of checking sandbox dependencies."""

    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ============================================================================
# Violation types
# ============================================================================


@dataclass
class SandboxViolationEvent:
    """A sandbox violation event."""

    tool: str = ""
    path: str = ""
    operation: str = ""
    message: str = ""


# ============================================================================
# Settings extraction helpers
# ============================================================================


def extract_sandbox_settings(raw: dict) -> SandboxSettings:
    """Extract SandboxSettings from a merged settings dict.

    Handles missing/None ``sandbox`` key gracefully.
    """
    sandbox_raw = raw.get("sandbox")
    if sandbox_raw is None or not isinstance(sandbox_raw, dict):
        return SandboxSettings()

    network_raw = sandbox_raw.get("network")
    network = (
        _extract_network_config(network_raw) if isinstance(network_raw, dict) else None
    )

    fs_raw = sandbox_raw.get("filesystem")
    filesystem = (
        _extract_filesystem_config(fs_raw) if isinstance(fs_raw, dict) else None
    )

    rg_raw = sandbox_raw.get("ripgrep")
    ripgrep = None
    if isinstance(rg_raw, dict):
        ripgrep = SandboxRipgrepConfig(
            command=rg_raw.get("command", ""),
            args=rg_raw.get("args"),
            argv0=rg_raw.get("argv0"),
        )

    return SandboxSettings(
        enabled=sandbox_raw.get("enabled"),
        fail_if_unavailable=sandbox_raw.get("failIfUnavailable"),
        enabled_platforms=sandbox_raw.get("enabledPlatforms"),
        auto_allow_bash_if_sandboxed=sandbox_raw.get("autoAllowBashIfSandboxed"),
        allow_unsandboxed_commands=sandbox_raw.get("allowUnsandboxedCommands"),
        network=network,
        filesystem=filesystem,
        ignore_violations=sandbox_raw.get("ignoreViolations"),
        enable_weaker_nested_sandbox=sandbox_raw.get("enableWeakerNestedSandbox"),
        enable_weaker_network_isolation=sandbox_raw.get("enableWeakerNetworkIsolation"),
        excluded_commands=sandbox_raw.get("excludedCommands"),
        ripgrep=ripgrep,
    )


def _extract_network_config(raw: dict) -> SandboxNetworkConfig:
    return SandboxNetworkConfig(
        allowed_domains=raw.get("allowedDomains"),
        allow_managed_domains_only=raw.get("allowManagedDomainsOnly"),
        allow_unix_sockets=raw.get("allowUnixSockets"),
        allow_all_unix_sockets=raw.get("allowAllUnixSockets"),
        allow_local_binding=raw.get("allowLocalBinding"),
        http_proxy_port=raw.get("httpProxyPort"),
        socks_proxy_port=raw.get("socksProxyPort"),
    )


def _extract_filesystem_config(raw: dict) -> SandboxFilesystemConfig:
    return SandboxFilesystemConfig(
        allow_write=raw.get("allowWrite"),
        deny_write=raw.get("denyWrite"),
        deny_read=raw.get("denyRead"),
        allow_read=raw.get("allowRead"),
        allow_managed_read_paths_only=raw.get("allowManagedReadPathsOnly"),
    )
