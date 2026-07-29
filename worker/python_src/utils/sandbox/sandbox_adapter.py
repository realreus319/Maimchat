"""Adapter layer wrapping sandbox-runtime with CLI-specific integrations.

Python port of src/utils/sandbox/sandbox-adapter.ts.

Provides the bridge between external sandbox-runtime semantics and the
settings system, tool integration, and process guardrails.  Linux-first
scope; macOS and Windows are deferred to later platform proof tasks.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...entrypoints.sandbox_types import (
    SandboxDependencyCheck,
    SandboxRuntimeConfig,
    SandboxRuntimeFilesystemConfig,
    SandboxRuntimeNetworkConfig,
    SandboxSettings,
    SandboxViolationEvent,
    extract_sandbox_settings,
)
from ...utils.permissions.permission_rule_parser import (
    permission_rule_value_from_string as _parse_permission_rule_value,
)
from ...utils.settings.constants import SETTING_SOURCES

# Tool name constants (mirrors TS toolName imports)
BASH_TOOL_NAME = "Bash"
FILE_EDIT_TOOL_NAME = "Edit"
FILE_READ_TOOL_NAME = "Read"
WEB_FETCH_TOOL_NAME = "WebFetch"


# ============================================================================
# Platform detection
# ============================================================================


@lru_cache(maxsize=1)
def get_platform() -> str:
    sys_platform = sys.platform
    if sys_platform == "darwin":
        return "macos"
    if sys_platform == "win32":
        return "windows"
    if sys_platform == "linux":
        try:
            proc_version = Path("/proc/version").read_text()
            lower = proc_version.lower()
            if "microsoft" in lower or "wsl" in lower:
                return "wsl"
        except (FileNotFoundError, OSError):
            pass
        return "linux"
    return "unknown"


SUPPORTED_PLATFORMS: List[str] = ["macos", "linux", "wsl"]


# ============================================================================
# Permission rule helpers (local copies to avoid circular dependency)
# ============================================================================


def permission_rule_value_from_string(rule_string: str) -> Dict[str, Any]:
    parsed = _parse_permission_rule_value(rule_string)
    return dict(parsed)


def permission_rule_extract_prefix(permission_rule: str) -> Optional[str]:
    import re

    m = re.match(r"^(.+):\*$", permission_rule)
    return m.group(1) if m else None


# ============================================================================
# Path resolution
# ============================================================================


def resolve_path_pattern_for_sandbox(pattern: str, source: str) -> str:
    if pattern.startswith("//"):
        return pattern[1:]
    if pattern.startswith("/") and not pattern.startswith("//"):
        root = _get_settings_root_path_for_source(source)
        return os.path.normpath(os.path.join(root, pattern[1:]))
    return pattern


def resolve_sandbox_filesystem_path(pattern: str, source: str) -> str:
    if pattern.startswith("//"):
        return pattern[1:]
    return _expand_path(pattern, _get_settings_root_path_for_source(source))


def _get_settings_root_path_for_source(source: str) -> str:
    from ...utils.settings.constants import get_setting_file_path

    path = get_setting_file_path(source)
    if path:
        return os.path.dirname(os.path.dirname(path))
    return os.getcwd()


def _expand_path(pattern: str, base: str) -> str:
    if pattern.startswith("~"):
        return os.path.expanduser(pattern)
    if os.path.isabs(pattern):
        return pattern
    return os.path.normpath(os.path.join(base, pattern))


# ============================================================================
# Sandbox settings queries
# ============================================================================


def _get_merged_settings() -> Dict[str, Any]:
    from ...utils.settings.settings import get_initial_settings

    return get_initial_settings()


def _get_settings_for_source(source: str) -> Optional[Dict[str, Any]]:
    from ...utils.settings.settings import get_settings_for_source

    return get_settings_for_source(source)


def _get_sandbox_settings() -> SandboxSettings:
    raw = _get_merged_settings()
    return extract_sandbox_settings(raw)


def get_sandbox_enabled_setting() -> bool:
    return _get_sandbox_settings().enabled or False


def is_auto_allow_bash_if_sandboxed_enabled() -> bool:
    val = _get_sandbox_settings().auto_allow_bash_if_sandboxed
    return val if val is not None else True


def are_unsandboxed_commands_allowed() -> bool:
    val = _get_sandbox_settings().allow_unsandboxed_commands
    return val if val is not None else True


def is_sandbox_required() -> bool:
    settings = _get_sandbox_settings()
    enabled = settings.enabled or False
    fail = settings.fail_if_unavailable or False
    return enabled and fail


def should_allow_managed_sandbox_domains_only() -> bool:
    policy = _get_settings_for_source("policySettings")
    if policy is None:
        return False
    return (
        policy.get("sandbox", {})
        .get("network", {})
        .get("allowManagedDomainsOnly", False)
        is True
    )


def _should_allow_managed_read_paths_only() -> bool:
    policy = _get_settings_for_source("policySettings")
    if policy is None:
        return False
    return (
        policy.get("sandbox", {})
        .get("filesystem", {})
        .get("allowManagedReadPathsOnly", False)
        is True
    )


# ============================================================================
# Dependency checking
# ============================================================================


@lru_cache(maxsize=1)
def check_dependencies() -> SandboxDependencyCheck:
    errors: List[str] = []
    warnings: List[str] = []

    current = get_platform()
    if current not in ("macos", "linux", "wsl"):
        errors.append(f"Unsupported platform: {current}")

    if current in ("linux", "wsl"):
        if not shutil.which("bwrap"):
            errors.append("bubblewrap (bwrap) not found")
        if not shutil.which("socat"):
            warnings.append("socat not found (needed for network proxying)")

    return SandboxDependencyCheck(errors=errors, warnings=warnings)


@lru_cache(maxsize=1)
def is_supported_platform() -> bool:
    return get_platform() in SUPPORTED_PLATFORMS


def is_platform_in_enabled_list() -> bool:
    settings = _get_sandbox_settings()
    enabled_platforms = settings.enabled_platforms
    if enabled_platforms is None:
        return True
    if len(enabled_platforms) == 0:
        return False
    return get_platform() in enabled_platforms


def is_sandboxing_enabled() -> bool:
    if not is_supported_platform():
        return False
    deps = check_dependencies()
    if deps.errors:
        return False
    if not is_platform_in_enabled_list():
        return False
    return get_sandbox_enabled_setting()


def get_sandbox_unavailable_reason() -> Optional[str]:
    if not get_sandbox_enabled_setting():
        return None

    if not is_supported_platform():
        p = get_platform()
        if p == "wsl":
            return "sandbox.enabled is set but WSL1 is not supported (requires WSL2)"
        return (
            f"sandbox.enabled is set but {p} is not supported "
            "(requires macOS, Linux, or WSL2)"
        )

    if not is_platform_in_enabled_list():
        return (
            f"sandbox.enabled is set but {get_platform()} "
            "is not in sandbox.enabledPlatforms"
        )

    deps = check_dependencies()
    if deps.errors:
        p = get_platform()
        hint = (
            "run /sandbox or /doctor for details"
            if p == "macos"
            else "install missing tools (e.g. apt install bubblewrap socat) "
            "or run /sandbox for details"
        )
        return (
            f"sandbox.enabled is set but dependencies are missing: "
            f"{', '.join(deps.errors)} · {hint}"
        )

    return None


def get_excluded_commands() -> List[str]:
    val = _get_sandbox_settings().excluded_commands
    return val if val is not None else []


# ============================================================================
# Settings converter
# ============================================================================


def convert_to_sandbox_runtime_config(
    settings: Dict[str, Any],
) -> SandboxRuntimeConfig:
    sandbox = extract_sandbox_settings(settings)
    permissions = settings.get("permissions") or {}

    allowed_domains: List[str] = []
    denied_domains: List[str] = []

    if should_allow_managed_sandbox_domains_only():
        policy = _get_settings_for_source("policySettings")
        if policy:
            for d in (
                policy.get("sandbox", {}).get("network", {}).get("allowedDomains", None)
                or []
            ):
                allowed_domains.append(d)
            for rs in policy.get("permissions", {}).get("allow", []):
                rule = permission_rule_value_from_string(rs)
                if rule["tool_name"] == WEB_FETCH_TOOL_NAME and rule.get(
                    "rule_content", ""
                ).startswith("domain:"):
                    allowed_domains.append(rule["rule_content"][len("domain:") :])
    else:
        for d in (sandbox.network.allowed_domains if sandbox.network else None) or []:
            allowed_domains.append(d)
        for rs in permissions.get("allow", []):
            rule = permission_rule_value_from_string(rs)
            if rule["tool_name"] == WEB_FETCH_TOOL_NAME and rule.get(
                "rule_content", ""
            ).startswith("domain:"):
                allowed_domains.append(rule["rule_content"][len("domain:") :])

    for rs in permissions.get("deny", []):
        rule = permission_rule_value_from_string(rs)
        if rule["tool_name"] == WEB_FETCH_TOOL_NAME and rule.get(
            "rule_content", ""
        ).startswith("domain:"):
            denied_domains.append(rule["rule_content"][len("domain:") :])

    allow_write: List[str] = [".", _get_claude_temp_dir()]
    deny_write: List[str] = []
    deny_read: List[str] = []
    allow_read: List[str] = []

    settings_paths = _collect_settings_file_paths()
    deny_write.extend(settings_paths)
    managed_dir = _get_managed_settings_drop_in_dir()
    if managed_dir:
        deny_write.append(managed_dir)

    cwd = os.getcwd()
    source_entries: List[tuple[str, Dict[str, Any]]] = []
    for source in SETTING_SOURCES:
        source_settings = _get_settings_for_source(source)
        if source_settings and isinstance(source_settings, dict):
            source_entries.append((source, source_settings))

    source_entries.append(("flagSettings", settings))

    for source, source_settings in source_entries:
        sp = source_settings.get("permissions")
        if sp and isinstance(sp, dict):
            for rs in sp.get("allow", []):
                rule = permission_rule_value_from_string(rs)
                if rule["tool_name"] == FILE_EDIT_TOOL_NAME and rule.get(
                    "rule_content"
                ):
                    allow_write.append(
                        resolve_path_pattern_for_sandbox(rule["rule_content"], source)
                    )

            for rs in sp.get("deny", []):
                rule = permission_rule_value_from_string(rs)
                if rule["tool_name"] == FILE_EDIT_TOOL_NAME and rule.get(
                    "rule_content"
                ):
                    deny_write.append(
                        resolve_path_pattern_for_sandbox(rule["rule_content"], source)
                    )
                if rule["tool_name"] == FILE_READ_TOOL_NAME and rule.get(
                    "rule_content"
                ):
                    deny_read.append(
                        resolve_path_pattern_for_sandbox(rule["rule_content"], source)
                    )

        fs_raw = (
            source_settings.get("sandbox", {}).get("filesystem")
            if isinstance(source_settings.get("sandbox"), dict)
            else None
        )
        if fs_raw and isinstance(fs_raw, dict):
            for p in fs_raw.get("allowWrite") or []:
                allow_write.append(resolve_sandbox_filesystem_path(p, source))
            for p in fs_raw.get("denyWrite") or []:
                deny_write.append(resolve_sandbox_filesystem_path(p, source))
            for p in fs_raw.get("denyRead") or []:
                deny_read.append(resolve_sandbox_filesystem_path(p, source))
            if (
                not _should_allow_managed_read_paths_only()
                or source == "policySettings"
            ):
                for p in fs_raw.get("allowRead") or []:
                    allow_read.append(resolve_sandbox_filesystem_path(p, source))

    _refresh_native_guardrail_paths(cwd, deny_write)

    allow_write = _dedupe_preserve_order(allow_write)
    deny_write = _dedupe_preserve_order(deny_write)
    deny_read = _dedupe_preserve_order(deny_read)
    allow_read = _dedupe_preserve_order(allow_read)

    net_cfg = sandbox.network
    ripgrep_cfg = sandbox.ripgrep

    return SandboxRuntimeConfig(
        network=SandboxRuntimeNetworkConfig(
            allowed_domains=allowed_domains,
            denied_domains=denied_domains,
            allow_unix_sockets=net_cfg.allow_unix_sockets if net_cfg else None,
            allow_all_unix_sockets=net_cfg.allow_all_unix_sockets if net_cfg else None,
            allow_local_binding=net_cfg.allow_local_binding if net_cfg else None,
            http_proxy_port=net_cfg.http_proxy_port if net_cfg else None,
            socks_proxy_port=net_cfg.socks_proxy_port if net_cfg else None,
        ),
        filesystem=SandboxRuntimeFilesystemConfig(
            deny_read=deny_read,
            allow_read=allow_read,
            allow_write=allow_write,
            deny_write=deny_write,
        ),
        ignore_violations=sandbox.ignore_violations,
        enable_weaker_nested_sandbox=sandbox.enable_weaker_nested_sandbox,
        enable_weaker_network_isolation=sandbox.enable_weaker_network_isolation,
        ripgrep=ripgrep_cfg,
    )


def _collect_settings_file_paths() -> List[str]:
    paths: List[str] = []
    from ...utils.settings.constants import get_setting_file_path

    for source in SETTING_SOURCES:
        p = get_setting_file_path(source)
        if p:
            paths.append(p)
    return paths


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen: set[str] = set()
    deduped: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _get_claude_temp_dir() -> str:
    from ..config import get_claude_config_home

    return os.path.join(get_claude_config_home(), "tmp")


def _get_managed_settings_drop_in_dir() -> Optional[str]:
    from ...utils.settings.constants import get_setting_file_path

    path = get_setting_file_path("policySettings")
    if path:
        base = os.path.dirname(path)
        return os.path.join(base, "managed-settings.d")
    return None


# ============================================================================
# Linux sandbox execution (bubblewrap)
# ============================================================================


def _build_bwrap_args(
    config: SandboxRuntimeConfig,
    command: str,
    shell: Optional[str] = None,
) -> List[str]:
    args = ["bwrap"]

    args.extend(["--ro-bind", "/", "/"])
    args.extend(["--dev", "/dev"])
    args.extend(["--proc", "/proc"])
    args.extend(["--tmpfs", "/tmp"])

    for p in config.filesystem.allow_write:
        if p and os.path.exists(p):
            real = os.path.realpath(p)
            args.extend(["--bind", real, real])

    for p in config.filesystem.deny_write:
        if p and os.path.exists(p):
            real = os.path.realpath(p)
            args.extend(["--ro-bind", real, real])

    for p in config.filesystem.deny_read:
        if p and os.path.exists(p):
            real = os.path.realpath(p)
            args.extend(["--tmpfs", real])

    args.append("--")
    bin_shell = shell or os.environ.get("SHELL", "/bin/sh")
    args.extend([bin_shell, "-c", command])
    return args


async def wrap_with_sandbox(
    command: str,
    bin_shell: Optional[str] = None,
    custom_config: Optional[SandboxRuntimeConfig] = None,
    abort_signal: Optional[Any] = None,
) -> str:
    if not is_sandboxing_enabled():
        return command

    if not shutil.which("bwrap"):
        if is_sandbox_required():
            raise RuntimeError("Sandbox is required but bubblewrap is not installed")
        return command

    settings = _get_merged_settings()
    base_config = convert_to_sandbox_runtime_config(settings)
    if custom_config:
        config = _merge_configs(base_config, custom_config)
    else:
        config = base_config

    bwrap_args = _build_bwrap_args(config, command, shell=bin_shell)
    return " ".join(shlex_quote(a) for a in bwrap_args)


def shlex_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def _merge_configs(
    base: SandboxRuntimeConfig, override: SandboxRuntimeConfig
) -> SandboxRuntimeConfig:
    return SandboxRuntimeConfig(
        network=override.network if override.network else base.network,
        filesystem=override.filesystem if override.filesystem else base.filesystem,
        ignore_violations=override.ignore_violations or base.ignore_violations,
        enable_weaker_nested_sandbox=override.enable_weaker_nested_sandbox
        if override.enable_weaker_nested_sandbox is not None
        else base.enable_weaker_nested_sandbox,
        enable_weaker_network_isolation=override.enable_weaker_network_isolation
        if override.enable_weaker_network_isolation is not None
        else base.enable_weaker_network_isolation,
        ripgrep=override.ripgrep or base.ripgrep,
    )


# ============================================================================
# Command allow/block semantics
# ============================================================================


def is_command_allowed(command: str) -> bool:
    excluded = get_excluded_commands()
    if not excluded:
        return True
    for pattern in excluded:
        if command == pattern or command.startswith(pattern + " "):
            return False
    return True


def is_command_sandboxed(command: str) -> bool:
    if not is_sandboxing_enabled():
        return False
    if not is_command_allowed(command):
        return False
    return True


def should_use_sandbox(command: str) -> bool:
    if not is_command_allowed(command):
        return False
    return is_sandboxing_enabled()


# ============================================================================
# Linux glob pattern warnings
# ============================================================================


def get_linux_glob_pattern_warnings() -> List[str]:
    p = get_platform()
    if p not in ("linux", "wsl"):
        return []

    settings = _get_merged_settings()
    sandbox_cfg = settings.get("sandbox")
    if not sandbox_cfg or not isinstance(sandbox_cfg, dict):
        return []
    if not sandbox_cfg.get("enabled"):
        return []

    permissions = settings.get("permissions") or {}
    warnings: List[str] = []

    import re

    def _has_globs(path: str) -> bool:
        stripped = re.sub(r"/\*\*$", "", path)
        return bool(re.search(r"[*?\[\]]", stripped))

    for rs in [
        *(permissions.get("allow") or []),
        *(permissions.get("deny") or []),
    ]:
        rule = permission_rule_value_from_string(rs)
        if (
            rule["tool_name"] in (FILE_EDIT_TOOL_NAME, FILE_READ_TOOL_NAME)
            and rule.get("rule_content")
            and _has_globs(rule["rule_content"])
        ):
            warnings.append(rs)

    return warnings


# ============================================================================
# Settings locked by policy
# ============================================================================


def are_sandbox_settings_locked_by_policy() -> bool:
    for source in ("flagSettings", "policySettings"):
        s = _get_settings_for_source(source)
        if not s or not isinstance(s, dict):
            continue
        sandbox = s.get("sandbox")
        if not sandbox or not isinstance(sandbox, dict):
            continue
        if any(
            sandbox.get(k) is not None
            for k in (
                "enabled",
                "autoAllowBashIfSandboxed",
                "allowUnsandboxedCommands",
            )
        ):
            return True
    return False


# ============================================================================
# Sandbox manager interface
# ============================================================================


_initialized = False
_initialization_lock: Optional[asyncio.Lock] = None
_bare_git_repo_scrub_paths: List[str] = []
_sandbox_violation_store: List[SandboxViolationEvent] = []


async def initialize(
    sandbox_ask_callback: Optional[Callable] = None,
) -> None:
    global _initialized
    if _initialized:
        return

    if not is_sandboxing_enabled():
        return

    deps = check_dependencies()
    if deps.errors:
        if is_sandbox_required():
            raise RuntimeError(
                f"Sandbox required but cannot initialize: {', '.join(deps.errors)}"
            )
        return

    _initialized = True


def refresh_config() -> None:
    if not is_sandboxing_enabled():
        return
    _get_merged_settings()


def reset() -> None:
    global _initialized
    _initialized = False
    check_dependencies.cache_clear()
    is_supported_platform.cache_clear()
    get_platform.cache_clear()
    _bare_git_repo_scrub_paths.clear()
    _sandbox_violation_store.clear()


def cleanup_after_command() -> None:
    _scrub_bare_git_repo_files()


def get_sandbox_violation_store() -> List[SandboxViolationEvent]:
    return _sandbox_violation_store


def annotate_stderr_with_sandbox_failures(command: str, stderr: str) -> str:
    if not _sandbox_violation_store:
        return stderr

    lines = [
        f"{event.tool or 'Sandbox'} {event.operation or 'blocked'}: {event.path or event.message}"
        for event in _sandbox_violation_store
    ]
    annotation = (
        "<sandbox_violations>\n"
        f"command: {command}\n" + "\n".join(lines) + "\n</sandbox_violations>"
    )
    if not stderr:
        return annotation
    if stderr.endswith("\n"):
        return f"{stderr}{annotation}"
    return f"{stderr}\n{annotation}"


def _refresh_native_guardrail_paths(cwd: str, deny_write: List[str]) -> None:
    _bare_git_repo_scrub_paths.clear()
    for entry in ("HEAD", "objects", "refs", "hooks", "config"):
        path = os.path.join(cwd, entry)
        if os.path.exists(path):
            deny_write.append(path)
        else:
            _bare_git_repo_scrub_paths.append(path)


def _scrub_bare_git_repo_files() -> None:
    for path in _bare_git_repo_scrub_paths:
        if not os.path.lexists(path):
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def validate_sandbox_core_contract() -> tuple[bool, tuple[str, ...]]:
    errors: List[str] = []
    reset()

    if permission_rule_value_from_string("Bash()") != {"tool_name": "Bash"}:
        errors.append("Bash() must parse as a tool-wide rule")

    if permission_rule_value_from_string("Bash(*)") != {"tool_name": "Bash"}:
        errors.append("Bash(*) must parse as a tool-wide rule")

    deny_read_config = convert_to_sandbox_runtime_config(
        {"permissions": {"deny": ["Read(/etc/shadow)"]}}
    )
    if not deny_read_config.filesystem.deny_read:
        errors.append("Read deny permission rules must populate filesystem.deny_read")

    store = get_sandbox_violation_store()
    store.clear()
    store.append(
        SandboxViolationEvent(
            tool="Read",
            path="/tmp/secret.txt",
            operation="read",
            message="blocked",
        )
    )
    annotated = annotate_stderr_with_sandbox_failures("cat /tmp/secret.txt", "stderr")
    if "<sandbox_violations>" not in annotated or "/tmp/secret.txt" not in annotated:
        errors.append(
            "sandbox failures must be annotated with violation tags and paths"
        )
    store.clear()

    previous_cwd = os.getcwd()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            config = convert_to_sandbox_runtime_config({})
            expected_head = os.path.join(temp_dir, "HEAD")
            if expected_head not in _bare_git_repo_scrub_paths:
                errors.append(
                    "missing bare git repo files must be registered for cleanup"
                )

            Path(expected_head).write_text("ref: refs/heads/main\n")
            Path(temp_dir, "objects").mkdir()
            cleanup_after_command()
            if os.path.exists(expected_head) or os.path.exists(
                Path(temp_dir, "objects")
            ):
                errors.append(
                    "cleanup_after_command must scrub planted bare git repo files"
                )

            Path(expected_head).write_text("ref: refs/heads/main\n")
            existing_guardrail_config = convert_to_sandbox_runtime_config({})
            if expected_head not in existing_guardrail_config.filesystem.deny_write:
                errors.append(
                    "existing bare git repo files must move into deny_write guardrails"
                )
    finally:
        os.chdir(previous_cwd)
        reset()

    return (not errors, tuple(errors))


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--validate-sandbox-core"]:
        passed, errors = validate_sandbox_core_contract()
        print("Sandbox core contract")
        print(f"Passed: {passed}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            print("STATUS: FAILED")
            return 1
        print("STATUS: PASSED")
        return 0
    print(
        "Usage: python python_src/utils/sandbox/sandbox_adapter.py --validate-sandbox-core",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
