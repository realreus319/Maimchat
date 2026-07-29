"""Bootstrap state and global runtime configuration.

Python port of src/bootstrap/state.ts.

This module provides a single-process global state store for startup flags
and runtime configuration.  Every setter records the value in a module-level
dict; every getter reads from it.  No I/O, no side-effects beyond the dict.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Internal state store — mirrors the TS bootstrap/state pattern where each
# flag has its own get/set pair backed by a module-level variable.
# ---------------------------------------------------------------------------

_state: Dict[str, object] = {
    "is_interactive": True,
    "client_type": "cli",
    "entrypoint": None,
    "question_preview_format": None,
    "session_source": None,
    "flag_settings_path": None,
    "allowed_setting_sources": None,
    "inline_plugins": [],
    "original_cwd": None,
    "additional_directories": [],
    "main_loop_model_override": None,
    "main_thread_agent_type": None,
    "teleported_session_info": None,
    "is_remote_mode": False,
    "session_bypass_permissions_mode": False,
    "session_persistence_disabled": False,
    "sdk_betas": [],
    "initial_effort_value": None,
    "allowed_channels": [],
    "kairos_active": False,
    "initial_main_loop_model": None,
    "is_non_interactive_session": False,
    "session_id": None,
    "user_msg_opt_in": None,
    "cwd_state": None,
    "direct_connect_server_url": None,
    "chrome_flag_override": None,
}


def _get(key: str, default: object = None) -> object:
    return _state.get(key, default)


def _set(key: str, value: object) -> None:
    _state[key] = value


# ---------------------------------------------------------------------------
# Getters / setters — one per TS export
# ---------------------------------------------------------------------------


def getIsInteractive() -> bool:
    return bool(_get("is_interactive", True))


def setIsInteractive(value: bool) -> None:
    _set("is_interactive", value)


def getClientType() -> str:
    return str(_get("client_type", "cli"))


def setClientType(value: str) -> None:
    _set("client_type", value)


def getEntrypoint() -> Optional[str]:
    return _get("entrypoint")  # type: ignore[return-value]


def getQuestionPreviewFormat() -> Optional[str]:
    return _get("question_preview_format")  # type: ignore[return-value]


def setQuestionPreviewFormat(value: Optional[str]) -> None:
    _set("question_preview_format", value)


def getSessionSource() -> Optional[str]:
    return _get("session_source")  # type: ignore[return-value]


def setSessionSource(value: str) -> None:
    _set("session_source", value)


def getFlagSettingsPath() -> Optional[str]:
    return _get("flag_settings_path")  # type: ignore[return-value]


def setFlagSettingsPath(value: str) -> None:
    _set("flag_settings_path", value)


def getAllowedSettingSources() -> Optional[Sequence[str]]:
    return _get("allowed_setting_sources")  # type: ignore[return-value]


def setAllowedSettingSources(value: Sequence[str]) -> None:
    _set("allowed_setting_sources", list(value))


def getInlinePlugins() -> List[str]:
    return list(_get("inline_plugins", []))  # type: ignore[arg-type]


def setInlinePlugins(value: List[str]) -> None:
    _set("inline_plugins", list(value))


def getOriginalCwd() -> Optional[str]:
    return _get("original_cwd")  # type: ignore[return-value]


def setOriginalCwd(value: str) -> None:
    _set("original_cwd", value)


def getAdditionalDirectories() -> List[str]:
    return list(_get("additional_directories", []))  # type: ignore[arg-type]


def setAdditionalDirectories(value: List[str]) -> None:
    _set("additional_directories", list(value))


def getMainLoopModelOverride() -> Optional[str]:
    return _get("main_loop_model_override")  # type: ignore[return-value]


def setMainLoopModelOverride(value: Optional[str]) -> None:
    _set("main_loop_model_override", value)


def getMainThreadAgentType() -> Optional[str]:
    return _get("main_thread_agent_type")  # type: ignore[return-value]


def setMainThreadAgentType(value: Optional[str]) -> None:
    _set("main_thread_agent_type", value)


def getTeleportedSessionInfo() -> Optional[Dict]:
    return _get("teleported_session_info")  # type: ignore[return-value]


def setTeleportedSessionInfo(value: Optional[Dict]) -> None:
    _set("teleported_session_info", value)


def getIsRemoteMode() -> bool:
    return bool(_get("is_remote_mode", False))


def setIsRemoteMode(value: bool) -> None:
    _set("is_remote_mode", value)


def getSessionBypassPermissionsMode() -> bool:
    return bool(_get("session_bypass_permissions_mode", False))


def setSessionBypassPermissionsMode(value: bool) -> None:
    _set("session_bypass_permissions_mode", value)


def getSessionPersistenceDisabled() -> bool:
    return bool(_get("session_persistence_disabled", False))


def setSessionPersistenceDisabled(value: bool) -> None:
    _set("session_persistence_disabled", value)


def getSdkBetas() -> List[str]:
    return list(_get("sdk_betas", []))  # type: ignore[arg-type]


def setSdkBetas(value: List[str]) -> None:
    _set("sdk_betas", list(value))


def getInitialEffortValue() -> Optional[str]:
    return _get("initial_effort_value")  # type: ignore[return-value]


def setInitialEffortValue(value: Optional[str]) -> None:
    _set("initial_effort_value", value)


def getAllowedChannels() -> List:
    return list(_get("allowed_channels", []))  # type: ignore[arg-type]


def setAllowedChannels(value: List) -> None:
    _set("allowed_channels", list(value))


def getKairosActive() -> bool:
    return bool(_get("kairos_active", False))


def setKairosActive(value: bool) -> None:
    _set("kairos_active", value)


def getInitialMainLoopModel() -> Optional[str]:
    return _get("initial_main_loop_model")  # type: ignore[return-value]


def setInitialMainLoopModel(value: Optional[str]) -> None:
    _set("initial_main_loop_model", value)


def getIsNonInteractiveSession() -> bool:
    return bool(_get("is_non_interactive_session", False))


def setIsNonInteractiveSession(value: bool) -> None:
    _set("is_non_interactive_session", value)


def getSessionId() -> Optional[str]:
    return _get("session_id")  # type: ignore[return-value]


def setSessionId(value: Optional[str]) -> None:
    _set("session_id", value)


def getUserMsgOptIn() -> Optional[bool]:
    return _get("user_msg_opt_in")  # type: ignore[return-value]


def setUserMsgOptIn(value: Optional[bool]) -> None:
    _set("user_msg_opt_in", value)


def getCwdState() -> Optional[str]:
    return _get("cwd_state")  # type: ignore[return-value]


def setCwdState(value: Optional[str]) -> None:
    _set("cwd_state", value)


def getDirectConnectServerUrl() -> Optional[str]:
    return _get("direct_connect_server_url")  # type: ignore[return-value]


def setDirectConnectServerUrl(value: Optional[str]) -> None:
    _set("direct_connect_server_url", value)


def getChromeFlagOverride() -> Optional[bool]:
    return _get("chrome_flag_override")  # type: ignore[return-value]


def setChromeFlagOverride(value: Optional[bool]) -> None:
    _set("chrome_flag_override", value)


def switchSession(session_id: Optional[str]) -> None:
    _set("session_id", session_id)
