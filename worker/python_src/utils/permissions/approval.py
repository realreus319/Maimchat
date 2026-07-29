from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple, Union

from ...types.permissions import (
    AsyncAgentDecisionReason,
    HookDecisionReason,
    PermissionAskDecision,
    PermissionAllowDecision,
    PermissionDecision,
    PermissionDenyDecision,
)


AUTO_REJECT_REASON = "Permission prompts are not available in this context"


class ApprovalContractError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalAllowState:
    behavior: str = "allow"
    updated_input: Optional[dict[str, object]] = None
    permission_updates: Tuple[Mapping[str, object], ...] = ()
    feedback: Optional[str] = None
    content_blocks: Tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ApprovalDenyState:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False
    feedback: Optional[str] = None
    content_blocks: Tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ApprovalAskState:
    behavior: str = "ask"
    message: str = ""
    updated_input: Optional[dict[str, object]] = None
    feedback: Optional[str] = None
    content_blocks: Tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ApprovalDeferState:
    behavior: str = "defer"


ApprovalState = Union[
    ApprovalAllowState,
    ApprovalAskState,
    ApprovalDenyState,
    ApprovalDeferState,
]


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ApprovalContractError(f"{field_name} must be a mapping")
    return value


def _require_string_or_none(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApprovalContractError(f"{field_name} must be a string")
    return value


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ApprovalContractError(f"{field_name} must be a boolean")
    return value


def _normalize_mapping_sequence(
    value: object,
    field_name: str,
) -> Tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ApprovalContractError(f"{field_name} must be a sequence")
    normalized: list[Mapping[str, object]] = []
    for index, item in enumerate(value):
        normalized.append(_require_mapping(item, f"{field_name}[{index}]"))
    return tuple(normalized)


def normalize_approval_state(raw_state: Mapping[str, object]) -> ApprovalState:
    behavior = raw_state.get("behavior")
    if not isinstance(behavior, str):
        raise ApprovalContractError("behavior must be a string")

    if behavior == "allow":
        if "message" in raw_state:
            raise ApprovalContractError("allow state cannot include message")
        if "interrupt" in raw_state:
            raise ApprovalContractError("allow state cannot include interrupt")
        updated_input: Optional[dict[str, object]] = None
        if "updated_input" in raw_state and raw_state["updated_input"] is not None:
            updated_input = dict(
                _require_mapping(raw_state["updated_input"], "updated_input")
            )
        return ApprovalAllowState(
            updated_input=updated_input,
            permission_updates=_normalize_mapping_sequence(
                raw_state.get("permission_updates"),
                "permission_updates",
            ),
            feedback=_require_string_or_none(raw_state.get("feedback"), "feedback"),
            content_blocks=_normalize_mapping_sequence(
                raw_state.get("content_blocks"),
                "content_blocks",
            ),
        )

    if behavior == "deny":
        if "updated_input" in raw_state:
            raise ApprovalContractError("deny state cannot include updated_input")
        if "permission_updates" in raw_state:
            raise ApprovalContractError("deny state cannot include permission_updates")
        return ApprovalDenyState(
            message=(
                _require_string_or_none(raw_state.get("message"), "message") or ""
            ),
            interrupt=_require_bool(raw_state.get("interrupt", False), "interrupt"),
            feedback=_require_string_or_none(raw_state.get("feedback"), "feedback"),
            content_blocks=_normalize_mapping_sequence(
                raw_state.get("content_blocks"),
                "content_blocks",
            ),
        )

    if behavior == "ask":
        if "interrupt" in raw_state:
            raise ApprovalContractError("ask state cannot include interrupt")
        if "permission_updates" in raw_state:
            raise ApprovalContractError("ask state cannot include permission_updates")
        updated_input: Optional[dict[str, object]] = None
        if "updated_input" in raw_state and raw_state["updated_input"] is not None:
            updated_input = dict(
                _require_mapping(raw_state["updated_input"], "updated_input")
            )
        return ApprovalAskState(
            message=(
                _require_string_or_none(raw_state.get("message"), "message") or ""
            ),
            updated_input=updated_input,
            feedback=_require_string_or_none(raw_state.get("feedback"), "feedback"),
            content_blocks=_normalize_mapping_sequence(
                raw_state.get("content_blocks"),
                "content_blocks",
            ),
        )

    if behavior == "defer":
        extra_fields = sorted(k for k in raw_state if k != "behavior")
        if extra_fields:
            joined = ", ".join(extra_fields)
            raise ApprovalContractError(
                f"defer state cannot include extra fields: {joined}"
            )
        return ApprovalDeferState()

    raise ApprovalContractError(f"unsupported behavior: {behavior}")


def resolve_approval_state(
    tool_name: str,
    tool_input: Mapping[str, object],
    raw_state: Optional[Mapping[str, object]],
    *,
    hook_name: str = "PermissionRequest",
    hook_source: str | None = None,
) -> PermissionDecision | None:
    if raw_state is None:
        return None

    state = normalize_approval_state(raw_state)
    if isinstance(state, ApprovalDeferState):
        return None

    if isinstance(state, ApprovalAllowState):
        return PermissionAllowDecision(
            updated_input=state.updated_input or dict(tool_input),
            decision_reason=HookDecisionReason(
                hook_name=hook_name,
                hook_source=hook_source,
                reason=state.feedback,
            ),
        )

    if isinstance(state, ApprovalAskState):
        return PermissionAskDecision(
            message=state.message or "Permission request deferred to user",
            updated_input=state.updated_input or dict(tool_input),
            decision_reason=HookDecisionReason(
                hook_name=hook_name,
                hook_source=hook_source,
                reason=state.message or state.feedback,
            ),
        )

    return PermissionDenyDecision(
        message=state.message or "Permission denied by hook",
        decision_reason=HookDecisionReason(
            hook_name=hook_name,
            hook_source=hook_source,
            reason=state.message or state.feedback,
        ),
    )


def deny_for_unavailable_prompts(tool_name: str) -> PermissionDenyDecision:
    return PermissionDenyDecision(
        message=(
            f"Claude requested permissions to use {tool_name}, but approval prompts "
            "are not available in this context."
        ),
        decision_reason=AsyncAgentDecisionReason(reason=AUTO_REJECT_REASON),
    )


def validate_approval_contract() -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []

    allow = resolve_approval_state(
        "Edit",
        {"file_path": "src/main.py"},
        {
            "behavior": "allow",
            "updated_input": {"file_path": "src/updated.py"},
            "permission_updates": ({"type": "addRules"},),
        },
    )
    if not isinstance(allow, PermissionAllowDecision):
        errors.append("allow state must resolve to PermissionAllowDecision")
    elif allow.updated_input != {"file_path": "src/updated.py"}:
        errors.append("allow state must preserve updated_input")

    deny = resolve_approval_state(
        "Edit",
        {"file_path": "src/main.py"},
        {"behavior": "deny", "message": "blocked by hook", "interrupt": True},
    )
    if not isinstance(deny, PermissionDenyDecision):
        errors.append("deny state must resolve to PermissionDenyDecision")
    elif deny.message != "blocked by hook":
        errors.append("deny state must preserve message")

    defer = resolve_approval_state(
        "Edit",
        {"file_path": "src/main.py"},
        {"behavior": "defer"},
    )
    if defer is not None:
        errors.append("defer state must resolve to None")

    ask = resolve_approval_state(
        "Edit",
        {"file_path": "src/main.py"},
        {"behavior": "ask", "message": "needs confirmation"},
    )
    if not isinstance(ask, PermissionAskDecision):
        errors.append("ask state must resolve to PermissionAskDecision")
    elif ask.message != "needs confirmation":
        errors.append("ask state must preserve message")

    fallback = deny_for_unavailable_prompts("Edit")
    if (
        fallback.decision_reason is None
        or fallback.decision_reason.type != "asyncAgent"
    ):
        errors.append("unavailable prompts must produce asyncAgent deny reason")

    invalid_states = (
        ({"behavior": "defer", "message": "nope"}, "malformed defer state"),
        ({"behavior": "allow", "message": "nope"}, "allow/message conflict"),
        ({"behavior": "deny", "updated_input": {}}, "deny/updated_input conflict"),
        ({"behavior": "ask", "interrupt": True}, "ask/interrupt conflict"),
        ({"behavior": "bogus"}, "unsupported behavior"),
    )
    for invalid_state, label in invalid_states:
        try:
            normalize_approval_state(invalid_state)
        except ApprovalContractError:
            continue
        errors.append(f"{label} must raise ApprovalContractError")

    return (not errors, tuple(errors))
