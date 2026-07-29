from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar

from ..services.mcp.config import parse_mcp_config
from ..services.mcp.types import ConfigScope, McpJsonConfig, ValidationError
from ..state.app_state_store import (
    AppState,
    AppStateValidationIssue,
    normalize_app_state_in_place,
)
from ..tools.registry import (
    _SCHEMA_DEFAULT_MISSING,
    SCHEMA_ANY,
    SCHEMA_ARRAY,
    SCHEMA_BOOLEAN,
    SCHEMA_ENUM,
    SCHEMA_NUMBER,
    SCHEMA_OBJECT,
    SCHEMA_STRING,
    BUILTIN_TOOL_REGISTRY,
    SchemaField,
    ToolDefinition,
    ToolSchema,
    find_tool_by_name,
)
from .permissions.approval import (
    ApprovalContractError,
    ApprovalState,
    normalize_approval_state,
)
from .settings.schema_validation import (
    SchemaValidationIssue,
    validate_global_config_payload,
    validate_settings_payload,
)

T = TypeVar("T")
SchemaValidator = Callable[..., "SchemaRegistryResult[Any]"]


@dataclass(frozen=True)
class SchemaRegistryIssue:
    schema_name: str
    path: str
    message: str
    file: str | None = None
    severity: str = "error"


@dataclass(frozen=True)
class SchemaRegistryResult(Generic[T]):
    schema_name: str
    value: T | None
    issues: tuple[SchemaRegistryIssue, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(issue.severity not in {"info", "warning"} for issue in self.issues)

    @property
    def is_valid(self) -> bool:
        return not self.has_errors


@dataclass(frozen=True)
class SchemaRegistryEntry:
    name: str
    category: str
    description: str
    validator: SchemaValidator


class SchemaRegistryValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        issues: tuple[SchemaRegistryIssue, ...] = (),
    ) -> None:
        super().__init__(message)
        self.issues = issues


def list_schema_registry_entries() -> tuple[SchemaRegistryEntry, ...]:
    return tuple(_SCHEMA_REGISTRY[name] for name in sorted(_SCHEMA_REGISTRY))


def get_schema_registry_entry(schema_name: str) -> SchemaRegistryEntry | None:
    return _SCHEMA_REGISTRY.get(schema_name)


def schema_registry_entry_to_manifest(
    entry: SchemaRegistryEntry,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": entry.name,
        "category": entry.category,
        "description": entry.description,
        "validator": _callable_name(entry.validator),
    }

    tool_manifest = _tool_manifest_for_entry(entry.name)
    if tool_manifest is not None:
        payload["schema_kind"] = "tool"
        payload.update(tool_manifest)
        return payload

    payload["schema_kind"] = "custom"
    hint_schema = _builtin_registry_schema_hint(entry.name)
    if hint_schema is not None:
        payload["json_schema"] = hint_schema
    return payload


def export_schema_registry_manifest() -> dict[str, object]:
    entries = [
        schema_registry_entry_to_manifest(entry)
        for entry in list_schema_registry_entries()
    ]
    category_counts: dict[str, int] = {}
    for entry in entries:
        category = str(entry["category"])
        category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "version": 1,
        "entry_count": len(entries),
        "categories": category_counts,
        "entries": entries,
    }


def validate_registered_payload(
    schema_name: str,
    payload: object,
    **kwargs: Any,
) -> SchemaRegistryResult[Any]:
    entry = get_schema_registry_entry(schema_name)
    if entry is None:
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=None,
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=f"unknown schema registry entry: {schema_name}",
                    severity="fatal",
                ),
            ),
        )
    return entry.validator(payload, **kwargs)


def validate_settings_payload_via_registry(
    raw: object,
    *,
    file_path: str | None = None,
    source: str | None = None,
) -> SchemaRegistryResult[dict[str, Any]]:
    return validate_registered_payload(
        "settings",
        raw,
        file_path=file_path,
        source=source,
    )


def validate_global_config_payload_via_registry(
    raw: object,
    *,
    defaults: Mapping[str, Any],
    file_path: str | None = None,
) -> SchemaRegistryResult[dict[str, Any]]:
    return validate_registered_payload(
        "global_config",
        raw,
        defaults=defaults,
        file_path=file_path,
    )


def parse_mcp_config_via_registry(
    config_object: object,
    *,
    scope: ConfigScope,
    expand_vars: bool = True,
    file_path: str | None = None,
) -> SchemaRegistryResult[McpJsonConfig]:
    return validate_registered_payload(
        "mcp_config",
        config_object,
        scope=scope,
        expand_vars=expand_vars,
        file_path=file_path,
    )


def normalize_approval_state_via_registry(
    raw_state: object,
) -> ApprovalState:
    result = validate_registered_payload("approval_state", raw_state)
    if result.value is None or result.has_errors:
        raise ApprovalContractError(_format_registry_issues(result.issues))
    return result.value


def normalize_app_state_via_registry(
    app_state: AppState,
) -> tuple[SchemaRegistryIssue, ...]:
    result = validate_registered_payload("app_state", app_state)
    return result.issues


def validate_tool_input_payload_via_registry(
    tool_name: str,
    payload: Mapping[str, Any],
) -> None:
    schema_name = _tool_registry_entry_name("input", tool_name)
    _raise_if_invalid(validate_registered_payload(schema_name, payload))


def validate_tool_output_payload_via_registry(
    tool_name: str,
    payload: Mapping[str, Any] | str | None,
) -> None:
    schema_name = _tool_registry_entry_name("output", tool_name)
    _raise_if_invalid(validate_registered_payload(schema_name, payload))


def tool_definition_to_schema_payload(definition: ToolDefinition) -> dict[str, object]:
    if definition.raw_input_schema is not None:
        input_schema: dict[str, object] = dict(definition.raw_input_schema)
    else:
        schema = definition.input_schema or ToolSchema()
        input_schema = tool_schema_to_json_schema(schema)
    payload: dict[str, object] = {
        "name": definition.name,
        "description": definition.description,
        "input_schema": input_schema,
    }
    if definition.max_result_size_chars > 0:
        payload["max_result_size_chars"] = definition.max_result_size_chars
    if definition.strict:
        payload["strict"] = True
    if _tool_schema_should_defer_loading(definition):
        payload["defer_loading"] = True
    return payload


def tool_schema_to_json_schema(schema: ToolSchema) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for schema_field in schema.fields:
        properties[schema_field.name] = _schema_field_to_json_schema(schema_field)
        if schema_field.required:
            required.append(schema_field.name)
    payload: dict[str, object] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        payload["required"] = required
    return payload


def _tool_schema_should_defer_loading(definition: ToolDefinition) -> bool:
    if definition.always_load:
        return False
    if definition.name == "ToolSearch":
        return False
    if definition.name.startswith("mcp__"):
        return True
    return bool(definition.should_defer)


def _build_schema_registry() -> dict[str, SchemaRegistryEntry]:
    registry: dict[str, SchemaRegistryEntry] = {
        "settings": SchemaRegistryEntry(
            name="settings",
            category="config",
            description="Session/project settings payload",
            validator=_validate_settings_entry,
        ),
        "global_config": SchemaRegistryEntry(
            name="global_config",
            category="config",
            description="Global claude.json payload",
            validator=_validate_global_config_entry,
        ),
        "mcp_config": SchemaRegistryEntry(
            name="mcp_config",
            category="runtime",
            description="MCP server configuration object",
            validator=_validate_mcp_config_entry,
        ),
        "approval_state": SchemaRegistryEntry(
            name="approval_state",
            category="runtime",
            description="Hook approval_state contract payload",
            validator=_validate_approval_state_entry,
        ),
        "app_state": SchemaRegistryEntry(
            name="app_state",
            category="runtime",
            description="In-memory AppState normalization contract",
            validator=_validate_app_state_entry,
        ),
    }
    for definition in BUILTIN_TOOL_REGISTRY.values():
        registry[_tool_registry_entry_name("input", definition.name)] = (
            SchemaRegistryEntry(
                name=_tool_registry_entry_name("input", definition.name),
                category="tool_input",
                description=f"{definition.name} input schema",
                validator=_tool_payload_entry_validator(
                    definition.name,
                    direction="input",
                ),
            )
        )
        registry[_tool_registry_entry_name("output", definition.name)] = (
            SchemaRegistryEntry(
                name=_tool_registry_entry_name("output", definition.name),
                category="tool_output",
                description=f"{definition.name} output schema",
                validator=_tool_payload_entry_validator(
                    definition.name,
                    direction="output",
                ),
            )
        )
    return registry


def _validate_settings_entry(
    payload: object,
    *,
    file_path: str | None = None,
    source: str | None = None,
) -> SchemaRegistryResult[dict[str, Any]]:
    raw = _require_mapping(payload, schema_name="settings")
    if raw is None:
        return _invalid_mapping_result("settings", "settings payload must be an object")
    validated, issues = validate_settings_payload(
        raw,
        file_path=file_path,
        source=source,
    )
    return SchemaRegistryResult(
        schema_name="settings",
        value=validated,
        issues=_schema_issues_to_registry_issues("settings", issues),
    )


def _validate_global_config_entry(
    payload: object,
    *,
    defaults: Mapping[str, Any],
    file_path: str | None = None,
) -> SchemaRegistryResult[dict[str, Any]]:
    raw = _require_mapping(payload, schema_name="global_config")
    if raw is None:
        return _invalid_mapping_result(
            "global_config",
            "global config payload must be an object",
        )
    validated, issues = validate_global_config_payload(
        raw,
        defaults=defaults,
        file_path=file_path,
    )
    return SchemaRegistryResult(
        schema_name="global_config",
        value=validated,
        issues=_schema_issues_to_registry_issues("global_config", issues),
    )


def _validate_mcp_config_entry(
    payload: object,
    *,
    scope: ConfigScope,
    expand_vars: bool = True,
    file_path: str | None = None,
) -> SchemaRegistryResult[McpJsonConfig]:
    parsed, errors = parse_mcp_config(
        payload,
        scope=scope,
        expand_vars=expand_vars,
        file_path=file_path,
    )
    return SchemaRegistryResult(
        schema_name="mcp_config",
        value=parsed,
        issues=_mcp_errors_to_registry_issues(errors),
    )


def _validate_approval_state_entry(
    payload: object,
) -> SchemaRegistryResult[ApprovalState]:
    raw = _require_mapping(payload, schema_name="approval_state")
    if raw is None:
        return _invalid_mapping_result(
            "approval_state",
            "approval_state must be a mapping",
        )
    try:
        normalized = normalize_approval_state(raw)
    except ApprovalContractError as exc:
        return SchemaRegistryResult(
            schema_name="approval_state",
            value=None,
            issues=(
                SchemaRegistryIssue(
                    schema_name="approval_state",
                    path="",
                    message=str(exc),
                    severity="fatal",
                ),
            ),
        )
    return SchemaRegistryResult(schema_name="approval_state", value=normalized)


def _validate_app_state_entry(
    payload: object,
) -> SchemaRegistryResult[AppState]:
    if not isinstance(payload, AppState):
        return SchemaRegistryResult(
            schema_name="app_state",
            value=None,
            issues=(
                SchemaRegistryIssue(
                    schema_name="app_state",
                    path="",
                    message="app_state registry expects an AppState instance",
                    severity="fatal",
                ),
            ),
        )
    issues = normalize_app_state_in_place(payload)
    return SchemaRegistryResult(
        schema_name="app_state",
        value=payload,
        issues=_app_state_issues_to_registry_issues(issues),
    )


def _tool_payload_entry_validator(
    tool_name: str,
    *,
    direction: str,
) -> SchemaValidator:
    schema_name = _tool_registry_entry_name(direction, tool_name)

    def _validator(payload: object) -> SchemaRegistryResult[Any]:
        if direction == "input":
            return _validate_tool_input_entry(schema_name, tool_name, payload)
        return _validate_tool_output_entry(schema_name, tool_name, payload)

    return _validator


def _validate_tool_input_entry(
    schema_name: str,
    tool_name: str,
    payload: object,
) -> SchemaRegistryResult[Mapping[str, Any]]:
    definition = find_tool_by_name(tool_name)
    if definition is None or definition.input_schema is None:
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=dict(payload) if isinstance(payload, Mapping) else None,
        )
    if not isinstance(payload, Mapping):
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=None,
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=f"{tool_name} input must be an object matching the declared schema",
                    severity="fatal",
                ),
            ),
        )
    try:
        _validate_schema_mapping(
            payload,
            definition.input_schema,
            context=f"{tool_name} input",
            allow_aliases=True,
        )
    except ValueError as exc:
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=dict(payload),
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=str(exc),
                    severity="fatal",
                ),
            ),
        )
    return SchemaRegistryResult(schema_name=schema_name, value=dict(payload))


def _validate_tool_output_entry(
    schema_name: str,
    tool_name: str,
    payload: object,
) -> SchemaRegistryResult[Mapping[str, Any] | str | None]:
    definition = find_tool_by_name(tool_name)
    if definition is None or definition.output_schema is None or payload is None:
        return SchemaRegistryResult(schema_name=schema_name, value=payload)
    if isinstance(payload, str):
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=payload,
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=(
                        f"{tool_name} output must be an object matching the declared schema"
                    ),
                    severity="fatal",
                ),
            ),
        )
    if not isinstance(payload, Mapping):
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=None,
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=(
                        f"{tool_name} output must be an object matching the declared schema"
                    ),
                    severity="fatal",
                ),
            ),
        )
    normalized_payload = {
        key: value for key, value in payload.items() if value is not None
    }
    try:
        _validate_schema_mapping(
            normalized_payload,
            definition.output_schema,
            context=f"{tool_name} output",
            allow_aliases=False,
        )
    except ValueError as exc:
        return SchemaRegistryResult(
            schema_name=schema_name,
            value=normalized_payload,
            issues=(
                SchemaRegistryIssue(
                    schema_name=schema_name,
                    path="",
                    message=str(exc),
                    severity="fatal",
                ),
            ),
        )
    return SchemaRegistryResult(schema_name=schema_name, value=normalized_payload)


def _tool_registry_entry_name(direction: str, tool_name: str) -> str:
    normalized_direction = direction.strip().lower()
    return f"tool.{normalized_direction}:{tool_name}"


def _tool_manifest_for_entry(schema_name: str) -> dict[str, object] | None:
    if not schema_name.startswith("tool.") or ":" not in schema_name:
        return None
    head, tool_name = schema_name.split(":", 1)
    direction = head.split(".", 1)[1]
    definition = find_tool_by_name(tool_name)
    if definition is None:
        return None
    schema = (
        definition.input_schema
        if direction == "input"
        else definition.output_schema
    )
    return {
        "direction": direction,
        "tool": {
            "name": definition.name,
            "aliases": list(definition.aliases),
            "gate_type": definition.gate_type,
            "gate_expression": definition.gate_expression,
            "linux_exposure": definition.linux_exposure,
            "source_module": definition.source_module,
            "subsystem": definition.subsystem,
            "strict": definition.strict,
            "is_read_only": definition.is_read_only,
            "is_destructive": definition.is_destructive,
            "always_load": definition.always_load,
            "should_defer": definition.should_defer,
            "max_result_size_chars": definition.max_result_size_chars,
        },
        "json_schema": tool_schema_to_json_schema(schema or ToolSchema()),
    }


def _builtin_registry_schema_hint(schema_name: str) -> dict[str, object] | None:
    if schema_name in {"settings", "global_config", "mcp_config", "approval_state"}:
        return {
            "type": "object",
            "additionalProperties": True,
        }
    if schema_name == "app_state":
        return {
            "type": "object",
            "x-python-type": "AppState",
        }
    return None


def _callable_name(callback: object) -> str:
    name = getattr(callback, "__qualname__", None) or getattr(
        callback, "__name__", None
    )
    return str(name or callback.__class__.__name__)


def _require_mapping(
    payload: object,
    *,
    schema_name: str,
) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    return payload


def _invalid_mapping_result(
    schema_name: str,
    message: str,
) -> SchemaRegistryResult[Any]:
    return SchemaRegistryResult(
        schema_name=schema_name,
        value=None,
        issues=(
            SchemaRegistryIssue(
                schema_name=schema_name,
                path="",
                message=message,
                severity="fatal",
            ),
        ),
    )


def _schema_issues_to_registry_issues(
    schema_name: str,
    issues: Sequence[SchemaValidationIssue],
) -> tuple[SchemaRegistryIssue, ...]:
    return tuple(
        SchemaRegistryIssue(
            schema_name=schema_name,
            path=issue.path,
            message=issue.message,
            file=issue.file,
            severity="error",
        )
        for issue in issues
    )


def _app_state_issues_to_registry_issues(
    issues: Sequence[AppStateValidationIssue],
) -> tuple[SchemaRegistryIssue, ...]:
    return tuple(
        SchemaRegistryIssue(
            schema_name="app_state",
            path=issue.path,
            message=issue.message,
            severity="warning",
        )
        for issue in issues
    )


def _mcp_errors_to_registry_issues(
    issues: Sequence[ValidationError],
) -> tuple[SchemaRegistryIssue, ...]:
    return tuple(
        SchemaRegistryIssue(
            schema_name="mcp_config",
            path=issue.path,
            message=issue.message,
            file=issue.file,
            severity=issue.severity,
        )
        for issue in issues
    )


def _raise_if_invalid(result: SchemaRegistryResult[Any]) -> None:
    if not result.has_errors:
        return
    raise SchemaRegistryValidationError(
        _format_registry_issues(result.issues),
        issues=result.issues,
    )


def _format_registry_issues(issues: Sequence[SchemaRegistryIssue]) -> str:
    if not issues:
        return "schema validation failed"
    if len(issues) == 1:
        return issues[0].message
    details = ", ".join(
        _format_issue_detail(issue)
        for issue in issues[:3]
    )
    suffix = "" if len(issues) <= 3 else f" (+{len(issues) - 3} more)"
    return f"{details}{suffix}"


def _format_issue_detail(issue: SchemaRegistryIssue) -> str:
    if issue.path:
        return f"{issue.path}: {issue.message}"
    return issue.message


def _validate_schema_mapping(
    payload: Mapping[str, Any],
    schema: ToolSchema,
    *,
    context: str,
    allow_aliases: bool,
) -> None:
    for field in schema.fields:
        present, value = _lookup_schema_field(payload, field.name, allow_aliases)
        if not present:
            if field.required:
                raise ValueError(f"{context}.{field.name} is required")
            continue
        _validate_schema_value(
            value,
            field,
            context=f"{context}.{field.name}",
            allow_aliases=allow_aliases,
        )


def _lookup_schema_field(
    payload: Mapping[str, Any],
    field_name: str,
    allow_aliases: bool,
) -> tuple[bool, Any]:
    candidates = _schema_lookup_keys(field_name) if allow_aliases else (field_name,)
    for key in candidates:
        if key in payload:
            return True, payload[key]
    return False, None


def _schema_lookup_keys(field_name: str) -> tuple[str, ...]:
    candidates: list[str] = [field_name]
    if "_" in field_name:
        candidates.append(_snake_to_camel(field_name))
    elif any(char.isupper() for char in field_name):
        candidates.append(_camel_to_snake(field_name))
    unique: list[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _camel_to_snake(value: str) -> str:
    pieces: list[str] = []
    for index, char in enumerate(value):
        if char.isupper() and index > 0:
            pieces.append("_")
        pieces.append(char.lower())
    return "".join(pieces)


def _validate_schema_value(
    value: Any,
    field: SchemaField,
    *,
    context: str,
    allow_aliases: bool,
) -> None:
    field_type = field.field_type
    if field_type == SCHEMA_ANY:
        return
    if field_type == SCHEMA_STRING:
        if not isinstance(value, str):
            raise ValueError(f"{context} must be a string")
        return
    if field_type == SCHEMA_BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError(f"{context} must be a boolean")
        return
    if field_type == SCHEMA_NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{context} must be a number")
        return
    if field_type == SCHEMA_ENUM:
        if not isinstance(value, str):
            raise ValueError(f"{context} must be a string enum")
        if field.enum_values and value not in field.enum_values:
            allowed = ", ".join(field.enum_values)
            raise ValueError(f"{context} must be one of: {allowed}")
        return
    if field_type == SCHEMA_OBJECT:
        if not isinstance(value, Mapping):
            raise ValueError(f"{context} must be an object")
        if field.properties:
            _validate_schema_mapping(
                value,
                ToolSchema(fields=field.properties),
                context=context,
                allow_aliases=allow_aliases,
            )
        return
    if field_type == SCHEMA_ARRAY:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes, bytearray),
        ):
            raise ValueError(f"{context} must be an array")
        item_type = field.items_type or SCHEMA_ANY
        for index, item in enumerate(value):
            _validate_schema_array_item(
                item,
                item_type,
                context=f"{context}[{index}]",
                allow_aliases=allow_aliases,
            )
        return
    raise ValueError(f"{context} uses unsupported schema type {field_type!r}")


def _validate_schema_array_item(
    value: Any,
    item_type: str,
    *,
    context: str,
    allow_aliases: bool,
) -> None:
    field = SchemaField(
        name=context,
        field_type=item_type,
        required=True,
    )
    _validate_schema_value(
        value,
        field,
        context=context,
        allow_aliases=allow_aliases,
    )


def _schema_field_to_json_schema(field: SchemaField) -> dict[str, object]:
    payload: dict[str, object] = {}
    if field.field_type == SCHEMA_STRING:
        payload["type"] = "string"
    elif field.field_type == SCHEMA_BOOLEAN:
        payload["type"] = "boolean"
    elif field.field_type == SCHEMA_NUMBER:
        payload["type"] = "number"
    elif field.field_type == SCHEMA_ARRAY:
        payload["type"] = "array"
        item_type = field.items_type or SCHEMA_ANY
        payload["items"] = _json_schema_for_type(item_type)
    elif field.field_type == SCHEMA_OBJECT:
        payload["type"] = "object"
        if field.properties:
            payload["properties"] = {
                nested.name: _schema_field_to_json_schema(nested)
                for nested in field.properties
            }
            nested_required = [
                nested.name for nested in field.properties if nested.required
            ]
            if nested_required:
                payload["required"] = nested_required
    elif field.field_type == SCHEMA_ENUM:
        payload["type"] = "string"
        if field.enum_values:
            payload["enum"] = list(field.enum_values)
    else:
        payload.update(_json_schema_for_type(SCHEMA_ANY))
    if field.description:
        payload["description"] = field.description
    if field.default is not _SCHEMA_DEFAULT_MISSING:
        payload["default"] = field.default
    return payload


def _json_schema_for_type(field_type: str) -> dict[str, object]:
    if field_type == SCHEMA_STRING:
        return {"type": "string"}
    if field_type == SCHEMA_BOOLEAN:
        return {"type": "boolean"}
    if field_type == SCHEMA_NUMBER:
        return {"type": "number"}
    if field_type == SCHEMA_ARRAY:
        return {"type": "array", "items": {}}
    if field_type == SCHEMA_OBJECT:
        return {"type": "object"}
    if field_type == SCHEMA_ENUM:
        return {"type": "string"}
    return {}


_SCHEMA_REGISTRY = _build_schema_registry()


__all__ = [
    "SchemaRegistryEntry",
    "SchemaRegistryIssue",
    "SchemaRegistryResult",
    "SchemaRegistryValidationError",
    "export_schema_registry_manifest",
    "get_schema_registry_entry",
    "list_schema_registry_entries",
    "normalize_app_state_via_registry",
    "normalize_approval_state_via_registry",
    "parse_mcp_config_via_registry",
    "schema_registry_entry_to_manifest",
    "tool_definition_to_schema_payload",
    "tool_schema_to_json_schema",
    "validate_global_config_payload_via_registry",
    "validate_registered_payload",
    "validate_settings_payload_via_registry",
    "validate_tool_input_payload_via_registry",
    "validate_tool_output_payload_via_registry",
]
