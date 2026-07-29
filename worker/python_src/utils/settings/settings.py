"""Settings loading, merging, and precedence chain.

Python port of src/utils/settings/settings.ts (config precedence subset).

Implements the five-source merge chain:
  userSettings -> projectSettings -> localSettings -> flagSettings -> policySettings

Later sources override earlier ones via deep merge.  Arrays are concatenated
and deduplicated.  ``None`` values delete keys.  Policy settings use
first-source-wins internally (remote > MDM > file > HKCU) but for the Linux
port only the file-based path (managed-settings.json + drop-ins) is active.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "SettingsWithErrors",
    "ValidationError",
    "_deep_merge",
    "bind_app_state_settings_runtime_context",
    "get_initial_settings",
    "get_settings_for_source",
    "get_settings_runtime_signature",
    "get_settings_with_errors",
    "load_settings_from_disk",
    "parse_settings_file",
    "reset_settings_cache",
    "sync_app_state_settings_from_disk",
    "update_settings_for_source",
]

from .constants import (
    get_enabled_setting_sources,
    get_setting_file_path,
)
from ..schema_registry import (
    SchemaRegistryIssue,
    validate_settings_payload_via_registry,
)
from ..config import get_claude_config_home
from .settings_cache import (
    get_cached_parsed_file,
    get_cached_settings_for_source,
    get_plugin_settings_base,
    get_session_settings_cache,
    set_cached_parsed_file,
    set_cached_settings_for_source,
    set_session_settings_cache,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ValidationError:
    """A single validation issue found when parsing a settings file."""

    file: Optional[str] = None
    path: str = ""
    message: str = ""


@dataclass
class SettingsWithErrors:
    """Merged settings dict plus accumulated validation errors."""

    settings: Dict[str, Any] = field(default_factory=dict)
    errors: List[ValidationError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deep merge (matches lodash mergeWith + settingsMergeCustomizer)
# ---------------------------------------------------------------------------


def _deep_merge(
    base: Dict[str, Any],
    override: Dict[str, Any],
) -> Dict[str, Any]:
    """Recursively merge *override* into *base*.

    - Scalars in *override* replace scalars in *base*.
    - Arrays are concatenated and deduplicated (preserving order).
    - Nested dicts are merged recursively.
    - A value of ``None`` in *override* deletes the key from the result.
    """
    result: Dict[str, Any] = deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif (
            key in result and isinstance(result[key], list) and isinstance(value, list)
        ):
            # Concatenate and deduplicate preserving first-occurrence order
            seen: set = set()
            merged_list: list = []
            for item in result[key] + value:
                item_key = (
                    json.dumps(item, sort_keys=True)
                    if not isinstance(item, (str, int, float, bool))
                    else item
                )
                if item_key not in seen:
                    seen.add(item_key)
                    merged_list.append(item)
            result[key] = merged_list
        else:
            result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# parse_settings_file — single-file parser with cache
# ---------------------------------------------------------------------------


def parse_settings_file(
    path: str,
    *,
    source: str | None = None,
) -> Tuple[Optional[Dict[str, Any]], List[ValidationError]]:
    """Parse a single JSON settings file.

    Returns ``(settings_dict, errors)`` where *settings_dict* is:
    - ``{}`` for empty files,
    - ``None`` for malformed JSON or non-dict JSON,
    - ``None`` for missing files (with empty errors).
    """
    cache_key = _get_parsed_file_cache_key(path, source)
    signature = _get_parsed_file_signature(path)
    cached = get_cached_parsed_file(cache_key, signature=signature)
    if cached is not None:
        settings, errors = cached
        return (deepcopy(settings) if settings is not None else None, errors)

    result = _parse_settings_file_uncached(path, source=source)
    set_cached_parsed_file(cache_key, result, signature=signature)
    settings, errors = result
    return (deepcopy(settings) if settings is not None else None, errors)


def _parse_settings_file_uncached(
    path: str,
    *,
    source: str | None = None,
) -> Tuple[Optional[Dict[str, Any]], List[ValidationError]]:
    try:
        content = Path(path).read_text()
    except (FileNotFoundError, OSError):
        return (None, [])

    if content.strip() == "":
        return ({}, [])

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return (
            None,
            [ValidationError(file=path, path="", message=f"Invalid JSON: {exc}")],
        )

    if not isinstance(data, dict):
        return (
            None,
            [
                ValidationError(
                    file=path,
                    path="",
                    message="Settings file must contain a JSON object",
                )
            ],
        )

    result = validate_settings_payload_via_registry(
        data,
        file_path=path,
        source=source,
    )
    return ((result.value or {}), _schema_issues_to_validation_errors(result.issues))


def _get_parsed_file_cache_key(path: str, source: str | None) -> str:
    return f"{source or '_'}::{path}"


def _get_parsed_file_signature(path: str) -> Tuple[bool, Optional[int], Optional[int]]:
    try:
        stat_result = os.stat(path)
    except OSError:
        return (False, None, None)
    return (True, stat_result.st_mtime_ns, stat_result.st_size)


def _schema_issues_to_validation_errors(
    issues: Sequence[SchemaRegistryIssue],
) -> List[ValidationError]:
    return [
        ValidationError(file=issue.file, path=issue.path, message=issue.message)
        for issue in issues
    ]


def _normalize_settings_runtime_context(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
    allowed_sources: Sequence[str] | None = None,
) -> Dict[str, Any]:
    context: Dict[str, Any] = {}
    if isinstance(config_home, str) and config_home.strip():
        context["config_home"] = os.path.abspath(config_home)
    if isinstance(project_root, str) and project_root.strip():
        context["project_root"] = os.path.abspath(project_root)
    if isinstance(flag_path, str) and flag_path.strip():
        context["flag_path"] = os.path.abspath(flag_path)
    if isinstance(flag_inline, dict):
        context["flag_inline"] = deepcopy(flag_inline)
    if allowed_sources is not None:
        normalized_sources = tuple(
            source.strip()
            for source in allowed_sources
            if isinstance(source, str) and source.strip()
        )
        if normalized_sources:
            context["allowed_sources"] = normalized_sources
    return context


def _managed_settings_dropin_signature(
    *,
    config_home: str | None = None,
) -> Tuple[Any, ...]:
    home = config_home or get_claude_config_home()
    drop_dir = os.path.join(home, "managed-settings.d")
    entries: list[Any] = []
    try:
        names = sorted(
            name
            for name in os.listdir(drop_dir)
            if name.endswith(".json") and not name.startswith(".")
        )
    except (FileNotFoundError, OSError):
        names = []
    for name in names:
        path = os.path.abspath(os.path.join(drop_dir, name))
        exists, mtime_ns, size = _get_parsed_file_signature(path)
        entries.append((name, path, exists, mtime_ns, size))
    return tuple(entries)


def get_settings_runtime_signature(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
    allowed_sources: Sequence[str] | None = None,
) -> Tuple[Any, ...]:
    context = _normalize_settings_runtime_context(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        flag_inline=flag_inline,
        allowed_sources=allowed_sources,
    )
    normalized_sources = tuple(
        get_enabled_setting_sources(allowed_sources=context.get("allowed_sources"))
    )
    signature: list[Any] = [("sources", normalized_sources)]
    for source in normalized_sources:
        if source == "policySettings":
            policy_path = os.path.abspath(
                get_setting_file_path(
                    source,
                    config_home=context.get("config_home"),
                )
                or os.path.join(
                    context.get("config_home") or get_claude_config_home(),
                    "managed-settings.json",
                )
            )
            exists, mtime_ns, size = _get_parsed_file_signature(policy_path)
            signature.append((source, policy_path, exists, mtime_ns, size))
            signature.append(
                (
                    f"{source}.dropins",
                    _managed_settings_dropin_signature(
                        config_home=context.get("config_home"),
                    ),
                )
            )
            continue
        path = get_setting_file_path(
            source,
            config_home=context.get("config_home"),
            project_root=context.get("project_root"),
            flag_path=context.get("flag_path"),
        )
        if path is None:
            signature.append((source, None))
            continue
        normalized_path = os.path.abspath(path)
        exists, mtime_ns, size = _get_parsed_file_signature(normalized_path)
        signature.append((source, normalized_path, exists, mtime_ns, size))
    if "flag_inline" in context:
        signature.append(
            (
                "flagInline",
                json.dumps(
                    context["flag_inline"],
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        )
    return tuple(signature)


def bind_app_state_settings_runtime_context(
    app_state: object,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
    allowed_sources: Sequence[str] | None = None,
) -> None:
    context = _normalize_settings_runtime_context(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        flag_inline=flag_inline,
        allowed_sources=allowed_sources,
    )
    setattr(app_state, "_settings_runtime_context", context)
    setattr(
        app_state,
        "_settings_runtime_signature",
        get_settings_runtime_signature(**context),
    )


def sync_app_state_settings_from_disk(
    app_state: object,
    *,
    project_root: str | None = None,
) -> bool:
    raw_context = getattr(app_state, "_settings_runtime_context", None)
    if not isinstance(raw_context, Mapping):
        return False
    context = dict(raw_context)
    if isinstance(project_root, str) and project_root.strip():
        context["project_root"] = project_root
    normalized_context = _normalize_settings_runtime_context(**context)
    next_signature = get_settings_runtime_signature(**normalized_context)
    current_signature = getattr(app_state, "_settings_runtime_signature", None)
    if current_signature == next_signature:
        return False
    settings_with_errors = get_settings_with_errors(**normalized_context)
    next_settings = settings_with_errors.settings
    setattr(app_state, "settings", dict(next_settings))
    setattr(app_state, "_settings_runtime_context", normalized_context)
    setattr(app_state, "_settings_runtime_signature", next_signature)
    return True


# ---------------------------------------------------------------------------
# Managed (policy) file loading
# ---------------------------------------------------------------------------


def _load_managed_file_settings(
    config_home: str,
) -> Tuple[Optional[Dict[str, Any]], List[ValidationError]]:
    """Load managed-settings.json base + managed-settings.d/*.json drop-ins.

    Drop-in files are sorted alphabetically; later files override earlier ones
    via deep merge (matches systemd/sudoers convention).
    """
    errors: List[ValidationError] = []
    merged: Dict[str, Any] = {}
    found = False

    base_path = os.path.join(config_home, "managed-settings.json")
    base_settings, base_errors = parse_settings_file(
        base_path,
        source="policySettings",
    )
    errors.extend(base_errors)
    if base_settings and len(base_settings) > 0:
        merged = _deep_merge(merged, base_settings)
        found = True

    drop_dir = os.path.join(config_home, "managed-settings.d")
    try:
        entries = sorted(
            e
            for e in os.listdir(drop_dir)
            if e.endswith(".json") and not e.startswith(".")
        )
    except (FileNotFoundError, OSError):
        entries = []

    for name in entries:
        drop_path = os.path.join(drop_dir, name)
        drop_settings, drop_errors = parse_settings_file(
            drop_path,
            source="policySettings",
        )
        errors.extend(drop_errors)
        if drop_settings and len(drop_settings) > 0:
            merged = _deep_merge(merged, drop_settings)
            found = True

    return (merged if found else None, errors)


# ---------------------------------------------------------------------------
# get_settings_for_source — per-source loader (with optional cache bypass)
# ---------------------------------------------------------------------------


def get_settings_for_source(
    source: str,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Load settings for a single *source*.

    When custom paths (``config_home``, ``project_root``, ``flag_path``,
    ``flag_inline``) are provided, the per-source cache is **bypassed** so
    that tests using ``tempfile.TemporaryDirectory()`` never get stale data.
    """
    has_custom_args = any(
        v is not None for v in (config_home, project_root, flag_path, flag_inline)
    )
    if not has_custom_args:
        cached = get_cached_settings_for_source(source)
        if cached is not None:
            return deepcopy(cached)

    result, _ = _load_settings_for_source_with_errors(
        source,
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        flag_inline=flag_inline,
    )

    if not has_custom_args:
        set_cached_settings_for_source(source, result)

    return result


def _get_settings_for_source_uncached(
    source: str,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    settings, _ = _load_settings_for_source_with_errors(
        source,
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        flag_inline=flag_inline,
    )
    return settings


def _load_settings_for_source_with_errors(
    source: str,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
) -> Tuple[Optional[Dict[str, Any]], List[ValidationError]]:
    # Policy: file-based managed settings only (Linux port subset)
    if source == "policySettings":
        home = config_home or get_claude_config_home()
        return _load_managed_file_settings(home)

    file_path = get_setting_file_path(
        source,
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
    )

    file_settings: Optional[Dict[str, Any]] = None
    errors: List[ValidationError] = []
    if file_path:
        parsed, parse_errors = parse_settings_file(file_path, source=source)
        file_settings = parsed
        errors.extend(parse_errors)

    # For flagSettings, also merge any inline settings
    if source == "flagSettings" and flag_inline:
        inline_settings, inline_errors = _validate_inline_settings(
            flag_inline,
            source=source,
        )
        errors.extend(inline_errors)
        base = file_settings or {}
        return (_deep_merge(base, inline_settings), errors)

    return (file_settings, errors)


def _validate_inline_settings(
    raw: Dict[str, Any],
    *,
    source: str,
) -> Tuple[Dict[str, Any], List[ValidationError]]:
    result = validate_settings_payload_via_registry(
        raw,
        source=source,
    )
    return ((result.value or {}), _schema_issues_to_validation_errors(result.issues))


# ---------------------------------------------------------------------------
# load_settings_from_disk — full precedence chain
# ---------------------------------------------------------------------------


def load_settings_from_disk(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
    flag_inline: Dict[str, Any] | None = None,
    allowed_sources: Sequence[str] | None = None,
) -> SettingsWithErrors:
    """Iterate enabled sources in order and merge settings.

    Returns a ``SettingsWithErrors`` with the fully merged settings dict
    and any validation errors encountered along the way (deduplicated).
    """
    enabled = get_enabled_setting_sources(allowed_sources=allowed_sources)

    # Plugin settings base (lowest priority; merged from managed + inline plugins)
    plugin_base = get_plugin_settings_base(
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
        allowed_sources=allowed_sources,
        flag_inline=flag_inline,
    )
    merged: Dict[str, Any] = {}
    if plugin_base:
        merged = _deep_merge(merged, plugin_base)

    all_errors: List[ValidationError] = []

    for source in enabled:
        source_result, source_errors = _load_settings_for_source_with_errors(
            source,
            config_home=config_home,
            project_root=project_root,
            flag_path=flag_path,
            flag_inline=flag_inline,
        )

        all_errors.extend(source_errors)

        if source_result:
            merged = _deep_merge(merged, source_result)

    return SettingsWithErrors(
        settings=merged,
        errors=_dedupe_validation_errors(all_errors),
    )


# ---------------------------------------------------------------------------
# Convenience wrappers (session-cached)
# ---------------------------------------------------------------------------


def get_initial_settings(**kwargs: Any) -> Dict[str, Any]:
    """Return just the merged settings dict (no errors)."""
    swe = get_settings_with_errors(**kwargs)
    return swe.settings


def get_settings_with_errors(**kwargs: Any) -> SettingsWithErrors:
    """Return merged settings + errors, using session-level cache."""
    has_custom_args = any(value is not None for value in kwargs.values())
    if has_custom_args:
        return load_settings_from_disk(**kwargs)

    cached = get_session_settings_cache()
    if cached is not None:
        return cached

    result = load_settings_from_disk(**kwargs)
    set_session_settings_cache(result)
    return result


def update_settings_for_source(
    source: str,
    updates: Dict[str, Any],
    *,
    config_home: str | None = None,
    project_root: str | None = None,
    flag_path: str | None = None,
) -> Dict[str, Any]:
    file_path = get_setting_file_path(
        source,
        config_home=config_home,
        project_root=project_root,
        flag_path=flag_path,
    )
    if file_path is None:
        raise ValueError(f"Settings source '{source}' is not file-backed")

    parsed, _ = parse_settings_file(file_path, source=source)
    current = parsed or {}
    merged = _deep_merge(current, updates)
    result = validate_settings_payload_via_registry(
        merged,
        file_path=file_path,
        source=source,
    )
    validated = result.value or {}
    issues = result.issues
    if issues:
        raise ValueError(_format_validation_issue_message(file_path, issues))

    parent = os.path.dirname(file_path)
    os.makedirs(parent, exist_ok=True)
    Path(file_path).write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n")
    reset_settings_cache()
    return validated


def _dedupe_validation_errors(
    errors: Sequence[ValidationError],
) -> List[ValidationError]:
    deduped: List[ValidationError] = []
    seen: set[tuple[str | None, str, str]] = set()
    for error in errors:
        key = (error.file, error.path, error.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(error)
    return deduped


def _format_validation_issue_message(
    file_path: str,
    issues: Sequence[SchemaRegistryIssue],
) -> str:
    details = ", ".join(
        f"{issue.path}: {issue.message}"
        for issue in issues[:3]
    )
    suffix = "" if len(issues) <= 3 else f" (+{len(issues) - 3} more)"
    return f"Refusing to save invalid settings for {file_path}: {details}{suffix}"


# Re-export reset from cache module for convenience
from .settings_cache import reset_settings_cache  # noqa: E402
