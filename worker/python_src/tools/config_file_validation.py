from __future__ import annotations

import json
from pathlib import Path

from ..utils.config import create_default_global_config
from ..utils.schema_registry import (
    SchemaRegistryIssue,
    validate_global_config_payload_via_registry,
    validate_settings_payload_via_registry,
)

_SETTINGS_FILE_NAMES = {
    "settings.json",
    "settings.local.json",
    "managed-settings.json",
}


def validate_settings_like_file_content(file_path: str, content: str) -> None:
    path = Path(file_path)
    if not _is_schema_validated_settings_path(path):
        return

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path.name} must contain valid JSON: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")

    if path.name == "claude.json":
        result = validate_global_config_payload_via_registry(
            payload,
            defaults=create_default_global_config(),
            file_path=str(path),
        )
    else:
        result = validate_settings_payload_via_registry(
            payload,
            file_path=str(path),
            source=_infer_settings_source(path),
        )

    issues = result.issues
    if issues:
        raise ValueError(_format_validation_issues(path.name, issues))


def _is_schema_validated_settings_path(path: Path) -> bool:
    if path.name == "claude.json":
        return True
    if path.name in _SETTINGS_FILE_NAMES:
        return True
    return path.parent.name == "managed-settings.d" and path.suffix == ".json"


def _infer_settings_source(path: Path) -> str | None:
    if path.name == "managed-settings.json" or path.parent.name == "managed-settings.d":
        return "policySettings"
    if path.name == "settings.local.json":
        return "localSettings"
    return None


def _format_validation_issues(
    file_name: str,
    issues: tuple[SchemaRegistryIssue, ...],
) -> str:
    details = ", ".join(
        f"{issue.path}: {issue.message}"
        for issue in issues[:3]
    )
    suffix = "" if len(issues) <= 3 else f" (+{len(issues) - 3} more)"
    return f"{file_name} failed schema validation: {details}{suffix}"
