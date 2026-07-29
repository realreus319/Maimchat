from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Mapping, Optional, Protocol, Sequence, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from .query import (
    AttachmentMessage,
    AssistantMessage,
    AssistantPayload,
    CompactBoundaryMessage,
    Message,
    NO_CONTENT_MESSAGE,
    QuerySession,
    QueryStateTransitionError,
    RequestStartEvent,
    StreamEvent,
    SYNTHETIC_MODEL,
    TerminalReason,
    TerminalTransition,
    TombstoneMessage,
    ToolExecutionUpdate,
    ToolResultBlock,
    ToolUseSummaryMessage,
    ToolUseBlock,
    TextBlock,
    ThinkingBlock,
    UserMessage,
    _assistant_message_has_visible_output,
    _build_assistant_message_from_stream,
    _apply_message_delta,
    _default_usage,
    _initialize_stream_block,
    _merge_mapping,
    _normalize_tool_input,
    _record_tool_update,
    _uuid,
    createAttachmentMessage,
    createAssistantMessage,
    createCompactBoundaryMessage,
    createStreamEvent,
    createStreamRequestStartEvent,
    createSystemMessage,
    createToolUseSummaryMessage,
    createUserInterruptionMessage,
    createUserMessage,
    yieldMissingToolResultBlocks,
)
from .services.compact import (
    clear_compact_warning_suppression,
    group_messages_by_api_round,
    run_post_compact_cleanup,
)
from .services.api.with_retry import (
    RetrySettings,
    build_effective_retry_settings,
    classify_retryable_error,
    format_request_failure,
    persistent_retry_enabled,
    retry_window_exhausted,
    sleep_for_retry,
)
from .services.api.transport import pooled_urlopen as urlopen
from .utils.config import (
    get_anthropic_api_key,
    get_anthropic_auth_token,
    get_anthropic_base_url,
    get_api_timeout_ms,
    get_autocompact_threshold,
    get_global_config,
    get_model_provider,
    get_openai_api_key,
    get_openai_base_url,
    get_openai_default_model,
    get_openai_system_role,
)
from .utils.model_selection import (
    is_valid_effort_level,
    model_supports_adaptive_thinking,
    model_supports_effort,
    normalize_model_string_for_api,
    resolve_fallback_model,
)
from .utils.tokenization import count_text_tokens


__all__ = [
    "AnthropicStreamingModelAdapter",
    "OpenAIChatStreamingModelAdapter",
    "LocalEchoModelAdapter",
    "ModelAdapter",
    "QueryEvent",
    "QueryStreamConfig",
    "RequestStartEvent",
    "StreamEvent",
    "StreamingEnvelopes",
    "ToolBatchResult",
    "ToolExecutor",
    "ToolUpdate",
    "TombstoneMessage",
    "build_compaction_attachment_messages",
    "create_model_adapter_from_env",
    "create_tombstone",
    "stream_query_session",
    "yield_missing_tool_result_blocks",
]


_DEFAULT_TOOL_FALLBACK_MESSAGE = (
    "I requested a tool, but this minimal Python REPL runtime only completes a "
    "single assistant turn. I recorded a synthetic tool result so the session can "
    "finish cleanly."
)
_SESSION_MESSAGE_REPLACEMENT_KEY = "__replace_session_messages__"
_COMPACTION_TRANSCRIPT_CHAR_LIMIT = 24_000
_COMPACTION_OUTPUT_TOKEN_LIMIT = 768
_CONTEXT_COLLAPSE_CHAR_LIMIT = 4_000
_THINKING_CLEAR_LATCH_AFTER_MS = 60 * 60 * 1000
_CONTEXT_COLLAPSE_HEAD_CHARS = 1_400
_CONTEXT_COLLAPSE_TAIL_CHARS = 1_200
_INLINE_BINARY_KEYS = frozenset({"base64_data", "base64Data"})
_COMPACTION_MODEL_TIMEOUT_SECONDS = 20.0
_COMPACTION_LIST_LIMIT = 5
_EFFORT_BETA_HEADER = "effort-2025-11-24"
_TASK_BUDGETS_BETA_HEADER = "task-budgets-2026-03-13"
_PROMPT_CACHING_SCOPE_BETA_HEADER = "prompt-caching-scope-2026-01-05"
_CONTEXT_MANAGEMENT_BETA_HEADER = "context-management-2025-06-27"
_PROMPT_CACHE_BREAK_MIN_DROP_TOKENS = 2_000
_PROMPT_CACHE_BREAK_MIN_DROP_RATIO = 0.10
_ASSISTANT_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_COMPACTION_PROMPT_SYSTEM = (
    "You summarize earlier conversation history for a coding agent. "
    "Return strict JSON with keys: summary, user_requests, decisions, "
    "tool_activity, files, open_questions. Each list must contain short "
    "strings. Do not include markdown fences."
)
_DEFAULT_TOOL_RESULT_BUDGET_TOKENS = 12_000
_TOOL_RESULT_BUDGET_MARKER = "tool_result_budget"
_TOOL_USE_SUMMARY_PROMPT_SYSTEM = (
    "Summarize recent tool executions for a coding-agent transcript. "
    "Return one concise sentence with the most important outcomes only."
)
_TOOL_USE_SUMMARY_OUTPUT_TOKEN_LIMIT = 160
_TOOL_USE_SUMMARY_TIMEOUT_SECONDS = 8.0
_SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
# Slightly above the TS TaskOutput max of 600_000ms so tools can return their
# own timeout payload instead of being preempted by the outer executor guard.
_DEFAULT_TOOL_EXECUTOR_TIMEOUT_SECONDS = 605.0
_SYSTEM_PROMPT_ATTRIBUTION_PREFIX = "x-anthropic-billing-header"
_SYSTEM_PROMPT_KNOWN_PREFIXES = frozenset(
    {
        "You are Claude Code, Anthropic's official CLI for Claude.",
        "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.",
        "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
        "You are Codex, a coding agent based on GPT-5.",
    }
)
SystemPromptBlock = Mapping[str, object] | str
NormalizedSystemPrompt = str | tuple[dict[str, object], ...] | None
SystemPrompt = str | Sequence[SystemPromptBlock] | None
ToolSchemaDefinition = Mapping[str, object]
ToolDefinitions = Sequence[ToolSchemaDefinition] | None
QueryOptions = Mapping[str, Any] | None

# Recursion guard: prevents nested/recursive compact calls
_compact_in_progress: bool = False


class ModelAdapter(Protocol):
    def call_model(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AsyncGenerator[Union[Mapping[str, Any], StreamEvent], None]: ...


class ToolExecutor(Protocol):
    def run(
        self,
        tool_use_blocks: Sequence[AssistantMessage],
        *,
        signal: Optional[asyncio.Event] = None,
    ) -> AsyncGenerator[ToolExecutionUpdate, None]: ...


class _StreamingToolExecutionSession(Protocol):
    async def add_tool_use_messages(
        self,
        tool_use_blocks: Sequence[AssistantMessage],
    ) -> None: ...

    async def next_update(self) -> ToolExecutionUpdate | None: ...

    def get_completed_updates_nowait(self) -> Sequence[ToolExecutionUpdate]: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...

    def discard(self) -> None: ...


ToolUpdate = ToolExecutionUpdate


@dataclass(frozen=True)
class QueryEvent:
    session: QuerySession
    output: Optional[
        Union[RequestStartEvent, StreamEvent, Message, TombstoneMessage]
    ] = None
    terminal: Optional[TerminalTransition] = None


@dataclass(frozen=True)
class _PromptCacheDetectionState:
    cache_key: str
    model: str
    cache_read_input_tokens: int
    used_cache_breakpoints: bool = False
    component_digests: tuple[tuple[str, str], ...] = ()
    component_payloads: tuple[tuple[str, object], ...] = ()
    transcript_file_markers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _StreamingIdleWarning:
    idle_seconds: float
    timeout_seconds: float


@dataclass(frozen=True)
class ToolBatchResult:
    updates: tuple[ToolExecutionUpdate, ...] = ()
    updated_context: Optional[Mapping[str, Any]] = None
    used_fallback: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class StreamingEnvelopes:
    request_start: RequestStartEvent
    stream_events: tuple[StreamEvent, ...]
    assistant_messages: tuple[AssistantMessage, ...]
    tool_updates: tuple[ToolExecutionUpdate, ...] = ()


@dataclass(frozen=True)
class _PreparedCompaction:
    messages: tuple[Message, ...]
    emitted_messages: tuple[Message, ...]


@dataclass(frozen=True)
class _CompactionDraft:
    summary_message: UserMessage
    system_prefix: tuple[Message, ...]
    preserved_tail: tuple[Message, ...]
    removed_non_system: tuple[Message, ...]
    deleted_tool_use_ids: tuple[str, ...]
    preserved_message_uuids: tuple[str, ...]
    original_token_count: int


@dataclass(frozen=True)
class _TerminalHookOutcome:
    session: QuerySession
    messages: tuple[Message, ...] = ()
    terminal: Optional[TerminalTransition] = None
    should_continue: bool = False


@dataclass(frozen=True)
class QueryStreamConfig:
    system_prompt: SystemPrompt = None
    tools: ToolDefinitions = None
    signal: Optional[asyncio.Event] = None
    options: QueryOptions = None
    background_request_resolver: Optional[Callable[[], bool]] = None
    tool_executor_timeout_seconds: float = _DEFAULT_TOOL_EXECUTOR_TIMEOUT_SECONDS
    tool_fallback_message: str = _DEFAULT_TOOL_FALLBACK_MESSAGE
    max_turns: Optional[int] = None
    auto_compact_enabled: Optional[bool] = None
    auto_compact_threshold_ratio: float = get_autocompact_threshold()
    # Tuned for the kimi-k2.7-code worker (256K context window). Long-running react agents
    # (planner/worker) should use the model's full context; autocompact fires at 0.93 of this.
    max_context_tokens: int = 256_000
    blocking_limit_tokens: Optional[int] = None
    reactive_compact_tail_messages: int = 8
    max_output_tokens_recovery_limit: int = 3
    # kimi-k2.7-code max output is 16K — the recovery escalation must NOT exceed the model cap
    # (a 64K value would be rejected by the provider, so the recovery would error instead of retry).
    max_output_tokens_escalation: int = 16_000
    streaming_idle_timeout_seconds: Optional[float] = 90.0
    streaming_stall_threshold_seconds: Optional[float] = 30.0
    non_streaming_fallback_timeout_seconds: float = 30.0
    fallback_model_adapter: Optional["ModelAdapter"] = None
    on_streaming_stall: Optional[Callable[[float, int, float], Any]] = None
    on_streaming_idle_warning: Optional[Callable[[float, float], Any]] = None
    tool_result_budget_tokens: Optional[int] = _DEFAULT_TOOL_RESULT_BUDGET_TOKENS
    token_budget_total_tokens: Optional[int] = None
    token_budget_continue_threshold_tokens: int = 0
    token_budget_diminishing_returns_after_continuations: int = 3
    token_budget_diminishing_returns_min_output_tokens: int = 500
    token_budget_continue_message: str = (
        "Please continue until the task is complete. "
        "Remaining output budget: {remaining_tokens}."
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "system_prompt",
            _normalize_system_prompt(self.system_prompt),
        )
        object.__setattr__(
            self,
            "tools",
            _normalize_tools_payload(self.tools),
        )
        object.__setattr__(
            self,
            "options",
            _normalize_query_options(self.options),
        )


@dataclass
class LocalEchoModelAdapter:
    prefix: str = "You said"

    async def call_model(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AsyncGenerator[Mapping[str, Any], None]:
        del system_prompt, tools, signal, options
        reply_text = _build_echo_reply(messages, prefix=self.prefix)
        message_id = _uuid()
        yield {
            "type": "message_start",
            "message": {
                "id": message_id,
                "model": "python-port-local-echo",
                "role": "assistant",
                "stop_reason": None,
                "stop_sequence": "",
                "type": "message",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        yield {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        for chunk in _chunk_text(reply_text):
            await asyncio.sleep(0)
            yield {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": chunk},
            }
        yield {"type": "content_block_stop", "index": 0}
        yield {
            "type": "message_delta",
            "usage": {"output_tokens": len(reply_text.split())},
            "delta": {"stop_reason": "end_turn"},
        }
        yield {"type": "message_stop"}


@dataclass
class AnthropicStreamingModelAdapter:
    model: str
    api_key: str | None = None
    auth_token: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 600.0
    latched_beta_headers: tuple[str, ...] = ()
    retry_settings: RetrySettings = RetrySettings()
    fallback_model: str | None = None
    persistent_mode: bool = False
    fallback_after_529_attempts: int = 3

    @classmethod
    def from_env(
        cls,
        *,
        model: str,
        fallback_model: str | None = None,
    ) -> "AnthropicStreamingModelAdapter | None":
        api_key = get_anthropic_api_key()
        auth_token = get_anthropic_auth_token()
        if api_key is None and auth_token is None:
            return None
        return cls(
            model=model,
            api_key=api_key,
            auth_token=auth_token,
            base_url=get_anthropic_base_url(),
            timeout_seconds=get_api_timeout_ms() / 1000,
            fallback_model=resolve_fallback_model(
                primary_model=model,
                fallback=fallback_model,
            ),
        )

    async def call_model(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AsyncGenerator[Mapping[str, Any], None]:
        del signal
        normalized_options = _normalize_query_options(options)
        request_models = self._request_model_candidates(normalized_options)
        persistent_mode = self._persistent_retry_mode(normalized_options)
        retry_settings = build_effective_retry_settings(
            self.retry_settings,
            persistent_mode=persistent_mode,
        )
        started_at = time.monotonic()
        attempt = 0
        model_attempts_529: dict[str, int] = {model: 0 for model in request_models}
        current_model_index = 0
        while current_model_index < len(request_models):
            active_model = request_models[current_model_index]
            beta_headers = self._latched_beta_headers_for_model(
                active_model,
                normalized_options,
                messages=messages,
            )
            request = self._build_request(
                messages,
                model=active_model,
                system_prompt=system_prompt,
                tools=tools,
                options=normalized_options,
                beta_headers=beta_headers,
            )
            saw_payload = False
            attempt += 1
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    for payload in _iter_sse_payloads(response):
                        saw_payload = True
                        yield payload
                self.model = active_model
                return
            except (HTTPError, URLError) as exc:
                decision = classify_retryable_error(exc)
                if decision.status_code == 529 and _is_background_request(normalized_options):
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible background request dropped to avoid retry amplification",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                if decision.status_code == 529:
                    model_attempts_529[active_model] = (
                        model_attempts_529.get(active_model, 0) + 1
                    )
                else:
                    model_attempts_529[active_model] = 0
                if saw_payload or not decision.retryable:
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                switched_models = self._should_switch_529_fallback_model(
                    active_model=active_model,
                    current_model_index=current_model_index,
                    model_attempts_529=model_attempts_529,
                )
                if switched_models:
                    current_model_index += 1
                    continue
                if retry_window_exhausted(
                    attempt,
                    settings=retry_settings,
                    started_at=started_at,
                    retry_after_seconds=decision.retry_after_seconds,
                ):
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                await sleep_for_retry(
                    attempt,
                    settings=retry_settings,
                    retry_after_seconds=decision.retry_after_seconds,
                )
        raise RuntimeError("Anthropic-compatible request retry loop exhausted")

    async def call_model_nonstream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AssistantMessage:
        del signal
        normalized_options = _normalize_query_options(options)
        request_models = self._request_model_candidates(normalized_options)
        persistent_mode = self._persistent_retry_mode(normalized_options)
        retry_settings = build_effective_retry_settings(
            self.retry_settings,
            persistent_mode=persistent_mode,
        )
        started_at = time.monotonic()
        attempt = 0
        model_attempts_529: dict[str, int] = {model: 0 for model in request_models}
        current_model_index = 0
        while current_model_index < len(request_models):
            active_model = request_models[current_model_index]
            beta_headers = self._latched_beta_headers_for_model(
                active_model,
                normalized_options,
                messages=messages,
            )
            request = self._build_request(
                messages,
                model=active_model,
                system_prompt=system_prompt,
                tools=tools,
                options=normalized_options,
                beta_headers=beta_headers,
                stream=False,
                accept="application/json",
            )
            attempt += 1
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                self.model = active_model
                return _assistant_message_from_non_stream_payload(payload)
            except (HTTPError, URLError) as exc:
                decision = classify_retryable_error(exc)
                if decision.status_code == 529 and _is_background_request(normalized_options):
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible background request dropped to avoid retry amplification",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                if not decision.retryable:
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                if decision.status_code == 529:
                    model_attempts_529[active_model] = (
                        model_attempts_529.get(active_model, 0) + 1
                    )
                else:
                    model_attempts_529[active_model] = 0
                switched_models = self._should_switch_529_fallback_model(
                    active_model=active_model,
                    current_model_index=current_model_index,
                    model_attempts_529=model_attempts_529,
                )
                if switched_models:
                    current_model_index += 1
                    continue
                if retry_window_exhausted(
                    attempt,
                    settings=retry_settings,
                    started_at=started_at,
                    retry_after_seconds=decision.retry_after_seconds,
                ):
                    raise RuntimeError(
                        format_request_failure(
                            "Anthropic-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=active_model,
                            request_models=request_models,
                            persistent_mode=persistent_mode,
                        )
                    ) from exc
                await sleep_for_retry(
                    attempt,
                    settings=retry_settings,
                    retry_after_seconds=decision.retry_after_seconds,
                )
        raise RuntimeError("Anthropic-compatible request retry loop exhausted")

    def _request_model_candidates(
        self,
        options: Mapping[str, Any] | None,
    ) -> tuple[str, ...]:
        fallback_model = resolve_fallback_model(
            primary_model=self.model,
            fallback=(
                options.get("fallback_model")
                if isinstance(options, Mapping)
                else self.fallback_model
            )
            or self.fallback_model,
        )
        if fallback_model is None:
            return (self.model,)
        return (self.model, fallback_model)

    def _effective_retry_settings(
        self,
        options: Mapping[str, Any] | None,
    ) -> RetrySettings:
        return build_effective_retry_settings(
            self.retry_settings,
            persistent_mode=self._persistent_retry_mode(options),
        )

    def _persistent_retry_mode(
        self,
        options: Mapping[str, Any] | None,
    ) -> bool:
        return persistent_retry_enabled(
            options=options,
            default=self.persistent_mode,
        )

    def _latched_beta_headers_for_model(
        self,
        model: str,
        options: Mapping[str, Any] | None,
        *,
        messages: Sequence[Message] = (),
    ) -> tuple[str, ...]:
        beta_headers = _merge_beta_headers(
            self.latched_beta_headers,
            _resolve_anthropic_beta_headers(
                model,
                options,
                messages=messages,
            ),
        )
        self.latched_beta_headers = beta_headers
        return beta_headers

    def _build_request(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        system_prompt: SystemPrompt,
        tools: ToolDefinitions,
        options: Mapping[str, Any] | None,
        beta_headers: Sequence[str],
        stream: bool = True,
        accept: str = "text/event-stream",
    ) -> Request:
        request_body = _build_anthropic_request_body(
            messages,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            options=options,
            stream=stream,
            base_url=self.base_url or get_anthropic_base_url(),
        )
        return Request(
            f"{(self.base_url or get_anthropic_base_url()).rstrip('/')}/v1/messages",
            data=json.dumps(request_body).encode("utf-8"),
            headers=_build_anthropic_headers(
                api_key=self.api_key,
                auth_token=self.auth_token,
                beta_headers=beta_headers,
                accept=accept,
            ),
            method="POST",
        )

    def _should_switch_529_fallback_model(
        self,
        *,
        active_model: str,
        current_model_index: int,
        model_attempts_529: Mapping[str, int],
    ) -> bool:
        if self.fallback_after_529_attempts <= 0:
            return False
        if current_model_index <= 0 and model_attempts_529.get(active_model, 0) >= (
            self.fallback_after_529_attempts
        ):
            return True
        return False

    def clear_beta_header_latches(self) -> None:
        self.latched_beta_headers = ()


@dataclass
class OpenAIChatStreamingModelAdapter:
    model: str
    api_key: str
    base_url: str | None = None
    timeout_seconds: float = 600.0
    retry_settings: RetrySettings = RetrySettings()
    persistent_mode: bool = False
    supports_compaction_summary: bool = True

    @classmethod
    def from_env(
        cls,
        *,
        model: str,
    ) -> "OpenAIChatStreamingModelAdapter | None":
        api_key = get_openai_api_key()
        if api_key is None:
            return None
        default_model = get_openai_default_model()
        selected_model = default_model or model
        if selected_model.startswith("claude-") and default_model is None:
            selected_model = "gpt-4o-mini"
        return cls(
            model=selected_model,
            api_key=api_key,
            base_url=get_openai_base_url(),
            timeout_seconds=get_api_timeout_ms() / 1000,
        )

    async def call_model(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AsyncGenerator[Mapping[str, Any], None]:
        del signal
        normalized_options = _normalize_query_options(options)
        retry_settings = build_effective_retry_settings(
            self.retry_settings,
            persistent_mode=self._persistent_retry_mode(normalized_options),
        )
        started_at = time.monotonic()
        attempt = 0
        while True:
            request = self._build_request(
                messages,
                system_prompt=system_prompt,
                tools=tools,
                options=normalized_options,
            )
            saw_payload = False
            attempt += 1
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    for payload in _iter_openai_chat_stream_events(
                        _iter_sse_payloads(response)
                    ):
                        saw_payload = True
                        yield payload
                return
            except (HTTPError, URLError) as exc:
                decision = classify_retryable_error(exc)
                if decision.status_code == 529 and _is_background_request(normalized_options):
                    raise RuntimeError(
                        format_request_failure(
                            "OpenAI-compatible background request dropped to avoid retry amplification",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=self.model,
                            request_models=(self.model,),
                            persistent_mode=self._persistent_retry_mode(
                                normalized_options
                            ),
                        )
                    ) from exc
                if saw_payload or not decision.retryable:
                    raise RuntimeError(
                        format_request_failure(
                            "OpenAI-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=self.model,
                            request_models=(self.model,),
                            persistent_mode=self._persistent_retry_mode(
                                normalized_options
                            ),
                        )
                    ) from exc
                if retry_window_exhausted(
                    attempt,
                    settings=retry_settings,
                    started_at=started_at,
                    retry_after_seconds=decision.retry_after_seconds,
                ):
                    raise RuntimeError(
                        format_request_failure(
                            "OpenAI-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=self.model,
                            request_models=(self.model,),
                            persistent_mode=self._persistent_retry_mode(
                                normalized_options
                            ),
                        )
                    ) from exc
                await sleep_for_retry(
                    attempt,
                    settings=retry_settings,
                    retry_after_seconds=decision.retry_after_seconds,
                )

    async def call_model_nonstream(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt = None,
        tools: ToolDefinitions = None,
        signal: Optional[asyncio.Event] = None,
        options: QueryOptions = None,
    ) -> AssistantMessage:
        del signal
        normalized_options = _normalize_query_options(options)
        retry_settings = build_effective_retry_settings(
            self.retry_settings,
            persistent_mode=self._persistent_retry_mode(normalized_options),
        )
        started_at = time.monotonic()
        attempt = 0
        while True:
            request = self._build_request(
                messages,
                system_prompt=system_prompt,
                tools=tools,
                options=normalized_options,
                stream=False,
                accept="application/json",
            )
            attempt += 1
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                return _assistant_message_from_openai_chat_payload(payload)
            except (HTTPError, URLError) as exc:
                decision = classify_retryable_error(exc)
                if not decision.retryable:
                    raise RuntimeError(
                        format_request_failure(
                            "OpenAI-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=self.model,
                            request_models=(self.model,),
                            persistent_mode=self._persistent_retry_mode(
                                normalized_options
                            ),
                        )
                    ) from exc
                if retry_window_exhausted(
                    attempt,
                    settings=retry_settings,
                    started_at=started_at,
                    retry_after_seconds=decision.retry_after_seconds,
                ):
                    raise RuntimeError(
                        format_request_failure(
                            "OpenAI-compatible request failed",
                            exc,
                            decision=decision,
                            attempt=attempt,
                            max_attempts=retry_settings.max_attempts,
                            active_model=self.model,
                            request_models=(self.model,),
                            persistent_mode=self._persistent_retry_mode(
                                normalized_options
                            ),
                        )
                    ) from exc
                await sleep_for_retry(
                    attempt,
                    settings=retry_settings,
                    retry_after_seconds=decision.retry_after_seconds,
                )

    def _persistent_retry_mode(
        self,
        options: Mapping[str, Any] | None,
    ) -> bool:
        return persistent_retry_enabled(
            options=options,
            default=self.persistent_mode,
        )

    def _build_request(
        self,
        messages: Sequence[Message],
        *,
        system_prompt: SystemPrompt,
        tools: ToolDefinitions,
        options: Mapping[str, Any] | None,
        stream: bool = True,
        accept: str = "text/event-stream",
    ) -> Request:
        request_body = _build_openai_chat_request_body(
            messages,
            model=self.model,
            system_prompt=system_prompt,
            tools=tools,
            options=options,
            stream=stream,
            include_reasoning_content=_openai_chat_should_include_reasoning_content(
                self.model,
                self.base_url or get_openai_base_url(),
            ),
        )
        return Request(
            _openai_chat_completions_url(self.base_url or get_openai_base_url()),
            data=json.dumps(request_body).encode("utf-8"),
            headers=_build_openai_headers(api_key=self.api_key, accept=accept),
            method="POST",
        )


def create_model_adapter_from_env(*, model: str) -> ModelAdapter | None:
    provider = get_model_provider()
    if provider in {"openai", "openai-chat", "openai-compatible", "deepseek"}:
        return OpenAIChatStreamingModelAdapter.from_env(model=model)
    if provider == "anthropic":
        return AnthropicStreamingModelAdapter.from_env(model=model)
    if provider not in {"auto", ""}:
        return None
    return (
        AnthropicStreamingModelAdapter.from_env(model=model)
        or OpenAIChatStreamingModelAdapter.from_env(model=model)
    )


def yield_missing_tool_result_blocks(
    assistant_messages: Sequence[AssistantMessage],
    error_message: str,
) -> tuple[UserMessage, ...]:
    return yieldMissingToolResultBlocks(assistant_messages, error_message)


def create_tombstone(message: AssistantMessage) -> TombstoneMessage:
    return TombstoneMessage(message=message)


class _StreamingIdleTimeout(RuntimeError):
    pass


class _QueryInterrupted(RuntimeError):
    def __init__(self, reason: str = "interrupt") -> None:
        super().__init__(reason or "interrupt")
        self.reason = reason or "interrupt"


async def _apply_terminal_transition(
    session: QuerySession,
    terminal: TerminalTransition,
    *,
    tool_executor: Optional[ToolExecutor],
    turn_count: int,
    stop_hook_active: bool,
    allow_continue: bool = False,
) -> _TerminalHookOutcome:
    hook_messages: tuple[Message, ...] = ()
    should_continue = False
    override_terminal: Optional[TerminalTransition] = None

    emit_hook_event = getattr(tool_executor, "emit_hook_event", None)
    if callable(emit_hook_event):
        hook_result = await emit_hook_event(
            "Stop" if terminal.reason == TerminalReason.COMPLETED else "StopFailure",
            _build_terminal_hook_payload(
                session,
                terminal,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            ),
        )
        hook_messages = tuple(getattr(hook_result, "messages", ()) or ())
        behavior = _terminal_hook_behavior(
            getattr(hook_result, "approval_state", None)
        )
        if allow_continue and behavior == "continue":
            should_continue = True
        elif behavior == "stop":
            override_terminal = TerminalTransition(
                reason=(
                    TerminalReason.STOP_HOOK_PREVENTED
                    if terminal.reason == TerminalReason.COMPLETED
                    else TerminalReason.HOOK_STOPPED
                )
            )
        elif _messages_prevent_continuation(hook_messages):
            override_terminal = TerminalTransition(
                reason=(
                    TerminalReason.STOP_HOOK_PREVENTED
                    if terminal.reason == TerminalReason.COMPLETED
                    else TerminalReason.HOOK_STOPPED
                )
            )

    current_session = session
    for message in hook_messages:
        current_session = current_session.appendMessage(message)

    if should_continue:
        return _TerminalHookOutcome(
            session=current_session,
            messages=hook_messages,
            should_continue=True,
        )

    final_terminal = override_terminal or terminal
    return _TerminalHookOutcome(
        session=current_session.finishTurn(final_terminal),
        messages=hook_messages,
        terminal=final_terminal,
    )


def _build_terminal_hook_payload(
    session: QuerySession,
    terminal: TerminalTransition,
    *,
    turn_count: int,
    stop_hook_active: bool,
) -> dict[str, Any]:
    last_assistant = _last_assistant_message(session.messages)
    last_assistant_text = (
        _assistant_text_excerpt(last_assistant).strip()
        if last_assistant is not None
        else ""
    )
    payload: dict[str, Any] = {
        "reason": terminal.reason.value,
        "turnCount": turn_count,
        "stopHookActive": stop_hook_active,
        "sessionState": session.sessionState.value,
        "messageCount": len(session.messages),
        "estimatedTokenCount": _estimate_messages_tokens(session.messages),
        "lastAssistantMessage": last_assistant_text or None,
        "lastAssistantMessageUuid": last_assistant.uuid if last_assistant else None,
    }
    if terminal.error is not None:
        payload["error"] = terminal.error
    if terminal.turnCount is not None:
        payload["terminalTurnCount"] = terminal.turnCount
    return payload


def _last_assistant_message(messages: Sequence[Message]) -> Optional[AssistantMessage]:
    for message in reversed(messages):
        if isinstance(message, AssistantMessage):
            return message
    return None


def _terminal_hook_behavior(
    approval_state: Mapping[str, object] | None,
) -> str | None:
    if not isinstance(approval_state, Mapping):
        return None
    behavior = approval_state.get("behavior")
    if not isinstance(behavior, str):
        return None
    normalized = behavior.strip().lower()
    if normalized in {"continue", "retry"}:
        return "continue"
    if normalized in {"stop", "prevent", "deny"}:
        return "stop"
    return None


def _messages_prevent_continuation(messages: Sequence[Message]) -> bool:
    return any(bool(getattr(message, "preventContinuation", None)) for message in messages)


async def stream_query_session(
    session: QuerySession,
    *,
    model_adapter: ModelAdapter,
    tool_executor: Optional[ToolExecutor] = None,
    config: Optional[QueryStreamConfig] = None,
) -> AsyncGenerator[QueryEvent, None]:
    runtime_config = config or QueryStreamConfig()
    current_session = session
    current_context: Optional[Mapping[str, Any]] = None
    current_model_adapter = model_adapter
    max_tokens_override: Optional[int] = _extract_optional_positive_int(
        runtime_config.options,
        "max_tokens",
    )
    # Default to the kimi-k2.7-code full output budget (16K) when the caller didn't set one, so big
    # tool-call payloads (e.g. writing a whole source file) don't truncate on the first attempt.
    # Without this, the provider's small default output cap (~2K) silently truncates the file.
    if max_tokens_override is None:
        max_tokens_override = 16_000
    max_output_recovery_count = 0
    attempted_reactive_compact = False
    attempted_boundary_collapse = False
    using_fallback_model = False
    stop_hook_active = False
    token_budget_continuation_count = 0
    turn_count = 1
    max_turns = _configured_max_turns(runtime_config)
    remaining_token_budget = _configured_token_budget_total(runtime_config)

    request_start = createStreamRequestStartEvent()
    yield QueryEvent(session=current_session, output=request_start)

    while True:
        blocking_limit = _configured_blocking_limit(runtime_config)
        estimated_tokens = _estimate_messages_tokens(current_session.messages)
        if blocking_limit is not None and estimated_tokens >= blocking_limit:
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=TerminalReason.BLOCKING_LIMIT),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        if _auto_compact_enabled(runtime_config):
            auto_compacted = await _prepare_compaction(
                current_session.messages,
                model_adapter=current_model_adapter,
                tool_executor=tool_executor,
                runtime_config=runtime_config,
                trigger="auto_compact",
                keep_tail_messages=runtime_config.reactive_compact_tail_messages,
                max_context_tokens=runtime_config.max_context_tokens,
                threshold_ratio=runtime_config.auto_compact_threshold_ratio,
            )
            if auto_compacted is not None:
                current_session = QuerySession.fromMessages(
                    auto_compacted.messages,
                    sessionState=current_session.sessionState,
                    externalMetadata=current_session.externalMetadata,
                )
                for message in auto_compacted.emitted_messages:
                    yield QueryEvent(session=current_session, output=message)

        if _signal_is_set(runtime_config.signal):
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=TerminalReason.ABORTED_STREAMING),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        base_session = current_session
        prepared_messages = _prepare_messages_for_model(
            base_session.messages,
            runtime_config=runtime_config,
        )
        effective_options = _effective_options(
            runtime_config.options,
            max_tokens_override=max_tokens_override,
            remaining_token_budget=remaining_token_budget,
            background_request_resolver=runtime_config.background_request_resolver,
        )
        assistant_messages: list[AssistantMessage] = []
        content_blocks: dict[int, dict[str, Any]] = {}
        partial_message: Optional[dict[str, Any]] = None
        tool_use_messages: list[AssistantMessage] = []
        emitted_assistants: list[AssistantMessage] = []
        tool_result_ids: set[str] = set()
        used_fallback = False
        interrupted_tool_reason: Optional[str] = None
        tool_result_messages: list[UserMessage] = []
        fallback_reason = (
            "Synthetic tool result: tool execution unavailable in single-turn REPL runtime"
        )
        incremental_tool_session: _StreamingToolExecutionSession | None = None
        streaming_tools_started = False
        streaming_tools_prelaunched = False
        recoverable_issue: Optional[str] = None

        def _record_tool_update_result(
            update: ToolExecutionUpdate,
        ) -> Message | None:
            nonlocal current_context, current_session

            normalized_update, current_session, message_applied = (
                _apply_tool_update_session(current_session, update)
            )
            current_context = _record_tool_update(
                normalized_update,
                [],
                [],
                current_context,
            )
            tool_result_ids.update(_tool_result_ids_from_updates((normalized_update,)))
            if normalized_update.message is None:
                return None
            if _is_tool_result_message(normalized_update.message):
                tool_result_messages.append(normalized_update.message)
            if not message_applied:
                current_session = current_session.appendMessage(
                    normalized_update.message
                )
            return normalized_update.message

        async def _start_streaming_tool_execution() -> None:
            nonlocal fallback_reason
            nonlocal incremental_tool_session
            nonlocal streaming_tools_started
            nonlocal used_fallback

            if (
                streaming_tools_started
                or tool_executor is None
                or not tool_use_messages
                or used_fallback
            ):
                return
            try:
                _bind_tool_executor_session_messages(
                    tool_executor,
                    current_session.messages,
                )
                incremental_tool_session = await _open_streaming_tool_session(
                    tool_executor,
                    signal=runtime_config.signal,
                )
                if incremental_tool_session is None:
                    return
                await incremental_tool_session.add_tool_use_messages(tool_use_messages)
                streaming_tools_started = True
            except Exception as exc:
                used_fallback = True
                fallback_reason = f"Synthetic tool result: {exc}"
                streaming_tools_started = True
                await _close_streaming_tool_session(
                    incremental_tool_session,
                    discard=True,
                )
                incremental_tool_session = None

        async def _drain_streaming_tool_updates_nowait() -> tuple[Message, ...]:
            nonlocal fallback_reason
            nonlocal incremental_tool_session
            nonlocal used_fallback

            if incremental_tool_session is None:
                return ()
            try:
                updates = _collect_streaming_tool_updates_nowait(
                    incremental_tool_session
                )
            except Exception as exc:
                used_fallback = True
                fallback_reason = f"Synthetic tool result: {exc}"
                await _close_streaming_tool_session(
                    incremental_tool_session,
                    discard=True,
                )
                incremental_tool_session = None
                return ()
            output_messages: list[Message] = []
            for update in updates:
                output_message = _record_tool_update_result(update)
                if output_message is not None:
                    output_messages.append(output_message)
            return tuple(output_messages)

        try:
            async for raw_event, ttft_ms in _iter_stream_events(
                current_model_adapter,
                messages=prepared_messages,
                runtime_config=runtime_config,
                options=effective_options,
            ):
                if isinstance(raw_event, _StreamingIdleWarning):
                    yield QueryEvent(
                        session=current_session,
                        output=_build_streaming_idle_warning_message(raw_event),
                    )
                    continue

                part = dict(raw_event.event) if isinstance(raw_event, StreamEvent) else dict(raw_event)
                if ttft_ms is None and isinstance(raw_event, StreamEvent):
                    ttft_ms = raw_event.ttftMs

                recoverable_issue = _classify_model_issue(part)
                if recoverable_issue is not None and part.get("type") == "error":
                    break

                part_type = part.get("type")
                if part_type == "message_start":
                    raw_message = part.get("message")
                    if not isinstance(raw_message, Mapping):
                        raise QueryStateTransitionError(
                            "message_start requires message payload"
                        )
                    partial_message = dict(raw_message)
                elif part_type == "content_block_start":
                    raw_block = part.get("content_block")
                    if not isinstance(raw_block, Mapping):
                        raise QueryStateTransitionError(
                            "content_block_start requires content_block payload"
                        )
                    content_blocks[int(part["index"])] = _initialize_stream_block(raw_block)
                elif part_type == "content_block_delta":
                    index = int(part["index"])
                    if index not in content_blocks:
                        raise QueryStateTransitionError("Content block not found")
                    _apply_stream_delta(content_blocks, part)
                elif part_type == "content_block_stop":
                    block_index = int(part["index"])
                    if block_index not in content_blocks:
                        raise QueryStateTransitionError("Content block not found")
                    assistant = _build_assistant_message_from_stream(
                        partial_message,
                        content_blocks[block_index],
                    )
                    if assistant is not None:
                        assistant_messages.append(assistant)
                        current_session = current_session.appendMessage(assistant)
                        if _assistant_message_has_visible_output(assistant):
                            emitted_assistants.append(assistant)
                            yield QueryEvent(session=current_session, output=assistant)
                        if any(
                            isinstance(block, ToolUseBlock)
                            for block in assistant.message.content
                        ):
                            tool_use_messages.append(assistant)
                            if (
                                not streaming_tools_prelaunched
                                and not streaming_tools_started
                                and not used_fallback
                                and tool_executor is not None
                            ):
                                try:
                                    _bind_tool_executor_session_messages(
                                        tool_executor,
                                        current_session.messages,
                                    )
                                    incremental_tool_session = await _open_streaming_tool_session(
                                        tool_executor,
                                        signal=runtime_config.signal,
                                    )
                                    if incremental_tool_session is not None:
                                        streaming_tools_prelaunched = True
                                except Exception:
                                    pass
                            if (
                                streaming_tools_prelaunched
                                and incremental_tool_session is not None
                            ):
                                try:
                                    await incremental_tool_session.add_tool_use_messages(
                                        [assistant]
                                    )
                                    streaming_tools_started = True
                                except Exception:
                                    streaming_tools_prelaunched = False
                elif part_type == "message_delta":
                    _apply_message_delta(assistant_messages, part)
                    if partial_message is None:
                        partial_message = {}
                    partial_message = _merge_mapping(partial_message, part.get("delta"))
                    partial_message["usage"] = _merge_mapping(
                        partial_message.get("usage"),
                        part.get("usage"),
                    )
                    if partial_message.get("stop_reason") == "tool_use":
                        if not streaming_tools_prelaunched:
                            await _start_streaming_tool_execution()
                elif part_type == "message_stop":
                    pass
                elif part_type == "error":
                    raise RuntimeError(_extract_model_error_message(part))
                else:
                    raise QueryStateTransitionError(
                        f"Unsupported stream event type: {part_type!r}"
                    )

                yield QueryEvent(
                    session=current_session,
                    output=createStreamEvent(part, ttftMs=ttft_ms),
                )
                for output_message in await _drain_streaming_tool_updates_nowait():
                    yield QueryEvent(
                        session=current_session,
                        output=output_message,
                    )
        except _QueryInterrupted as exc:
            await _close_streaming_tool_session(
                incremental_tool_session,
                discard=True,
            )
            incremental_tool_session = None
            for update in _missing_tool_updates(
                tool_use_messages,
                tool_result_ids=frozenset(),
                reason="Interrupted by user",
            ):
                normalized_update, current_session, message_applied = (
                    _apply_tool_update_session(current_session, update)
                )
                current_context = _record_tool_update(
                    normalized_update,
                    [],
                    [],
                    current_context,
                )
                if normalized_update.message is None:
                    continue
                if not message_applied:
                    current_session = current_session.appendMessage(
                        normalized_update.message
                    )
                yield QueryEvent(
                    session=current_session,
                    output=normalized_update.message,
                )
            if exc.reason != "interrupt":
                interruption = createUserInterruptionMessage(toolUse=False)
                yield QueryEvent(session=current_session, output=interruption)
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=TerminalReason.ABORTED_STREAMING),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return
        except _StreamingIdleTimeout:
            await _close_streaming_tool_session(
                incremental_tool_session,
                discard=True,
            )
            incremental_tool_session = None
            if _adapter_supports_non_streaming(current_model_adapter):
                for assistant in emitted_assistants:
                    yield QueryEvent(session=base_session, output=create_tombstone(assistant))
                current_session = base_session
                fallback_assistant = await _run_non_streaming_fallback(
                    current_model_adapter,
                    messages=_prepare_messages_for_model(
                        current_session.messages,
                        runtime_config=runtime_config,
                    ),
                    runtime_config=runtime_config,
                    options=effective_options,
                )
                current_session = current_session.appendMessage(fallback_assistant)
                yield QueryEvent(session=current_session, output=fallback_assistant)
                cache_warning = _maybe_prompt_cache_break_warning(
                    current_model_adapter,
                    runtime_config=runtime_config,
                    options=effective_options,
                    assistant_messages=(fallback_assistant,),
                    session_messages=current_session.messages,
                )
                if cache_warning is not None:
                    current_session = current_session.appendMessage(cache_warning)
                    yield QueryEvent(session=current_session, output=cache_warning)
                hook_outcome = await _apply_terminal_transition(
                    current_session,
                    TerminalTransition(reason=TerminalReason.COMPLETED),
                    tool_executor=tool_executor,
                    turn_count=turn_count,
                    stop_hook_active=stop_hook_active,
                    allow_continue=True,
                )
                current_session = hook_outcome.session
                for message in hook_outcome.messages:
                    yield QueryEvent(session=current_session, output=message)
                if hook_outcome.should_continue:
                    stop_hook_active = True
                    continue
                yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
                return
            hook_outcome = await _apply_terminal_transition(
                base_session,
                TerminalTransition(
                    reason=TerminalReason.MODEL_ERROR,
                    error="stream_idle_timeout",
                ),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
                allow_continue=True,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            if hook_outcome.should_continue:
                stop_hook_active = True
                continue
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return
        except Exception as exc:
            recoverable_issue = _classify_model_issue(exc)
            if recoverable_issue is not None:
                await _close_streaming_tool_session(
                    incremental_tool_session,
                    discard=True,
                )
                incremental_tool_session = None
            if recoverable_issue is None:
                await _close_streaming_tool_session(
                    incremental_tool_session,
                    discard=True,
                )
                incremental_tool_session = None
                hook_outcome = await _apply_terminal_transition(
                    base_session,
                    TerminalTransition(
                        reason=TerminalReason.MODEL_ERROR,
                        error=str(exc),
                    ),
                    tool_executor=tool_executor,
                    turn_count=turn_count,
                    stop_hook_active=stop_hook_active,
                    allow_continue=True,
                )
                current_session = hook_outcome.session
                for message in hook_outcome.messages:
                    yield QueryEvent(session=current_session, output=message)
                if hook_outcome.should_continue:
                    stop_hook_active = True
                    continue
                yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
                return

        if recoverable_issue is not None:
            await _close_streaming_tool_session(
                incremental_tool_session,
                discard=True,
            )
            incremental_tool_session = None

        if recoverable_issue in {"prompt_too_long", "media_size_error"}:
            for assistant in emitted_assistants:
                yield QueryEvent(session=base_session, output=create_tombstone(assistant))
            current_session = base_session
            if not attempted_boundary_collapse:
                collapsed_messages = _messages_after_last_compact_boundary(
                    current_session.messages
                )
                if collapsed_messages != current_session.messages:
                    current_session = QuerySession.fromMessages(
                        collapsed_messages,
                        sessionState=current_session.sessionState,
                        externalMetadata=current_session.externalMetadata,
                    )
                    attempted_boundary_collapse = True
                    continue
            compacted_messages = await _prepare_compaction(
                current_session.messages,
                model_adapter=current_model_adapter,
                tool_executor=tool_executor,
                runtime_config=runtime_config,
                trigger="reactive_compact",
                keep_tail_messages=runtime_config.reactive_compact_tail_messages,
                max_context_tokens=runtime_config.max_context_tokens,
                threshold_ratio=None,
            )
            if compacted_messages is not None and not attempted_reactive_compact:
                current_session = QuerySession.fromMessages(
                    compacted_messages.messages,
                    sessionState=current_session.sessionState,
                    externalMetadata=current_session.externalMetadata,
                )
                attempted_reactive_compact = True
                for message in compacted_messages.emitted_messages:
                    yield QueryEvent(session=current_session, output=message)
                continue

            terminal_reason = (
                TerminalReason.PROMPT_TOO_LONG
                if recoverable_issue == "prompt_too_long"
                else TerminalReason.IMAGE_ERROR
            )
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=terminal_reason),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        if recoverable_issue == "fallback_triggered":
            if runtime_config.fallback_model_adapter is not None and not using_fallback_model:
                for assistant in emitted_assistants:
                    yield QueryEvent(session=base_session, output=create_tombstone(assistant))
                current_session = base_session
                current_model_adapter = runtime_config.fallback_model_adapter
                using_fallback_model = True
                continue
            hook_outcome = await _apply_terminal_transition(
                base_session,
                TerminalTransition(
                    reason=TerminalReason.MODEL_ERROR,
                    error="fallback_triggered",
                ),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
                allow_continue=True,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            if hook_outcome.should_continue:
                stop_hook_active = True
                continue
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        if recoverable_issue == "max_output_tokens" and not assistant_messages:
            if max_output_recovery_count >= runtime_config.max_output_tokens_recovery_limit:
                hook_outcome = await _apply_terminal_transition(
                    base_session,
                    TerminalTransition(
                        reason=TerminalReason.MODEL_ERROR,
                        error="max_output_tokens",
                    ),
                    tool_executor=tool_executor,
                    turn_count=turn_count,
                    stop_hook_active=stop_hook_active,
                    allow_continue=True,
                )
                current_session = hook_outcome.session
                for message in hook_outcome.messages:
                    yield QueryEvent(session=current_session, output=message)
                if hook_outcome.should_continue:
                    stop_hook_active = True
                    continue
                yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
                return
            max_output_recovery_count += 1
            max_tokens_override = max(
                max_tokens_override or 0,
                runtime_config.max_output_tokens_escalation,
            )
            continue

        if assistant_messages:
            current_session, post_sampling_messages = await _apply_post_sampling_hooks(
                current_session,
                assistant_messages=assistant_messages,
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            for message in post_sampling_messages:
                yield QueryEvent(session=current_session, output=message)
            remaining_token_budget = _consume_token_budget(
                remaining_token_budget,
                assistant_messages,
            )
            cache_warning = _maybe_prompt_cache_break_warning(
                current_model_adapter,
                runtime_config=runtime_config,
                options=effective_options,
                assistant_messages=assistant_messages,
                session_messages=current_session.messages,
            )
            if cache_warning is not None:
                current_session = current_session.appendMessage(cache_warning)
                yield QueryEvent(session=current_session, output=cache_warning)

        if _should_continue_after_max_output_tokens(assistant_messages):
            if max_output_recovery_count >= runtime_config.max_output_tokens_recovery_limit:
                hook_outcome = await _apply_terminal_transition(
                    current_session,
                    TerminalTransition(
                        reason=TerminalReason.MODEL_ERROR,
                        error="max_output_tokens",
                    ),
                    tool_executor=tool_executor,
                    turn_count=turn_count,
                    stop_hook_active=stop_hook_active,
                    allow_continue=True,
                )
                current_session = hook_outcome.session
                for message in hook_outcome.messages:
                    yield QueryEvent(session=current_session, output=message)
                if hook_outcome.should_continue:
                    stop_hook_active = True
                    continue
                yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
                return
            max_output_recovery_count += 1
            max_tokens_override = max(
                max_tokens_override or 0,
                runtime_config.max_output_tokens_escalation,
            )
            if max_turns is not None and turn_count + 1 > max_turns:
                attachment = createAttachmentMessage(
                    {
                        "type": "max_turns_reached",
                        "maxTurns": max_turns,
                        "turnCount": turn_count + 1,
                    }
                )
                current_session = current_session.appendMessage(attachment)
                yield QueryEvent(session=current_session, output=attachment)
                hook_outcome = await _apply_terminal_transition(
                    current_session,
                    TerminalTransition(
                        reason=TerminalReason.MAX_TURNS,
                        turnCount=turn_count + 1,
                    ),
                    tool_executor=tool_executor,
                    turn_count=turn_count,
                    stop_hook_active=stop_hook_active,
                )
                current_session = hook_outcome.session
                for message in hook_outcome.messages:
                    yield QueryEvent(session=current_session, output=message)
                yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
                return
            turn_count += 1
            resume_message = createUserMessage(
                content=(
                    TextBlock("Please continue exactly where you left off."),
                ),
                isMeta=True,
                origin="max_output_tokens_recovery",
            )
            current_session = current_session.appendMessage(resume_message)
            yield QueryEvent(session=current_session, output=resume_message)
            continue

        max_output_recovery_count = 0
        attempted_boundary_collapse = False
        attempted_reactive_compact = False

        if not tool_use_messages:
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=TerminalReason.COMPLETED),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
                allow_continue=True,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            if hook_outcome.should_continue:
                stop_hook_active = True
                continue
            if (
                hook_outcome.terminal is not None
                and hook_outcome.terminal.reason == TerminalReason.COMPLETED
                and _should_continue_for_token_budget(
                    remaining_token_budget,
                    runtime_config=runtime_config,
                    continuation_count=token_budget_continuation_count,
                    last_output_tokens=_assistant_output_tokens(assistant_messages),
                )
            ):
                if max_turns is not None and turn_count + 1 > max_turns:
                    attachment = createAttachmentMessage(
                        {
                            "type": "max_turns_reached",
                            "maxTurns": max_turns,
                            "turnCount": turn_count + 1,
                        }
                    )
                    current_session = current_session.startTurn().appendMessage(attachment)
                    yield QueryEvent(session=current_session, output=attachment)
                    hook_outcome = await _apply_terminal_transition(
                        current_session,
                        TerminalTransition(
                            reason=TerminalReason.MAX_TURNS,
                            turnCount=turn_count + 1,
                        ),
                        tool_executor=tool_executor,
                        turn_count=turn_count,
                        stop_hook_active=True,
                    )
                    current_session = hook_outcome.session
                    for message in hook_outcome.messages:
                        yield QueryEvent(session=current_session, output=message)
                    yield QueryEvent(
                        session=current_session,
                        terminal=hook_outcome.terminal,
                    )
                    return
                turn_count += 1
                token_budget_continuation_count += 1
                stop_hook_active = True
                current_session = hook_outcome.session.startTurn()
                budget_message = createUserMessage(
                    content=(
                        TextBlock(
                            runtime_config.token_budget_continue_message.format(
                                remaining_tokens=remaining_token_budget
                            )
                        ),
                    ),
                    isMeta=True,
                    origin="token_budget_continuation",
                )
                current_session = current_session.appendMessage(budget_message)
                yield QueryEvent(session=current_session, output=budget_message)
                continue
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        tool_updates_iter: AsyncGenerator[ToolExecutionUpdate, None] | None = None
        try:
            if _signal_is_set(runtime_config.signal):
                interrupted_tool_reason = _signal_reason(runtime_config.signal)
            elif tool_executor is None:
                used_fallback = True
            elif incremental_tool_session is not None:
                await incremental_tool_session.finish()
                while True:
                    update = await _await_with_interrupt(
                        incremental_tool_session.next_update(),
                        runtime_config.signal,
                        timeout=runtime_config.tool_executor_timeout_seconds,
                        prefer_signal=True,
                    )
                    if update is None:
                        break
                    output_message = _record_tool_update_result(update)
                    if output_message is None:
                        continue
                    yield QueryEvent(
                        session=current_session,
                        output=output_message,
                    )
            elif not streaming_tools_started:
                _bind_tool_executor_session_messages(
                    tool_executor,
                    current_session.messages,
                )
                tool_updates_iter = _run_tool_executor(
                    tool_executor,
                    tool_use_messages,
                    signal=runtime_config.signal,
                )
                while True:
                    try:
                        update = await _await_with_interrupt(
                            tool_updates_iter.__anext__(),
                            runtime_config.signal,
                            timeout=runtime_config.tool_executor_timeout_seconds,
                            prefer_signal=True,
                        )
                    except StopAsyncIteration:
                        break
                    output_message = _record_tool_update_result(update)
                    if output_message is None:
                        continue
                    yield QueryEvent(
                        session=current_session,
                        output=output_message,
                    )
            else:
                used_fallback = True
        except _QueryInterrupted as exc:
            interrupted_tool_reason = exc.reason
        except asyncio.TimeoutError:
            used_fallback = True
            fallback_reason = "Synthetic tool result: tool executor timed out"
        except Exception as exc:
            used_fallback = True
            fallback_reason = f"Synthetic tool result: {exc}"
        finally:
            if tool_updates_iter is not None:
                await _close_tool_updates(tool_updates_iter)
            await _close_streaming_tool_session(
                incremental_tool_session,
                discard=used_fallback or interrupted_tool_reason is not None,
            )
            incremental_tool_session = None

        if interrupted_tool_reason is not None:
            for update in _missing_tool_updates(
                tool_use_messages,
                tool_result_ids=tool_result_ids,
                reason="Interrupted by user",
            ):
                normalized_update, current_session, message_applied = (
                    _apply_tool_update_session(current_session, update)
                )
                current_context = _record_tool_update(
                    normalized_update,
                    [],
                    [],
                    current_context,
                )
                if normalized_update.message is None:
                    continue
                if _is_tool_result_message(normalized_update.message):
                    tool_result_messages.append(normalized_update.message)
                if not message_applied:
                    current_session = current_session.appendMessage(
                        normalized_update.message
                    )
                yield QueryEvent(
                    session=current_session,
                    output=normalized_update.message,
                )
            if interrupted_tool_reason != "interrupt":
                interruption = createUserInterruptionMessage(toolUse=True)
                yield QueryEvent(session=current_session, output=interruption)
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(reason=TerminalReason.ABORTED_TOOLS),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return

        for update in _missing_tool_updates(
            tool_use_messages,
            tool_result_ids=tool_result_ids,
            reason=fallback_reason,
        ):
            used_fallback = True
            normalized_update, current_session, message_applied = _apply_tool_update_session(
                current_session,
                update,
            )
            current_context = _record_tool_update(
                normalized_update,
                [],
                [],
                current_context,
            )
            if normalized_update.message is None:
                continue
            if _is_tool_result_message(normalized_update.message):
                tool_result_messages.append(normalized_update.message)
            if not message_applied:
                current_session = current_session.appendMessage(normalized_update.message)
            yield QueryEvent(session=current_session, output=normalized_update.message)

        for attachment in _build_tool_attachment_messages(
            tool_use_messages,
            tool_result_messages=tool_result_messages,
        ):
            current_session = current_session.appendMessage(attachment)
            yield QueryEvent(session=current_session, output=attachment)

        tool_use_summary = await _build_tool_use_summary_message(
            tool_use_messages,
            tool_result_messages=tool_result_messages,
            model_adapter=current_model_adapter,
            runtime_config=runtime_config,
        )
        if tool_use_summary is not None:
            current_session = current_session.appendMessage(tool_use_summary)
            yield QueryEvent(session=current_session, output=tool_use_summary)

        if max_turns is not None and turn_count + 1 > max_turns:
            attachment = createAttachmentMessage(
                {
                    "type": "max_turns_reached",
                    "maxTurns": max_turns,
                    "turnCount": turn_count + 1,
                }
            )
            current_session = current_session.appendMessage(attachment)
            yield QueryEvent(session=current_session, output=attachment)
            hook_outcome = await _apply_terminal_transition(
                current_session,
                TerminalTransition(
                    reason=TerminalReason.MAX_TURNS,
                    turnCount=turn_count + 1,
                ),
                tool_executor=tool_executor,
                turn_count=turn_count,
                stop_hook_active=stop_hook_active,
            )
            current_session = hook_outcome.session
            for message in hook_outcome.messages:
                yield QueryEvent(session=current_session, output=message)
            yield QueryEvent(session=current_session, terminal=hook_outcome.terminal)
            return
        turn_count += 1


async def _iter_stream_events(
    model_adapter: ModelAdapter,
    *,
    messages: Sequence[Message],
    runtime_config: QueryStreamConfig,
    options: Optional[Mapping[str, Any]],
) -> AsyncGenerator[
    tuple[Union[Mapping[str, Any], StreamEvent, _StreamingIdleWarning], Optional[int]],
    None,
]:
    stream = model_adapter.call_model(
        messages,
        system_prompt=runtime_config.system_prompt,
        tools=runtime_config.tools,
        signal=runtime_config.signal,
        options=options,
    )
    loop = asyncio.get_running_loop()
    last_event_at: Optional[float] = None
    stall_count = 0
    total_stall_time = 0.0

    while True:
        operation_task = asyncio.create_task(stream.__anext__())
        signal_task: asyncio.Task[None] | None = None
        if runtime_config.signal is not None:
            signal_task = asyncio.create_task(_wait_for_signal(runtime_config.signal))
        timeout = runtime_config.streaming_idle_timeout_seconds
        warning_seconds = (
            timeout / 2.0
            if timeout is not None and timeout > 0.0
            else None
        )
        warning_emitted = False
        wait_started_at = loop.time()
        try:
            while True:
                if runtime_config.signal is not None and _signal_is_set(runtime_config.signal):
                    raise _QueryInterrupted(_signal_reason(runtime_config.signal))

                now = loop.time()
                warning_deadline = (
                    wait_started_at + warning_seconds
                    if warning_seconds is not None and not warning_emitted
                    else None
                )
                timeout_deadline = (
                    wait_started_at + timeout if timeout is not None else None
                )
                next_deadline = min(
                    (
                        deadline
                        for deadline in (warning_deadline, timeout_deadline)
                        if deadline is not None
                    ),
                    default=None,
                )
                wait_timeout = (
                    max(0.0, next_deadline - now) if next_deadline is not None else None
                )
                wait_tasks: tuple[asyncio.Task[Any], ...] = (
                    (operation_task, signal_task)
                    if signal_task is not None
                    else (operation_task,)
                )
                done, _ = await asyncio.wait(
                    wait_tasks,
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation_task in done:
                    raw_event = operation_task.result()
                    break
                if signal_task is not None and signal_task in done:
                    raise _QueryInterrupted(_signal_reason(runtime_config.signal))
                now = loop.time()
                if (
                    warning_deadline is not None
                    and not warning_emitted
                    and now >= warning_deadline
                ):
                    warning_emitted = True
                    warning = _StreamingIdleWarning(
                        idle_seconds=warning_seconds,
                        timeout_seconds=timeout,
                    )
                    if runtime_config.on_streaming_idle_warning is not None:
                        callback_result = runtime_config.on_streaming_idle_warning(
                            warning.idle_seconds,
                            warning.timeout_seconds,
                        )
                        if inspect.isawaitable(callback_result):
                            await callback_result
                    yield warning, None
                    continue
                if timeout_deadline is not None and now >= timeout_deadline:
                    raise asyncio.TimeoutError
        except StopAsyncIteration:
            return
        except _QueryInterrupted:
            await _cancel_task(operation_task)
            if signal_task is not None:
                await _cancel_task(signal_task)
            await _aclose_async_iter(stream)
            raise
        except asyncio.TimeoutError as exc:
            await _cancel_task(operation_task)
            if signal_task is not None:
                await _cancel_task(signal_task)
            await _aclose_async_iter(stream)
            raise _StreamingIdleTimeout from exc
        finally:
            if signal_task is not None:
                await _cancel_task(signal_task)

        now = loop.time()
        if (
            last_event_at is not None
            and runtime_config.streaming_stall_threshold_seconds is not None
        ):
            gap = now - last_event_at
            if gap > runtime_config.streaming_stall_threshold_seconds:
                stall_count += 1
                total_stall_time += gap
                if runtime_config.on_streaming_stall is not None:
                    callback_result = runtime_config.on_streaming_stall(
                        gap,
                        stall_count,
                        total_stall_time,
                    )
                    if inspect.isawaitable(callback_result):
                        await callback_result
        last_event_at = now

        ttft_ms = raw_event.ttftMs if isinstance(raw_event, StreamEvent) else None
        yield raw_event, ttft_ms


def _build_streaming_idle_warning_message(
    warning: _StreamingIdleWarning,
) -> Message:
    return createSystemMessage(
        "Streaming response is taking longer than expected. "
        f"No chunks have arrived for {warning.idle_seconds:g}s; "
        f"falling back if the stream stays idle for {warning.timeout_seconds:g}s.",
        "warning",
    )


def _run_tool_executor(
    tool_executor: ToolExecutor,
    tool_use_messages: Sequence[AssistantMessage],
    *,
    signal: Optional[asyncio.Event],
) -> AsyncGenerator[ToolExecutionUpdate, None]:
    run = tool_executor.run
    try:
        signature = inspect.signature(run)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "signal" in signature.parameters:
        return run(tool_use_messages, signal=signal)
    return run(tool_use_messages)


async def _open_streaming_tool_session(
    tool_executor: ToolExecutor | None,
    *,
    signal: Optional[asyncio.Event],
) -> _StreamingToolExecutionSession | None:
    if tool_executor is None:
        return None
    open_session = getattr(tool_executor, "open_streaming_session", None)
    if not callable(open_session):
        return None
    try:
        signature = inspect.signature(open_session)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "signal" in signature.parameters:
        session = open_session(signal=signal)
    else:
        session = open_session()
    if inspect.isawaitable(session):
        session = await session
    return session


def _bind_tool_executor_session_messages(
    tool_executor: ToolExecutor | None,
    messages: Sequence[Message],
) -> None:
    if tool_executor is None:
        return
    bind_session_messages = getattr(tool_executor, "bind_session_messages", None)
    if callable(bind_session_messages):
        bind_session_messages(messages)


async def _close_streaming_tool_session(
    session: _StreamingToolExecutionSession | None,
    *,
    discard: bool,
) -> None:
    if session is None:
        return
    if discard:
        discard_fn = getattr(session, "discard", None)
        if callable(discard_fn):
            discard_fn()
    close_fn = getattr(session, "close", None)
    if callable(close_fn):
        result = close_fn()
        if inspect.isawaitable(result):
            await result


def _collect_streaming_tool_updates_nowait(
    session: _StreamingToolExecutionSession | None,
) -> tuple[ToolExecutionUpdate, ...]:
    if session is None:
        return ()
    drain = getattr(session, "get_completed_updates_nowait", None)
    if not callable(drain):
        return ()
    updates = drain()
    return tuple(updates)


async def _await_existing_task(task: asyncio.Task[Any]) -> Any:
    return await task


def _signal_is_set(signal: Any) -> bool:
    if signal is None:
        return False
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        try:
            return bool(is_set())
        except TypeError:
            pass
    aborted = getattr(signal, "aborted", None)
    return bool(aborted)


def _signal_reason(signal: Any) -> str:
    if signal is None:
        return "interrupt"
    for key in ("reason", "abort_reason", "abortReason"):
        value = getattr(signal, key, None)
        if isinstance(value, str) and value:
            return value
    return "interrupt"


async def _wait_for_signal(signal: Any) -> None:
    if signal is None:
        return
    if _signal_is_set(signal):
        return
    wait = getattr(signal, "wait", None)
    if callable(wait):
        result = wait()
        if inspect.isawaitable(result):
            await result
        return
    while not _signal_is_set(signal):
        await asyncio.sleep(0.01)


async def _await_with_interrupt(
    awaitable: Any,
    signal: Any,
    *,
    timeout: float | None,
    prefer_signal: bool = False,
) -> Any:
    if signal is None:
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)
    if _signal_is_set(signal):
        raise _QueryInterrupted(_signal_reason(signal))

    operation_task = asyncio.create_task(awaitable)
    signal_task = asyncio.create_task(_wait_for_signal(signal))
    try:
        done, pending = await asyncio.wait(
            (operation_task, signal_task),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _cancel_task(operation_task)
            await _cancel_task(signal_task)
            raise asyncio.TimeoutError
        if prefer_signal and _signal_is_set(signal):
            await _cancel_task(operation_task)
            await _cancel_task(signal_task)
            raise _QueryInterrupted(_signal_reason(signal))
        if operation_task in done:
            await _cancel_task(signal_task)
            return operation_task.result()
        await _cancel_task(operation_task)
        raise _QueryInterrupted(_signal_reason(signal))
    finally:
        for task in pending:
            await _cancel_task(task)


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        return


async def _aclose_async_iter(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except RuntimeError:
            return


def _configured_max_turns(runtime_config: QueryStreamConfig) -> Optional[int]:
    if runtime_config.max_turns is not None and runtime_config.max_turns > 0:
        return runtime_config.max_turns
    return _extract_optional_positive_int(runtime_config.options, "max_turns")


def _configured_blocking_limit(runtime_config: QueryStreamConfig) -> Optional[int]:
    if (
        runtime_config.blocking_limit_tokens is not None
        and runtime_config.blocking_limit_tokens > 0
    ):
        return runtime_config.blocking_limit_tokens
    return _extract_optional_positive_int(runtime_config.options, "blocking_limit_tokens")


def _configured_token_budget_total(runtime_config: QueryStreamConfig) -> Optional[int]:
    if (
        runtime_config.token_budget_total_tokens is not None
        and runtime_config.token_budget_total_tokens > 0
    ):
        return runtime_config.token_budget_total_tokens
    return _extract_optional_positive_int(runtime_config.options, "token_budget_total_tokens")


def _configured_tool_result_budget(runtime_config: QueryStreamConfig) -> Optional[int]:
    if (
        runtime_config.tool_result_budget_tokens is not None
        and runtime_config.tool_result_budget_tokens > 0
    ):
        return runtime_config.tool_result_budget_tokens
    configured = _extract_optional_positive_int(
        runtime_config.options,
        "tool_result_budget_tokens",
    )
    if configured is not None:
        return configured
    return None


def _consume_token_budget(
    remaining_budget: Optional[int],
    assistant_messages: Sequence[AssistantMessage],
) -> Optional[int]:
    if remaining_budget is None:
        return None
    return max(0, remaining_budget - _assistant_output_tokens(assistant_messages))


def _assistant_output_tokens(assistant_messages: Sequence[AssistantMessage]) -> int:
    total = 0
    for message in assistant_messages:
        usage = getattr(message.message, "usage", None)
        if isinstance(usage, Mapping):
            output_tokens = usage.get("output_tokens")
            if isinstance(output_tokens, int) and output_tokens > 0:
                total += output_tokens
    return total


def _assistant_usage_peak_int(
    assistant_messages: Sequence[AssistantMessage],
    key: str,
) -> int:
    peak = 0
    for message in assistant_messages:
        usage = getattr(message.message, "usage", None)
        if not isinstance(usage, Mapping):
            continue
        value = usage.get(key)
        if isinstance(value, int) and value > peak:
            peak = value
    return peak


def _stable_prompt_cache_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def _prompt_cache_tracking_model_name(model_adapter: ModelAdapter) -> str:
    raw_model = getattr(model_adapter, "model", None)
    if isinstance(raw_model, str) and raw_model.strip():
        return raw_model.strip()
    return type(model_adapter).__name__


def _build_prompt_cache_detection_components(
    model_adapter: ModelAdapter,
    *,
    runtime_config: QueryStreamConfig,
    options: Optional[Mapping[str, Any]],
) -> dict[str, object]:
    model = _prompt_cache_tracking_model_name(model_adapter)
    tools = tuple(runtime_config.tools or ())
    return {
        "model": model,
        "system": _build_anthropic_system_payload(
            runtime_config.system_prompt,
            tools=tools,
            options=options,
        ),
        "tools": tools,
        "beta_headers": _resolve_anthropic_beta_headers(model, options),
        "extra_body": _extract_extra_body(options),
        "task_budget": _extract_task_budget(options),
        "effort": None if options is None else options.get("effort"),
        "prompt_cache_ttl": _extract_prompt_cache_ttl(options),
        "use_global_cache_scope": _extract_use_global_cache_scope(options),
    }


def _prompt_cache_component_digests(
    components: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, _stable_prompt_cache_digest(payload))
        for name, payload in components.items()
    )


def _prompt_cache_component_payloads(
    components: Mapping[str, object],
) -> tuple[tuple[str, object], ...]:
    return tuple((name, payload) for name, payload in components.items())


def _truncate_prompt_cache_label(
    value: str,
    *,
    limit: int = 64,
) -> str:
    trimmed = value.strip()
    if len(trimmed) <= limit:
        return trimmed
    return f"{trimmed[: limit - 3]}..."


def _prompt_cache_value_preview(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return repr(_truncate_prompt_cache_label(value))
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        preview_items = [
            _prompt_cache_value_preview(item) for item in tuple(value)[:3]
        ]
        suffix = ""
        if len(tuple(value)) > 3:
            suffix = ", ..."
        return "[" + ", ".join(preview_items) + suffix + "]"
    if isinstance(value, Mapping):
        keys = [str(key) for key in tuple(value.keys())[:4]]
        suffix = ""
        if len(value) > 4:
            suffix = ", ..."
        return "{" + ", ".join(keys) + suffix + "}"
    return repr(value)


def _prompt_cache_tools_by_name(
    payload: object,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes, bytearray)):
        return {}
    normalized: dict[str, Mapping[str, object]] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        normalized[raw_name.strip()] = entry
    return normalized


def _prompt_cache_mapping_key_diff(
    previous_payload: object,
    current_payload: object,
) -> str | None:
    if not isinstance(previous_payload, Mapping) or not isinstance(current_payload, Mapping):
        return None
    previous_keys = {str(key) for key in previous_payload.keys()}
    current_keys = {str(key) for key in current_payload.keys()}
    added = sorted(current_keys - previous_keys)
    removed = sorted(previous_keys - current_keys)
    updated = sorted(
        key
        for key in (previous_keys & current_keys)
        if previous_payload.get(key) != current_payload.get(key)
    )
    details: list[str] = []
    if added:
        details.append(
            "added " + ", ".join(_truncate_prompt_cache_label(key) for key in added[:3])
        )
    if removed:
        details.append(
            "removed "
            + ", ".join(_truncate_prompt_cache_label(key) for key in removed[:3])
        )
    if updated:
        details.append(
            "updated "
            + ", ".join(_truncate_prompt_cache_label(key) for key in updated[:3])
        )
    if not details:
        return None
    return "; ".join(details)


def _prompt_cache_sequence_diff(
    previous_payload: object,
    current_payload: object,
) -> str | None:
    if not isinstance(previous_payload, Sequence) or isinstance(
        previous_payload,
        (str, bytes, bytearray),
    ):
        return None
    if not isinstance(current_payload, Sequence) or isinstance(
        current_payload,
        (str, bytes, bytearray),
    ):
        return None
    previous_items = tuple(
        str(item).strip()
        for item in previous_payload
        if isinstance(item, str) and item.strip()
    )
    current_items = tuple(
        str(item).strip()
        for item in current_payload
        if isinstance(item, str) and item.strip()
    )
    previous_set = set(previous_items)
    current_set = set(current_items)
    added = sorted(current_set - previous_set)
    removed = sorted(previous_set - current_set)
    if not added and not removed and previous_items != current_items:
        return "order changed"
    details: list[str] = []
    if added:
        details.append(
            "+"
            + ", +".join(_truncate_prompt_cache_label(item) for item in added[:3])
        )
    if removed:
        details.append(
            "-"
            + ", -".join(_truncate_prompt_cache_label(item) for item in removed[:3])
        )
    if not details:
        return None
    return ", ".join(details)


def _prompt_cache_tool_schema_diff(
    previous_payload: object,
    current_payload: object,
) -> str | None:
    previous_tools = _prompt_cache_tools_by_name(previous_payload)
    current_tools = _prompt_cache_tools_by_name(current_payload)
    if not previous_tools and not current_tools:
        return None
    previous_names = set(previous_tools)
    current_names = set(current_tools)
    added = sorted(current_names - previous_names)
    removed = sorted(previous_names - current_names)
    changed = sorted(
        name
        for name in (previous_names & current_names)
        if _stable_prompt_cache_digest(previous_tools[name])
        != _stable_prompt_cache_digest(current_tools[name])
    )
    details: list[str] = []
    if added:
        details.append(
            "added "
            + ", ".join(_truncate_prompt_cache_label(name) for name in added[:3])
        )
    if removed:
        details.append(
            "removed "
            + ", ".join(_truncate_prompt_cache_label(name) for name in removed[:3])
        )
    if changed:
        details.append(
            "changed "
            + ", ".join(_truncate_prompt_cache_label(name) for name in changed[:3])
        )
    if not details:
        return None
    return "; ".join(details)


def _prompt_cache_system_diff(
    previous_payload: object,
    current_payload: object,
) -> str | None:
    if isinstance(previous_payload, str) and isinstance(current_payload, str):
        if previous_payload == current_payload:
            return None
        return "text changed"
    if isinstance(previous_payload, Sequence) and not isinstance(
        previous_payload,
        (str, bytes, bytearray),
    ) and isinstance(current_payload, Sequence) and not isinstance(
        current_payload,
        (str, bytes, bytearray),
    ):
        if tuple(previous_payload) == tuple(current_payload):
            return None
        if len(tuple(previous_payload)) != len(tuple(current_payload)):
            return (
                f"{len(tuple(previous_payload))} -> {len(tuple(current_payload))} blocks"
            )
        return "block contents changed"
    return None


def _prompt_cache_payload_detail(
    component_name: str,
    previous_payload: object,
    current_payload: object,
) -> str | None:
    if component_name == "model":
        return (
            f"{_prompt_cache_value_preview(previous_payload)}"
            f" -> {_prompt_cache_value_preview(current_payload)}"
        )
    if component_name == "system":
        return _prompt_cache_system_diff(previous_payload, current_payload)
    if component_name == "tools":
        return _prompt_cache_tool_schema_diff(previous_payload, current_payload)
    if component_name == "beta_headers":
        return _prompt_cache_sequence_diff(previous_payload, current_payload)
    if component_name == "extra_body":
        return _prompt_cache_mapping_key_diff(previous_payload, current_payload)
    return (
        f"{_prompt_cache_value_preview(previous_payload)}"
        f" -> {_prompt_cache_value_preview(current_payload)}"
    )


def _digestable_prompt_cache_payload(value: object) -> object:
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in _INLINE_BINARY_KEYS:
                item_length = len(item) if isinstance(item, (bytes, bytearray)) else len(
                    str(item)
                )
                normalized[normalized_key] = {"__inline_binary__": item_length}
                continue
            normalized[normalized_key] = _digestable_prompt_cache_payload(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_digestable_prompt_cache_payload(item) for item in value)
    if isinstance(value, str):
        if len(value) <= _CONTEXT_COLLAPSE_CHAR_LIMIT:
            return value
        return {
            "__truncated_text__": {
                "length": len(value),
                "head": value[:_CONTEXT_COLLAPSE_HEAD_CHARS],
                "tail": value[-_CONTEXT_COLLAPSE_TAIL_CHARS:],
            }
        }
    return value


def _extract_diff_paths_from_text(text: str) -> tuple[str, ...]:
    if "diff --git " not in text and "\n+++ " not in text:
        return ()
    paths: list[str] = []
    seen: set[str] = set()
    patterns = (
        re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE),
        re.compile(r"^\+\+\+ b/(.+?)$", re.MULTILINE),
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            for group in match.groups():
                candidate = group.strip()
                if not candidate or candidate == "/dev/null" or candidate in seen:
                    continue
                seen.add(candidate)
                paths.append(candidate)
    return tuple(paths)


def _iter_prompt_cache_payload_paths(payload: object) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()

    def _collect(value: object) -> None:
        if isinstance(value, Mapping):
            for key in ("file_path", "filePath", "notebook_path", "notebookPath", "filename"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    normalized = candidate.strip()
                    if (
                        normalized
                        and normalized != "/dev/null"
                        and normalized not in seen
                    ):
                        seen.add(normalized)
                        paths.append(normalized)
            for nested_value in value.values():
                _collect(nested_value)
            return
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            for item in value:
                _collect(item)
            return
        if isinstance(value, str):
            for candidate in _extract_diff_paths_from_text(value):
                if candidate in seen:
                    continue
                seen.add(candidate)
                paths.append(candidate)

    _collect(payload)
    return tuple(paths)


def _prompt_cache_transcript_file_markers(
    prepared_messages: Sequence[Message],
) -> tuple[tuple[str, str], ...]:
    markers: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in prepared_messages:
        if not isinstance(message, UserMessage):
            continue
        content = message.message.content
        if isinstance(content, str):
            continue
        for block in content:
            if not isinstance(block, ToolResultBlock):
                continue
            payload = _digestable_prompt_cache_payload(block.content)
            payload_digest = _stable_prompt_cache_digest(payload)
            for path in _iter_prompt_cache_payload_paths(block.content):
                marker = (path, payload_digest)
                if marker in seen:
                    continue
                seen.add(marker)
                markers.append(marker)
    return tuple(markers)


def _prompt_cache_file_marker_diff(
    previous_markers: Sequence[tuple[str, str]],
    current_markers: Sequence[tuple[str, str]],
) -> str | None:
    previous = dict(previous_markers)
    current = dict(current_markers)
    previous_paths = set(previous)
    current_paths = set(current)
    added = sorted(current_paths - previous_paths)
    removed = sorted(previous_paths - current_paths)
    changed = sorted(
        path
        for path in (previous_paths & current_paths)
        if previous[path] != current[path]
    )
    details: list[str] = []
    if added:
        details.append(
            "+"
            + ", +".join(_truncate_prompt_cache_label(path) for path in added[:3])
        )
    if removed:
        details.append(
            "-"
            + ", -".join(_truncate_prompt_cache_label(path) for path in removed[:3])
        )
    if changed:
        details.append(
            "~"
            + ", ~".join(_truncate_prompt_cache_label(path) for path in changed[:3])
        )
    if not details:
        return None
    return "transcript file refs (" + ", ".join(details) + ")"


def _prompt_cache_break_contributors(
    previous_state: _PromptCacheDetectionState,
    current_state: _PromptCacheDetectionState,
) -> tuple[str, ...]:
    contributor_labels = (
        ("model", "model selection"),
        ("system", "system prompt blocks"),
        ("tools", "tool schemas"),
        ("beta_headers", "beta headers"),
        ("extra_body", "extra request body"),
        ("task_budget", "task budget"),
        ("effort", "effort settings"),
        ("prompt_cache_ttl", "prompt cache TTL"),
        ("use_global_cache_scope", "prompt cache scope"),
    )
    previous_digests = dict(previous_state.component_digests)
    current_digests = dict(current_state.component_digests)
    previous_payloads = dict(previous_state.component_payloads)
    current_payloads = dict(current_state.component_payloads)
    contributors: list[str] = []
    for component_name, label in contributor_labels:
        if previous_digests.get(component_name) != current_digests.get(component_name):
            detail = _prompt_cache_payload_detail(
                component_name,
                previous_payloads.get(component_name),
                current_payloads.get(component_name),
            )
            if detail:
                contributors.append(f"{label} ({detail})")
                continue
            contributors.append(label)
    file_marker_detail = _prompt_cache_file_marker_diff(
        previous_state.transcript_file_markers,
        current_state.transcript_file_markers,
    )
    if file_marker_detail is not None:
        contributors.append(file_marker_detail)
    return tuple(contributors)


def _build_prompt_cache_detection_state(
    model_adapter: ModelAdapter,
    *,
    runtime_config: QueryStreamConfig,
    options: Optional[Mapping[str, Any]],
    assistant_messages: Sequence[AssistantMessage],
    session_messages: Optional[Sequence[Message]] = None,
) -> _PromptCacheDetectionState:
    model = _prompt_cache_tracking_model_name(model_adapter)
    components = _build_prompt_cache_detection_components(
        model_adapter,
        runtime_config=runtime_config,
        options=options,
    )
    prepared_messages = ()
    if session_messages is not None:
        prepared_messages = _prepare_messages_for_model(
            session_messages,
            runtime_config=runtime_config,
        )
    return _PromptCacheDetectionState(
        cache_key=_stable_prompt_cache_digest(components),
        model=model,
        cache_read_input_tokens=_assistant_usage_peak_int(
            assistant_messages,
            "cache_read_input_tokens",
        ),
        used_cache_breakpoints=_extract_cache_breakpoints(options) is not None,
        component_digests=_prompt_cache_component_digests(components),
        component_payloads=_prompt_cache_component_payloads(components),
        transcript_file_markers=_prompt_cache_transcript_file_markers(prepared_messages),
    )


def _maybe_prompt_cache_break_warning(
    model_adapter: ModelAdapter,
    *,
    runtime_config: QueryStreamConfig,
    options: Optional[Mapping[str, Any]],
    assistant_messages: Sequence[AssistantMessage],
    session_messages: Optional[Sequence[Message]] = None,
) -> Optional[Message]:
    if not assistant_messages:
        return None

    current_state = _build_prompt_cache_detection_state(
        model_adapter,
        runtime_config=runtime_config,
        options=options,
        assistant_messages=assistant_messages,
        session_messages=session_messages,
    )
    previous_state = getattr(model_adapter, "_prompt_cache_detection_state", None)
    setattr(model_adapter, "_prompt_cache_detection_state", current_state)

    if not isinstance(previous_state, _PromptCacheDetectionState):
        return None
    if previous_state.used_cache_breakpoints or current_state.used_cache_breakpoints:
        return None

    previous_read = previous_state.cache_read_input_tokens
    current_read = current_state.cache_read_input_tokens
    if previous_read <= 0 or current_read >= previous_read:
        return None

    token_drop = previous_read - current_read
    if token_drop < _PROMPT_CACHE_BREAK_MIN_DROP_TOKENS:
        return None
    if (token_drop / previous_read) < _PROMPT_CACHE_BREAK_MIN_DROP_RATIO:
        return None

    contributors = _prompt_cache_break_contributors(previous_state, current_state)
    if contributors:
        contributor_details = ", ".join(contributors)
        suffix = (
            "Detected cache-affecting changes in: "
            f"{contributor_details}. Possible prompt cache break detected."
        )
    else:
        suffix = "Possible prompt cache break detected."
        if (
            previous_state.cache_key == current_state.cache_key
            and previous_state.model == current_state.model
        ):
            suffix = (
                "without a cacheable prompt change. "
                "Possible prompt cache break detected."
            )

    return createSystemMessage(
        (
            "Prompt cache read tokens dropped from "
            f"{previous_read:,} to {current_read:,} {suffix}"
        ),
        "warning",
    )


def _should_continue_for_token_budget(
    remaining_budget: Optional[int],
    *,
    runtime_config: QueryStreamConfig,
    continuation_count: int = 0,
    last_output_tokens: Optional[int] = None,
) -> bool:
    if remaining_budget is None:
        return False
    if remaining_budget <= max(
        0,
        runtime_config.token_budget_continue_threshold_tokens,
    ):
        return False
    if (
        continuation_count
        >= runtime_config.token_budget_diminishing_returns_after_continuations
        and isinstance(last_output_tokens, int)
        and last_output_tokens > 0
        and last_output_tokens
        < runtime_config.token_budget_diminishing_returns_min_output_tokens
    ):
        return False
    return True


def _auto_compact_enabled(runtime_config: QueryStreamConfig) -> bool:
    if os.environ.get("CLAUDE_CODE_DISABLE_AUTOCOMPACT") in ("1", "true", "True"):
        return False
    if runtime_config.auto_compact_enabled is not None:
        return runtime_config.auto_compact_enabled
    return bool(get_global_config().get("autoCompactEnabled", True))


def _prepare_messages_for_model(
    messages: Sequence[Message],
    *,
    runtime_config: QueryStreamConfig,
) -> tuple[Message, ...]:
    prepared = tuple(
        message
        for message in messages
        if not isinstance(message, (ToolUseSummaryMessage, AttachmentMessage))
    )
    prepared = _strip_images_from_messages(prepared)
    tool_result_budget = _configured_tool_result_budget(runtime_config)
    if tool_result_budget is None:
        return prepared
    return _apply_tool_result_budget(prepared, tool_result_budget)


def _strip_images_from_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
    rewritten: list[Message] = []
    changed = False
    for message in messages:
        updated = _strip_images_from_message(message)
        rewritten.append(updated)
        changed = changed or updated is not message
    if not changed:
        return tuple(messages)
    return tuple(rewritten)


def _strip_images_from_message(message: Message) -> Message:
    if not isinstance(message, UserMessage):
        return message
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return message

    raw_values = _tool_result_raw_values(message)
    if raw_values is None:
        return message

    payload_hint = (
        dict(message.toolUsePayload)
        if isinstance(message.toolUsePayload, Mapping)
        else None
    )
    rewritten_blocks: list[ToolResultBlock] = []
    rewritten_values: list[Any] = []
    strip_metadata: list[dict[str, Any]] = []
    changed = False

    for block, raw_value in zip(content, raw_values):
        sanitized_value, metadata_entries = _sanitize_tool_result_value_for_model_context(
            raw_value,
            payload_hint=payload_hint,
        )
        sanitized_text = _stringify_tool_result_content(sanitized_value)
        original_text = _stringify_tool_result_content(block.content)
        if sanitized_text != original_text:
            changed = True
        rewritten_blocks.append(
            ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=sanitized_text,
                is_error=block.is_error,
            )
        )
        rewritten_values.append(sanitized_value)
        for entry in metadata_entries:
            strip_metadata.append(
                {
                    **dict(entry),
                    "toolUseId": block.tool_use_id,
                }
            )

    sanitized_payload = payload_hint
    payload_entries: list[Mapping[str, Any]] = []
    if payload_hint is not None:
        sanitized_payload, payload_entries = _sanitize_inline_binary_payload(payload_hint)
        if payload_entries:
            changed = True
            for entry in payload_entries:
                strip_metadata.append(dict(entry))

    if not changed:
        return message

    mcp_meta = dict(message.mcpMeta or {})
    mcp_meta["inlineBinaryStripped"] = tuple(strip_metadata)
    tool_use_result: Any = message.toolUseResult
    if message.toolUseResult is not None:
        tool_use_result = (
            rewritten_values[0]
            if len(rewritten_values) == 1
            else tuple(rewritten_values)
        )

    return createUserMessage(
        content=tuple(rewritten_blocks),
        isMeta=message.isMeta,
        isVisibleInTranscriptOnly=message.isVisibleInTranscriptOnly,
        isVirtual=message.isVirtual,
        isCompactSummary=message.isCompactSummary,
        summarizeMetadata=message.summarizeMetadata,
        toolUseResult=tool_use_result,
        toolUsePayload=sanitized_payload,
        mcpMeta=mcp_meta,
        uuid=message.uuid,
        timestamp=message.timestamp,
        imagePasteIds=message.imagePasteIds,
        sourceToolAssistantUUID=message.sourceToolAssistantUUID,
        permissionMode=message.permissionMode,
        origin=message.origin,
    )


def _sanitize_tool_result_value_for_model_context(
    value: Any,
    *,
    payload_hint: Mapping[str, Any] | None,
) -> tuple[Any, tuple[Mapping[str, Any], ...]]:
    hint_payload: Mapping[str, Any] | None = payload_hint
    if isinstance(value, Mapping):
        sanitized, entries = _sanitize_inline_binary_payload(value)
        return sanitized, entries
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized, entries = _sanitize_inline_binary_payload(value)
        return sanitized, entries
    if not isinstance(value, str):
        return value, ()

    stripped = value.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            sanitized, entries = _sanitize_inline_binary_payload(parsed)
            if entries:
                return _stringify_tool_result_content(sanitized), entries

    if hint_payload is None:
        return value, ()

    sanitized_hint, hint_entries = _sanitize_inline_binary_payload(hint_payload)
    if hint_entries:
        return _stringify_tool_result_content(sanitized_hint), hint_entries
    return value, ()


def _sanitize_inline_binary_payload(
    value: Any,
) -> tuple[Any, tuple[Mapping[str, Any], ...]]:
    if isinstance(value, Mapping):
        rewritten: dict[str, Any] = {}
        metadata: list[Mapping[str, Any]] = []
        changed = False
        descriptor = _inline_binary_descriptor(value)
        for key, item in value.items():
            if key in _INLINE_BINARY_KEYS and isinstance(item, str) and item:
                changed = True
                rewritten[key] = _inline_binary_placeholder(
                    descriptor=descriptor,
                    char_count=len(item),
                )
                metadata.append(
                    {
                        "kind": descriptor,
                        "key": key,
                        "removedChars": len(item),
                    }
                )
                continue
            updated_item, child_metadata = _sanitize_inline_binary_payload(item)
            if child_metadata:
                changed = True
                metadata.extend(child_metadata)
            rewritten[key] = updated_item
        if metadata and "inline_binary_stripped" not in rewritten:
            rewritten["inline_binary_stripped"] = True
        if not changed:
            return value, ()
        return rewritten, tuple(metadata)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rewritten_items: list[Any] = []
        metadata: list[Mapping[str, Any]] = []
        changed = False
        for item in value:
            updated_item, child_metadata = _sanitize_inline_binary_payload(item)
            rewritten_items.append(updated_item)
            if child_metadata:
                changed = True
                metadata.extend(child_metadata)
        if not changed:
            return value, ()
        return rewritten_items, tuple(metadata)

    return value, ()


def _inline_binary_descriptor(payload: Mapping[str, Any]) -> str:
    media_type = payload.get("media_type", payload.get("mediaType"))
    if isinstance(media_type, str) and media_type.strip():
        return media_type.strip()
    result_type = payload.get("result_type", payload.get("resultType"))
    if isinstance(result_type, str) and result_type.strip():
        return result_type.strip()
    return "binary"


def _inline_binary_placeholder(*, descriptor: str, char_count: int) -> str:
    return (
        f"[{descriptor} payload omitted from model context: "
        f"base64_chars={max(char_count, 0)}]"
    )


def _tool_result_raw_values(message: Message) -> tuple[Any, ...] | None:
    if not isinstance(message, UserMessage):
        return None
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return None
    if len(content) == 1 and message.toolUseResult is not None:
        return (message.toolUseResult,)
    if (
        len(content) > 1
        and isinstance(message.toolUseResult, Sequence)
        and not isinstance(message.toolUseResult, (str, bytes, bytearray))
        and len(message.toolUseResult) == len(content)
    ):
        return tuple(message.toolUseResult)
    return tuple(block.content for block in content)


def _apply_tool_result_budget(
    messages: Sequence[Message],
    budget_tokens: int,
) -> tuple[Message, ...]:
    if budget_tokens <= 0:
        return tuple(messages)

    prepared = list(messages)
    changed = False
    remaining_budget = budget_tokens
    for index in range(len(prepared) - 1, -1, -1):
        message = prepared[index]
        raw_tool_results = _tool_result_raw_texts(message)
        if raw_tool_results is None:
            continue
        replacement, consumed_tokens = _rewrite_tool_result_message_for_budget(
            message,
            raw_tool_results=raw_tool_results,
            budget_tokens=max(remaining_budget, 0),
        )
        remaining_budget = max(0, remaining_budget - consumed_tokens)
        if replacement is not message:
            prepared[index] = replacement
            changed = True
    if not changed:
        return tuple(messages)
    return tuple(prepared)


def _tool_result_raw_texts(message: Message) -> tuple[str, ...] | None:
    raw_values = _tool_result_raw_values(message)
    if raw_values is None:
        return None
    return tuple(_stringify_tool_result_content(item) for item in raw_values)


def _compute_kept_tokens_for_block(
    raw_text: str,
    budget_tokens: int,
    label: str,
) -> tuple[str, int, int]:
    raw_tokens = _count_text_tokens(raw_text)
    if budget_tokens > 0 and raw_tokens <= budget_tokens:
        return raw_text, raw_tokens, raw_tokens
    rewritten_text = _truncate_text_to_token_budget(
        raw_text,
        budget_tokens=budget_tokens,
        label=label,
    )
    kept_tokens = min(_count_text_tokens(rewritten_text), max(budget_tokens, 0))
    return rewritten_text, raw_tokens, kept_tokens


def _rewrite_tool_result_message_for_budget(
    message: UserMessage,
    *,
    raw_tool_results: Sequence[str],
    budget_tokens: int,
) -> tuple[UserMessage, int]:
    content = message.message.content
    assert isinstance(content, tuple)
    consumed_tokens = 0
    changed = False
    rewritten_blocks: list[ToolResultBlock] = []
    budget_metadata: list[dict[str, Any]] = []

    for block, raw_text in zip(content, raw_tool_results):
        assert isinstance(block, ToolResultBlock)
        rewritten_text, raw_tokens, kept_tokens = _compute_kept_tokens_for_block(
            raw_text,
            budget_tokens,
            f"tool result {block.tool_use_id}",
        )
        budget_tokens = max(0, budget_tokens - kept_tokens)
        consumed_tokens += kept_tokens
        if rewritten_text != _stringify_tool_result_content(block.content):
            changed = True
        rewritten_blocks.append(
            ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=rewritten_text,
                is_error=block.is_error,
            )
        )
        budget_metadata.append(
            {
                "toolUseId": block.tool_use_id,
                "originalTokens": raw_tokens,
                "keptTokens": kept_tokens,
            }
        )

    if not changed:
        return message, consumed_tokens

    mcp_meta = dict(message.mcpMeta or {})
    mcp_meta[_TOOL_RESULT_BUDGET_MARKER] = tuple(budget_metadata)
    return (
        createUserMessage(
            content=tuple(rewritten_blocks),
            isMeta=message.isMeta,
            isVisibleInTranscriptOnly=message.isVisibleInTranscriptOnly,
            isVirtual=message.isVirtual,
            isCompactSummary=message.isCompactSummary,
            summarizeMetadata=message.summarizeMetadata,
            toolUseResult=message.toolUseResult,
            toolUsePayload=message.toolUsePayload,
            mcpMeta=mcp_meta,
            uuid=message.uuid,
            timestamp=message.timestamp,
            imagePasteIds=message.imagePasteIds,
            sourceToolAssistantUUID=message.sourceToolAssistantUUID,
            permissionMode=message.permissionMode,
            origin=message.origin,
        ),
        consumed_tokens,
    )


def _truncate_text_to_token_budget(
    text: str,
    *,
    budget_tokens: int,
    label: str,
) -> str:
    normalized = text.strip()
    marker = f"[{label} truncated by {_TOOL_RESULT_BUDGET_MARKER}]"
    marker_tokens = _count_text_tokens(marker)
    if budget_tokens <= marker_tokens + 4:
        return marker
    if _count_text_tokens(normalized) <= budget_tokens:
        return normalized

    head_chars = min(max(64, budget_tokens * 2), len(normalized))
    tail_chars = min(max(24, budget_tokens), max(len(normalized) - head_chars, 0))
    candidate = _format_budgeted_text(marker, normalized, head_chars, tail_chars)

    while _count_text_tokens(candidate) > budget_tokens and (head_chars > 24 or tail_chars > 12):
        if head_chars >= tail_chars and head_chars > 24:
            head_chars = max(24, int(head_chars * 0.8))
        elif tail_chars > 12:
            tail_chars = max(12, int(tail_chars * 0.75))
        candidate = _format_budgeted_text(marker, normalized, head_chars, tail_chars)

    if _count_text_tokens(candidate) <= budget_tokens:
        return candidate

    clipped_words: list[str] = []
    for word in normalized.split():
        candidate_with_word = marker
        joined = " ".join([*clipped_words, word]).strip()
        if joined:
            candidate_with_word = f"{marker}\n{joined}"
        if _count_text_tokens(candidate_with_word) > budget_tokens:
            break
        clipped_words.append(word)
    if clipped_words:
        return f"{marker}\n{' '.join(clipped_words)}"
    return marker


def _format_budgeted_text(
    marker: str,
    text: str,
    head_chars: int,
    tail_chars: int,
) -> str:
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip() if tail_chars > 0 else ""
    if tail and tail != head:
        return f"{marker}\n{head}\n...\n{tail}"
    return f"{marker}\n{head}"


def _effective_options(
    options: Optional[Mapping[str, Any]],
    *,
    max_tokens_override: Optional[int],
    remaining_token_budget: Optional[int],
    background_request_resolver: Optional[Callable[[], bool]] = None,
) -> Optional[Mapping[str, Any]]:
    merged = dict(options or {})
    if max_tokens_override is not None:
        merged["max_tokens"] = max_tokens_override
    if "task_budget" not in merged:
        total_budget = _extract_optional_positive_int(merged, "token_budget_total_tokens")
        if total_budget is not None:
            task_budget: dict[str, Any] = {"type": "tokens", "total": total_budget}
            if type(remaining_token_budget) is int and remaining_token_budget >= 0:
                task_budget["remaining"] = remaining_token_budget
            merged["task_budget"] = task_budget
    merged = _merge_background_request_option(
        merged,
        background_request_resolver=background_request_resolver,
    )
    return merged or None


def _merge_background_request_option(
    options: Mapping[str, Any] | None,
    *,
    background_request_resolver: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    merged = dict(options or {})
    should_mark_background = False
    if callable(background_request_resolver):
        try:
            should_mark_background = bool(background_request_resolver())
        except Exception:
            should_mark_background = False
    if should_mark_background:
        merged["background_request"] = True
    else:
        merged.pop("background_request", None)
    return merged


def _is_background_request(options: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(options, Mapping):
        return False
    return options.get("background_request") is True


def _extract_optional_positive_int(
    options: Optional[Mapping[str, Any]],
    key: str,
) -> Optional[int]:
    if options is None:
        return None
    raw = options.get(key)
    if type(raw) is int and raw > 0:
        return raw
    return None


async def _apply_post_sampling_hooks(
    session: QuerySession,
    *,
    assistant_messages: Sequence[AssistantMessage],
    tool_executor: Optional[ToolExecutor],
    turn_count: int,
    stop_hook_active: bool,
) -> tuple[QuerySession, tuple[Message, ...]]:
    emit_hook_event = getattr(tool_executor, "emit_hook_event", None)
    if not callable(emit_hook_event):
        return session, ()

    hook_result = await emit_hook_event(
        "PostSampling",
        _build_post_sampling_hook_payload(
            session,
            assistant_messages=assistant_messages,
            turn_count=turn_count,
            stop_hook_active=stop_hook_active,
        ),
    )
    hook_messages = tuple(getattr(hook_result, "messages", ()) or ())
    current_session = session
    for message in hook_messages:
        current_session = current_session.appendMessage(message)
    return current_session, hook_messages


def _build_post_sampling_hook_payload(
    session: QuerySession,
    *,
    assistant_messages: Sequence[AssistantMessage],
    turn_count: int,
    stop_hook_active: bool,
) -> dict[str, Any]:
    last_assistant = assistant_messages[-1] if assistant_messages else _last_assistant_message(
        session.messages
    )
    last_assistant_text = (
        _assistant_text_excerpt(last_assistant).strip()
        if last_assistant is not None
        else ""
    )
    tool_use_ids = tuple(
        block.id
        for message in assistant_messages
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    )
    return {
        "turnCount": turn_count,
        "stopHookActive": stop_hook_active,
        "sessionState": session.sessionState.value,
        "messageCount": len(session.messages),
        "estimatedTokenCount": _estimate_messages_tokens(session.messages),
        "assistantMessageCount": len(assistant_messages),
        "assistantOutputTokens": _assistant_output_tokens(assistant_messages),
        "hasToolUse": bool(tool_use_ids),
        "toolUseIds": tool_use_ids,
        "lastAssistantMessage": last_assistant_text,
        "lastAssistantMessageUuid": getattr(last_assistant, "uuid", None),
    }


def _should_continue_after_max_output_tokens(
    assistant_messages: Sequence[AssistantMessage],
) -> bool:
    if not assistant_messages:
        return False
    # Accept BOTH the Anthropic-native name and the OpenAI-compat mapped name. The OpenAI adapter
    # maps finish_reason="length" -> stop_reason="max_tokens" (see _openai_finish_reason_to_stop_reason),
    # so without "max_tokens" here the max-output-tokens recovery never fires for OpenAI-compatible
    # providers (kimi/qwen/step) — they truncate forever instead of escalating + continuing.
    return assistant_messages[-1].message.stop_reason in ("max_output_tokens", "max_tokens")


def _classify_model_issue(raw: object) -> Optional[str]:
    payload_text = ""
    if isinstance(raw, Mapping):
        payload_text = json.dumps(raw, ensure_ascii=False, sort_keys=True).lower()
    elif isinstance(raw, BaseException):
        payload_text = str(raw).lower()
    elif raw is not None:
        payload_text = str(raw).lower()

    if not payload_text:
        return None
    if "prompt_too_long" in payload_text or "prompt too long" in payload_text:
        return "prompt_too_long"
    if "media_size_error" in payload_text or "image too large" in payload_text:
        return "media_size_error"
    if "max_output_tokens" in payload_text or "output token" in payload_text:
        return "max_output_tokens"
    if "fallbacktriggerederror" in payload_text or "fallback triggered" in payload_text:
        return "fallback_triggered"
    return None


def _extract_model_error_message(part: Mapping[str, Any]) -> str:
    error_payload = part.get("error")
    if isinstance(error_payload, Mapping):
        message = error_payload.get("message")
        if isinstance(message, str) and message:
            return message
        if error_payload:
            return json.dumps(error_payload, ensure_ascii=False, sort_keys=True)
    message = part.get("message")
    if isinstance(message, str) and message:
        return message
    return json.dumps(dict(part), ensure_ascii=False, sort_keys=True)


def _adapter_supports_non_streaming(model_adapter: ModelAdapter) -> bool:
    return callable(getattr(model_adapter, "call_model_nonstream", None)) or callable(
        getattr(model_adapter, "call_model_once", None)
    )


async def _run_non_streaming_fallback(
    model_adapter: ModelAdapter,
    *,
    messages: Sequence[Message],
    runtime_config: QueryStreamConfig,
    options: Optional[Mapping[str, Any]],
) -> AssistantMessage:
    call = getattr(model_adapter, "call_model_nonstream", None)
    if callable(call):
        pending = call(
            messages,
            system_prompt=runtime_config.system_prompt,
            tools=runtime_config.tools,
            signal=runtime_config.signal,
            options=options,
        )
        if inspect.isawaitable(pending):
            result = await asyncio.wait_for(
                pending,
                timeout=runtime_config.non_streaming_fallback_timeout_seconds,
            )
        else:
            result = pending
    else:
        single_call = getattr(model_adapter, "call_model_once", None)
        if not callable(single_call):
            raise RuntimeError("model adapter does not support non-streaming fallback")
        pending = single_call(
            messages,
            system_prompt=runtime_config.system_prompt,
            tools=runtime_config.tools,
            signal=runtime_config.signal,
            options=options,
        )
        if inspect.isawaitable(pending):
            result = await asyncio.wait_for(
                pending,
                timeout=runtime_config.non_streaming_fallback_timeout_seconds,
            )
        else:
            result = pending

    if isinstance(result, AssistantMessage):
        return result
    if isinstance(result, str):
        return createAssistantMessage(content=result)
    if isinstance(result, Mapping):
        content = result.get("content")
        if isinstance(content, str):
            return createAssistantMessage(content=content)
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            return createAssistantMessage(content=tuple(content))
    raise RuntimeError("non-streaming fallback returned an unsupported payload")


async def _prepare_compaction(
    messages: Sequence[Message],
    *,
    model_adapter: ModelAdapter,
    tool_executor: Optional[ToolExecutor],
    runtime_config: QueryStreamConfig,
    trigger: str,
    keep_tail_messages: int,
    max_context_tokens: int,
    threshold_ratio: Optional[float],
) -> Optional[_PreparedCompaction]:
    global _compact_in_progress
    if _compact_in_progress:
        return None
    _compact_in_progress = True
    try:
        clear_compact_warning_suppression()

        pre_messages: tuple[Message, ...] = ()
        post_messages: tuple[Message, ...] = ()
        compacted: tuple[Message, ...] | None = None
        emit_hook_event = getattr(tool_executor, "emit_hook_event", None)

        # Phase 1: PreCompact hook fires BEFORE compaction
        if callable(emit_hook_event):
            # Do initial compaction pass to get preview for PreCompact payload
            initial_compacted = await _compact_session_messages(
                messages,
                model_adapter=model_adapter,
                runtime_config=runtime_config,
                trigger=trigger,
                keep_tail_messages=keep_tail_messages,
                max_context_tokens=max_context_tokens,
                threshold_ratio=threshold_ratio,
            )
            if initial_compacted is not None:
                hook_payload = _build_compaction_hook_payload(
                    messages,
                    initial_compacted,
                    trigger=trigger,
                    keep_tail_messages=keep_tail_messages,
                    max_context_tokens=max_context_tokens,
                    threshold_ratio=threshold_ratio,
                )
                pre_result = await emit_hook_event("PreCompact", hook_payload)
                pre_messages = tuple(getattr(pre_result, "messages", ()) or ())
                compaction_instructions = _extract_compaction_hook_instructions(pre_result)
                if compaction_instructions:
                    # Re-compact with instructions from PreCompact
                    compacted = await _compact_session_messages(
                        messages,
                        model_adapter=model_adapter,
                        runtime_config=runtime_config,
                        trigger=trigger,
                        keep_tail_messages=keep_tail_messages,
                        max_context_tokens=max_context_tokens,
                        threshold_ratio=threshold_ratio,
                        compaction_instructions=compaction_instructions,
                    )
                else:
                    compacted = initial_compacted

        # Phase 2: Main compaction (if not already done via PreCompact instructions)
        if compacted is None:
            compacted = await _compact_session_messages(
                messages,
                model_adapter=model_adapter,
                runtime_config=runtime_config,
                trigger=trigger,
                keep_tail_messages=keep_tail_messages,
                max_context_tokens=max_context_tokens,
                threshold_ratio=threshold_ratio,
            )
        if compacted is None:
            return None

        # Phase 3: PostCompact hook fires AFTER compaction
        if callable(emit_hook_event):
            hook_payload = _build_compaction_hook_payload(
                messages,
                compacted,
                trigger=trigger,
                keep_tail_messages=keep_tail_messages,
                max_context_tokens=max_context_tokens,
                threshold_ratio=threshold_ratio,
            )
            post_result = await emit_hook_event("PostCompact", hook_payload)
            post_messages = tuple(getattr(post_result, "messages", ()) or ())

        final_messages = _inject_compaction_hook_messages(
            compacted,
            pre_messages=pre_messages,
            post_messages=post_messages,
        )
        run_post_compact_cleanup(
            "repl_main_thread",
            tool_executor=tool_executor,
            model_adapter=model_adapter,
        )
        return _PreparedCompaction(
            messages=final_messages,
            emitted_messages=(
                *pre_messages,
                *_compaction_emitted_messages(compacted),
                *post_messages,
            ),
        )
    finally:
        _compact_in_progress = False


def _compaction_emitted_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
    emitted: list[Message] = []
    for message in messages:
        if isinstance(message, CompactBoundaryMessage):
            emitted.append(message)
            continue
        if isinstance(message, UserMessage) and message.isCompactSummary:
            emitted.append(message)
    return tuple(emitted)


def _inject_compaction_hook_messages(
    messages: Sequence[Message],
    *,
    pre_messages: Sequence[Message],
    post_messages: Sequence[Message],
) -> tuple[Message, ...]:
    injected: list[Message] = []
    inserted_pre_messages = False
    for message in messages:
        if not inserted_pre_messages and isinstance(message, CompactBoundaryMessage):
            injected.extend(pre_messages)
            inserted_pre_messages = True
        injected.append(message)
    if not inserted_pre_messages and pre_messages:
        injected = [*pre_messages, *injected]
    injected.extend(post_messages)
    return tuple(injected)


def _build_compaction_hook_payload(
    original_messages: Sequence[Message],
    compacted_messages: Sequence[Message],
    *,
    trigger: str,
    keep_tail_messages: int,
    max_context_tokens: int,
    threshold_ratio: Optional[float],
) -> dict[str, Any]:
    boundary = next(
        (
            message
            for message in compacted_messages
            if isinstance(message, CompactBoundaryMessage)
        ),
        None,
    )
    summary = next(
        (
            message
            for message in compacted_messages
            if isinstance(message, UserMessage) and message.isCompactSummary
        ),
        None,
    )
    summary_content = ""
    summary_metadata: Mapping[str, Any] | None = None
    if isinstance(summary, UserMessage):
        summary_content = _render_message_for_estimation(summary)
        if isinstance(summary.summarizeMetadata, Mapping):
            summary_metadata = dict(summary.summarizeMetadata)

    payload: dict[str, Any] = {
        "trigger": trigger,
        "keepTailMessages": keep_tail_messages,
        "maxContextTokens": max_context_tokens,
        "thresholdRatio": threshold_ratio,
        "messageCount": len(original_messages),
        "estimatedTokenCount": _estimate_messages_tokens(original_messages),
        "compactedMessageCount": len(compacted_messages),
        "compactedEstimatedTokenCount": _estimate_messages_tokens(compacted_messages),
        "summary": summary_content,
    }
    if summary_metadata is not None:
        payload["summaryMetadata"] = dict(summary_metadata)
    if boundary is not None:
        payload.update(
            {
                "originalTokenCount": boundary.originalTokenCount,
                "newTokenCount": boundary.newTokenCount,
                "compact_type": boundary.compact_type,
                "deletedToolUseIds": list(boundary.deletedToolUseIds),
                "preservedMessageUuids": list(boundary.preservedMessageUuids),
            }
        )
    return payload


async def _compact_session_messages(
    messages: Sequence[Message],
    *,
    model_adapter: ModelAdapter | None,
    runtime_config: QueryStreamConfig,
    trigger: str,
    keep_tail_messages: int,
    max_context_tokens: int,
    threshold_ratio: Optional[float],
    compaction_instructions: str | None = None,
) -> Optional[tuple[Message, ...]]:
    if threshold_ratio is not None:
        estimated_tokens = _estimate_messages_tokens(messages)
        if estimated_tokens < int(max_context_tokens * threshold_ratio):
            return None

    if keep_tail_messages < 1:
        keep_tail_messages = 1

    non_system_indices = [
        index for index, message in enumerate(messages) if not _is_system_like_message(message)
    ]
    if len(non_system_indices) <= keep_tail_messages:
        return None

    tail_start = _compaction_tail_start_index(
        messages,
        keep_tail_messages=keep_tail_messages,
    )
    removed_non_system = tuple(
        message
        for message in messages[:tail_start]
        if not _is_system_like_message(message)
    )
    if not removed_non_system:
        return None

    preserved_tail = tuple(messages[tail_start:])
    compaction_attachments = build_compaction_attachment_messages(
        removed_non_system,
        preserved_messages=preserved_tail,
    )
    preserved_message_uuids = tuple(
        message.uuid
        for message in preserved_tail
        if isinstance(message, (AssistantMessage, UserMessage, CompactBoundaryMessage))
    )
    deleted_tool_use_ids = tuple(
        block.id
        for message in removed_non_system
        if isinstance(message, AssistantMessage)
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    )
    original_token_count = _estimate_messages_tokens(messages)
    summary_message = await build_compaction_summary_message(
        removed_non_system,
        preserved_message_uuids=preserved_message_uuids,
        model_adapter=model_adapter,
        runtime_config=runtime_config,
        origin="snip",
        compaction_instructions=compaction_instructions,
    )
    system_prefix = tuple(
        message
        for message in messages[:tail_start]
        if _is_system_like_message(message)
    )
    provisional = (
        *system_prefix,
        createCompactBoundaryMessage(
            trigger=trigger,
            originalTokenCount=original_token_count,
            newTokenCount=0,
            deletedToolUseIds=deleted_tool_use_ids,
            preservedMessageUuids=preserved_message_uuids,
        ),
        summary_message,
        *preserved_tail,
        *compaction_attachments,
    )
    new_token_count = _estimate_messages_tokens(provisional)
    return (
        *system_prefix,
        createCompactBoundaryMessage(
            trigger=trigger,
            originalTokenCount=original_token_count,
            newTokenCount=new_token_count,
            deletedToolUseIds=deleted_tool_use_ids,
            preservedMessageUuids=preserved_message_uuids,
        ),
        summary_message,
        *preserved_tail,
        *compaction_attachments,
    )


async def build_compaction_summary_message(
    messages: Sequence[Message],
    *,
    preserved_message_uuids: Sequence[str],
    model_adapter: ModelAdapter | None,
    runtime_config: QueryStreamConfig,
    origin: str,
    compaction_instructions: str | None = None,
) -> UserMessage:
    sanitized_messages = _strip_images_from_messages(messages)
    heuristic_payload = _build_heuristic_compaction_payload(sanitized_messages)
    payload = dict(heuristic_payload)
    payload["summarySource"] = "heuristic"
    compaction_count = _count_prior_compactions(messages) + 1

    if _supports_model_compaction_summary(model_adapter):
        model_payload = await _build_model_compaction_payload(
            sanitized_messages,
            heuristic_payload=heuristic_payload,
            model_adapter=model_adapter,
            runtime_config=runtime_config,
            compaction_instructions=compaction_instructions,
        )
        if model_payload is not None:
            payload = {
                **heuristic_payload,
                **model_payload,
                "summarySource": "model",
            }

    metadata = {
        "summary": payload["summary"],
        "summarySource": payload["summarySource"],
        "snippedMessages": len(messages),
        "preservedMessageUuids": tuple(preserved_message_uuids),
        "messageBuckets": dict(payload.get("messageBuckets") or {}),
        "userRequests": tuple(payload.get("userRequests") or ()),
        "decisions": tuple(payload.get("decisions") or ()),
        "toolActivity": tuple(payload.get("toolActivity") or ()),
        "files": tuple(payload.get("files") or ()),
        "openQuestions": tuple(payload.get("openQuestions") or ()),
        "compactionCount": compaction_count,
    }
    return createUserMessage(
        content=_render_compaction_summary_text(payload),
        isMeta=True,
        isCompactSummary=True,
        summarizeMetadata=metadata,
        origin=origin,
    )


def _supports_model_compaction_summary(model_adapter: ModelAdapter | None) -> bool:
    if model_adapter is None:
        return False
    if callable(getattr(model_adapter, "call_compaction_summary", None)):
        return True
    if bool(getattr(model_adapter, "supports_compaction_summary", False)):
        return True
    return isinstance(model_adapter, AnthropicStreamingModelAdapter)


async def _build_model_compaction_payload(
    messages: Sequence[Message],
    *,
    heuristic_payload: Mapping[str, Any],
    model_adapter: ModelAdapter | None,
    runtime_config: QueryStreamConfig,
    compaction_instructions: str | None = None,
) -> dict[str, Any] | None:
    if model_adapter is None:
        return None

    custom_call = getattr(model_adapter, "call_compaction_summary", None)
    if callable(custom_call):
        call_kwargs: dict[str, Any] = {
            "heuristic_payload": dict(heuristic_payload),
        }
        normalized_instructions = _normalize_compaction_instruction_text(
            compaction_instructions
        )
        if normalized_instructions:
            if _callable_accepts_keyword(custom_call, "custom_instructions"):
                call_kwargs["custom_instructions"] = normalized_instructions
            elif _callable_accepts_keyword(custom_call, "instructions"):
                call_kwargs["instructions"] = normalized_instructions
        result = custom_call(tuple(messages), **call_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return _normalize_compaction_payload(result)

    transcript = _build_compaction_transcript(messages)
    if not transcript:
        return None

    prompt = _build_compaction_prompt(
        transcript=transcript,
        heuristic_payload=heuristic_payload,
        custom_instructions=compaction_instructions,
    )
    try:
        text = await asyncio.wait_for(
            _collect_stream_text(
                model_adapter.call_model(
                    (
                        createUserMessage(content=prompt, isMeta=True, origin="compact_prompt"),
                    ),
                    system_prompt=_COMPACTION_PROMPT_SYSTEM,
                    tools=None,
                    signal=runtime_config.signal,
                    options=_merge_background_request_option(
                        {"max_tokens": _COMPACTION_OUTPUT_TOKEN_LIMIT},
                        background_request_resolver=runtime_config.background_request_resolver,
                    ),
                )
            ),
            timeout=_COMPACTION_MODEL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    return _normalize_compaction_payload(text)


async def _collect_stream_text(
    stream: AsyncGenerator[Union[Mapping[str, Any], StreamEvent], None],
) -> str:
    fragments: list[str] = []
    async for raw_event in stream:
        payload = raw_event.event if isinstance(raw_event, StreamEvent) else raw_event
        if not isinstance(payload, Mapping):
            continue
        part_type = payload.get("type")
        if part_type == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, Mapping) and block.get("type") == "text":
                initial = block.get("text")
                if isinstance(initial, str) and initial:
                    fragments.append(initial)
        elif part_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, Mapping) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    fragments.append(text)
        elif part_type == "message":
            content = payload.get("content")
            if isinstance(content, str) and content:
                fragments.append(content)
    return "".join(fragments).strip()


def _normalize_compaction_payload(raw: Any) -> dict[str, Any] | None:
    payload: Mapping[str, Any] | None = None
    if isinstance(raw, Mapping):
        payload = raw
    elif isinstance(raw, str):
        raw_text = raw.strip()
        if not raw_text:
            return None
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = _extract_json_object(raw_text)
        if isinstance(parsed, Mapping):
            payload = parsed
        else:
            return {
                "summary": _truncate_summary_text(raw_text),
                "userRequests": (),
                "decisions": (),
                "toolActivity": (),
                "files": (),
                "openQuestions": (),
            }
    if payload is None:
        return None
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return {
        "summary": _truncate_summary_text(summary),
        "userRequests": _normalize_compaction_string_list(
            payload.get("user_requests", payload.get("userRequests"))
        ),
        "decisions": _normalize_compaction_string_list(payload.get("decisions")),
        "toolActivity": _normalize_compaction_string_list(
            payload.get("tool_activity", payload.get("toolActivity"))
        ),
        "files": _normalize_compaction_string_list(payload.get("files")),
        "openQuestions": _normalize_compaction_string_list(
            payload.get("open_questions", payload.get("openQuestions"))
        ),
    }


def _extract_json_object(text: str) -> Mapping[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _build_compaction_prompt(
    *,
    transcript: str,
    heuristic_payload: Mapping[str, Any],
    custom_instructions: str | None,
) -> str:
    prompt = (
        "Summarize this compacted conversation history for the next model turn.\n"
        "Use the heuristic summary only as fallback context.\n"
        f"Heuristic summary: {heuristic_payload['summary']}"
    )
    normalized_instructions = _normalize_compaction_instruction_text(
        custom_instructions
    )
    if normalized_instructions:
        prompt += (
            "\n\nAdditional Instructions:\n"
            f"{normalized_instructions}"
        )
    prompt += f"\n\nTranscript:\n{transcript}"
    return prompt


def _extract_compaction_hook_instructions(hook_result: object) -> str | None:
    updated_input = getattr(hook_result, "updated_input", None)
    explicit_instructions: str | None = None
    if isinstance(updated_input, Mapping):
        for key in (
            "custom_instructions",
            "customInstructions",
            "instructions",
            "prompt",
            "additional_instructions",
            "additionalInstructions",
        ):
            value = updated_input.get(key)
            if isinstance(value, str) and value.strip():
                explicit_instructions = _merge_compaction_instructions(
                    explicit_instructions,
                    value,
                )
    hook_messages = tuple(getattr(hook_result, "messages", ()) or ())
    return _merge_compaction_instructions(
        explicit_instructions,
        _render_compaction_hook_instructions(hook_messages),
    )


def _render_compaction_hook_instructions(messages: Sequence[Message]) -> str | None:
    rendered: list[str] = []
    seen: set[str] = set()
    for message in messages:
        level = getattr(message, "level", None)
        if isinstance(level, str) and level.lower() not in {"info", "success"}:
            continue
        text = _normalize_compaction_instruction_text(
            _render_message_for_estimation(message)
        )
        if text is None or text in seen:
            continue
        seen.add(text)
        rendered.append(text)
    if not rendered:
        return None
    return "\n\n".join(rendered)


def _merge_compaction_instructions(
    base: str | None,
    extra: str | None,
) -> str | None:
    normalized_base = _normalize_compaction_instruction_text(base)
    normalized_extra = _normalize_compaction_instruction_text(extra)
    if normalized_extra is None:
        return normalized_base
    if normalized_base is None:
        return normalized_extra
    if normalized_base == normalized_extra:
        return normalized_base
    return f"{normalized_base}\n\n{normalized_extra}"


def _normalize_compaction_instruction_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _callable_accepts_keyword(callable_obj: object, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return keyword in signature.parameters


def _build_heuristic_compaction_payload(messages: Sequence[Message]) -> dict[str, Any]:
    buckets: Counter[str] = Counter(_message_bucket(message) for message in messages)
    merged = _merge_prior_compaction_metadata(messages)
    user_requests = list(merged["userRequests"])
    decisions = list(merged["decisions"])
    tool_activity = list(merged["toolActivity"])
    files = list(merged["files"])
    open_questions = list(merged["openQuestions"])

    for message in messages:
        if isinstance(message, UserMessage) and message.isCompactSummary:
            continue
        if isinstance(message, UserMessage):
            content = message.message.content
            if isinstance(content, tuple) and all(
                isinstance(block, ToolResultBlock) for block in content
            ):
                for block in content:
                    snippet = _truncate_summary_text(
                        _stringify_tool_result_content(block.content)
                    )
                    if snippet:
                        tool_activity.append(f"tool result: {snippet}")
                continue
            snippet = _truncate_summary_text(_render_message_for_estimation(message))
            if snippet:
                if "?" in snippet:
                    open_questions.append(snippet)
                else:
                    user_requests.append(snippet)
            files.extend(_extract_paths_from_text(snippet))
            continue
        if isinstance(message, AssistantMessage):
            assistant_text = _truncate_summary_text(
                _assistant_text_excerpt(message)
            )
            if assistant_text:
                decisions.append(assistant_text)
                files.extend(_extract_paths_from_text(assistant_text))
            tool_activity.extend(_assistant_tool_activity(message))
            continue

    user_requests = _dedupe_strings(user_requests)
    decisions = _dedupe_strings(decisions)
    tool_activity = _dedupe_strings(tool_activity)
    files = _dedupe_strings(files)
    open_questions = _dedupe_strings(open_questions)

    summary_parts: list[str] = []
    if user_requests:
        summary_parts.append(f"user asked for {user_requests[0]}")
    if decisions:
        summary_parts.append(f"agent covered {decisions[0]}")
    if tool_activity:
        summary_parts.append(f"tools included {tool_activity[0]}")
    if not summary_parts:
        summary_parts.append(_build_compaction_summary(messages))

    return {
        "summary": _truncate_summary_text(". ".join(summary_parts)),
        "messageBuckets": dict(buckets),
        "userRequests": tuple(user_requests[:_COMPACTION_LIST_LIMIT]),
        "decisions": tuple(decisions[:_COMPACTION_LIST_LIMIT]),
        "toolActivity": tuple(tool_activity[:_COMPACTION_LIST_LIMIT]),
        "files": tuple(files[:_COMPACTION_LIST_LIMIT]),
        "openQuestions": tuple(open_questions[:_COMPACTION_LIST_LIMIT]),
    }


def _merge_prior_compaction_metadata(
    messages: Sequence[Message],
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {
        "userRequests": [],
        "decisions": [],
        "toolActivity": [],
        "files": [],
        "openQuestions": [],
    }
    for message in messages:
        if not (
            isinstance(message, UserMessage)
            and message.isCompactSummary
            and isinstance(message.summarizeMetadata, Mapping)
        ):
            continue
        for key in merged:
            values = message.summarizeMetadata.get(key)
            if isinstance(values, Sequence) and not isinstance(
                values,
                (str, bytes, bytearray),
            ):
                merged[key].extend(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
    return {key: tuple(_dedupe_strings(values)) for key, values in merged.items()}


def _count_prior_compactions(messages: Sequence[Message]) -> int:
    observed = 0
    highest_compaction_count = 0
    for message in messages:
        if not (
            isinstance(message, UserMessage)
            and message.isCompactSummary
            and isinstance(message.summarizeMetadata, Mapping)
        ):
            continue
        observed += 1
        count = message.summarizeMetadata.get("compactionCount")
        if isinstance(count, int) and count > highest_compaction_count:
            highest_compaction_count = count
    return max(observed, highest_compaction_count)


def _assistant_text_excerpt(message: AssistantMessage) -> str:
    parts: list[str] = []
    for block in message.message.content:
        if isinstance(block, TextBlock) and block.text.strip():
            parts.append(block.text.strip())
    return "\n".join(parts)


def _is_tool_result_message(message: Message | None) -> bool:
    return (
        isinstance(message, UserMessage)
        and isinstance(message.message.content, tuple)
        and bool(message.message.content)
        and all(isinstance(block, ToolResultBlock) for block in message.message.content)
    )


def _build_tool_attachment_messages(
    tool_use_messages: Sequence[AssistantMessage],
    *,
    tool_result_messages: Sequence[UserMessage],
) -> tuple[AttachmentMessage, ...]:
    tool_blocks_by_id = {
        block.id: block
        for message in tool_use_messages
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    }
    attachments: list[AttachmentMessage] = []
    seen_signatures: set[tuple[Any, ...]] = set()
    for message in tool_result_messages:
        payload = message.toolUsePayload
        if not isinstance(payload, Mapping):
            continue
        content = message.message.content
        if not isinstance(content, tuple):
            continue
        for block in content:
            if not isinstance(block, ToolResultBlock) or bool(block.is_error):
                continue
            attachment = _attachment_for_tool_result(
                block,
                payload,
                tool_blocks_by_id.get(block.tool_use_id),
            )
            if attachment is None:
                continue
            signature = _attachment_signature(attachment)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            attachments.append(attachment)
    return tuple(attachments)


def build_compaction_attachment_messages(
    messages: Sequence[Message],
    *,
    preserved_messages: Sequence[Message] = (),
) -> tuple[AttachmentMessage, ...]:
    tool_use_messages = tuple(
        message
        for message in messages
        if isinstance(message, AssistantMessage)
        and any(
            isinstance(block, ToolUseBlock) for block in message.message.content
        )
    )
    tool_result_messages = tuple(
        message
        for message in messages
        if isinstance(message, UserMessage)
        and _is_tool_result_message(message)
    )
    if not tool_use_messages or not tool_result_messages:
        return ()

    attachments = _build_tool_attachment_messages(
        tool_use_messages,
        tool_result_messages=tool_result_messages,
    )
    if not attachments:
        return ()

    seen_signatures = {
        _attachment_signature(message)
        for message in preserved_messages
        if isinstance(message, AttachmentMessage)
    }
    deduped: list[AttachmentMessage] = []
    for attachment in attachments:
        signature = _attachment_signature(attachment)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        deduped.append(attachment)
    return tuple(deduped)


def _attachment_for_tool_result(
    tool_result_block: ToolResultBlock,
    payload: Mapping[str, Any],
    tool_block: ToolUseBlock | None,
) -> AttachmentMessage | None:
    tool_name = tool_block.name if isinstance(tool_block, ToolUseBlock) else ""
    tool_input = tool_block.input if isinstance(tool_block, ToolUseBlock) else {}

    file_path = _mapping_string(payload, "file_path", "filePath")
    notebook_path = _mapping_string(payload, "notebook_path", "notebookPath")
    session_file_type = _mapping_string(
        payload,
        "session_file_type",
        "sessionFileType",
    )
    task_id = _mapping_string(payload, "task_id", "taskId")
    task_type = _mapping_string(payload, "task_type", "taskType")
    description = _mapping_string(payload, "description")
    command_name = _mapping_string(payload, "commandName", "command_name")

    if tool_name == "Read" and file_path and session_file_type:
        return createAttachmentMessage(
            {
                "type": "memory_file",
                "filePath": file_path,
                "sessionFileType": session_file_type,
                "toolUseId": tool_result_block.tool_use_id,
            }
        )

    if tool_name in {"Write", "Edit"} and file_path:
        return createAttachmentMessage(
            {
                "type": "edited_text_file",
                "filePath": file_path,
                "toolUseId": tool_result_block.tool_use_id,
            }
        )

    if tool_name == "NotebookEdit" and notebook_path:
        return createAttachmentMessage(
            {
                "type": "edited_notebook_file",
                "filePath": notebook_path,
                "toolUseId": tool_result_block.tool_use_id,
            }
        )

    if tool_name == "Bash" and task_id:
        command = ""
        if isinstance(tool_input, Mapping):
            command = _mapping_string(tool_input, "command") or ""
        return createAttachmentMessage(
            {
                "type": "queued_command",
                "taskId": task_id,
                "taskType": task_type,
                "description": description,
                "command": command,
                "toolUseId": tool_result_block.tool_use_id,
            }
        )

    if tool_name == "Skill" and command_name:
        return createAttachmentMessage(
            {
                "type": "skill_activated",
                "commandName": command_name,
                "toolUseId": tool_result_block.tool_use_id,
            }
        )

    return None


def _attachment_signature(message: AttachmentMessage) -> tuple[Any, ...]:
    attachment = message.attachment
    return (
        attachment.get("type"),
        attachment.get("toolUseId"),
        attachment.get("filePath"),
        attachment.get("sessionFileType"),
        attachment.get("taskId"),
        attachment.get("commandName"),
    )


def _mapping_string(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _build_tool_use_summary_message(
    tool_use_messages: Sequence[AssistantMessage],
    *,
    tool_result_messages: Sequence[UserMessage],
    model_adapter: ModelAdapter | None,
    runtime_config: QueryStreamConfig,
) -> ToolUseSummaryMessage | None:
    tool_blocks = [
        block
        for message in tool_use_messages
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    ]
    if not tool_blocks:
        return None

    heuristic_summary = _build_heuristic_tool_use_summary(
        tool_blocks,
        tool_result_messages=tool_result_messages,
    )
    summary_text = await _maybe_build_model_tool_use_summary(
        tool_blocks,
        tool_result_messages=tool_result_messages,
        heuristic_summary=heuristic_summary,
        model_adapter=model_adapter,
        runtime_config=runtime_config,
    )
    normalized_summary = _truncate_summary_text(summary_text or heuristic_summary)
    if not normalized_summary:
        return None
    return createToolUseSummaryMessage(
        normalized_summary,
        [block.id for block in tool_blocks],
    )


def _build_heuristic_tool_use_summary(
    tool_blocks: Sequence[ToolUseBlock],
    *,
    tool_result_messages: Sequence[UserMessage],
) -> str:
    tool_results: dict[str, list[tuple[bool, str]]] = {}
    for message in tool_result_messages:
        raw_texts = _tool_result_raw_texts(message)
        if raw_texts is None:
            continue
        content = message.message.content
        assert isinstance(content, tuple)
        for block, raw_text in zip(content, raw_texts):
            assert isinstance(block, ToolResultBlock)
            tool_results.setdefault(block.tool_use_id, []).append(
                (bool(block.is_error), raw_text)
            )

    segments: list[str] = []
    for tool_block in tool_blocks:
        result_entries = tool_results.get(tool_block.id, [])
        if result_entries:
            status = (
                "failed"
                if all(is_error for is_error, _ in result_entries)
                else "completed"
            )
            snippets = [
                _truncate_summary_text(text, limit=96)
                for _, text in result_entries
                if text.strip()
            ]
            segment = f"{tool_block.name} {status}"
            if snippets:
                segment = f"{segment}: {' | '.join(snippets)}"
        else:
            segment = f"{tool_block.name} completed"
        segments.append(segment)

    if not segments:
        return ""
    return _truncate_summary_text("; ".join(segments))


async def _maybe_build_model_tool_use_summary(
    tool_blocks: Sequence[ToolUseBlock],
    *,
    tool_result_messages: Sequence[UserMessage],
    heuristic_summary: str,
    model_adapter: ModelAdapter | None,
    runtime_config: QueryStreamConfig,
) -> str | None:
    if model_adapter is None:
        return None

    custom_call = getattr(model_adapter, "call_tool_use_summary", None)
    if callable(custom_call):
        result = custom_call(
            tuple(tool_blocks),
            tool_result_messages=tuple(tool_result_messages),
            heuristic_summary=heuristic_summary,
        )
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, str) and result.strip():
            return result.strip()

    if not isinstance(model_adapter, AnthropicStreamingModelAdapter):
        return None

    prompt_lines = [
        f"Heuristic summary: {heuristic_summary}",
        "Recent tool executions:",
    ]
    for tool_block in tool_blocks:
        prompt_lines.append(
            f"- {tool_block.name} ({tool_block.id}) input={json.dumps(tool_block.input, ensure_ascii=False, sort_keys=True)}"
        )
    for message in tool_result_messages:
        raw_texts = _tool_result_raw_texts(message)
        if raw_texts is None:
            continue
        content = message.message.content
        assert isinstance(content, tuple)
        for block, raw_text in zip(content, raw_texts):
            assert isinstance(block, ToolResultBlock)
            prompt_lines.append(
                f"- result {block.tool_use_id} error={block.is_error}: {_truncate_summary_text(raw_text, limit=320)}"
            )
    try:
        response = await asyncio.wait_for(
            model_adapter.call_model_nonstream(
                (
                    createUserMessage(
                        content="\n".join(prompt_lines),
                        isMeta=True,
                        origin="tool_use_summary_prompt",
                    ),
                ),
                system_prompt=_TOOL_USE_SUMMARY_PROMPT_SYSTEM,
                tools=None,
                signal=runtime_config.signal,
                options=_merge_background_request_option(
                    {"max_tokens": _TOOL_USE_SUMMARY_OUTPUT_TOKEN_LIMIT},
                    background_request_resolver=runtime_config.background_request_resolver,
                ),
            ),
            timeout=_TOOL_USE_SUMMARY_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    response_text = _assistant_text_excerpt(response).strip()
    if response_text:
        return response_text
    return None


def _assistant_tool_activity(message: AssistantMessage) -> tuple[str, ...]:
    activity = [
        f"invoked {block.name}"
        for block in message.message.content
        if isinstance(block, ToolUseBlock)
    ]
    return tuple(activity)


def _normalize_compaction_string_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    items = [
        _truncate_summary_text(item)
        for item in raw
        if isinstance(item, str) and item.strip()
    ]
    return tuple(_dedupe_strings(items)[:_COMPACTION_LIST_LIMIT])


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _build_compaction_transcript(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    total_chars = 0
    for message in messages:
        rendered = _render_compaction_message(message)
        if not rendered:
            continue
        remaining = _COMPACTION_TRANSCRIPT_CHAR_LIMIT - total_chars
        if remaining <= 0:
            lines.append("[transcript truncated]")
            break
        if len(rendered) > remaining:
            rendered = f"{rendered[:remaining].rstrip()}\n[transcript truncated]"
        lines.append(rendered)
        total_chars += len(rendered)
    return "\n".join(lines)


def _render_compaction_message(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        text = _assistant_text_excerpt(message)
        tool_uses = ", ".join(_assistant_tool_activity(message))
        payload = text or tool_uses
        if not payload:
            return ""
        collapsed, _ = _collapse_text_for_context(
            payload,
            label="assistant message",
        )
        return f"assistant: {collapsed}"
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, tuple) and all(
            isinstance(block, ToolResultBlock) for block in content
        ):
            rendered_blocks = []
            for block in content:
                collapsed, _ = _collapse_text_for_context(
                    _stringify_tool_result_content(block.content),
                    label=f"tool result {block.tool_use_id}",
                )
                rendered_blocks.append(collapsed)
            return f"user(tool_result): {' | '.join(rendered_blocks)}"
        rendered = _render_message_for_estimation(message)
        collapsed, _ = _collapse_text_for_context(rendered, label="user message")
        return f"user: {collapsed}"
    return _render_message_for_estimation(message)


def _render_compaction_summary_text(payload: Mapping[str, Any]) -> str:
    lines = [
        "Earlier conversation history was compacted.",
        f"Summary: {payload['summary']}",
    ]
    if payload.get("userRequests"):
        lines.append(
            "User requests: " + "; ".join(payload["userRequests"])
        )
    if payload.get("decisions"):
        lines.append("Progress: " + "; ".join(payload["decisions"]))
    if payload.get("toolActivity"):
        lines.append("Tool activity: " + "; ".join(payload["toolActivity"]))
    if payload.get("files"):
        lines.append("Relevant files: " + "; ".join(payload["files"]))
    if payload.get("openQuestions"):
        lines.append("Open questions: " + "; ".join(payload["openQuestions"]))
    return "\n".join(lines)


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return json.dumps(dict(content), ensure_ascii=False, sort_keys=True)
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return json.dumps(list(content), ensure_ascii=False)
    return str(content)


def _collapse_text_for_context(
    text: str,
    *,
    label: str,
) -> tuple[str, Mapping[str, Any] | None]:
    normalized = text.strip()
    if len(normalized) <= _CONTEXT_COLLAPSE_CHAR_LIMIT:
        return normalized, None
    omitted_chars = len(normalized) - (
        _CONTEXT_COLLAPSE_HEAD_CHARS + _CONTEXT_COLLAPSE_TAIL_CHARS
    )
    collapsed = (
        f"[{label} collapsed: total_chars={len(normalized)} omitted_chars={max(omitted_chars, 0)}]\n"
        f"{normalized[:_CONTEXT_COLLAPSE_HEAD_CHARS].rstrip()}\n"
        "[...]\n"
        f"{normalized[-_CONTEXT_COLLAPSE_TAIL_CHARS:].lstrip()}"
    )
    return collapsed, {
        "label": label,
        "totalChars": len(normalized),
        "omittedChars": max(omitted_chars, 0),
    }


def _truncate_summary_text(text: str, *, limit: int = 240) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _extract_paths_from_text(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    candidates = re.findall(r"(?:[\w./-]+/)?[\w.-]+\.[A-Za-z0-9]{1,8}", text)
    return tuple(
        candidate
        for candidate in candidates
        if "/" in candidate or "." in candidate
    )


def _messages_after_last_compact_boundary(
    messages: Sequence[Message],
) -> tuple[Message, ...]:
    last_boundary_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, CompactBoundaryMessage):
            last_boundary_index = index
    if last_boundary_index < 0:
        return tuple(messages)
    return tuple(messages[last_boundary_index:])


def _compaction_tail_start_index(
    messages: Sequence[Message],
    *,
    keep_tail_messages: int,
) -> int:
    groups = group_messages_by_api_round(messages)
    if len(groups) < 2:
        non_system_indices = [
            index
            for index, message in enumerate(messages)
            if not _is_system_like_message(message)
        ]
        return non_system_indices[-keep_tail_messages]

    preserved_groups: list[tuple[Message, ...]] = []
    preserved_non_system = 0
    for group in reversed(groups):
        preserved_groups.append(group)
        preserved_non_system += sum(
            1 for message in group if not _is_system_like_message(message)
        )
        if preserved_non_system >= keep_tail_messages:
            break

    if len(preserved_groups) >= len(groups):
        non_system_indices = [
            index
            for index, message in enumerate(messages)
            if not _is_system_like_message(message)
        ]
        return non_system_indices[-keep_tail_messages]

    preserved = tuple(
        message for group in reversed(preserved_groups) for message in group
    )
    first_preserved = preserved[0]
    return next(
        index for index, message in enumerate(messages) if message is first_preserved
    )


def _is_system_like_message(message: Message) -> bool:
    return isinstance(message, CompactBoundaryMessage) or getattr(message, "type", "") == "system"


def _build_compaction_summary(messages: Sequence[Message]) -> str:
    buckets: Counter[str] = Counter(_message_bucket(message) for message in messages)
    fragments = [f"{count} {bucket}" for bucket, count in sorted(buckets.items())]
    summary = ", ".join(fragments) if fragments else "no prior messages"
    return (
        "Earlier conversation history was compacted. "
        f"Removed {len(messages)} messages ({summary})."
    )


def _message_bucket(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        return "assistant"
    if isinstance(message, UserMessage):
        content = message.message.content
        if (
            isinstance(content, tuple)
            and content
            and all(isinstance(block, ToolResultBlock) for block in content)
        ):
            return "tool_result"
        return "user"
    return getattr(message, "subtype", getattr(message, "type", "unknown"))


def _estimate_messages_tokens(messages: Sequence[Message]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _estimate_message_tokens(message: Message) -> int:
    rendered = _render_message_for_estimation(message)
    if not rendered:
        return 0
    return _count_text_tokens(rendered)


def _render_message_for_estimation(message: Message) -> str:
    if isinstance(message, AssistantMessage):
        parts: list[str] = []
        for block in message.message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                parts.append(block.name)
                if block.input:
                    parts.append(json.dumps(block.input, ensure_ascii=False, sort_keys=True))
        return "\n".join(part for part in parts if part)
    if isinstance(message, UserMessage):
        content = message.message.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(str(block.content))
        return "\n".join(part for part in parts if part)
    if isinstance(message, CompactBoundaryMessage):
        return (
            f"{message.trigger} {message.originalTokenCount} "
            f"{message.newTokenCount}"
        )
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    summary = getattr(message, "summary", None)
    if isinstance(summary, str):
        return summary
    return ""


def _count_text_tokens(text: str) -> int:
    return count_text_tokens(text)


def _apply_stream_delta(
    content_blocks: dict[int, dict[str, Any]],
    part: Mapping[str, Any],
) -> None:
    index = int(part["index"])
    if index not in content_blocks:
        raise QueryStateTransitionError("Content block not found")
    block = content_blocks[index]
    delta = part.get("delta")
    if not isinstance(delta, Mapping):
        raise QueryStateTransitionError("content_block_delta requires delta payload")
    delta_type = delta.get("type")
    if delta_type == "text_delta":
        if block.get("type") != "text":
            raise QueryStateTransitionError("Content block is not a text block")
        block["text"] = f"{block.get('text', '')}{delta.get('text', '')}"
        return
    if delta_type == "input_json_delta":
        if block.get("type") != "tool_use":
            raise QueryStateTransitionError("Content block is not a input_json block")
        block["input"] = f"{block.get('input', '')}{delta.get('partial_json', '')}"
        return
    if delta_type == "thinking_delta":
        if block.get("type") != "thinking":
            raise QueryStateTransitionError("Content block is not a thinking block")
        block["thinking"] = f"{block.get('thinking', '')}{delta.get('thinking', '')}"
        return
    if delta_type == "signature_delta":
        if block.get("type") != "thinking":
            raise QueryStateTransitionError("Content block is not a thinking block")
        block["signature"] = f"{block.get('signature', '')}{delta.get('signature', '')}"
        return
    raise QueryStateTransitionError(
        f"Unsupported content_block_delta type: {delta_type!r}"
    )


def _apply_tool_update_session(
    current_session: QuerySession,
    update: ToolExecutionUpdate,
) -> tuple[ToolExecutionUpdate, QuerySession, bool]:
    normalized_message = _normalize_tool_update_message(update.message)
    if normalized_message is not update.message:
        update = ToolExecutionUpdate(
            message=normalized_message,
            newContext=update.newContext,
        )
    replacement_messages = _extract_session_replacement_messages(update.newContext)
    if replacement_messages is None:
        return update, current_session, False

    replacement_session = QuerySession.fromMessages(
        replacement_messages,
        sessionState=current_session.sessionState,
        externalMetadata=current_session.externalMetadata,
    )
    if update.message is not None and (
        not replacement_messages or replacement_messages[-1] != update.message
    ):
        replacement_session = replacement_session.appendMessage(update.message)

    normalized_context = _strip_session_replacement_context(update.newContext)
    return (
        ToolExecutionUpdate(
            message=normalized_message,
            newContext=normalized_context,
        ),
        replacement_session,
        normalized_message is not None,
    )


def _normalize_tool_update_message(message: Message | None) -> Message | None:
    if not isinstance(message, UserMessage):
        return message
    content = message.message.content
    if not (
        isinstance(content, tuple)
        and content
        and all(isinstance(block, ToolResultBlock) for block in content)
    ):
        return message

    collapsed_blocks: list[ToolResultBlock] = []
    collapse_metadata: list[Mapping[str, Any]] = []
    raw_contents: list[str] = []
    changed = False
    for block in content:
        raw_text = _stringify_tool_result_content(block.content)
        raw_contents.append(raw_text)
        collapsed_text, metadata = _collapse_text_for_context(
            raw_text,
            label=f"tool result {block.tool_use_id}",
        )
        if metadata is not None:
            changed = True
            collapse_metadata.append(
                {
                    **metadata,
                    "toolUseId": block.tool_use_id,
                }
            )
        collapsed_blocks.append(
            ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=collapsed_text,
                is_error=block.is_error,
            )
        )
    if not changed:
        return message

    mcp_meta = dict(message.mcpMeta or {})
    mcp_meta["contextCollapse"] = tuple(collapse_metadata)
    tool_use_result = message.toolUseResult
    if tool_use_result is None:
        tool_use_result = raw_contents[0] if len(raw_contents) == 1 else tuple(raw_contents)
    return createUserMessage(
        content=tuple(collapsed_blocks),
        isMeta=message.isMeta,
        isVisibleInTranscriptOnly=message.isVisibleInTranscriptOnly,
        isVirtual=message.isVirtual,
        isCompactSummary=message.isCompactSummary,
        summarizeMetadata=message.summarizeMetadata,
        toolUseResult=tool_use_result,
        toolUsePayload=message.toolUsePayload,
        mcpMeta=mcp_meta,
        uuid=message.uuid,
        timestamp=message.timestamp,
        imagePasteIds=message.imagePasteIds,
        sourceToolAssistantUUID=message.sourceToolAssistantUUID,
        permissionMode=message.permissionMode,
        origin=message.origin,
    )


def _extract_session_replacement_messages(
    context: Optional[Mapping[str, Any]],
) -> Optional[tuple[Message, ...]]:
    if context is None or _SESSION_MESSAGE_REPLACEMENT_KEY not in context:
        return None
    raw_messages = context.get(_SESSION_MESSAGE_REPLACEMENT_KEY)
    if not isinstance(raw_messages, Iterable) or isinstance(
        raw_messages,
        (str, bytes, bytearray),
    ):
        raise QueryStateTransitionError(
            "session replacement context must provide a message sequence"
        )
    messages = tuple(raw_messages)
    for message in messages:
        if not isinstance(message, (AssistantMessage, UserMessage)) and getattr(
            message,
            "type",
            None,
        ) != "system":
            raise QueryStateTransitionError(
                "session replacement context contains an unsupported message"
            )
    return messages


def _strip_session_replacement_context(
    context: Optional[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    if context is None or _SESSION_MESSAGE_REPLACEMENT_KEY not in context:
        return context
    filtered = {
        key: value
        for key, value in context.items()
        if key != _SESSION_MESSAGE_REPLACEMENT_KEY
    }
    return filtered or None


def _build_anthropic_request_body(
    messages: Sequence[Message],
    *,
    model: str,
    system_prompt: SystemPrompt = None,
    tools: ToolDefinitions = None,
    options: QueryOptions = None,
    stream: bool = True,
    base_url: str | None = None,
) -> dict[str, Any]:
    normalized_system_prompt = _normalize_system_prompt(system_prompt)
    normalized_tools = _prepare_anthropic_tools_payload(
        _normalize_tools_payload(tools),
        base_url=base_url,
    )
    normalized_options = _normalize_query_options(options)
    normalized_model = normalize_model_string_for_api(model).strip() or model
    max_tokens = _extract_max_tokens(normalized_options)
    extra_body, thinking_payload = _resolve_anthropic_request_overrides(
        normalized_model,
        normalized_options,
        max_tokens=max_tokens,
        messages=messages,
    )
    anthropic_messages = _apply_prompt_caching_to_anthropic_messages(
        _convert_messages_to_anthropic(
            messages,
            tool_search_enabled=_tool_search_enabled(normalized_tools),
        ),
        options=normalized_options,
        original_messages=messages,
    )
    payload: dict[str, Any] = {
        "model": normalized_model,
        "max_tokens": max_tokens,
        "messages": anthropic_messages,
        "stream": stream,
    }
    system_payload = _build_anthropic_system_payload(
        normalized_system_prompt,
        tools=normalized_tools,
        options=normalized_options,
    )
    if system_payload is not None:
        payload["system"] = system_payload
    if normalized_tools is not None:
        payload["tools"] = list(normalized_tools)
    if thinking_payload is not None and "thinking" not in extra_body:
        payload["thinking"] = thinking_payload
    if extra_body:
        payload = _merge_mapping(payload, extra_body)
    return payload


def _build_openai_chat_request_body(
    messages: Sequence[Message],
    *,
    model: str,
    system_prompt: SystemPrompt = None,
    tools: ToolDefinitions = None,
    options: QueryOptions = None,
    stream: bool = True,
    include_reasoning_content: bool = False,
) -> dict[str, Any]:
    normalized_system_prompt = _normalize_system_prompt(system_prompt)
    normalized_tools = _prepare_openai_chat_tools_payload(
        _normalize_tools_payload(tools)
    )
    normalized_options = _normalize_query_options(options)
    payload: dict[str, Any] = {
        "model": model.strip() or model,
        "messages": _convert_messages_to_openai_chat(
            messages,
            system_prompt=normalized_system_prompt,
            system_role=get_openai_system_role(),
            include_reasoning_content=include_reasoning_content,
        ),
        "stream": stream,
        "max_tokens": _extract_max_tokens(normalized_options),
    }
    if normalized_tools is not None:
        payload["tools"] = list(normalized_tools)
        payload["tool_choice"] = "auto"

    extra_body = _merge_mapping(
        _load_json_object_env("OPENAI_EXTRA_BODY"),
        _extract_extra_body(normalized_options),
    )
    if extra_body:
        payload = _merge_mapping(payload, extra_body)
    return payload


def _prepare_openai_chat_tools_payload(
    tools: tuple[dict[str, object], ...] | None,
) -> tuple[dict[str, object], ...] | None:
    if tools is None:
        return None
    normalized: list[dict[str, object]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        function_payload: dict[str, object] = {
            "name": name.strip(),
            "parameters": dict(tool.get("input_schema") or {"type": "object"}),
        }
        description = tool.get("description")
        if isinstance(description, str) and description.strip():
            function_payload["description"] = description
        normalized.append({"type": "function", "function": function_payload})
    return tuple(normalized) or None


def _convert_messages_to_openai_chat(
    messages: Sequence[Message],
    *,
    system_prompt: NormalizedSystemPrompt,
    system_role: str,
    include_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    system_text = _openai_system_prompt_text(system_prompt)
    if system_text:
        converted.append({"role": system_role, "content": system_text})
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AssistantMessage) and not bool(message.isVirtual):
            assistant_group, deferred_messages, index = _collect_openai_assistant_round(
                messages,
                index,
            )
            assistant_payload = _convert_assistant_messages_to_openai_chat(
                assistant_group,
                include_reasoning_content=include_reasoning_content,
            )
            if assistant_payload is not None:
                converted.append(assistant_payload)
            for deferred_message in deferred_messages:
                converted.extend(
                    _convert_message_to_openai_chat(
                        deferred_message,
                        include_reasoning_content=include_reasoning_content,
                    )
                )
            continue
        converted.extend(
            _convert_message_to_openai_chat(
                message,
                include_reasoning_content=include_reasoning_content,
            )
        )
        index += 1
    return converted


def _collect_openai_assistant_round(
    messages: Sequence[Message],
    start_index: int,
) -> tuple[list[AssistantMessage], list[Message], int]:
    first_message = messages[start_index]
    if not isinstance(first_message, AssistantMessage):
        return [], [], start_index
    assistant_group = [first_message]
    deferred_messages: list[Message] = []
    message_id = getattr(first_message.message, "id", None)
    index = start_index + 1
    if not message_id:
        return assistant_group, deferred_messages, index

    while index < len(messages):
        message = messages[index]
        if (
            isinstance(message, AssistantMessage)
            and not bool(message.isVirtual)
            and getattr(message.message, "id", None) == message_id
        ):
            assistant_group.append(message)
            index += 1
            continue
        if _openai_user_message_is_tool_results_only(message):
            deferred_messages.append(message)
            index += 1
            continue
        break
    return assistant_group, deferred_messages, index


def _convert_message_to_openai_chat(
    message: Message,
    *,
    include_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    if isinstance(message, UserMessage):
        if bool(message.isVirtual):
            return []
        return _convert_user_message_to_openai_chat(message)
    if isinstance(message, AssistantMessage):
        if bool(message.isVirtual):
            return []
        converted = _convert_assistant_message_to_openai_chat(
            message,
            include_reasoning_content=include_reasoning_content,
        )
        return [converted] if converted is not None else []
    return []


def _convert_user_message_to_openai_chat(message: UserMessage) -> list[dict[str, Any]]:
    content = message.message.content
    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    converted: list[dict[str, Any]] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        text = "\n".join(part for part in pending_text if part)
        pending_text.clear()
        if text.strip():
            converted.append({"role": "user", "content": text})

    for block in content:
        text = _openai_text_from_user_block(block)
        if text is not None:
            pending_text.append(text)
            continue
        tool_result = _openai_tool_result_from_user_block(block)
        if tool_result is not None:
            flush_text()
            converted.append(tool_result)
    flush_text()
    return converted


def _openai_user_message_is_tool_results_only(message: Message) -> bool:
    if not isinstance(message, UserMessage) or bool(message.isVirtual):
        return False
    content = message.message.content
    if not isinstance(content, Sequence) or isinstance(
        content,
        (str, bytes, bytearray),
    ):
        return False
    return bool(content) and all(
        _openai_tool_result_from_user_block(block) is not None for block in content
    )


def _convert_assistant_message_to_openai_chat(
    message: AssistantMessage,
    *,
    include_reasoning_content: bool = False,
) -> dict[str, Any] | None:
    return _convert_assistant_messages_to_openai_chat(
        (message,),
        include_reasoning_content=include_reasoning_content,
    )


def _convert_assistant_messages_to_openai_chat(
    messages: Sequence[AssistantMessage],
    *,
    include_reasoning_content: bool = False,
) -> dict[str, Any] | None:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for message in messages:
        for block in message.message.content:
            reasoning = _openai_reasoning_content_from_assistant_block(block)
            if reasoning is not None:
                reasoning_parts.append(reasoning)
                continue
            text = _openai_text_from_assistant_block(block)
            if text is not None:
                text_parts.append(text)
                continue
            tool_call = _openai_tool_call_from_assistant_block(block)
            if tool_call is not None:
                tool_calls.append(tool_call)
    text = "\n".join(part for part in text_parts if part)
    reasoning_text = "\n".join(part for part in reasoning_parts if part)
    if not text and not tool_calls and not (include_reasoning_content and reasoning_text):
        return None
    converted: dict[str, Any] = {
        "role": "assistant",
        "content": text if text else None,
    }
    if include_reasoning_content and reasoning_text:
        converted["reasoning_content"] = reasoning_text
    if tool_calls:
        converted["tool_calls"] = tool_calls
    return converted


def _openai_system_prompt_text(system_prompt: NormalizedSystemPrompt) -> str | None:
    if system_prompt is None:
        return None
    if isinstance(system_prompt, str):
        return system_prompt if system_prompt.strip() else None
    parts: list[str] = []
    for block in system_prompt:
        if not isinstance(block, Mapping):
            continue
        text = block.get("text", block.get("content"))
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n\n".join(parts) or None


def _openai_text_from_user_block(block: object) -> str | None:
    if isinstance(block, TextBlock):
        return block.text
    if isinstance(block, Mapping) and block.get("type") == "text":
        text = block.get("text", block.get("content"))
        return text if isinstance(text, str) else None
    return None


def _openai_tool_result_from_user_block(block: object) -> dict[str, Any] | None:
    if isinstance(block, ToolResultBlock):
        tool_use_id = block.tool_use_id
        content = _normalize_tool_result_content_for_openai(block.content)
        if block.is_error and content:
            content = f"Error: {content}"
        return {"role": "tool", "tool_call_id": tool_use_id, "content": content}
    if not isinstance(block, Mapping) or block.get("type") != "tool_result":
        return None
    tool_use_id = block.get("tool_use_id", block.get("tool_call_id"))
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        return None
    content = _normalize_tool_result_content_for_openai(block.get("content", ""))
    if block.get("is_error") is True and content:
        content = f"Error: {content}"
    return {"role": "tool", "tool_call_id": tool_use_id, "content": content}


def _openai_text_from_assistant_block(block: object) -> str | None:
    if isinstance(block, TextBlock):
        return block.text if block.text.strip() else None
    if isinstance(block, Mapping) and block.get("type") == "text":
        text = block.get("text", block.get("content"))
        if isinstance(text, str) and text.strip():
            return text
    return None


def _openai_tool_call_from_assistant_block(block: object) -> dict[str, Any] | None:
    if isinstance(block, ToolUseBlock):
        return {
            "id": block.id,
            "type": "function",
            "function": {
                "name": block.name,
                "arguments": json.dumps(dict(block.input), ensure_ascii=False),
            },
        }
    if not isinstance(block, Mapping) or block.get("type") != "tool_use":
        return None
    name = block.get("name")
    tool_id = block.get("id")
    if not isinstance(name, str) or not isinstance(tool_id, str):
        return None
    try:
        normalized_input = _normalize_tool_input(block.get("input", ""))
    except QueryStateTransitionError:
        normalized_input = {}
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(dict(normalized_input), ensure_ascii=False),
        },
    }


def _openai_reasoning_content_from_assistant_block(block: object) -> str | None:
    if isinstance(block, ThinkingBlock):
        return block.thinking if block.thinking.strip() else None
    if isinstance(block, Mapping) and block.get("type") == "thinking":
        thinking = block.get("thinking", block.get("content"))
        if isinstance(thinking, str) and thinking.strip():
            return thinking
    return None


def _normalize_tool_result_content_for_openai(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(
        content,
        (str, bytes, bytearray),
    ):
        texts: list[str] = []
        for item in content:
            if isinstance(item, TextBlock):
                texts.append(item.text)
                continue
            if isinstance(item, Mapping):
                text = item.get("text", item.get("content"))
                if isinstance(text, str):
                    texts.append(text)
                    continue
            if isinstance(item, str):
                texts.append(item)
                continue
            texts.append(str(item))
        return "\n".join(texts)
    return str(content)


def _extract_max_tokens(options: Optional[Mapping[str, Any]]) -> int:
    raw = None if options is None else options.get("max_tokens")
    if isinstance(raw, int) and raw > 0:
        return raw
    return 1024


def _build_anthropic_system_payload(
    system_prompt: NormalizedSystemPrompt,
    *,
    tools: tuple[dict[str, object], ...] | None,
    options: Optional[Mapping[str, Any]],
) -> str | list[dict[str, object]] | None:
    if system_prompt is None:
        return None
    if isinstance(system_prompt, str):
        return system_prompt
    if not system_prompt:
        return None
    blocks = tuple(dict(block) for block in system_prompt)
    if any(not _is_plain_text_system_block(block) for block in blocks):
        return list(blocks)
    if any("cache_control" in block for block in blocks):
        return list(blocks)
    if any(set(block.keys()) - {"type", "text"} for block in blocks):
        return list(blocks)
    return _split_system_prompt_blocks_for_anthropic(
        blocks,
        tools=tools,
        options=options,
    )


def _is_plain_text_system_block(block: Mapping[str, object]) -> bool:
    block_type = block.get("type")
    text = block.get("text")
    return (block_type in (None, "text")) and isinstance(text, str)


def _split_system_prompt_blocks_for_anthropic(
    blocks: Sequence[Mapping[str, object]],
    *,
    tools: tuple[dict[str, object], ...] | None,
    options: Optional[Mapping[str, Any]],
) -> list[dict[str, object]]:
    use_global_cache_scope = _extract_use_global_cache_scope(options)
    skip_global_cache = _extract_skip_global_cache_for_system_prompt(
        options,
        tools=tools,
    )
    prompt_cache_ttl = _extract_prompt_cache_ttl(options)

    attribution_header: str | None = None
    system_prefix: str | None = None
    boundary_index = -1
    text_values = [str(block.get("text")) for block in blocks]
    if use_global_cache_scope:
        for index, text in enumerate(text_values):
            if text == _SYSTEM_PROMPT_DYNAMIC_BOUNDARY:
                boundary_index = index
                break

    static_blocks: list[str] = []
    dynamic_blocks: list[str] = []
    rest_blocks: list[str] = []
    for index, text in enumerate(text_values):
        if not text or text == _SYSTEM_PROMPT_DYNAMIC_BOUNDARY:
            continue
        if text.startswith(_SYSTEM_PROMPT_ATTRIBUTION_PREFIX):
            attribution_header = text
            continue
        if _looks_like_system_prompt_prefix(text):
            system_prefix = text
            continue
        if use_global_cache_scope and not skip_global_cache and boundary_index >= 0:
            if index < boundary_index:
                static_blocks.append(text)
            else:
                dynamic_blocks.append(text)
            continue
        rest_blocks.append(text)

    result: list[dict[str, object]] = []
    if attribution_header:
        result.append(
            _make_system_prompt_block(
                attribution_header,
                cache_scope=None,
                prompt_cache_ttl=prompt_cache_ttl,
                options=options,
            )
        )
    if system_prefix:
        result.append(
            _make_system_prompt_block(
                system_prefix,
                cache_scope=(
                    None
                    if use_global_cache_scope and not skip_global_cache and boundary_index >= 0
                    else "org"
                ),
                prompt_cache_ttl=prompt_cache_ttl,
                options=options,
            )
        )
    if use_global_cache_scope and not skip_global_cache and boundary_index >= 0:
        static_joined = "\n\n".join(block for block in static_blocks if block.strip())
        dynamic_joined = "\n\n".join(block for block in dynamic_blocks if block.strip())
        if static_joined:
            result.append(
                _make_system_prompt_block(
                    static_joined,
                    cache_scope="global",
                    prompt_cache_ttl=prompt_cache_ttl,
                    options=options,
                )
            )
        if dynamic_joined:
            result.append(
                _make_system_prompt_block(
                    dynamic_joined,
                    cache_scope=None,
                    prompt_cache_ttl=prompt_cache_ttl,
                    options=options,
                )
            )
        return result

    rest_joined = "\n\n".join(block for block in rest_blocks if block.strip())
    if rest_joined:
        result.append(
            _make_system_prompt_block(
                rest_joined,
                cache_scope="org",
                prompt_cache_ttl=prompt_cache_ttl,
                options=options,
            )
        )
    return result


def _looks_like_system_prompt_prefix(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if normalized in _SYSTEM_PROMPT_KNOWN_PREFIXES:
        return True
    return normalized.startswith("You are ") and "\n" not in normalized and len(normalized) <= 240


def _make_system_prompt_block(
    text: str,
    *,
    cache_scope: str | None,
    prompt_cache_ttl: str | None,
    options: Optional[Mapping[str, Any]],
) -> dict[str, object]:
    block: dict[str, object] = {"type": "text", "text": text}
    if _should_apply_system_prompt_caching(options) and cache_scope is not None:
        block["cache_control"] = _build_prompt_cache_control(
            scope=cache_scope,
            ttl=prompt_cache_ttl,
        )
    return block


def _build_prompt_cache_control(
    *,
    scope: str | None = None,
    ttl: str | None = None,
) -> dict[str, object]:
    cache_control: dict[str, object] = {"type": "ephemeral"}
    if ttl == "1h":
        cache_control["ttl"] = ttl
    if scope == "global":
        cache_control["scope"] = scope
    return cache_control


def _apply_prompt_caching_to_anthropic_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    options: Optional[Mapping[str, Any]],
    original_messages: Sequence[Message] = (),
) -> list[dict[str, Any]]:
    converted = [_clone_anthropic_message(message) for message in messages]
    if not converted:
        return converted

    prompt_cache_ttl = _extract_prompt_cache_ttl(options)
    enable_message_prompt_caching = _should_apply_message_prompt_caching(options)
    if enable_message_prompt_caching:
        _attach_message_cache_control_marker(
            converted,
            prompt_cache_ttl=prompt_cache_ttl,
            skip_cache_write=_extract_skip_cache_write(options),
        )

    cache_breakpoints = _extract_cache_breakpoints(options)
    if cache_breakpoints is not None:
        _inject_cache_breakpoints(converted, cache_breakpoints)

    if enable_message_prompt_caching:
        _attach_tool_result_cache_references(converted)

    if original_messages:
        _inject_cached_microcompact_edits(converted, original_messages)

    return converted


def _clone_anthropic_message(message: Mapping[str, Any]) -> dict[str, Any]:
    cloned = dict(message)
    content = cloned.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        cloned["content"] = [
            dict(block) if isinstance(block, Mapping) else block
            for block in content
        ]
    return cloned


def _attach_message_cache_control_marker(
    messages: list[dict[str, Any]],
    *,
    prompt_cache_ttl: str | None,
    skip_cache_write: bool,
) -> None:
    if not messages:
        return
    marker_index = len(messages) - 2 if skip_cache_write and len(messages) > 1 else len(messages) - 1
    marker_index = max(0, marker_index)
    message = messages[marker_index]
    content = message.get("content")
    cache_control = _build_prompt_cache_control(ttl=prompt_cache_ttl)
    if isinstance(content, str):
        message["content"] = [
            {
                "type": "text",
                "text": content,
                "cache_control": cache_control,
            }
        ]
        return
    if not isinstance(content, list) or not content:
        return
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if not isinstance(block, Mapping):
            continue
        content[index] = {
            **dict(block),
            "cache_control": cache_control,
        }
        return


def _inject_cache_breakpoints(
    messages: list[dict[str, Any]],
    cache_breakpoints: Mapping[str, Any],
) -> None:
    seen_delete_refs: set[str] = set()
    for pinned in cache_breakpoints.get("pinned_edits", ()):
        assert isinstance(pinned, Mapping)
        user_message_index = int(pinned["user_message_index"])
        if user_message_index < 0 or user_message_index >= len(messages):
            continue
        message = messages[user_message_index]
        if message.get("role") != "user":
            continue
        refs = _dedupe_cache_edit_refs(
            pinned.get("cache_references", ()),
            seen_delete_refs,
        )
        if refs:
            _insert_cache_edits_block(message, refs)

    new_refs = _dedupe_cache_edit_refs(
        cache_breakpoints.get("new_edits", ()),
        seen_delete_refs,
    )
    if not new_refs:
        return
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        _insert_cache_edits_block(message, new_refs)
        break


def _dedupe_cache_edit_refs(
    refs: object,
    seen_delete_refs: set[str],
) -> tuple[str, ...]:
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes, bytearray)):
        return ()
    unique: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            continue
        normalized = ref.strip()
        if not normalized or normalized in seen_delete_refs:
            continue
        seen_delete_refs.add(normalized)
        unique.append(normalized)
    return tuple(unique)


def _insert_cache_edits_block(
    message: dict[str, Any],
    refs: Sequence[str],
) -> None:
    content = message.get("content")
    if isinstance(content, str):
        content_blocks: list[dict[str, object]] = [{"type": "text", "text": content}]
        message["content"] = content_blocks
    elif isinstance(content, list):
        content_blocks = content
    else:
        content_blocks = []
        message["content"] = content_blocks

    cache_edits_block = {
        "type": "cache_edits",
        "edits": [
            {"type": "delete", "cache_reference": ref}
            for ref in refs
        ],
    }
    insertion_index = len(content_blocks)
    for index in range(len(content_blocks) - 1, -1, -1):
        block = content_blocks[index]
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            insertion_index = index + 1
            while insertion_index < len(content_blocks):
                trailing_block = content_blocks[insertion_index]
                if not (
                    isinstance(trailing_block, Mapping)
                    and trailing_block.get("type") == "cache_edits"
                ):
                    break
                insertion_index += 1
            break
    content_blocks.insert(insertion_index, cache_edits_block)


def _attach_tool_result_cache_references(messages: list[dict[str, Any]]) -> None:
    last_cache_control_message_index = -1
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, Mapping) and "cache_control" in block
            for block in content
        ):
            last_cache_control_message_index = index

    if last_cache_control_message_index <= 0:
        return
    for index in range(last_cache_control_message_index):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id.strip():
                continue
            content[block_index] = {
                **dict(block),
                "cache_reference": tool_use_id,
            }


def _inject_cached_microcompact_edits(
    messages: list[dict[str, Any]],
    original_messages: Sequence[Message],
) -> None:
    from .query import _MICROCOMPACT_CACHE as _cache, _compute_microcompact_cache_key as _key_fn

    cache_key = _key_fn(original_messages)
    if cache_key is None:
        return

    if cache_key not in _cache:
        return

    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, Mapping):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if not isinstance(tool_use_id, str):
                continue
            if "cache_reference" not in block:
                content[block_index] = {
                    **dict(block),
                    "cache_reference": tool_use_id,
                }


def _extract_enable_prompt_caching(options: Optional[Mapping[str, Any]]) -> bool:
    if options is None:
        return False
    raw = options.get("enable_prompt_caching")
    return raw if isinstance(raw, bool) else False


def _should_apply_system_prompt_caching(
    options: Optional[Mapping[str, Any]],
) -> bool:
    return _extract_enable_prompt_caching(options) or _extract_use_global_cache_scope(
        options
    )


def _should_apply_message_prompt_caching(
    options: Optional[Mapping[str, Any]],
) -> bool:
    if _extract_enable_prompt_caching(options):
        return True
    cache_breakpoints = _extract_cache_breakpoints(options)
    if not isinstance(cache_breakpoints, Mapping):
        return False
    return any(
        key in cache_breakpoints
        for key in ("new_edits", "pinned_edits", "skip_cache_write")
    )


def _extract_use_global_cache_scope(options: Optional[Mapping[str, Any]]) -> bool:
    if options is None:
        return _env_truthy("CLAUDE_CODE_USE_GLOBAL_CACHE_SCOPE")
    raw = options.get("use_global_cache_scope")
    if isinstance(raw, bool):
        return raw
    return _env_truthy("CLAUDE_CODE_USE_GLOBAL_CACHE_SCOPE")


def _extract_prompt_cache_ttl(options: Optional[Mapping[str, Any]]) -> str | None:
    if options is None:
        return None
    raw = options.get("prompt_cache_ttl")
    return raw if raw == "1h" else None


def _extract_skip_global_cache_for_system_prompt(
    options: Optional[Mapping[str, Any]],
    *,
    tools: tuple[dict[str, object], ...] | None,
) -> bool:
    if options is not None:
        raw = options.get("skip_global_cache_for_system_prompt")
        if isinstance(raw, bool):
            return raw
    return any(_tool_definition_looks_like_mcp(tool) for tool in tools or ())


def _tool_definition_looks_like_mcp(tool: Mapping[str, object]) -> bool:
    if tool.get("is_mcp") is True:
        return True
    name = tool.get("name")
    return isinstance(name, str) and name.startswith("mcp__")


def _extract_skip_cache_write(options: Optional[Mapping[str, Any]]) -> bool:
    if options is None:
        return False
    cache_breakpoints = options.get("cache_breakpoints")
    if isinstance(cache_breakpoints, Mapping):
        raw_nested = cache_breakpoints.get("skip_cache_write")
        if isinstance(raw_nested, bool):
            return raw_nested
    raw = options.get("skip_cache_write")
    return raw if isinstance(raw, bool) else False


def _extract_cache_breakpoints(
    options: Optional[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if options is None:
        return None
    raw = options.get("cache_breakpoints")
    return raw if isinstance(raw, Mapping) else None


def _normalize_system_prompt(system_prompt: object) -> NormalizedSystemPrompt:
    if system_prompt is None:
        return None
    if isinstance(system_prompt, str):
        if not system_prompt.strip():
            return None
        return system_prompt
    if not isinstance(system_prompt, Sequence) or isinstance(
        system_prompt, (bytes, bytearray)
    ):
        raise QueryStateTransitionError(
            "system_prompt must be a string or a sequence of strings/mappings"
        )

    normalized_blocks: list[dict[str, object]] = []
    for index, block in enumerate(system_prompt):
        if isinstance(block, str):
            if not block.strip():
                continue
            normalized_blocks.append({"type": "text", "text": block})
            continue
        if not isinstance(block, Mapping):
            raise QueryStateTransitionError(
                f"system_prompt block at index {index} must be a string or object"
            )
        normalized_block = dict(block)
        block_type = normalized_block.get("type")
        text = normalized_block.get("text")
        content = normalized_block.get("content")
        cache_control = normalized_block.get("cache_control")

        if block_type is not None and not isinstance(block_type, str):
            raise QueryStateTransitionError(
                f"system_prompt block at index {index} has a non-string type"
            )
        if text is None and isinstance(content, str):
            text = content
            normalized_block["text"] = content
        if text is not None and not isinstance(text, str):
            raise QueryStateTransitionError(
                f"system_prompt block at index {index} has a non-string text"
            )
        if text is not None and not text.strip():
            continue
        if cache_control is not None and not isinstance(cache_control, Mapping):
            raise QueryStateTransitionError(
                f"system_prompt block at index {index} has a non-object cache_control"
            )
        if text is not None and block_type is None:
            normalized_block["type"] = "text"
        normalized_blocks.append(normalized_block)

    return tuple(normalized_blocks) or None


def _normalize_tools_payload(
    tools: object,
) -> tuple[dict[str, object], ...] | None:
    if tools is None:
        return None
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes, bytearray)):
        raise QueryStateTransitionError("tools must be a sequence of mappings")
    normalized: list[dict[str, object]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping):
            raise QueryStateTransitionError(
                f"tool definition at index {index} must be an object"
            )
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise QueryStateTransitionError(
                f"tool definition at index {index} must include a non-empty name"
            )
        input_schema = tool.get("input_schema")
        if input_schema is not None and not isinstance(input_schema, Mapping):
            raise QueryStateTransitionError(
                f"tool definition {name!r} has a non-object input_schema"
            )
        normalized.append(dict(tool))
    return tuple(normalized)


def _prepare_anthropic_tools_payload(
    tools: tuple[dict[str, object], ...] | None,
    *,
    base_url: str | None,
) -> tuple[dict[str, object], ...] | None:
    if tools is None:
        return None
    normalized = [dict(tool) for tool in tools]
    if _is_direct_anthropic_base_url(base_url):
        for tool in normalized:
            tool["eager_input_streaming"] = True
    if _env_truthy("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        normalized = [_strip_experimental_tool_fields(tool) for tool in normalized]
    return tuple(normalized)


def _strip_experimental_tool_fields(tool: Mapping[str, object]) -> dict[str, object]:
    allowed_fields = {
        "name",
        "description",
        "input_schema",
        "cache_control",
    }
    return {
        key: value
        for key, value in tool.items()
        if key in allowed_fields
    }


def _is_direct_anthropic_base_url(base_url: str | None) -> bool:
    if not isinstance(base_url, str) or not base_url.strip():
        return False
    parsed = urlparse(base_url)
    hostname = parsed.hostname or ""
    return hostname.lower() == "api.anthropic.com"


def _normalize_query_options(
    options: object,
) -> dict[str, Any] | None:
    if options is None:
        return None
    if not isinstance(options, Mapping):
        raise QueryStateTransitionError("options must be a mapping")
    normalized = dict(options)
    max_tokens = normalized.get("max_tokens")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise QueryStateTransitionError("options.max_tokens must be a positive integer")
    for key in (
        "enable_prompt_caching",
        "use_global_cache_scope",
        "skip_global_cache_for_system_prompt",
        "skip_cache_write",
        "persistent_mode",
        "background_request",
    ):
        value = normalized.get(key)
        if value is not None and not isinstance(value, bool):
            raise QueryStateTransitionError(f"options.{key} must be a boolean")
    fallback_model = normalized.get("fallback_model")
    if fallback_model is not None:
        if not isinstance(fallback_model, str) or not fallback_model.strip():
            raise QueryStateTransitionError("options.fallback_model must be a non-empty string")
        normalized["fallback_model"] = fallback_model.strip()
    prompt_cache_ttl = normalized.get("prompt_cache_ttl")
    if prompt_cache_ttl is not None:
        if prompt_cache_ttl != "1h":
            raise QueryStateTransitionError("options.prompt_cache_ttl must be '1h'")
    thinking_enabled = normalized.get("thinking_enabled")
    if thinking_enabled is not None and not isinstance(thinking_enabled, bool):
        raise QueryStateTransitionError("options.thinking_enabled must be a boolean")
    for key in (
        "max_turns",
        "blocking_limit_tokens",
        "token_budget_total_tokens",
        "tool_result_budget_tokens",
        "thinking_budget_tokens",
    ):
        value = normalized.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            raise QueryStateTransitionError(f"options.{key} must be a positive integer")
    beta_headers = normalized.get("beta_headers")
    if beta_headers is not None:
        if not isinstance(beta_headers, Sequence) or isinstance(
            beta_headers, (str, bytes, bytearray)
        ):
            raise QueryStateTransitionError("options.beta_headers must be a sequence of strings")
        normalized["beta_headers"] = tuple(
            beta.strip()
            for beta in beta_headers
            if isinstance(beta, str) and beta.strip()
        )
    extra_body = normalized.get("extra_body")
    if extra_body is not None:
        if not isinstance(extra_body, Mapping):
            raise QueryStateTransitionError("options.extra_body must be an object")
        normalized["extra_body"] = dict(extra_body)
    context_management = normalized.get("context_management")
    if context_management is not None:
        normalized["context_management"] = _normalize_context_management(
            context_management
        )
    thinking = normalized.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, Mapping):
            raise QueryStateTransitionError("options.thinking must be an object")
        normalized["thinking"] = dict(thinking)
    effort = normalized.get("effort")
    if effort is not None:
        if isinstance(effort, str):
            effort = effort.strip().lower()
            if not is_valid_effort_level(effort):
                raise QueryStateTransitionError("options.effort must be a valid effort level")
            normalized["effort"] = effort
        elif type(effort) is not int or effort <= 0:
            raise QueryStateTransitionError(
                "options.effort must be a valid effort level or positive integer"
            )
    task_budget = normalized.get("task_budget")
    if task_budget is not None:
        if not isinstance(task_budget, Mapping):
            raise QueryStateTransitionError("options.task_budget must be an object")
        total = task_budget.get("total")
        if type(total) is not int or total <= 0:
            raise QueryStateTransitionError("options.task_budget.total must be a positive integer")
        normalized_task_budget: dict[str, Any] = {"type": "tokens", "total": total}
        remaining = task_budget.get("remaining")
        if remaining is not None:
            if type(remaining) is not int or remaining < 0:
                raise QueryStateTransitionError(
                    "options.task_budget.remaining must be a non-negative integer"
                )
            normalized_task_budget["remaining"] = remaining
        normalized["task_budget"] = normalized_task_budget
    cache_breakpoints = normalized.get("cache_breakpoints")
    if cache_breakpoints is not None:
        normalized["cache_breakpoints"] = _normalize_cache_breakpoints(cache_breakpoints)
    return normalized


def _normalize_context_management(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise QueryStateTransitionError("options.context_management must be an object")
    normalized = dict(raw)
    edits = raw.get("edits")
    if edits is not None:
        if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes, bytearray)):
            raise QueryStateTransitionError(
                "options.context_management.edits must be a sequence"
            )
        normalized["edits"] = tuple(
            _normalize_context_management_edit(edit, index=index)
            for index, edit in enumerate(edits)
        )
    return normalized


def _normalize_context_management_edit(
    raw: object,
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise QueryStateTransitionError(
            f"options.context_management.edits[{index}] must be an object"
        )
    normalized = dict(raw)
    edit_type = raw.get("type")
    if not isinstance(edit_type, str) or not edit_type.strip():
        raise QueryStateTransitionError(
            f"options.context_management.edits[{index}].type must be a non-empty string"
        )
    normalized["type"] = edit_type.strip()
    if edit_type == "clear_thinking_20251015":
        keep = raw.get("keep")
        if keep is not None:
            normalized["keep"] = _normalize_context_management_keep(
                keep,
                field_name=f"options.context_management.edits[{index}].keep",
                keep_type="thinking_turns",
            )
        return normalized
    if edit_type == "clear_tool_uses_20250919":
        trigger = raw.get("trigger")
        if trigger is not None:
            normalized["trigger"] = _normalize_context_management_counter(
                trigger,
                field_name=f"options.context_management.edits[{index}].trigger",
                allowed_types=("input_tokens", "tool_uses"),
            )
        keep = raw.get("keep")
        if keep is not None:
            normalized["keep"] = _normalize_context_management_keep(
                keep,
                field_name=f"options.context_management.edits[{index}].keep",
                keep_type="tool_uses",
            )
        clear_at_least = raw.get("clear_at_least")
        if clear_at_least is not None:
            normalized["clear_at_least"] = _normalize_context_management_counter(
                clear_at_least,
                field_name=f"options.context_management.edits[{index}].clear_at_least",
                allowed_types=("input_tokens",),
            )
        exclude_tools = raw.get("exclude_tools")
        if exclude_tools is not None:
            if not isinstance(exclude_tools, Sequence) or isinstance(
                exclude_tools, (str, bytes, bytearray)
            ):
                raise QueryStateTransitionError(
                    f"options.context_management.edits[{index}].exclude_tools must be a sequence of strings"
                )
            normalized["exclude_tools"] = tuple(
                tool.strip()
                for tool in exclude_tools
                if isinstance(tool, str) and tool.strip()
            )
        clear_tool_inputs = raw.get("clear_tool_inputs")
        if clear_tool_inputs is not None and not isinstance(clear_tool_inputs, bool):
            raise QueryStateTransitionError(
                f"options.context_management.edits[{index}].clear_tool_inputs must be a boolean"
            )
        return normalized
    return normalized


def _normalize_context_management_keep(
    raw: object,
    *,
    field_name: str,
    keep_type: str,
) -> str | dict[str, Any]:
    if raw == "all":
        return "all"
    return _normalize_context_management_counter(
        raw,
        field_name=field_name,
        allowed_types=(keep_type,),
    )


def _normalize_context_management_counter(
    raw: object,
    *,
    field_name: str,
    allowed_types: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise QueryStateTransitionError(f"{field_name} must be an object")
    counter_type = raw.get("type")
    if counter_type not in allowed_types:
        allowed = ", ".join(repr(value) for value in allowed_types)
        raise QueryStateTransitionError(f"{field_name}.type must be one of {allowed}")
    value = raw.get("value")
    if type(value) is not int or value <= 0:
        raise QueryStateTransitionError(f"{field_name}.value must be a positive integer")
    return {"type": counter_type, "value": value}


def _normalize_cache_breakpoints(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise QueryStateTransitionError("options.cache_breakpoints must be an object")
    normalized: dict[str, Any] = {}
    new_edits = _normalize_cache_edit_references(
        raw.get("new_edits"),
        field_name="options.cache_breakpoints.new_edits",
    )
    if new_edits:
        normalized["new_edits"] = new_edits
    pinned_raw = raw.get("pinned_edits")
    if pinned_raw is not None:
        if not isinstance(pinned_raw, Sequence) or isinstance(
            pinned_raw,
            (str, bytes, bytearray),
        ):
            raise QueryStateTransitionError(
                "options.cache_breakpoints.pinned_edits must be a sequence"
            )
        normalized_pinned: list[dict[str, Any]] = []
        for index, entry in enumerate(pinned_raw):
            if not isinstance(entry, Mapping):
                raise QueryStateTransitionError(
                    f"options.cache_breakpoints.pinned_edits[{index}] must be an object"
                )
            user_message_index = entry.get("user_message_index")
            if type(user_message_index) is not int or user_message_index < 0:
                raise QueryStateTransitionError(
                    f"options.cache_breakpoints.pinned_edits[{index}].user_message_index must be a non-negative integer"
                )
            refs = _normalize_cache_edit_references(
                entry.get("cache_references"),
                field_name=(
                    "options.cache_breakpoints.pinned_edits"
                    f"[{index}].cache_references"
                ),
            )
            normalized_pinned.append(
                {
                    "user_message_index": user_message_index,
                    "cache_references": refs,
                }
            )
        normalized["pinned_edits"] = tuple(normalized_pinned)
    skip_cache_write = raw.get("skip_cache_write")
    if skip_cache_write is not None:
        if not isinstance(skip_cache_write, bool):
            raise QueryStateTransitionError(
                "options.cache_breakpoints.skip_cache_write must be a boolean"
            )
        normalized["skip_cache_write"] = skip_cache_write
    return normalized


def _normalize_cache_edit_references(
    raw: object,
    *,
    field_name: str,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        raw = raw.get("edits")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise QueryStateTransitionError(f"{field_name} must be a sequence")
    refs: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        cache_reference: str | None = None
        if isinstance(item, str):
            cache_reference = item
        elif isinstance(item, Mapping):
            item_type = item.get("type")
            if item_type not in (None, "delete"):
                raise QueryStateTransitionError(
                    f"{field_name}[{index}].type must be 'delete'"
                )
            raw_cache_reference = item.get("cache_reference")
            if isinstance(raw_cache_reference, str):
                cache_reference = raw_cache_reference
        if not isinstance(cache_reference, str) or not cache_reference.strip():
            raise QueryStateTransitionError(
                f"{field_name}[{index}] must contain a non-empty cache_reference"
            )
        normalized = cache_reference.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        refs.append(normalized)
    return tuple(refs)


def _convert_messages_to_anthropic(
    messages: Sequence[Message],
    *,
    tool_search_enabled: bool = False,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    last_assistant_index = _last_payload_assistant_index(messages)
    for index, message in enumerate(messages):
        anthropic_message = _convert_message_to_anthropic(
            message,
            is_last_assistant=index == last_assistant_index,
            tool_search_enabled=tool_search_enabled,
        )
        if anthropic_message is None:
            continue
        converted.append(anthropic_message)
    return converted


def _last_payload_assistant_index(messages: Sequence[Message]) -> int | None:
    last_index: int | None = None
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage) and not bool(message.isVirtual):
            last_index = index
    return last_index


def _convert_message_to_anthropic(
    message: Message,
    *,
    is_last_assistant: bool = False,
    tool_search_enabled: bool = False,
) -> dict[str, Any] | None:
    if isinstance(message, UserMessage):
        if bool(message.isVirtual):
            return None
        content = message.message.content
        if isinstance(content, str):
            return {"role": "user", "content": content}
        blocks = []
        for block in content:
            normalized = _normalize_user_block_for_anthropic(
                block,
                tool_search_enabled=tool_search_enabled,
            )
            if normalized is not None:
                blocks.append(normalized)
        return {"role": "user", "content": blocks}

    if isinstance(message, AssistantMessage):
        if bool(message.isVirtual):
            return None
        blocks, removed_non_payload_blocks = _normalize_assistant_blocks_for_anthropic(
            message.message.content,
            strip_trailing_thinking=is_last_assistant,
        )
        if not blocks:
            if is_last_assistant and removed_non_payload_blocks:
                return {
                    "role": "assistant",
                    "content": [{"type": "text", "text": NO_CONTENT_MESSAGE}],
                }
            return None
        return {"role": "assistant", "content": blocks}

    return None


def _normalize_user_block_for_anthropic(
    block: object,
    *,
    tool_search_enabled: bool,
) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": _normalize_tool_result_content_for_anthropic(block.content),
            "is_error": block.is_error,
        }
    if not isinstance(block, Mapping):
        return None
    block_type = block.get("type")
    if block_type == "text":
        text = block.get("text", block.get("content"))
        if isinstance(text, str):
            return {"type": "text", "text": text}
        return None
    if block_type == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": str(block.get("tool_use_id", "")),
            "content": _normalize_tool_result_content_for_anthropic(
                block.get("content", "")
            ),
            "is_error": bool(block.get("is_error", False)),
        }
    if block_type == "tool_reference" and tool_search_enabled:
        return dict(block)
    return None


def _normalize_assistant_blocks_for_anthropic(
    content: Sequence[object],
    *,
    strip_trailing_thinking: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    blocks: list[dict[str, Any]] = []
    removed_non_payload_blocks = False
    for block in content:
        normalized, removed = _normalize_assistant_block_for_anthropic(block)
        if normalized is not None:
            blocks.append(normalized)
        if removed:
            removed_non_payload_blocks = True
    if strip_trailing_thinking:
        while blocks and _is_assistant_thinking_block(blocks[-1]):
            blocks.pop()
            removed_non_payload_blocks = True
    if blocks and all(_is_assistant_thinking_block(block) for block in blocks):
        return [], True
    return blocks, removed_non_payload_blocks


def _is_assistant_thinking_block(block: Mapping[str, object]) -> bool:
    return block.get("type") in _ASSISTANT_THINKING_BLOCK_TYPES


def _normalize_assistant_block_for_anthropic(
    block: object,
) -> tuple[dict[str, Any] | None, bool]:
    if isinstance(block, TextBlock):
        if block.text.strip():
            return {"type": "text", "text": block.text}, False
        return None, False
    if isinstance(block, ThinkingBlock):
        if not block.thinking and not block.signature:
            return None, True
        thinking_block = {"type": "thinking", "thinking": block.thinking}
        if block.signature:
            thinking_block["signature"] = block.signature
        return thinking_block, False
    if isinstance(block, ToolUseBlock):
        tool_use = {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
        if block.signature:
            tool_use["signature"] = block.signature
        return tool_use, False
    if isinstance(block, Mapping):
        return _normalize_assistant_mapping_block_for_anthropic(block)
    return None, True


def _normalize_assistant_mapping_block_for_anthropic(
    block: Mapping[str, object],
) -> tuple[dict[str, Any] | None, bool]:
    block_type = block.get("type")
    if not isinstance(block_type, str):
        return None, True
    if block_type in _ASSISTANT_THINKING_BLOCK_TYPES:
        return dict(block), False
    if block_type == "connector_text":
        return None, True
    if block_type == "text":
        text = block.get("text", block.get("content"))
        if isinstance(text, str) and text.strip():
            return {"type": "text", "text": text}, False
        return None, False
    if block_type == "tool_use":
        try:
            normalized_input = _normalize_tool_input(block.get("input", ""))
        except QueryStateTransitionError:
            return None, True
        tool_use = {
            "type": "tool_use",
            "id": str(block.get("id", "")),
            "name": str(block.get("name", "")),
            "input": dict(normalized_input),
        }
        signature = block.get("signature")
        if isinstance(signature, str) and signature:
            tool_use["signature"] = signature
        return tool_use, False
    return None, True


def _normalize_tool_result_content_for_anthropic(
    content: object,
) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(
        content,
        (str, bytes, bytearray),
    ):
        normalized_blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, Mapping):
                normalized_blocks.append(dict(item))
                continue
            if isinstance(item, TextBlock):
                normalized_blocks.append({"type": "text", "text": item.text})
                continue
            if isinstance(item, str):
                normalized_blocks.append({"type": "text", "text": item})
                continue
            return str(content)
        return normalized_blocks
    return str(content)


def _tool_search_enabled(tools: Sequence[Mapping[str, object]] | None) -> bool:
    if tools is None:
        return False
    for tool in tools:
        name = tool.get("name")
        if isinstance(name, str) and name == "ToolSearch":
            return True
    return False


def _build_anthropic_headers(
    *,
    api_key: str | None,
    auth_token: str | None,
    beta_headers: Sequence[str] | None = None,
    accept: str = "text/event-stream",
) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "accept": accept,
    }
    if api_key:
        headers["x-api-key"] = api_key
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    joined_betas = ",".join(_merge_beta_headers(beta_headers))
    if joined_betas:
        headers["anthropic-beta"] = joined_betas
    return headers


def _build_openai_headers(*, api_key: str, accept: str = "text/event-stream") -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": accept,
        "authorization": f"Bearer {api_key}",
    }


def _openai_chat_should_include_reasoning_content(
    model: str,
    base_url: str | None,
) -> bool:
    if get_model_provider() == "deepseek":
        return True
    if "deepseek" in (model or "").lower():
        return True
    try:
        hostname = urlparse(base_url or "").hostname or ""
    except ValueError:
        hostname = ""
    return hostname.endswith("deepseek.com") or ".deepseek.com" in hostname


def _openai_chat_completions_url(base_url: str) -> str:
    normalized = (base_url or "https://api.openai.com").rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _resolve_anthropic_request_overrides(
    model: str,
    options: Optional[Mapping[str, Any]],
    *,
    max_tokens: int,
    messages: Sequence[Message] = (),
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    extra_body = _merge_mapping(
        _load_json_object_env("CLAUDE_CODE_EXTRA_BODY"),
        _extract_extra_body(options),
    )
    output_config = _extract_output_config(extra_body)
    _configure_effort_request_params(
        model,
        options,
        output_config=output_config,
        extra_body=extra_body,
    )
    _configure_task_budget_request_params(
        options,
        output_config=output_config,
    )
    if output_config:
        extra_body["output_config"] = output_config
    elif "output_config" in extra_body:
        extra_body.pop("output_config", None)
    thinking_payload = _resolve_thinking_request_payload(
        model,
        options,
        max_tokens=max_tokens,
    )
    context_management_payload = _resolve_context_management_request_payload(
        options,
        extra_body=extra_body,
        messages=messages,
    )
    if context_management_payload is not None and "context_management" not in extra_body:
        extra_body["context_management"] = context_management_payload
    return extra_body, thinking_payload


def _resolve_anthropic_beta_headers(
    model: str,
    options: Optional[Mapping[str, Any]],
    *,
    messages: Sequence[Message] = (),
) -> tuple[str, ...]:
    beta_headers = list(_extract_beta_headers(options))
    if _extract_use_global_cache_scope(options):
        beta_headers.append(_PROMPT_CACHING_SCOPE_BETA_HEADER)
    extra_body = _merge_mapping(
        _load_json_object_env("CLAUDE_CODE_EXTRA_BODY"),
        _extract_extra_body(options),
    )
    output_config = _extract_output_config(extra_body)
    effort = None if options is None else options.get("effort")
    if model_supports_effort(model) and "effort" not in output_config:
        if effort is None or isinstance(effort, str):
            beta_headers.append(_EFFORT_BETA_HEADER)
    if _extract_task_budget(options) is not None and "task_budget" not in output_config:
        beta_headers.append(_TASK_BUDGETS_BETA_HEADER)
    if (
        _resolve_context_management_request_payload(
            options,
            extra_body=extra_body,
            messages=messages,
        )
        is not None
    ):
        beta_headers.append(_CONTEXT_MANAGEMENT_BETA_HEADER)
    return _merge_beta_headers(beta_headers)


def _configure_effort_request_params(
    model: str,
    options: Optional[Mapping[str, Any]],
    *,
    output_config: dict[str, Any],
    extra_body: dict[str, Any],
) -> None:
    if not model_supports_effort(model) or "effort" in output_config:
        return
    effort = None if options is None else options.get("effort")
    if isinstance(effort, str):
        output_config["effort"] = effort
        return
    if type(effort) is int:
        anthropic_internal = dict(extra_body.get("anthropic_internal", {}))
        anthropic_internal["effort_override"] = effort
        extra_body["anthropic_internal"] = anthropic_internal


def _configure_task_budget_request_params(
    options: Optional[Mapping[str, Any]],
    *,
    output_config: dict[str, Any],
) -> None:
    task_budget = _extract_task_budget(options)
    if task_budget is None or "task_budget" in output_config:
        return
    output_config["task_budget"] = task_budget


def _resolve_thinking_request_payload(
    model: str,
    options: Optional[Mapping[str, Any]],
    *,
    max_tokens: int,
) -> Optional[dict[str, Any]]:
    explicit = None if options is None else options.get("thinking")
    if isinstance(explicit, Mapping):
        return dict(explicit)

    thinking_enabled = bool(options.get("thinking_enabled")) if options is not None else False
    if not thinking_enabled:
        return None
    if model_supports_adaptive_thinking(model) and not _env_truthy(
        "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"
    ):
        return {"type": "adaptive"}

    thinking_budget = _extract_optional_positive_int(options, "thinking_budget_tokens")
    if thinking_budget is None:
        thinking_budget = _env_positive_int("MAX_THINKING_TOKENS")
    if thinking_budget is None:
        thinking_budget = max_tokens - 1
    thinking_budget = max(1, min(max_tokens - 1, thinking_budget))
    return {"type": "enabled", "budget_tokens": thinking_budget}


def _resolve_context_management_request_payload(
    options: Optional[Mapping[str, Any]],
    *,
    extra_body: Mapping[str, Any] | None = None,
    messages: Sequence[Message] = (),
    current_time_ms: int | None = None,
) -> dict[str, Any] | None:
    resolved: dict[str, Any] | None = None
    if isinstance(extra_body, Mapping):
        raw_extra_context_management = extra_body.get("context_management")
        if isinstance(raw_extra_context_management, Mapping):
            resolved = _clone_json_mapping(raw_extra_context_management)
    if resolved is None and options is not None:
        raw = options.get("context_management")
        if isinstance(raw, Mapping):
            resolved = _clone_json_mapping(raw)
    auto_latched_context_management = _derive_latched_context_management(
        messages,
        current_time_ms=current_time_ms,
    )
    if auto_latched_context_management is None:
        return resolved
    if resolved is None:
        return auto_latched_context_management
    return _merge_context_management_payloads(
        resolved,
        auto_latched_context_management,
    )


def _derive_latched_context_management(
    messages: Sequence[Message],
    *,
    current_time_ms: int | None = None,
) -> dict[str, Any] | None:
    if not _session_contains_thinking_blocks(messages):
        return None
    last_completion_ms = _latest_assistant_completion_timestamp_ms(messages)
    if last_completion_ms is None:
        return None
    now_ms = (
        current_time_ms
        if type(current_time_ms) is int and current_time_ms >= 0
        else int(time.time() * 1000)
    )
    if now_ms - last_completion_ms <= _THINKING_CLEAR_LATCH_AFTER_MS:
        return None
    return {"edits": [{"type": "clear_thinking_20251015"}]}


def _session_contains_thinking_blocks(messages: Sequence[Message]) -> bool:
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.message.content:
            if isinstance(block, ThinkingBlock):
                return True
            if isinstance(block, Mapping) and block.get("type") == "thinking":
                return True
    return False


def _latest_assistant_completion_timestamp_ms(
    messages: Sequence[Message],
) -> int | None:
    latest: int | None = None
    for message in messages:
        if not isinstance(message, AssistantMessage) or message.isApiErrorMessage:
            continue
        timestamp_ms = _parse_iso_timestamp_ms(message.timestamp)
        if timestamp_ms is None:
            continue
        latest = timestamp_ms if latest is None else max(latest, timestamp_ms)
    return latest


def _parse_iso_timestamp_ms(value: str | None) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _merge_context_management_payloads(
    explicit: Mapping[str, Any],
    derived: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _clone_json_mapping(explicit)
    explicit_edits = _clone_context_management_edits(merged.get("edits"))
    derived_edits = _clone_context_management_edits(derived.get("edits"))
    if not derived_edits:
        return merged
    seen_types = {
        edit_type
        for edit in explicit_edits
        if (edit_type := _context_management_edit_type(edit)) is not None
    }
    for edit in derived_edits:
        edit_type = _context_management_edit_type(edit)
        if edit_type is not None and edit_type in seen_types:
            continue
        explicit_edits.append(edit)
        if edit_type is not None:
            seen_types.add(edit_type)
    merged["edits"] = explicit_edits
    return merged


def _clone_context_management_edits(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    edits: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            edits.append(_clone_json_mapping(item))
    return edits


def _context_management_edit_type(edit: Mapping[str, Any]) -> str | None:
    raw_type = edit.get("type")
    if not isinstance(raw_type, str):
        return None
    normalized = raw_type.strip()
    return normalized or None


def _clone_json_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _clone_json_value(value)
        for key, value in raw.items()
    }


def _clone_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _clone_json_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_clone_json_value(item) for item in value]
    return value


def _extract_beta_headers(options: Optional[Mapping[str, Any]]) -> tuple[str, ...]:
    if options is None:
        return ()
    raw = options.get("beta_headers")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(
        beta.strip()
        for beta in raw
        if isinstance(beta, str) and beta.strip()
    )


def _extract_extra_body(options: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    raw = None if options is None else options.get("extra_body")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _extract_output_config(extra_body: Mapping[str, Any]) -> dict[str, Any]:
    raw = extra_body.get("output_config")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _extract_task_budget(options: Optional[Mapping[str, Any]]) -> dict[str, Any] | None:
    if options is None:
        return None
    raw = options.get("task_budget")
    if not isinstance(raw, Mapping):
        return None
    total = raw.get("total")
    if type(total) is not int or total <= 0:
        return None
    task_budget: dict[str, Any] = {"type": "tokens", "total": total}
    remaining = raw.get("remaining")
    if type(remaining) is int and remaining >= 0:
        task_budget["remaining"] = remaining
    return task_budget


def _merge_beta_headers(*groups: Sequence[str] | None) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for header in group or ():
            if not isinstance(header, str):
                continue
            normalized = header.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _load_json_object_env(name: str) -> dict[str, Any]:
    raw = os.environ.get(name)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _env_positive_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name)
    return isinstance(raw, str) and raw.strip().lower() in {"1", "true", "yes", "on"}


def _assistant_message_from_non_stream_payload(payload: object) -> AssistantMessage:
    if not isinstance(payload, Mapping):
        raise RuntimeError("Anthropic-compatible non-stream response must be an object")
    content = payload.get("content")
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        raise RuntimeError("Anthropic-compatible non-stream response is missing content")
    blocks: list[object] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                blocks.append(TextBlock(text))
        elif block_type == "thinking":
            thinking = block.get("thinking")
            signature = block.get("signature")
            if isinstance(thinking, str):
                blocks.append(
                    ThinkingBlock(
                        thinking,
                        signature=signature if isinstance(signature, str) else None,
                    )
                )
        elif block_type == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            signature = block.get("signature")
            if isinstance(tool_id, str) and isinstance(name, str):
                blocks.append(
                    ToolUseBlock(
                        id=tool_id,
                        name=name,
                        input=_normalize_tool_input(block.get("input")),
                        signature=signature if isinstance(signature, str) else "",
                    )
                )
    if not blocks:
        blocks.append(TextBlock(NO_CONTENT_MESSAGE))
    usage = payload.get("usage")
    return AssistantMessage(
        message=AssistantPayload(
            id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            role="assistant",
            stop_reason=payload.get("stop_reason")
            if isinstance(payload.get("stop_reason"), str)
            else None,
            stop_sequence=payload.get("stop_sequence")
            if isinstance(payload.get("stop_sequence"), str)
            else None,
            type="message",
            content=tuple(blocks),
            usage=dict(usage) if isinstance(usage, Mapping) else _default_usage(),
        ),
        signature=payload.get("signature")
        if isinstance(payload.get("signature"), str)
        else None,
    )


def _assistant_message_from_openai_chat_payload(payload: object) -> AssistantMessage:
    if not isinstance(payload, Mapping):
        raise RuntimeError("OpenAI-compatible non-stream response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        raise RuntimeError("OpenAI-compatible non-stream response is missing choices")
    choice = next((item for item in choices if isinstance(item, Mapping)), None)
    if choice is None:
        raise RuntimeError("OpenAI-compatible non-stream response has no choice")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise RuntimeError("OpenAI-compatible non-stream choice is missing message")

    blocks: list[object] = []
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        blocks.append(ThinkingBlock(reasoning_content))
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append(TextBlock(content))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(
        tool_calls,
        (str, bytes, bytearray),
    ):
        for tool_call in tool_calls:
            block = _tool_use_block_from_openai_tool_call(tool_call)
            if block is not None:
                blocks.append(block)
    if not blocks:
        blocks.append(TextBlock(NO_CONTENT_MESSAGE))
    usage = _openai_usage_to_internal_usage(payload.get("usage"))
    return AssistantMessage(
        message=AssistantPayload(
            id=payload.get("id") if isinstance(payload.get("id"), str) else None,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            role="assistant",
            stop_reason=_openai_finish_reason_to_stop_reason(
                choice.get("finish_reason")
            ),
            stop_sequence="",
            type="message",
            content=tuple(blocks),
            usage=usage,
        )
    )


def _iter_openai_chat_stream_events(
    chunks: Iterable[Mapping[str, Any]],
) -> Iterable[dict[str, Any]]:
    message_started = False
    message_id = _uuid()
    model = SYNTHETIC_MODEL
    role = "assistant"
    usage: dict[str, Any] = {}
    finish_reason: object = "stop"
    next_block_index = 0
    thinking_block_open = False
    text_block_open = False
    emitted_content_blocks = False
    tool_calls: dict[int, dict[str, str]] = {}

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        error_payload = chunk.get("error")
        if isinstance(error_payload, Mapping):
            yield {"type": "error", "error": dict(error_payload)}
            return
        if isinstance(chunk.get("id"), str):
            message_id = str(chunk["id"])
        if isinstance(chunk.get("model"), str):
            model = str(chunk["model"])
        chunk_usage = _openai_usage_to_internal_usage(chunk.get("usage"))
        if chunk_usage:
            usage = _merge_mapping(usage, chunk_usage)

        choices = chunk.get("choices")
        if not isinstance(choices, Sequence) or isinstance(
            choices,
            (str, bytes, bytearray),
        ):
            continue
        if not choices:
            continue
        choice = next((item for item in choices if isinstance(item, Mapping)), None)
        if choice is None:
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            delta = {}
        if isinstance(delta.get("role"), str):
            role = str(delta["role"])

        if not message_started:
            message_started = True
            yield {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "model": model,
                    "role": role,
                    "stop_reason": None,
                    "stop_sequence": "",
                    "type": "message",
                    "usage": usage or _default_usage(),
                },
            }

        reasoning_content = delta.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            if text_block_open:
                yield {"type": "content_block_stop", "index": next_block_index}
                next_block_index += 1
                text_block_open = False
            if not thinking_block_open:
                thinking_block_open = True
                emitted_content_blocks = True
                yield {
                    "type": "content_block_start",
                    "index": next_block_index,
                    "content_block": {"type": "thinking", "thinking": ""},
                }
            yield {
                "type": "content_block_delta",
                "index": next_block_index,
                "delta": {
                    "type": "thinking_delta",
                    "thinking": reasoning_content,
                },
            }

        content = delta.get("content")
        if isinstance(content, str) and content:
            if thinking_block_open:
                yield {"type": "content_block_stop", "index": next_block_index}
                next_block_index += 1
                thinking_block_open = False
            if not text_block_open:
                text_block_open = True
                emitted_content_blocks = True
                yield {
                    "type": "content_block_start",
                    "index": next_block_index,
                    "content_block": {"type": "text", "text": ""},
                }
            yield {
                "type": "content_block_delta",
                "index": next_block_index,
                "delta": {"type": "text_delta", "text": content},
            }

        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, Sequence) and not isinstance(
            raw_tool_calls,
            (str, bytes, bytearray),
        ):
            for raw_tool_call in raw_tool_calls:
                _accumulate_openai_tool_call_delta(tool_calls, raw_tool_call)

        if choice.get("finish_reason") is not None:
            finish_reason = choice.get("finish_reason")

    if not message_started:
        yield {
            "type": "message_start",
            "message": {
                "id": message_id,
                "model": model,
                "role": role,
                "stop_reason": None,
                "stop_sequence": "",
                "type": "message",
                "usage": usage or _default_usage(),
            },
        }

    if thinking_block_open:
        yield {"type": "content_block_stop", "index": next_block_index}
        next_block_index += 1

    if text_block_open:
        yield {"type": "content_block_stop", "index": next_block_index}
        next_block_index += 1

    for index in sorted(tool_calls):
        tool_call = tool_calls[index]
        tool_id = tool_call.get("id") or f"call_{index}"
        name = tool_call.get("name") or "unknown"
        arguments = tool_call.get("arguments") or "{}"
        emitted_content_blocks = True
        yield {
            "type": "content_block_start",
            "index": next_block_index,
            "content_block": {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": "",
            },
        }
        if arguments:
            yield {
                "type": "content_block_delta",
                "index": next_block_index,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            }
        yield {"type": "content_block_stop", "index": next_block_index}
        next_block_index += 1

    if not emitted_content_blocks:
        yield {
            "type": "content_block_start",
            "index": next_block_index,
            "content_block": {"type": "text", "text": ""},
        }
        yield {
            "type": "content_block_delta",
            "index": next_block_index,
            "delta": {"type": "text_delta", "text": NO_CONTENT_MESSAGE},
        }
        yield {"type": "content_block_stop", "index": next_block_index}

    yield {
        "type": "message_delta",
        "usage": usage,
        "delta": {
            "stop_reason": _openai_finish_reason_to_stop_reason(finish_reason)
        },
    }
    yield {"type": "message_stop"}


def _accumulate_openai_tool_call_delta(
    tool_calls: dict[int, dict[str, str]],
    raw_tool_call: object,
) -> None:
    if not isinstance(raw_tool_call, Mapping):
        return
    raw_index = raw_tool_call.get("index")
    index = raw_index if isinstance(raw_index, int) else len(tool_calls)
    accumulated = tool_calls.setdefault(index, {})
    tool_id = raw_tool_call.get("id")
    if isinstance(tool_id, str) and tool_id:
        accumulated["id"] = tool_id
    raw_type = raw_tool_call.get("type")
    if isinstance(raw_type, str) and raw_type:
        accumulated["type"] = raw_type
    function_payload = raw_tool_call.get("function")
    if not isinstance(function_payload, Mapping):
        return
    name = function_payload.get("name")
    if isinstance(name, str) and name:
        accumulated["name"] = accumulated.get("name", "") + name
    arguments = function_payload.get("arguments")
    if isinstance(arguments, str) and arguments:
        accumulated["arguments"] = accumulated.get("arguments", "") + arguments


def _tool_use_block_from_openai_tool_call(tool_call: object) -> ToolUseBlock | None:
    if not isinstance(tool_call, Mapping):
        return None
    tool_id = tool_call.get("id")
    function_payload = tool_call.get("function")
    if not isinstance(tool_id, str) or not isinstance(function_payload, Mapping):
        return None
    name = function_payload.get("name")
    if not isinstance(name, str):
        return None
    return ToolUseBlock(
        id=tool_id,
        name=name,
        input=_normalize_tool_input(function_payload.get("arguments", "")),
    )


def _openai_usage_to_internal_usage(raw_usage: object) -> dict[str, Any]:
    if not isinstance(raw_usage, Mapping):
        return {}
    prompt_tokens = raw_usage.get("prompt_tokens")
    completion_tokens = raw_usage.get("completion_tokens")
    usage = _default_usage()
    if isinstance(prompt_tokens, int):
        usage["input_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        usage["output_tokens"] = completion_tokens
    return usage


def _openai_finish_reason_to_stop_reason(raw: object) -> str:
    if raw == "tool_calls":
        return "tool_use"
    if raw == "length":
        return "max_tokens"
    if raw == "stop" or raw is None:
        return "end_turn"
    return str(raw)


def _iter_sse_payloads(response: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    current_event = "message"
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            payload = _parse_sse_frame(current_event, data_lines)
            current_event = "message"
            data_lines = []
            if payload is not None:
                yield payload
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())

    payload = _parse_sse_frame(current_event, data_lines)
    if payload is not None:
        yield payload


def _parse_sse_frame(
    event_name: str, data_lines: Sequence[str]
) -> dict[str, Any] | None:
    if not data_lines:
        return None
    raw_data = "\n".join(data_lines)
    if raw_data == "[DONE]" or event_name == "ping":
        return None
    payload = json.loads(raw_data)
    if not isinstance(payload, dict):
        raise QueryStateTransitionError("SSE payload must decode to an object")
    payload_type = payload.get("type")
    if isinstance(payload_type, str) and payload_type:
        return payload
    payload["type"] = event_name
    return payload


async def _collect_tool_updates(
    assistant_messages: Sequence[AssistantMessage],
    *,
    tool_executor: Optional[ToolExecutor],
    timeout_seconds: float,
) -> ToolBatchResult:
    if tool_executor is None:
        return _synthetic_tool_batch(assistant_messages, error=None)

    try:
        updates = await asyncio.wait_for(
            _consume_tool_updates(tool_executor.run(assistant_messages)),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return _synthetic_tool_batch(
            assistant_messages,
            error="tool executor timed out",
        )
    except Exception as exc:
        return _synthetic_tool_batch(assistant_messages, error=str(exc))

    tool_result_ids = _tool_result_ids_from_updates(updates)
    updated_context: Optional[Mapping[str, Any]] = None
    synthesized: list[ToolExecutionUpdate] = []
    for message in yieldMissingToolResultBlocks(
        assistant_messages,
        "Synthetic tool result: tool execution unavailable in single-turn REPL runtime",
    ):
        first_block = message.message.content[0]
        if not isinstance(first_block, ToolResultBlock):
            continue
        tool_use_id = first_block.tool_use_id
        if tool_use_id in tool_result_ids:
            continue
        synthesized.append(ToolExecutionUpdate(message=message))

    for update in (*updates, *tuple(synthesized)):
        updated_context = _record_tool_update(update, [], [], updated_context)

    return ToolBatchResult(
        updates=tuple((*updates, *tuple(synthesized))),
        updated_context=updated_context,
        used_fallback=bool(synthesized),
    )


async def _consume_tool_updates(
    updates: AsyncGenerator[ToolExecutionUpdate, None],
) -> tuple[ToolExecutionUpdate, ...]:
    collected: list[ToolExecutionUpdate] = []
    async for update in updates:
        collected.append(update)
    return tuple(collected)


def _synthetic_tool_batch(
    assistant_messages: Sequence[AssistantMessage],
    *,
    error: Optional[str],
) -> ToolBatchResult:
    reason = "Synthetic tool result: " + (
        error or "tool execution unavailable in single-turn REPL runtime"
    )
    updates = tuple(
        ToolExecutionUpdate(message=message)
        for message in yieldMissingToolResultBlocks(assistant_messages, reason)
    )
    return ToolBatchResult(
        updates=updates,
        used_fallback=True,
        error=error,
    )


async def _close_tool_updates(
    updates: AsyncGenerator[ToolExecutionUpdate, None],
) -> None:
    aclose = getattr(updates, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except RuntimeError:
            return


def _missing_tool_updates(
    assistant_messages: Sequence[AssistantMessage],
    *,
    tool_result_ids: frozenset[str] | set[str],
    reason: str,
) -> tuple[ToolExecutionUpdate, ...]:
    synthesized: list[ToolExecutionUpdate] = []
    for message in yieldMissingToolResultBlocks(assistant_messages, reason):
        first_block = message.message.content[0]
        if not isinstance(first_block, ToolResultBlock):
            continue
        if first_block.tool_use_id in tool_result_ids:
            continue
        synthesized.append(ToolExecutionUpdate(message=message))
    return tuple(synthesized)


def _tool_result_ids_from_updates(
    updates: Sequence[ToolExecutionUpdate],
) -> frozenset[str]:
    found: set[str] = set()
    for update in updates:
        message = update.message
        if not isinstance(message, UserMessage):
            continue
        content = message.message.content
        if not isinstance(content, tuple):
            continue
        for block in content:
            if isinstance(block, ToolResultBlock):
                found.add(block.tool_use_id)
    return frozenset(found)


def _build_echo_reply(messages: Sequence[Message], *, prefix: str) -> str:
    latest_user_text = ""
    for message in reversed(messages):
        if not isinstance(message, UserMessage):
            continue
        content = message.message.content
        if isinstance(content, str):
            latest_user_text = content
            break
        rendered_blocks: list[str] = []
        for block in content:
            if isinstance(block, ToolResultBlock):
                rendered_blocks.append(str(block.content))
        if rendered_blocks:
            latest_user_text = " ".join(rendered_blocks).strip()
            break
    if latest_user_text:
        return f"{prefix}: {latest_user_text}"
    return f"{prefix}: Ready."


def _chunk_text(text: str, *, chunk_size: int = 24) -> tuple[str, ...]:
    if not text:
        return ("",)
    return tuple(
        text[index : index + chunk_size] for index in range(0, len(text), chunk_size)
    )
