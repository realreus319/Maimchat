from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple


DEFAULT_SESSION_TIMEOUT_MS = 24 * 60 * 60 * 1000
BRIDGE_LOGIN_INSTRUCTION = (
    "Remote Control is only available with claude.ai subscriptions. "
    "Please use `/login` to sign in with your claude.ai account."
)
BRIDGE_LOGIN_ERROR = (
    "Error: You must be logged in to use Remote Control.\n\n" + BRIDGE_LOGIN_INSTRUCTION
)
REMOTE_CONTROL_DISCONNECTED_MSG = "Remote Control disconnected."

BridgeState = str

_ALLOWED_TRANSITIONS = {
    "ready": frozenset({"connected", "failed"}),
    "connected": frozenset({"reconnecting", "failed"}),
    "reconnecting": frozenset({"connected", "failed"}),
    "failed": frozenset(),
}


@dataclass(frozen=True)
class WorkSecret:
    version: int
    session_ingress_token: str
    api_base_url: str
    sources: Tuple[Mapping[str, Any], ...] = ()
    auth: Tuple[Mapping[str, str], ...] = ()
    claude_code_args: Optional[Mapping[str, str]] = None
    mcp_config: Optional[Any] = None
    environment_variables: Optional[Mapping[str, str]] = None
    use_code_sessions: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkSecret":
        return cls(
            version=int(value["version"]),
            session_ingress_token=str(value["session_ingress_token"]),
            api_base_url=str(value["api_base_url"]),
            sources=tuple(value.get("sources", ())),
            auth=tuple(value.get("auth", ())),
            claude_code_args=value.get("claude_code_args"),
            mcp_config=value.get("mcp_config"),
            environment_variables=value.get("environment_variables"),
            use_code_sessions=bool(value.get("use_code_sessions", False)),
        )


@dataclass(frozen=True)
class BridgeEnvelope:
    message_type: str
    payload: Mapping[str, Any]
    uuid: Optional[str] = None


@dataclass(frozen=True)
class BridgeLifecycle:
    state: BridgeState = "ready"
    detail: Optional[str] = None
    history: Tuple[Tuple[BridgeState, Optional[str]], ...] = field(
        default_factory=lambda: (("ready", None),)
    )

    def transition(
        self, next_state: BridgeState, detail: Optional[str] = None
    ) -> "BridgeLifecycle":
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if next_state not in allowed:
            raise ValueError(
                "Invalid bridge state transition: {} -> {}".format(
                    self.state, next_state
                )
            )
        return BridgeLifecycle(
            state=next_state,
            detail=detail,
            history=self.history + ((next_state, detail),),
        )


def build_bridge_envelopes(
    messages: Sequence[Mapping[str, Any]],
) -> Tuple[BridgeEnvelope, ...]:
    envelopes = []
    for message in messages:
        envelopes.append(
            BridgeEnvelope(
                message_type=str(message.get("type", "unknown")),
                payload=message,
                uuid=(
                    str(message["uuid"])
                    if isinstance(message.get("uuid"), str)
                    else None
                ),
            )
        )
    return tuple(envelopes)
