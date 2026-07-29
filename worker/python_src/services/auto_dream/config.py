from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AutoDreamConfig:
    enabled: bool
    min_hours: float = 24.0
    min_sessions: int = 5
    scan_interval_seconds: float = 600.0


def load_auto_dream_config(settings: Mapping[str, Any] | None = None) -> AutoDreamConfig:
    source = settings or {}
    enabled = _bool_setting(
        source,
        "autoDreamEnabled",
        "auto_dream_enabled",
        env_name="CLAUDE_CODE_AUTO_DREAM",
        default=False,
    )
    min_hours = _float_setting(
        source,
        "autoDreamMinHours",
        "auto_dream_min_hours",
        env_name="CLAUDE_CODE_AUTO_DREAM_MIN_HOURS",
        default=24.0,
    )
    min_sessions = _int_setting(
        source,
        "autoDreamMinSessions",
        "auto_dream_min_sessions",
        env_name="CLAUDE_CODE_AUTO_DREAM_MIN_SESSIONS",
        default=5,
    )
    scan_interval_seconds = _float_setting(
        source,
        "autoDreamScanIntervalSeconds",
        "auto_dream_scan_interval_seconds",
        env_name="CLAUDE_CODE_AUTO_DREAM_SCAN_INTERVAL_SECONDS",
        default=600.0,
    )
    return AutoDreamConfig(
        enabled=enabled,
        min_hours=max(min_hours, 0.0),
        min_sessions=max(min_sessions, 1),
        scan_interval_seconds=max(scan_interval_seconds, 1.0),
    )


def _bool_setting(
    settings: Mapping[str, Any],
    *keys: str,
    env_name: str,
    default: bool,
) -> bool:
    for key in keys:
        if key in settings:
            return _to_bool(settings[key], default=default)
    env_value = os.environ.get(env_name)
    return _to_bool(env_value, default=default) if env_value is not None else default


def _float_setting(
    settings: Mapping[str, Any],
    *keys: str,
    env_name: str,
    default: float,
) -> float:
    for key in keys:
        if key in settings:
            return _to_float(settings[key], default=default)
    return _to_float(os.environ.get(env_name), default=default)


def _int_setting(
    settings: Mapping[str, Any],
    *keys: str,
    env_name: str,
    default: int,
) -> int:
    for key in keys:
        if key in settings:
            return _to_int(settings[key], default=default)
    return _to_int(os.environ.get(env_name), default=default)


def _to_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _to_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
