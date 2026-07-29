from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Sequence
from uuid import uuid4

from ..query import (
    AssistantMessage,
    AssistantPayload,
    AttachmentMessage,
    CompactBoundaryMessage,
    Message,
    MessagePhase,
    MessageState,
    ProgressMessage,
    QuerySession,
    RequiresActionDetails,
    SessionExternalMetadata,
    SessionMemory,
    SessionState,
    SystemInformationalMessage,
    TerminalReason,
    TerminalTransition,
    ThinkingBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseSummaryMessage,
    UserMessage,
    UserPayload,
    createAssistantAPIErrorMessage,
    createUserMessage,
    initSessionMemory,
)
from ..utils.config import (
    get_claude_config_home,
    get_global_claude_file_for_home,
    get_global_config_for_home,
    save_global_config,
)
from ..utils.settings.settings import get_initial_settings, update_settings_for_source
from .app_state_store import AppState, get_default_app_state


_SESSION_STORE_VERSION = 1
_SESSION_INDEX_NAME = "index.json"
_SESSION_RECOVERY_ERROR = (
    "Recovered from an interrupted previous run. The last turn was closed "
    "with synthetic recovery messages so the conversation can continue."
)
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SessionSnapshotValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    session: QuerySession
    display_name: str | None = None


def _legacy_state_artifact_path(config_home: str | None = None) -> str:
    home = config_home or get_claude_config_home()
    return os.path.join(home, "state.json")


def _sessions_dir(config_home: str | None = None) -> str:
    home = config_home or get_claude_config_home()
    return os.path.join(home, "sessions")


def _session_index_path(config_home: str | None = None) -> str:
    return os.path.join(_sessions_dir(config_home), _SESSION_INDEX_NAME)


def _backup_path(path: str) -> str:
    return f"{path}.bak"


def _validate_session_storage_id(session_id: str) -> str:
    if _SAFE_SESSION_ID_RE.match(session_id):
        return session_id
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        session_id,
        re.IGNORECASE,
    ):
        return session_id
    raise SessionSnapshotValidationError(
        f"Unsafe session id for persistence: {session_id!r}"
    )


def session_snapshot_path(
    session_id: str,
    *,
    config_home: str | None = None,
) -> str:
    safe_session_id = _validate_session_storage_id(session_id)
    return os.path.join(_sessions_dir(config_home), f"{safe_session_id}.json")


def generate_session_id() -> str:
    return str(uuid4())


def _expanded_view_from_global_config(config: Dict[str, Any]) -> str:
    if config.get("showExpandedTodos") is True:
        return "tasks"
    if config.get("showSpinnerTree") is True:
        return "teammates"
    return "none"


def _load_legacy_state(config_home: str | None) -> Dict[str, Any]:
    path = _legacy_state_artifact_path(config_home)
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except (FileNotFoundError, OSError):
        return {}
    if not content.strip():
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_persisted_state(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> AppState:
    settings = get_initial_settings(config_home=config_home, project_root=project_root)
    state = get_default_app_state(
        settings=settings,
        settings_runtime_context={
            "config_home": config_home,
            "project_root": project_root,
        },
    )
    global_config = get_global_config_for_home(config_home)
    legacy_state = _load_legacy_state(config_home)
    effective_config_home = config_home or get_claude_config_home()
    global_config_exists = os.path.exists(
        get_global_claude_file_for_home(effective_config_home)
    )

    if not global_config_exists and isinstance(legacy_state.get("verbose"), bool):
        state.verbose = legacy_state["verbose"]
    else:
        state.verbose = bool(global_config.get("verbose", False))
    state.expanded_view = _expanded_view_from_global_config(global_config)
    if state.expanded_view == "none" and isinstance(
        legacy_state.get("expandedView"), str
    ):
        state.expanded_view = legacy_state["expandedView"]

    model = settings.get("model")
    if not isinstance(model, str):
        model = legacy_state.get("mainLoopModel")
    if isinstance(model, str):
        state.main_loop_model = model
        state.main_loop_model_for_session = model

    fast_mode = settings.get("fastMode", legacy_state.get("fastMode", False))
    state.fast_mode = bool(fast_mode)

    effort_value = settings.get("effortValue", legacy_state.get("effortValue"))
    if isinstance(effort_value, str):
        state.effort_value = effort_value

    return state


def save_persisted_state(
    state: AppState,
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> None:
    show_expanded_todos = state.expanded_view == "tasks"
    show_spinner_tree = state.expanded_view == "teammates"
    save_global_config(
        lambda current: {
            **current,
            "verbose": state.verbose,
            "showExpandedTodos": show_expanded_todos,
            "showSpinnerTree": show_spinner_tree,
        },
        config_home=config_home,
    )
    update_settings_for_source(
        "userSettings",
        {
            "model": state.main_loop_model,
            "fastMode": state.fast_mode,
            "effortValue": state.effort_value,
        },
        config_home=config_home,
        project_root=project_root,
    )


def clear_persisted_state(
    *,
    config_home: str | None = None,
    project_root: str | None = None,
) -> bool:
    legacy_path = _legacy_state_artifact_path(config_home)
    try:
        os.unlink(legacy_path)
    except FileNotFoundError:
        pass

    default_state = get_default_app_state()
    save_global_config(
        lambda current: {
            **current,
            "verbose": default_state.verbose,
            "showExpandedTodos": False,
            "showSpinnerTree": False,
        },
        config_home=config_home,
    )
    update_settings_for_source(
        "userSettings",
        {"model": None, "fastMode": None, "effortValue": None},
        config_home=config_home,
        project_root=project_root,
    )
    return True


def save_session_snapshot(
    session: QuerySession,
    *,
    session_id: str,
    cwd: str,
    config_home: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    display_name: str | None = None,
) -> SessionSnapshot:
    safe_session_id = _validate_session_storage_id(session_id)
    snapshot = SessionSnapshot(
        session_id=safe_session_id,
        cwd=os.path.abspath(cwd),
        created_at=created_at or updated_at or _iso_now(),
        updated_at=updated_at or _iso_now(),
        display_name=display_name,
        session=session,
    )
    path = session_snapshot_path(safe_session_id, config_home=config_home)
    payload = _serialize_session_snapshot(snapshot)
    _atomic_write_json(path, payload)
    _update_session_index(snapshot, config_home=config_home)
    return snapshot


def load_session_snapshot(
    session_id: str,
    *,
    config_home: str | None = None,
    recover: bool = False,
) -> SessionSnapshot | None:
    path = session_snapshot_path(session_id, config_home=config_home)
    payload = _load_json_mapping(path)
    if payload is None:
        return None
    snapshot = _deserialize_session_snapshot(payload)
    if recover:
        snapshot = SessionSnapshot(
            session_id=snapshot.session_id,
            cwd=snapshot.cwd,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            display_name=snapshot.display_name,
            session=recover_interrupted_session(snapshot.session),
        )
    return snapshot


def load_latest_session_snapshot(
    *,
    config_home: str | None = None,
    cwd: str | None = None,
    recover: bool = False,
) -> SessionSnapshot | None:
    normalized_cwd = os.path.abspath(cwd) if isinstance(cwd, str) else None
    index_entries = _load_session_index(config_home)
    ordered_ids = [
        entry["session_id"]
        for entry in sorted(
            index_entries,
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )
        if isinstance(entry.get("session_id"), str)
        and (
            normalized_cwd is None
            or os.path.abspath(str(entry.get("cwd", ""))) == normalized_cwd
        )
    ]
    seen_ids: set[str] = set()
    for session_id in ordered_ids:
        if session_id in seen_ids:
            continue
        seen_ids.add(session_id)
        snapshot = load_session_snapshot(
            session_id,
            config_home=config_home,
            recover=recover,
        )
        if snapshot is not None:
            return snapshot

    sessions_dir = _sessions_dir(config_home)
    try:
        entries = sorted(
            (
                entry
                for entry in os.scandir(sessions_dir)
                if entry.is_file()
                and entry.name.endswith(".json")
                and entry.name != _SESSION_INDEX_NAME
            ),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        return None

    for entry in entries:
        try:
            snapshot = load_session_snapshot(
                entry.name[:-5],
                config_home=config_home,
                recover=recover,
            )
        except SessionSnapshotValidationError:
            continue
        if snapshot is None:
            continue
        if normalized_cwd is not None and os.path.abspath(snapshot.cwd) != normalized_cwd:
            continue
        return snapshot
    return None


def recover_interrupted_session(session: QuerySession) -> QuerySession:
    if (
        session.sessionState == SessionState.IDLE
        and session.messageState.phase in {MessagePhase.IDLE, MessagePhase.AWAITING_USER}
    ):
        return session

    if (
        not session.messages
        and session.messageState.phase == MessagePhase.IDLE
        and session.sessionState != SessionState.IDLE
    ):
        return QuerySession()

    current = session
    if current.sessionState == SessionState.REQUIRES_ACTION:
        current = current.resumeTurn()
    elif current.sessionState == SessionState.IDLE:
        current = QuerySession(
            sessionState=SessionState.RUNNING,
            externalMetadata=current.externalMetadata,
            messageState=current.messageState,
            messages=current.messages,
            lastTerminal=current.lastTerminal,
        )

    original_phase = current.messageState.phase
    if original_phase == MessagePhase.AWAITING_TOOL_RESULTS:
        for tool_use_id in current.messageState.pendingToolUseIds:
            current = current.appendMessage(
                _create_recovery_tool_result_message(current, tool_use_id)
            )

    if current.messageState.phase in {
        MessagePhase.AWAITING_ASSISTANT,
        MessagePhase.AWAITING_ASSISTANT_AFTER_TOOL_RESULTS,
        MessagePhase.IDLE,
    }:
        current = current.appendMessage(
            createAssistantAPIErrorMessage(
                content=_SESSION_RECOVERY_ERROR,
                apiError="conversation_recovered",
                errorDetails=_SESSION_RECOVERY_ERROR,
            )
        )

    if current.messageState.phase == MessagePhase.AWAITING_USER:
        terminal = (
            TerminalTransition(reason=TerminalReason.COMPLETED)
            if original_phase == MessagePhase.AWAITING_USER
            else TerminalTransition(
                reason=TerminalReason.MODEL_ERROR,
                error="conversation_recovered",
            )
        )
        return current.finishTurn(terminal)

    if current.sessionState != SessionState.IDLE:
        return current.finishTurn(
            TerminalTransition(
                reason=TerminalReason.MODEL_ERROR,
                error="conversation_recovered",
            )
        )
    return current


def _create_recovery_tool_result_message(
    session: QuerySession,
    tool_use_id: str,
) -> UserMessage:
    source_tool_assistant_uuid: str | None = None
    for message in reversed(session.messages):
        if not isinstance(message, AssistantMessage):
            continue
        if any(
            isinstance(block, ToolUseBlock) and block.id == tool_use_id
            for block in message.message.content
        ):
            source_tool_assistant_uuid = message.uuid
            break
    return createUserMessage(
        content=(
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=_SESSION_RECOVERY_ERROR,
                is_error=True,
            ),
        ),
        isMeta=True,
        toolUseResult=_SESSION_RECOVERY_ERROR,
        sourceToolAssistantUUID=source_tool_assistant_uuid,
        origin="conversation_recovery",
    )


def _load_session_index(config_home: str | None = None) -> list[dict[str, Any]]:
    payload = _load_json_mapping(_session_index_path(config_home))
    if payload is None:
        return []
    if not isinstance(payload.get("sessions"), list):
        return []
    entries: list[dict[str, Any]] = []
    for raw_entry in payload["sessions"]:
        if not isinstance(raw_entry, Mapping):
            continue
        session_id = raw_entry.get("session_id")
        updated_at = raw_entry.get("updated_at")
        cwd = raw_entry.get("cwd")
        if not (
            isinstance(session_id, str)
            and isinstance(updated_at, str)
            and isinstance(cwd, str)
        ):
            continue
        entries.append(
            {
                "session_id": session_id,
                "updated_at": updated_at,
                "created_at": raw_entry.get("created_at"),
                "cwd": cwd,
                "display_name": raw_entry.get("display_name"),
                "message_count": raw_entry.get("message_count"),
            }
        )
    return entries


def _update_session_index(
    snapshot: SessionSnapshot,
    *,
    config_home: str | None = None,
) -> None:
    current_entries = [
        entry
        for entry in _load_session_index(config_home)
        if entry.get("session_id") != snapshot.session_id
    ]
    current_entries.append(
        {
            "session_id": snapshot.session_id,
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "cwd": snapshot.cwd,
            "display_name": snapshot.display_name,
            "message_count": len(snapshot.session.messages),
        }
    )
    current_entries.sort(key=lambda entry: str(entry.get("updated_at", "")), reverse=True)
    _atomic_write_json(
        _session_index_path(config_home),
        {
            "version": _SESSION_STORE_VERSION,
            "sessions": current_entries,
        },
    )


def _serialize_session_snapshot(snapshot: SessionSnapshot) -> dict[str, Any]:
    return {
        "version": _SESSION_STORE_VERSION,
        "session_id": snapshot.session_id,
        "cwd": snapshot.cwd,
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "display_name": snapshot.display_name,
        "session": _serialize_query_session(snapshot.session),
    }


def _deserialize_session_snapshot(raw: Mapping[str, Any]) -> SessionSnapshot:
    _require_mapping(raw, "snapshot")
    session_id = _validate_session_storage_id(_require_string(raw, "session_id", "snapshot"))
    cwd = os.path.abspath(_require_string(raw, "cwd", "snapshot"))
    created_at = _require_string(raw, "created_at", "snapshot")
    updated_at = _require_string(raw, "updated_at", "snapshot")
    display_name = _optional_string(raw, "display_name", "snapshot")
    session_payload = _require_mapping(raw.get("session"), "snapshot.session")
    return SessionSnapshot(
        session_id=session_id,
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        display_name=display_name,
        session=_deserialize_query_session(session_payload),
    )


def _serialize_query_session(session: QuerySession) -> dict[str, Any]:
    return {
        "sessionState": session.sessionState.value,
        "externalMetadata": _serialize_external_metadata(session.externalMetadata),
        "messageState": _serialize_message_state(session.messageState),
        "messages": [_serialize_message(message) for message in session.messages],
        "lastTerminal": _serialize_terminal_transition(session.lastTerminal),
    }


def _deserialize_query_session(raw: Mapping[str, Any]) -> QuerySession:
    session_state = SessionState(
        _require_string(raw, "sessionState", "session")
    )
    external_metadata = _deserialize_external_metadata(
        _require_mapping(raw.get("externalMetadata"), "session.externalMetadata")
    )
    raw_messages = raw.get("messages")
    if not isinstance(raw_messages, list):
        raise SessionSnapshotValidationError("session.messages must be a list")
    messages = tuple(
        _deserialize_message(
            _require_mapping(message, f"session.messages[{index}]")
        )
        for index, message in enumerate(raw_messages)
    )
    message_state = _deserialize_message_state(
        raw.get("messageState"),
        messages=messages,
    )
    last_terminal = _deserialize_terminal_transition(raw.get("lastTerminal"))
    session_memory = initSessionMemory(
        messages,
        external_metadata.session_memory,
    )
    if session_memory != external_metadata.session_memory:
        external_metadata = SessionExternalMetadata(
            permission_mode=external_metadata.permission_mode,
            is_ultraplan_mode=external_metadata.is_ultraplan_mode,
            model=external_metadata.model,
            pending_action=external_metadata.pending_action,
            post_turn_summary=external_metadata.post_turn_summary,
            task_summary=external_metadata.task_summary,
            session_memory=session_memory,
        )
    return QuerySession(
        sessionState=session_state,
        externalMetadata=external_metadata,
        messageState=message_state,
        messages=messages,
        lastTerminal=last_terminal,
    )


def _serialize_external_metadata(metadata: SessionExternalMetadata) -> dict[str, Any]:
    return {
        "permission_mode": metadata.permission_mode,
        "is_ultraplan_mode": metadata.is_ultraplan_mode,
        "model": metadata.model,
        "pending_action": _serialize_requires_action_details(metadata.pending_action),
        "post_turn_summary": _jsonify(metadata.post_turn_summary),
        "task_summary": metadata.task_summary,
        "session_memory": _serialize_session_memory(metadata.session_memory),
    }


def _deserialize_external_metadata(raw: Mapping[str, Any]) -> SessionExternalMetadata:
    return SessionExternalMetadata(
        permission_mode=_optional_string(raw, "permission_mode", "externalMetadata"),
        is_ultraplan_mode=_optional_bool(
            raw, "is_ultraplan_mode", "externalMetadata"
        ),
        model=_optional_string(raw, "model", "externalMetadata"),
        pending_action=_deserialize_requires_action_details(raw.get("pending_action")),
        post_turn_summary=_jsonify(raw.get("post_turn_summary")),
        task_summary=_optional_string(raw, "task_summary", "externalMetadata"),
        session_memory=_deserialize_session_memory(raw.get("session_memory")),
    )


def _serialize_session_memory(memory: SessionMemory | None) -> dict[str, Any] | None:
    if memory is None:
        return None
    return {
        "summary": memory.summary,
        "user_requests": list(memory.user_requests),
        "decisions": list(memory.decisions),
        "tool_activity": list(memory.tool_activity),
        "files": list(memory.files),
        "open_questions": list(memory.open_questions),
        "compaction_count": memory.compaction_count,
        "summary_source": memory.summary_source,
        "summary_origin": memory.summary_origin,
        "last_updated_at": memory.last_updated_at,
        "summary_message_uuids": list(memory.summary_message_uuids),
    }


def _deserialize_session_memory(raw: Any) -> SessionMemory | None:
    if raw is None:
        return None
    mapping = _require_mapping(raw, "externalMetadata.session_memory")
    return SessionMemory(
        summary=_optional_string(mapping, "summary", "externalMetadata.session_memory"),
        user_requests=_optional_string_tuple(
            mapping,
            "user_requests",
            "externalMetadata.session_memory",
        ),
        decisions=_optional_string_tuple(
            mapping,
            "decisions",
            "externalMetadata.session_memory",
        ),
        tool_activity=_optional_string_tuple(
            mapping,
            "tool_activity",
            "externalMetadata.session_memory",
        ),
        files=_optional_string_tuple(
            mapping,
            "files",
            "externalMetadata.session_memory",
        ),
        open_questions=_optional_string_tuple(
            mapping,
            "open_questions",
            "externalMetadata.session_memory",
        ),
        compaction_count=_optional_int(
            mapping,
            "compaction_count",
            "externalMetadata.session_memory",
        )
        or 0,
        summary_source=_optional_string(
            mapping,
            "summary_source",
            "externalMetadata.session_memory",
        ),
        summary_origin=_optional_string(
            mapping,
            "summary_origin",
            "externalMetadata.session_memory",
        ),
        last_updated_at=_optional_string(
            mapping,
            "last_updated_at",
            "externalMetadata.session_memory",
        ),
        summary_message_uuids=_optional_string_tuple(
            mapping,
            "summary_message_uuids",
            "externalMetadata.session_memory",
        ),
    )


def _serialize_requires_action_details(
    details: RequiresActionDetails | None,
) -> dict[str, Any] | None:
    if details is None:
        return None
    return {
        "tool_name": details.tool_name,
        "action_description": details.action_description,
        "tool_use_id": details.tool_use_id,
        "request_id": details.request_id,
        "input": _jsonify(details.input),
    }


def _deserialize_requires_action_details(
    raw: Any,
) -> RequiresActionDetails | None:
    if raw is None:
        return None
    mapping = _require_mapping(raw, "externalMetadata.pending_action")
    return RequiresActionDetails(
        tool_name=_require_string(
            mapping, "tool_name", "externalMetadata.pending_action"
        ),
        action_description=_require_string(
            mapping, "action_description", "externalMetadata.pending_action"
        ),
        tool_use_id=_require_string(
            mapping, "tool_use_id", "externalMetadata.pending_action"
        ),
        request_id=_require_string(
            mapping, "request_id", "externalMetadata.pending_action"
        ),
        input=_optional_mapping(mapping, "input", "externalMetadata.pending_action"),
    )


def _serialize_message_state(message_state: MessageState) -> dict[str, Any]:
    return {
        "phase": message_state.phase.value,
        "pendingToolUseIds": list(message_state.pendingToolUseIds),
        "seenToolUseIds": sorted(message_state.seenToolUseIds),
    }


def _deserialize_message_state(
    raw: Any,
    *,
    messages: Sequence[Message],
) -> MessageState:
    derived = MessageState()
    for message in messages:
        derived = derived.apply(message)
    if raw is None:
        return derived
    mapping = _require_mapping(raw, "session.messageState")
    phase = MessagePhase(_require_string(mapping, "phase", "session.messageState"))
    pending_tool_use_ids = tuple(
        _require_string(item, None, "session.messageState.pendingToolUseIds")
        for item in _require_list(
            mapping.get("pendingToolUseIds"), "session.messageState.pendingToolUseIds"
        )
    )
    seen_tool_use_ids = frozenset(
        _require_string(item, None, "session.messageState.seenToolUseIds")
        for item in _require_list(
            mapping.get("seenToolUseIds"), "session.messageState.seenToolUseIds"
        )
    )
    if (
        phase != derived.phase
        or pending_tool_use_ids != derived.pendingToolUseIds
        or seen_tool_use_ids != derived.seenToolUseIds
    ):
        return derived
    return MessageState(
        phase=phase,
        pendingToolUseIds=pending_tool_use_ids,
        seenToolUseIds=seen_tool_use_ids,
    )


def _serialize_terminal_transition(
    terminal: TerminalTransition | None,
) -> dict[str, Any] | None:
    if terminal is None:
        return None
    return {
        "reason": terminal.reason.value,
        "error": _jsonify(terminal.error),
        "turnCount": terminal.turnCount,
    }


def _deserialize_terminal_transition(raw: Any) -> TerminalTransition | None:
    if raw is None:
        return None
    mapping = _require_mapping(raw, "session.lastTerminal")
    reason = TerminalReason(_require_string(mapping, "reason", "session.lastTerminal"))
    error = _jsonify(mapping.get("error"))
    turn_count = _optional_int(mapping, "turnCount", "session.lastTerminal")
    return TerminalTransition(reason=reason, error=error, turnCount=turn_count)


def _serialize_message(message: Message) -> dict[str, Any]:
    if isinstance(message, AssistantMessage):
        return {
            "type": "assistant",
            "message": {
                "content": [
                    _serialize_assistant_block(block) for block in message.message.content
                ],
                "id": message.message.id,
                "container": _jsonify(message.message.container),
                "model": message.message.model,
                "role": message.message.role,
                "stop_reason": message.message.stop_reason,
                "stop_sequence": message.message.stop_sequence,
                "type": message.message.type,
                "usage": _jsonify(message.message.usage),
                "context_management": _jsonify(message.message.context_management),
            },
            "uuid": message.uuid,
            "timestamp": message.timestamp,
            "signature": message.signature,
            "requestId": message.requestId,
            "apiError": message.apiError,
            "error": _jsonify(message.error),
            "errorDetails": message.errorDetails,
            "isApiErrorMessage": message.isApiErrorMessage,
            "isVirtual": message.isVirtual,
        }
    if isinstance(message, UserMessage):
        content = message.message.content
        serialized_content: Any
        if isinstance(content, str):
            serialized_content = content
        else:
            serialized_content = [_serialize_user_block(block) for block in content]
        return {
            "type": "user",
            "message": {
                "content": serialized_content,
                "role": message.message.role,
            },
            "uuid": message.uuid,
            "timestamp": message.timestamp,
            "isMeta": message.isMeta,
            "isVisibleInTranscriptOnly": message.isVisibleInTranscriptOnly,
            "isVirtual": message.isVirtual,
            "isCompactSummary": message.isCompactSummary,
            "summarizeMetadata": _jsonify(message.summarizeMetadata),
            "toolUseResult": _jsonify(message.toolUseResult),
            "toolUsePayload": _jsonify(message.toolUsePayload),
            "mcpMeta": _jsonify(message.mcpMeta),
            "imagePasteIds": _jsonify(message.imagePasteIds),
            "sourceToolAssistantUUID": message.sourceToolAssistantUUID,
            "permissionMode": message.permissionMode,
            "origin": message.origin,
        }
    if isinstance(message, SystemInformationalMessage):
        return {
            "type": "system",
            "subtype": "informational",
            "content": message.content,
            "level": message.level,
            "isMeta": message.isMeta,
            "timestamp": message.timestamp,
            "uuid": message.uuid,
            "toolUseID": message.toolUseID,
            "preventContinuation": message.preventContinuation,
        }
    if isinstance(message, CompactBoundaryMessage):
        return {
            "type": "system",
            "subtype": "compact_boundary",
            "trigger": message.trigger,
            "originalTokenCount": message.originalTokenCount,
            "newTokenCount": message.newTokenCount,
            "deletedToolUseIds": list(message.deletedToolUseIds),
            "preservedMessageUuids": list(message.preservedMessageUuids),
            "isMeta": message.isMeta,
            "timestamp": message.timestamp,
            "uuid": message.uuid,
        }
    if isinstance(message, ToolUseSummaryMessage):
        return {
            "type": "tool_use_summary",
            "summary": message.summary,
            "precedingToolUseIds": list(message.precedingToolUseIds),
            "uuid": message.uuid,
            "timestamp": message.timestamp,
        }
    if isinstance(message, ProgressMessage):
        return {
            "type": "progress",
            "data": _jsonify(message.data),
            "toolUseID": message.toolUseID,
            "parentToolUseID": message.parentToolUseID,
            "uuid": message.uuid,
            "timestamp": message.timestamp,
        }
    if isinstance(message, AttachmentMessage):
        return {
            "type": "attachment",
            "attachment": _jsonify(message.attachment),
            "uuid": message.uuid,
            "timestamp": message.timestamp,
        }
    raise SessionSnapshotValidationError(
        f"Unsupported message type for persistence: {type(message)!r}"
    )


def _deserialize_message(raw: Mapping[str, Any]) -> Message:
    message_type = _require_string(raw, "type", "message")
    if message_type == "assistant":
        payload = _require_mapping(raw.get("message"), "assistant.message")
        return AssistantMessage(
            message=AssistantPayload(
                content=tuple(
                    _deserialize_assistant_block(
                        _require_mapping(block, f"assistant.message.content[{index}]")
                    )
                    for index, block in enumerate(
                        _require_list(payload.get("content"), "assistant.message.content")
                    )
                ),
                id=_require_string(payload, "id", "assistant.message"),
                container=_jsonify(payload.get("container")),
                model=_require_string(payload, "model", "assistant.message"),
                role=_require_string(payload, "role", "assistant.message"),
                stop_reason=_require_string(payload, "stop_reason", "assistant.message"),
                stop_sequence=_require_string(
                    payload, "stop_sequence", "assistant.message"
                ),
                type=_require_string(payload, "type", "assistant.message"),
                usage=_optional_mapping(payload, "usage", "assistant.message") or {},
                context_management=_jsonify(payload.get("context_management")),
            ),
            uuid=_require_string(raw, "uuid", "assistant"),
            timestamp=_require_string(raw, "timestamp", "assistant"),
            signature=_optional_string(raw, "signature", "assistant"),
            requestId=_optional_string(raw, "requestId", "assistant"),
            apiError=_optional_string(raw, "apiError", "assistant"),
            error=_jsonify(raw.get("error")),
            errorDetails=_optional_string(raw, "errorDetails", "assistant"),
            isApiErrorMessage=bool(raw.get("isApiErrorMessage", False)),
            isVirtual=_optional_bool(raw, "isVirtual", "assistant"),
        )
    if message_type == "user":
        payload = _require_mapping(raw.get("message"), "user.message")
        content_raw = payload.get("content")
        if isinstance(content_raw, str):
            content: str | tuple[Any, ...] = content_raw
        else:
            content = tuple(
                _deserialize_user_block(
                    _require_mapping(block, f"user.message.content[{index}]")
                )
                for index, block in enumerate(
                    _require_list(content_raw, "user.message.content")
                )
            )
        image_paste_ids_raw = raw.get("imagePasteIds")
        image_paste_ids = None
        if image_paste_ids_raw is not None:
            image_paste_ids = tuple(
                _require_int(item, None, "user.imagePasteIds")
                for item in _require_list(image_paste_ids_raw, "user.imagePasteIds")
            )
        return UserMessage(
            message=UserPayload(
                content=content,
                role=_require_string(payload, "role", "user.message"),
            ),
            uuid=_require_string(raw, "uuid", "user"),
            timestamp=_require_string(raw, "timestamp", "user"),
            isMeta=_optional_bool(raw, "isMeta", "user"),
            isVisibleInTranscriptOnly=_optional_bool(
                raw, "isVisibleInTranscriptOnly", "user"
            ),
            isVirtual=_optional_bool(raw, "isVirtual", "user"),
            isCompactSummary=_optional_bool(raw, "isCompactSummary", "user"),
            summarizeMetadata=_optional_mapping(raw, "summarizeMetadata", "user"),
            toolUseResult=_jsonify(raw.get("toolUseResult")),
            toolUsePayload=_optional_mapping(raw, "toolUsePayload", "user"),
            mcpMeta=_optional_mapping(raw, "mcpMeta", "user"),
            imagePasteIds=image_paste_ids,
            sourceToolAssistantUUID=_optional_string(
                raw, "sourceToolAssistantUUID", "user"
            ),
            permissionMode=_optional_string(raw, "permissionMode", "user"),
            origin=_optional_string(raw, "origin", "user"),
        )
    if message_type == "system":
        subtype = _require_string(raw, "subtype", "system")
        if subtype == "informational":
            return SystemInformationalMessage(
                content=_require_string(raw, "content", "system"),
                level=_require_string(raw, "level", "system"),
                isMeta=bool(raw.get("isMeta", False)),
                timestamp=_require_string(raw, "timestamp", "system"),
                uuid=_require_string(raw, "uuid", "system"),
                toolUseID=_optional_string(raw, "toolUseID", "system"),
                preventContinuation=_optional_bool(
                    raw, "preventContinuation", "system"
                ),
            )
        if subtype == "compact_boundary":
            return CompactBoundaryMessage(
                trigger=_require_string(raw, "trigger", "compact_boundary"),
                originalTokenCount=_require_int(
                    raw, "originalTokenCount", "compact_boundary"
                ),
                newTokenCount=_require_int(raw, "newTokenCount", "compact_boundary"),
                deletedToolUseIds=tuple(
                    _require_string(item, None, "compact_boundary.deletedToolUseIds")
                    for item in _require_list(
                        raw.get("deletedToolUseIds"),
                        "compact_boundary.deletedToolUseIds",
                    )
                ),
                preservedMessageUuids=tuple(
                    _require_string(
                        item, None, "compact_boundary.preservedMessageUuids"
                    )
                    for item in _require_list(
                        raw.get("preservedMessageUuids"),
                        "compact_boundary.preservedMessageUuids",
                    )
                ),
                isMeta=bool(raw.get("isMeta", False)),
                timestamp=_require_string(raw, "timestamp", "compact_boundary"),
                uuid=_require_string(raw, "uuid", "compact_boundary"),
            )
        raise SessionSnapshotValidationError(f"Unsupported system subtype: {subtype!r}")
    if message_type == "tool_use_summary":
        return ToolUseSummaryMessage(
            summary=_require_string(raw, "summary", "tool_use_summary"),
            precedingToolUseIds=tuple(
                _require_string(item, None, "tool_use_summary.precedingToolUseIds")
                for item in _require_list(
                    raw.get("precedingToolUseIds"),
                    "tool_use_summary.precedingToolUseIds",
                )
            ),
            uuid=_require_string(raw, "uuid", "tool_use_summary"),
            timestamp=_require_string(raw, "timestamp", "tool_use_summary"),
        )
    if message_type == "progress":
        return ProgressMessage(
            data=_optional_mapping(raw, "data", "progress") or {},
            toolUseID=_require_string(raw, "toolUseID", "progress"),
            parentToolUseID=_require_string(raw, "parentToolUseID", "progress"),
            uuid=_require_string(raw, "uuid", "progress"),
            timestamp=_require_string(raw, "timestamp", "progress"),
        )
    if message_type == "attachment":
        return AttachmentMessage(
            attachment=_optional_mapping(raw, "attachment", "attachment") or {},
            uuid=_require_string(raw, "uuid", "attachment"),
            timestamp=_require_string(raw, "timestamp", "attachment"),
        )
    raise SessionSnapshotValidationError(f"Unsupported message type: {message_type!r}")


def _serialize_assistant_block(
    block: TextBlock | ToolUseBlock | ThinkingBlock,
) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        payload = {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": _jsonify(block.input),
        }
        if block.signature:
            payload["signature"] = block.signature
        return payload
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    raise SessionSnapshotValidationError(
        f"Unsupported assistant block for persistence: {type(block)!r}"
    )


def _deserialize_assistant_block(
    raw: Mapping[str, Any],
) -> TextBlock | ToolUseBlock | ThinkingBlock:
    block_type = _require_string(raw, "type", "assistant_block")
    if block_type == "text":
        return TextBlock(text=_require_string(raw, "text", "assistant_block"))
    if block_type == "tool_use":
        return ToolUseBlock(
            id=_require_string(raw, "id", "assistant_block"),
            name=_require_string(raw, "name", "assistant_block"),
            input=_optional_mapping(raw, "input", "assistant_block") or {},
            signature=_optional_string(raw, "signature", "assistant_block") or "",
        )
    if block_type == "thinking":
        return ThinkingBlock(
            thinking=_require_string(raw, "thinking", "assistant_block"),
            signature=_optional_string(raw, "signature", "assistant_block") or "",
        )
    raise SessionSnapshotValidationError(
        f"Unsupported assistant block type: {block_type!r}"
    )


def _serialize_user_block(block: TextBlock | ToolResultBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": _jsonify(block.content),
            "is_error": block.is_error,
        }
    raise SessionSnapshotValidationError(
        f"Unsupported user block for persistence: {type(block)!r}"
    )


def _deserialize_user_block(raw: Mapping[str, Any]) -> TextBlock | ToolResultBlock:
    block_type = _require_string(raw, "type", "user_block")
    if block_type == "text":
        return TextBlock(text=_require_string(raw, "text", "user_block"))
    if block_type == "tool_result":
        return ToolResultBlock(
            tool_use_id=_require_string(raw, "tool_use_id", "user_block"),
            content=_jsonify(raw.get("content")),
            is_error=bool(raw.get("is_error", False)),
        )
    raise SessionSnapshotValidationError(f"Unsupported user block type: {block_type!r}")


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseException):
        return {
            "type": value.__class__.__name__,
            "message": str(value),
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonify(item) for item in value]
    return str(value)


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    content = json.dumps(_jsonify(dict(payload)), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, content, write_backup=True)


def _atomic_write_text(
    path: str,
    content: str,
    *,
    write_backup: bool,
) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".session.tmp.",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if write_backup:
        try:
            _atomic_write_text(_backup_path(path), content, write_backup=False)
        except OSError:
            pass


def _load_json_mapping(
    path: str,
    *,
    allow_backup: bool = True,
) -> dict[str, Any] | None:
    payload = _read_json_mapping(path)
    if payload is not None:
        return payload
    if not allow_backup:
        return None
    backup_payload = _load_json_mapping(_backup_path(path), allow_backup=False)
    if backup_payload is None:
        return None
    try:
        _atomic_write_json(path, backup_payload)
    except OSError:
        pass
    return backup_payload


def _read_json_mapping(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except (FileNotFoundError, OSError):
        return None
    if not content.strip():
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SessionSnapshotValidationError(f"{path} must be an object")
    return raw


def _require_list(raw: Any, path: str) -> list[Any]:
    if not isinstance(raw, list):
        raise SessionSnapshotValidationError(f"{path} must be a list")
    return raw


def _require_string(raw: Any, key: str | None, path: str) -> str:
    value = raw.get(key) if key is not None else raw
    if not isinstance(value, str):
        if key is None:
            raise SessionSnapshotValidationError(f"{path} must be a string")
        raise SessionSnapshotValidationError(f"{path}.{key} must be a string")
    return value


def _optional_string(raw: Mapping[str, Any], key: str, path: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SessionSnapshotValidationError(f"{path}.{key} must be a string")
    return value


def _optional_mapping(
    raw: Mapping[str, Any],
    key: str,
    path: str,
) -> dict[str, Any] | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise SessionSnapshotValidationError(f"{path}.{key} must be an object")
    return dict(value)


def _optional_bool(raw: Mapping[str, Any], key: str, path: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SessionSnapshotValidationError(f"{path}.{key} must be a boolean")
    return value


def _require_int(raw: Any, key: str | None, path: str) -> int:
    value = raw.get(key) if key is not None else raw
    if not isinstance(value, int):
        if key is None:
            raise SessionSnapshotValidationError(f"{path} must be an integer")
        raise SessionSnapshotValidationError(f"{path}.{key} must be an integer")
    return value


def _optional_int(raw: Mapping[str, Any], key: str, path: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise SessionSnapshotValidationError(f"{path}.{key} must be an integer")
    return value


def _optional_string_tuple(
    raw: Mapping[str, Any],
    key: str,
    path: str,
) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SessionSnapshotValidationError(f"{path}.{key} must be a list")
    return tuple(_require_string(item, None, f"{path}.{key}") for item in value)
