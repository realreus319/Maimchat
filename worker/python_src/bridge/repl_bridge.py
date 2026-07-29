from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from .bridge_messaging import is_eligible_bridge_message
from .types import BridgeEnvelope, BridgeLifecycle, build_bridge_envelopes
from ..cli.transports.ccr_client import (
    accumulate_stream_events,
    clear_stream_accumulator_for_message,
    create_stream_accumulator,
)
from ..cli.transports.hybrid_transport import HybridTransport
from ..cli.transports.serial_batch_event_uploader import SerialBatchEventUploader
from ..cli.transports.sse_transport import SSETransport, parse_sse_frames
from ..cli.transports.transport_utils import get_transport_for_url
from ..cli.transports.websocket_transport import WebSocketTransport
from ..remote.remote_permission_bridge import (
    create_synthetic_assistant_message,
    create_tool_stub,
)
from ..remote.remote_session_manager import RemoteSessionConfig, RemoteSessionManager
from ..remote.sdk_message_adapter import convert_sdk_message
from ..remote.session_websocket import SessionsWebSocket


@dataclass
class ReplBridgeHandle:
    bridge_session_id: str
    environment_id: str
    session_ingress_url: str
    lifecycle: BridgeLifecycle = field(default_factory=BridgeLifecycle)
    posted: list[BridgeEnvelope] = field(default_factory=list)

    def write_messages(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> Tuple[BridgeEnvelope, ...]:
        eligible = [
            message for message in messages if is_eligible_bridge_message(message)
        ]
        envelopes = build_bridge_envelopes(eligible)
        self.posted.extend(envelopes)
        return envelopes

    def send_result(self, subtype: str = "success") -> BridgeEnvelope:
        envelope = BridgeEnvelope("result", {"type": "result", "subtype": subtype})
        self.posted.append(envelope)
        return envelope

    def mark_connected(self) -> None:
        self.lifecycle = self.lifecycle.transition("connected")

    def mark_reconnecting(self, detail: Optional[str] = None) -> None:
        self.lifecycle = self.lifecycle.transition("reconnecting", detail)

    def mark_failed(self, detail: Optional[str] = None) -> None:
        self.lifecycle = self.lifecycle.transition("failed", detail)


def validate_bridge_remote_contract() -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []

    handle = ReplBridgeHandle(
        bridge_session_id="bridge-session",
        environment_id="env-1",
        session_ingress_url="wss://example.test/session",
    )
    title_text = {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "<ide_opened_file>Ship it"}]},
    }
    if handle.write_messages(
        [
            title_text,
            {"type": "assistant", "isVirtual": True, "message": {"content": "noop"}},
            {"type": "system", "subtype": "local_command", "content": "/help"},
        ]
    ) != (
        BridgeEnvelope("user", title_text, None),
        BridgeEnvelope(
            "system",
            {"type": "system", "subtype": "local_command", "content": "/help"},
            None,
        ),
    ):
        errors.append("repl bridge should wrap only eligible messages")

    handle.mark_connected()
    handle.mark_reconnecting("network flap")
    handle.mark_connected()
    handle.mark_failed("auth expired")
    if handle.lifecycle.history[-1] != ("failed", "auth expired"):
        errors.append("bridge lifecycle should track reconnect and failure transitions")

    ws = SessionsWebSocket("session-1", "org-1", lambda: "token")
    connect_decision = ws.handle_close(4001)
    if not connect_decision.should_reconnect or connect_decision.delay_ms != 2000:
        errors.append("session websocket should retry 4001 once with growing backoff")
    terminal_4003 = SessionsWebSocket(
        "session-1", "org-1", lambda: "token"
    ).handle_close(4003)
    if terminal_4003.should_reconnect:
        errors.append("session websocket should not reconnect on 4003")

    manager = RemoteSessionManager(
        RemoteSessionConfig(
            session_id="session-1", get_access_token=lambda: "token", org_uuid="org-1"
        )
    )
    permission_event = manager.handle_message(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "input": {"command": "pwd"},
            },
        }
    )
    if (
        permission_event.kind != "permission_request"
        or manager.pending_request_count != 1
    ):
        errors.append("remote session manager should track permission requests")
    if not manager.respond_to_permission_request(
        "req-1",
        {"behavior": "allow", "updatedInput": {"command": "pwd"}},
    ):
        errors.append(
            "remote session manager should answer pending permission requests"
        )

    unsupported = RemoteSessionManager(
        RemoteSessionConfig(
            session_id="session-1", get_access_token=lambda: "token", org_uuid="org-1"
        )
    )
    unsupported.handle_message(
        {
            "type": "control_request",
            "request_id": "req-2",
            "request": {"subtype": "set_model", "model": "haiku"},
        }
    )
    if unsupported.websocket.outbound_messages[-1]["response"]["subtype"] != "error":
        errors.append(
            "unsupported control requests should produce error control responses"
        )

    converted = convert_sdk_message(
        {
            "type": "system",
            "subtype": "status",
            "status": "compacting",
            "uuid": "sys-1",
        }
    )
    if (
        converted.type != "message"
        or converted.message is None
        or converted.message["content"] != "Compacting conversation…"
    ):
        errors.append(
            "sdk adapter should convert compacting status to informational system message"
        )

    synthetic = create_synthetic_assistant_message(
        {
            "tool_name": "mcp__remote",
            "tool_use_id": "tool-x",
            "input": {"path": "/tmp"},
        },
        "request-77",
    )
    if synthetic["message"]["content"][0]["id"] != "tool-x":
        errors.append(
            "remote permission bridge should synthesize assistant tool_use messages"
        )
    tool_stub = create_tool_stub("remote-tool")
    if (
        tool_stub.render_tool_use_message({"path": "/tmp", "count": 2})
        != "path: /tmp, count: 2"
    ):
        errors.append("tool stub should render short input previews")

    selected_default = get_transport_for_url("wss://example.test/sessions/abc", env={})
    if not isinstance(selected_default, WebSocketTransport):
        errors.append("transport utils should default to WebSocketTransport")
    selected_v2 = get_transport_for_url(
        "wss://example.test/sessions/abc", env={"CLAUDE_CODE_USE_CCR_V2": "1"}
    )
    if not isinstance(selected_v2, SSETransport):
        errors.append("transport utils should choose SSETransport in CCR v2 mode")
    selected_post = get_transport_for_url(
        "wss://example.test/sessions/abc",
        env={"CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2": "true"},
    )
    if not isinstance(selected_post, HybridTransport):
        errors.append(
            "transport utils should choose HybridTransport for post-ingress mode"
        )

    cli_ws = WebSocketTransport("wss://example.test/sessions/abc")
    first_retry = cli_ws.handle_disconnect(close_code=1011, now_ms=0)
    second_retry = cli_ws.handle_disconnect(close_code=1011, now_ms=1000)
    if first_retry.delay_ms != 1000 or second_retry.delay_ms != 2000:
        errors.append(
            "websocket transport should exponential-backoff transient disconnects"
        )
    if (
        WebSocketTransport("wss://example.test/sessions/abc")
        .handle_disconnect(4003, 0)
        .should_reconnect
    ):
        errors.append("websocket transport should stop on permanent close codes")

    frames = parse_sse_frames(
        ':keepalive\n\nid: 4\nevent: client_event\ndata: {"x":1}\n\nrest'
    )
    if len(frames["frames"]) != 2 or frames["remaining"] != "rest":
        errors.append(
            "sse parser should emit comments and framed data while preserving tail"
        )

    uploader = SerialBatchEventUploader[int](
        max_batch_size=2,
        max_queue_size=8,
        base_delay_ms=500,
        max_delay_ms=8000,
        jitter_ms=0,
        max_consecutive_failures=2,
    )
    uploader.enqueue([1, 2, 3])
    batch = uploader.take_batch()
    uploader.requeue_front(batch)
    uploader.record_failure()
    uploader.record_failure()
    if uploader.dropped_batch_count != 1:
        errors.append(
            "serial uploader should drop after hitting max consecutive failures"
        )

    accumulator = create_stream_accumulator()
    coalesced = accumulate_stream_events(
        [
            {
                "type": "stream_event",
                "uuid": "a",
                "session_id": "session-1",
                "parent_tool_use_id": None,
                "event": {"type": "message_start", "message": {"id": "msg-1"}},
            },
            {
                "type": "stream_event",
                "uuid": "b",
                "session_id": "session-1",
                "parent_tool_use_id": None,
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            },
            {
                "type": "stream_event",
                "uuid": "c",
                "session_id": "session-1",
                "parent_tool_use_id": None,
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            },
        ],
        accumulator,
    )
    if coalesced[-1]["event"]["delta"]["text"] != "Hello":
        errors.append(
            "ccr client should coalesce text deltas into full-so-far snapshots"
        )
    clear_stream_accumulator_for_message(
        accumulator,
        {
            "session_id": "session-1",
            "parent_tool_use_id": None,
            "message": {"id": "msg-1"},
        },
    )
    if accumulator.by_message:
        errors.append(
            "ccr client should clear stream accumulator state when assistant completes"
        )

    return (len(errors) == 0, tuple(errors))
