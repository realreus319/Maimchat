from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from typing import (
    Any,
    AsyncGenerator,
    AsyncIterable,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Union,
)


BASH_TOOL_NAME = "Bash"
REJECT_MESSAGE = "User rejected tool use"
STREAMING_FALLBACK_RESULT = "Streaming fallback - tool execution discarded"
_TRUNCATED_SUMMARY_SUFFIX = "\u2026"


def _identity_validator(raw_input: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
    return (True, raw_input)


def _default_interrupt_behavior() -> str:
    return "block"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: Mapping[str, Any]
    type: str = "tool_use"


@dataclass(frozen=True)
class AssistantToolUseMessage:
    uuid: str
    tool_use_ids: tuple[str, ...]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


@dataclass(frozen=True)
class UserMessage:
    content: tuple[ToolResultBlock, ...]
    tool_use_result: Optional[str] = None
    source_tool_assistant_uuid: Optional[str] = None
    type: str = "user"


@dataclass(frozen=True)
class ProgressMessage:
    tool_use_id: str
    parent_tool_use_id: str
    data: Mapping[str, Any]
    type: str = "progress"


Message = Union[UserMessage, ProgressMessage, Any]


@dataclass(frozen=True)
class ContextModifier:
    tool_use_id: str
    modify_context: Callable[["ToolUseContext"], "ToolUseContext"]


@dataclass(frozen=True)
class MessageUpdateLazy:
    message: Message
    context_modifier: Optional[ContextModifier] = None


@dataclass(frozen=True)
class MessageUpdate:
    message: Optional[Message] = None
    new_context: Optional["ToolUseContext"] = None


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    aliases: tuple[str, ...] = ()
    validate_input: Callable[[Mapping[str, Any]], tuple[bool, Mapping[str, Any]]] = (
        _identity_validator
    )
    is_concurrency_safe: Callable[[Mapping[str, Any]], bool] = lambda _input: False
    interrupt_behavior: Callable[[], str] = _default_interrupt_behavior


@dataclass(frozen=True)
class Batch:
    is_concurrency_safe: bool
    blocks: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ToolSchedulerOptions:
    tools: tuple[ToolDefinition, ...]


class AbortSignal:
    def __init__(self) -> None:
        self.aborted = False
        self.reason: Any = None
        self._callbacks: list[tuple[Callable[[], None], bool]] = []

    def add_callback(self, callback: Callable[[], None], *, once: bool = False) -> None:
        if self.aborted:
            callback()
            return
        self._callbacks.append((callback, once))

    def _fire(self) -> None:
        callbacks = list(self._callbacks)
        if callbacks:
            self._callbacks = [entry for entry in self._callbacks if not entry[1]]
        for callback, _ in callbacks:
            callback()


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Any = None) -> None:
        if self.signal.aborted:
            return
        self.signal.aborted = True
        self.signal.reason = reason
        self.signal._fire()


def create_child_abort_controller(parent: AbortController) -> AbortController:
    child = AbortController()

    def _forward_abort() -> None:
        child.abort(parent.signal.reason)

    parent.signal.add_callback(_forward_abort, once=True)
    return child


@dataclass(frozen=True)
class ToolUseContext:
    options: ToolSchedulerOptions
    state: Mapping[str, Any] = field(default_factory=dict)
    abort_controller: AbortController = field(default_factory=AbortController)
    in_progress_tool_use_ids: frozenset[str] = field(default_factory=frozenset)
    has_interruptible_tool_in_progress: bool = False

    def set_in_progress_tool_use_ids(
        self,
        updater: Callable[[set[str]], Iterable[str]],
    ) -> "ToolUseContext":
        updated = frozenset(updater(set(self.in_progress_tool_use_ids)))
        return replace(self, in_progress_tool_use_ids=updated)

    def set_has_interruptible_tool_in_progress(self, value: bool) -> "ToolUseContext":
        return replace(self, has_interruptible_tool_in_progress=value)

    def with_state(self, **changes: Any) -> "ToolUseContext":
        next_state = dict(self.state)
        next_state.update(changes)
        return replace(self, state=next_state)


RunToolUseFn = Callable[
    [ToolCall, AssistantToolUseMessage, Any, ToolUseContext],
    AsyncIterable[MessageUpdateLazy],
]


def get_max_tool_use_concurrency() -> int:
    raw = os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "")
    try:
        value = int(raw, 10)
    except ValueError:
        value = 0
    return value or 10


def create_tool_result_message(
    tool_use_id: str,
    content: str,
    *,
    is_error: bool = False,
    tool_use_result: Optional[str] = None,
    source_tool_assistant_uuid: Optional[str] = None,
) -> UserMessage:
    return UserMessage(
        content=(
            ToolResultBlock(
                tool_use_id=tool_use_id,
                content=content,
                is_error=is_error,
            ),
        ),
        tool_use_result=tool_use_result,
        source_tool_assistant_uuid=source_tool_assistant_uuid,
    )


def tool_matches_name(tool: ToolDefinition, name: str) -> bool:
    return tool.name == name or name in tool.aliases


def find_tool_by_name(
    tools: Sequence[ToolDefinition],
    name: str,
) -> Optional[ToolDefinition]:
    for tool in tools:
        if tool_matches_name(tool, name):
            return tool
    return None


def find_assistant_message(
    tool_use_id: str,
    assistant_messages: Sequence[AssistantToolUseMessage],
) -> AssistantToolUseMessage:
    for assistant_message in assistant_messages:
        if tool_use_id in assistant_message.tool_use_ids:
            return assistant_message
    raise KeyError(f"No assistant message found for tool use id {tool_use_id!r}")


def partition_tool_calls(
    tool_use_messages: Sequence[ToolCall],
    tool_use_context: ToolUseContext,
) -> tuple[Batch, ...]:
    batches: list[Batch] = []
    current_read_batch: list[ToolCall] = []
    for tool_use in tool_use_messages:
        tool = find_tool_by_name(tool_use_context.options.tools, tool_use.name)
        is_concurrency_safe = False
        if tool is not None:
            parsed_success, parsed_input = tool.validate_input(tool_use.input)
            if parsed_success:
                try:
                    is_concurrency_safe = bool(tool.is_concurrency_safe(parsed_input))
                except Exception:
                    is_concurrency_safe = False
        if is_concurrency_safe:
            current_read_batch.append(tool_use)
            continue
        if current_read_batch:
            batches.append(
                Batch(is_concurrency_safe=True, blocks=tuple(current_read_batch))
            )
            current_read_batch = []
        batches.append(Batch(is_concurrency_safe=False, blocks=(tool_use,)))
    if current_read_batch:
        batches.append(
            Batch(is_concurrency_safe=True, blocks=tuple(current_read_batch))
        )
    return tuple(batches)


def mark_tool_use_as_complete(
    tool_use_context: ToolUseContext, tool_use_id: str
) -> ToolUseContext:
    return tool_use_context.set_in_progress_tool_use_ids(
        lambda prev: set(prev) - {tool_use_id}
    )


async def run_tools(
    tool_use_messages: Sequence[ToolCall],
    assistant_messages: Sequence[AssistantToolUseMessage],
    can_use_tool: Any,
    tool_use_context: ToolUseContext,
    run_tool_use: RunToolUseFn,
) -> AsyncGenerator[MessageUpdate, None]:
    current_context = tool_use_context
    for batch in partition_tool_calls(tool_use_messages, current_context):
        if batch.is_concurrency_safe:
            queued_context_modifiers: dict[
                str, list[Callable[[ToolUseContext], ToolUseContext]]
            ] = {}
            async for update in run_tools_concurrently(
                batch.blocks,
                assistant_messages,
                can_use_tool,
                current_context,
                run_tool_use,
            ):
                if update.context_modifier is not None:
                    queued_context_modifiers.setdefault(
                        update.context_modifier.tool_use_id, []
                    ).append(update.context_modifier.modify_context)
                yield MessageUpdate(message=update.message, new_context=current_context)
            for block in batch.blocks:
                for modifier in queued_context_modifiers.get(block.id, ()):
                    current_context = modifier(current_context)
            yield MessageUpdate(new_context=current_context)
            continue
        async for update in run_tools_serially(
            batch.blocks,
            assistant_messages,
            can_use_tool,
            current_context,
            run_tool_use,
        ):
            if update.new_context is not None:
                current_context = update.new_context
            yield MessageUpdate(message=update.message, new_context=current_context)


async def run_tools_serially(
    tool_use_messages: Sequence[ToolCall],
    assistant_messages: Sequence[AssistantToolUseMessage],
    can_use_tool: Any,
    tool_use_context: ToolUseContext,
    run_tool_use: RunToolUseFn,
) -> AsyncGenerator[MessageUpdate, None]:
    current_context = tool_use_context
    for tool_use in tool_use_messages:
        current_context = current_context.set_in_progress_tool_use_ids(
            lambda prev, tool_id=tool_use.id: set(prev) | {tool_id}
        )
        async for update in run_tool_use(
            tool_use,
            find_assistant_message(tool_use.id, assistant_messages),
            can_use_tool,
            current_context,
        ):
            if update.context_modifier is not None:
                current_context = update.context_modifier.modify_context(
                    current_context
                )
            yield MessageUpdate(message=update.message, new_context=current_context)
        current_context = mark_tool_use_as_complete(current_context, tool_use.id)


async def run_tools_concurrently(
    tool_use_messages: Sequence[ToolCall],
    assistant_messages: Sequence[AssistantToolUseMessage],
    can_use_tool: Any,
    tool_use_context: ToolUseContext,
    run_tool_use: RunToolUseFn,
) -> AsyncGenerator[MessageUpdateLazy, None]:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    semaphore = asyncio.Semaphore(get_max_tool_use_concurrency())
    tasks: list[asyncio.Task[None]] = []

    async def _pump(tool_use: ToolCall) -> None:
        nonlocal tool_use_context
        tool_use_context = tool_use_context.set_in_progress_tool_use_ids(
            lambda prev, tool_id=tool_use.id: set(prev) | {tool_id}
        )
        try:
            async with semaphore:
                async for update in run_tool_use(
                    tool_use,
                    find_assistant_message(tool_use.id, assistant_messages),
                    can_use_tool,
                    tool_use_context,
                ):
                    await queue.put(("update", update))
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            tool_use_context = mark_tool_use_as_complete(tool_use_context, tool_use.id)
            await queue.put(("done", tool_use.id))

    for tool_use in tool_use_messages:
        tasks.append(asyncio.create_task(_pump(tool_use)))

    remaining = len(tasks)
    try:
        while remaining > 0:
            kind, payload = await queue.get()
            if kind == "update":
                yield payload
                continue
            if kind == "error":
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise payload
            remaining -= 1
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


def _message_type(message: Any) -> str | None:
    value = getattr(message, "type", None)
    return value if isinstance(value, str) and value else None


def _tool_result_blocks(message: Any) -> tuple[Any, ...]:
    content = getattr(message, "content", None)
    if content is None:
        content = getattr(getattr(message, "message", None), "content", None)
    if not isinstance(content, tuple):
        return ()
    blocks: list[Any] = []
    for block in content:
        if getattr(block, "type", None) != "tool_result":
            continue
        if getattr(block, "tool_use_id", None) is None and getattr(
            block, "toolUseID", None
        ) is None:
            continue
        blocks.append(block)
    return tuple(blocks)


def _is_progress_message(message: Any) -> bool:
    return _message_type(message) == "progress"


def _progress_tool_use_id(message: Any) -> str | None:
    tool_use_id = getattr(message, "tool_use_id", None)
    if isinstance(tool_use_id, str) and tool_use_id:
        return tool_use_id
    tool_use_id = getattr(message, "toolUseID", None)
    return tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None


def _progress_data(message: Any) -> Mapping[str, Any]:
    data = getattr(message, "data", None)
    return data if isinstance(data, Mapping) else {}


def _tool_use_id_from_block(block: Any) -> str | None:
    tool_use_id = getattr(block, "tool_use_id", None)
    if isinstance(tool_use_id, str) and tool_use_id:
        return tool_use_id
    tool_use_id = getattr(block, "toolUseID", None)
    return tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None


def _message_tool_use_result(message: Any) -> str | None:
    result = getattr(message, "tool_use_result", None)
    if isinstance(result, str):
        return result
    result = getattr(message, "toolUseResult", None)
    return result if isinstance(result, str) else None


def _is_error_result(message: Message) -> bool:
    return any(bool(getattr(block, "is_error", False)) for block in _tool_result_blocks(message))


@dataclass
class _TrackedTool:
    id: str
    block: ToolCall
    assistant_message: AssistantToolUseMessage
    status: str
    is_concurrency_safe: bool
    pending_progress: list[Message] = field(default_factory=list)
    promise: Optional[asyncio.Task[None]] = None
    results: Optional[list[Message]] = None
    context_modifiers: list[Callable[[ToolUseContext], ToolUseContext]] = field(
        default_factory=list
    )


class StreamingToolExecutor:
    def __init__(
        self,
        tool_definitions: Sequence[ToolDefinition],
        can_use_tool: Any,
        tool_use_context: ToolUseContext,
        run_tool_use: RunToolUseFn,
    ) -> None:
        self._tools: list[_TrackedTool] = []
        self._tool_definitions = tuple(tool_definitions)
        self._can_use_tool = can_use_tool
        self._tool_use_context = tool_use_context
        self._run_tool_use = run_tool_use
        self._has_errored = False
        self._errored_tool_description = ""
        self._sibling_abort_controller = create_child_abort_controller(
            tool_use_context.abort_controller
        )
        self._discarded = False
        self._progress_event = asyncio.Event()

    def discard(self) -> None:
        self._discarded = True

    def add_tool(
        self, block: ToolCall, assistant_message: AssistantToolUseMessage
    ) -> None:
        tool_definition = find_tool_by_name(self._tool_definitions, block.name)
        if tool_definition is None:
            self._tools.append(
                _TrackedTool(
                    id=block.id,
                    block=block,
                    assistant_message=assistant_message,
                    status="completed",
                    is_concurrency_safe=True,
                    results=[
                        create_tool_result_message(
                            block.id,
                            f"<tool_use_error>Error: No such tool available: {block.name}</tool_use_error>",
                            is_error=True,
                            tool_use_result=f"Error: No such tool available: {block.name}",
                            source_tool_assistant_uuid=assistant_message.uuid,
                        )
                    ],
                )
            )
            return
        parsed_success, parsed_input = tool_definition.validate_input(block.input)
        is_concurrency_safe = False
        if parsed_success:
            try:
                is_concurrency_safe = bool(
                    tool_definition.is_concurrency_safe(parsed_input)
                )
            except Exception:
                is_concurrency_safe = False
        self._tools.append(
            _TrackedTool(
                id=block.id,
                block=block,
                assistant_message=assistant_message,
                status="queued",
                is_concurrency_safe=is_concurrency_safe,
            )
        )
        asyncio.create_task(self._process_queue())

    def _can_execute_tool(self, is_concurrency_safe: bool) -> bool:
        executing = [tool for tool in self._tools if tool.status == "executing"]
        return not executing or (
            is_concurrency_safe and all(tool.is_concurrency_safe for tool in executing)
        )

    async def _process_queue(self) -> None:
        for tool in self._tools:
            if tool.status != "queued":
                continue
            if self._can_execute_tool(tool.is_concurrency_safe):
                await self._execute_tool(tool)
            elif not tool.is_concurrency_safe:
                break

    def _create_synthetic_error_message(
        self,
        tool_use_id: str,
        reason: str,
        assistant_message: AssistantToolUseMessage,
    ) -> UserMessage:
        if reason == "user_interrupted":
            return create_tool_result_message(
                tool_use_id,
                REJECT_MESSAGE,
                is_error=True,
                tool_use_result=REJECT_MESSAGE,
                source_tool_assistant_uuid=assistant_message.uuid,
            )
        if reason == "streaming_fallback":
            return create_tool_result_message(
                tool_use_id,
                f"<tool_use_error>Error: {STREAMING_FALLBACK_RESULT}</tool_use_error>",
                is_error=True,
                tool_use_result=STREAMING_FALLBACK_RESULT,
                source_tool_assistant_uuid=assistant_message.uuid,
            )
        if self._errored_tool_description:
            message = f"Cancelled: parallel tool call {self._errored_tool_description} errored"
        else:
            message = "Cancelled: parallel tool call errored"
        return create_tool_result_message(
            tool_use_id,
            f"<tool_use_error>{message}</tool_use_error>",
            is_error=True,
            tool_use_result=message,
            source_tool_assistant_uuid=assistant_message.uuid,
        )

    def _get_abort_reason(self, tool: _TrackedTool) -> Optional[str]:
        if self._discarded:
            return "streaming_fallback"
        if self._has_errored:
            return "sibling_error"
        signal = self._tool_use_context.abort_controller.signal
        if signal.aborted:
            if signal.reason == "interrupt":
                return (
                    "user_interrupted"
                    if self._get_tool_interrupt_behavior(tool) == "cancel"
                    else None
                )
            return "user_interrupted"
        return None

    def _get_tool_interrupt_behavior(self, tool: _TrackedTool) -> str:
        definition = find_tool_by_name(self._tool_definitions, tool.block.name)
        if definition is None:
            return "block"
        try:
            return definition.interrupt_behavior()
        except Exception:
            return "block"

    def _get_tool_description(self, tool: _TrackedTool) -> str:
        summary = tool.block.input.get("command")
        if not isinstance(summary, str) or not summary:
            summary = tool.block.input.get("file_path")
        if not isinstance(summary, str) or not summary:
            summary = tool.block.input.get("pattern")
        if isinstance(summary, str) and summary:
            if len(summary) > 40:
                summary = summary[:40] + _TRUNCATED_SUMMARY_SUFFIX
            return f"{tool.block.name}({summary})"
        return tool.block.name

    def _update_interruptible_state(self) -> None:
        executing = [tool for tool in self._tools if tool.status == "executing"]
        self._tool_use_context = (
            self._tool_use_context.set_has_interruptible_tool_in_progress(
                bool(executing)
                and all(
                    self._get_tool_interrupt_behavior(tool) == "cancel"
                    for tool in executing
                )
            )
        )

    async def _execute_tool(self, tool: _TrackedTool) -> None:
        tool.status = "executing"
        self._tool_use_context = self._tool_use_context.set_in_progress_tool_use_ids(
            lambda prev, tool_id=tool.id: set(prev) | {tool_id}
        )
        self._update_interruptible_state()
        messages: list[Message] = []
        context_modifiers: list[Callable[[ToolUseContext], ToolUseContext]] = []

        async def _collect_results() -> None:
            initial_abort_reason = self._get_abort_reason(tool)
            if initial_abort_reason is not None:
                messages.append(
                    self._create_synthetic_error_message(
                        tool.id,
                        initial_abort_reason,
                        tool.assistant_message,
                    )
                )
                tool.results = messages
                tool.context_modifiers = context_modifiers
                tool.status = "completed"
                self._update_interruptible_state()
                return
            tool_abort_controller = create_child_abort_controller(
                self._sibling_abort_controller
            )

            def _bubble_abort() -> None:
                if (
                    tool_abort_controller.signal.reason != "sibling_error"
                    and not self._tool_use_context.abort_controller.signal.aborted
                    and not self._discarded
                ):
                    self._tool_use_context.abort_controller.abort(
                        tool_abort_controller.signal.reason
                    )

            tool_abort_controller.signal.add_callback(_bubble_abort, once=True)
            tool_context = replace(
                self._tool_use_context, abort_controller=tool_abort_controller
            )
            this_tool_errored = False
            async for update in self._run_tool_use(
                tool.block,
                tool.assistant_message,
                self._can_use_tool,
                tool_context,
            ):
                abort_reason = self._get_abort_reason(tool)
                if abort_reason is not None and not this_tool_errored:
                    messages.append(
                        self._create_synthetic_error_message(
                            tool.id,
                            abort_reason,
                            tool.assistant_message,
                        )
                    )
                    break
                if _is_error_result(update.message):
                    this_tool_errored = True
                    if tool.block.name == BASH_TOOL_NAME:
                        self._has_errored = True
                        self._errored_tool_description = self._get_tool_description(
                            tool
                        )
                        self._sibling_abort_controller.abort("sibling_error")
                if _is_progress_message(update.message):
                    tool.pending_progress.append(update.message)
                    self._progress_event.set()
                else:
                    messages.append(update.message)
                if update.context_modifier is not None:
                    context_modifiers.append(update.context_modifier.modify_context)
            tool.results = messages
            tool.context_modifiers = context_modifiers
            tool.status = "completed"
            self._update_interruptible_state()
            if not tool.is_concurrency_safe and context_modifiers:
                for modifier in context_modifiers:
                    self._tool_use_context = modifier(self._tool_use_context)

        tool.promise = asyncio.create_task(_collect_results())
        tool.promise.add_done_callback(
            lambda _task: asyncio.create_task(self._process_queue())
        )

    def get_completed_results(self) -> list[MessageUpdate]:
        if self._discarded:
            return []
        updates: list[MessageUpdate] = []
        for tool in self._tools:
            while tool.pending_progress:
                updates.append(
                    MessageUpdate(
                        message=tool.pending_progress.pop(0),
                        new_context=self._tool_use_context,
                    )
                )
            if tool.status == "yielded":
                continue
            if tool.status == "completed" and tool.results is not None:
                tool.status = "yielded"
                for message in tool.results:
                    updates.append(
                        MessageUpdate(
                            message=message, new_context=self._tool_use_context
                        )
                    )
                self._tool_use_context = mark_tool_use_as_complete(
                    self._tool_use_context,
                    tool.id,
                )
            elif tool.status == "executing" and not tool.is_concurrency_safe:
                break
        return updates

    def _has_pending_progress(self) -> bool:
        return any(tool.pending_progress for tool in self._tools)

    def _has_completed_results(self) -> bool:
        return any(tool.status == "completed" for tool in self._tools)

    def _has_executing_tools(self) -> bool:
        return any(tool.status == "executing" for tool in self._tools)

    def _has_unfinished_tools(self) -> bool:
        return any(tool.status != "yielded" for tool in self._tools)

    async def get_remaining_results(self) -> AsyncGenerator[MessageUpdate, None]:
        if self._discarded:
            return
        while self._has_unfinished_tools():
            await self._process_queue()
            for result in self.get_completed_results():
                yield result
            if (
                self._has_executing_tools()
                and not self._has_completed_results()
                and not self._has_pending_progress()
            ):
                executing_tasks = [
                    tool.promise
                    for tool in self._tools
                    if tool.status == "executing" and tool.promise is not None
                ]
                progress_waiter = asyncio.create_task(self._progress_event.wait())
                done, pending = await asyncio.wait(
                    [*executing_tasks, progress_waiter],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if progress_waiter in done:
                    self._progress_event.clear()
                else:
                    progress_waiter.cancel()
                    try:
                        await progress_waiter
                    except asyncio.CancelledError:
                        pass
        for result in self.get_completed_results():
            yield result

    def get_updated_context(self) -> ToolUseContext:
        return self._tool_use_context


def _message_label(message: Message) -> str:
    if _is_progress_message(message):
        return (
            f"progress:{_progress_tool_use_id(message)}:"
            f"{_progress_data(message).get('step')}"
        )
    blocks = _tool_result_blocks(message)
    if blocks:
        block = blocks[0]
        tool_use_id = _tool_use_id_from_block(block)
        tool_use_result = _message_tool_use_result(message)
        if bool(getattr(block, "is_error", False)):
            return f"error:{tool_use_id}:{tool_use_result}"
        return f"result:{tool_use_id}:{tool_use_result}"
    raise TypeError(f"Unsupported message type: {type(message)!r}")


async def _collect_async(
    gen: AsyncIterable[MessageUpdate],
) -> tuple[MessageUpdate, ...]:
    items: list[MessageUpdate] = []
    async for item in gen:
        items.append(item)
    return tuple(items)


def _make_tool(
    name: str,
    *,
    concurrency_safe: bool,
    interrupt_behavior: str = "block",
    explode_on_concurrency_probe: bool = False,
) -> ToolDefinition:
    def _concurrency(_input: Mapping[str, Any]) -> bool:
        if explode_on_concurrency_probe:
            raise ValueError("probe failed")
        return concurrency_safe

    return ToolDefinition(
        name=name,
        is_concurrency_safe=_concurrency,
        interrupt_behavior=lambda: interrupt_behavior,
    )


async def _run_tool_scheduler_checks() -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    tools = (
        _make_tool("Read", concurrency_safe=True, interrupt_behavior="cancel"),
        _make_tool("Write", concurrency_safe=False),
        _make_tool("Bash", concurrency_safe=True, interrupt_behavior="cancel"),
        _make_tool(
            "ProbeThrow", concurrency_safe=True, explode_on_concurrency_probe=True
        ),
    )
    base_context = ToolUseContext(options=ToolSchedulerOptions(tools=tools))

    partitioned = partition_tool_calls(
        (
            ToolCall(id="r1", name="Read", input={}),
            ToolCall(id="r2", name="Read", input={}),
            ToolCall(id="w1", name="Write", input={}),
            ToolCall(id="r3", name="Read", input={}),
            ToolCall(id="p1", name="ProbeThrow", input={}),
        ),
        base_context,
    )
    partition_signature = tuple(
        (batch.is_concurrency_safe, tuple(block.id for block in batch.blocks))
        for batch in partitioned
    )
    if partition_signature != (
        (True, ("r1", "r2")),
        (False, ("w1",)),
        (True, ("r3",)),
        (False, ("p1",)),
    ):
        errors.append(f"partitionToolCalls parity mismatch: {partition_signature!r}")

    assistant_messages = (
        AssistantToolUseMessage(uuid="assistant-a", tool_use_ids=("read-a",)),
        AssistantToolUseMessage(uuid="assistant-b", tool_use_ids=("read-b",)),
        AssistantToolUseMessage(uuid="assistant-c", tool_use_ids=("write-c",)),
    )

    async def scripted_run_tool_use(
        tool_use: ToolCall,
        assistant_message: AssistantToolUseMessage,
        _can_use_tool: Any,
        _tool_use_context: ToolUseContext,
    ) -> AsyncIterable[MessageUpdateLazy]:
        if tool_use.id == "read-a":
            await asyncio.sleep(0.03)
            yield MessageUpdateLazy(
                message=create_tool_result_message(
                    tool_use.id,
                    "alpha",
                    tool_use_result="alpha",
                    source_tool_assistant_uuid=assistant_message.uuid,
                ),
                context_modifier=ContextModifier(
                    tool_use_id=tool_use.id,
                    modify_context=lambda context: context.with_state(order="A"),
                ),
            )
            return
        if tool_use.id == "read-b":
            await asyncio.sleep(0.01)
            yield MessageUpdateLazy(
                message=create_tool_result_message(
                    tool_use.id,
                    "beta",
                    tool_use_result="beta",
                    source_tool_assistant_uuid=assistant_message.uuid,
                ),
                context_modifier=ContextModifier(
                    tool_use_id=tool_use.id,
                    modify_context=lambda context: context.with_state(
                        order="AB", seen="beta"
                    ),
                ),
            )
            return
        yield MessageUpdateLazy(
            message=create_tool_result_message(
                tool_use.id,
                "write-done",
                tool_use_result="write-done",
                source_tool_assistant_uuid=assistant_message.uuid,
            ),
            context_modifier=ContextModifier(
                tool_use_id=tool_use.id,
                modify_context=lambda context: context.with_state(serial="applied"),
            ),
        )

    updates = await _collect_async(
        run_tools(
            (
                ToolCall(id="read-a", name="Read", input={}),
                ToolCall(id="read-b", name="Read", input={}),
                ToolCall(id="write-c", name="Write", input={}),
            ),
            assistant_messages,
            None,
            base_context,
            scripted_run_tool_use,
        )
    )
    labels = tuple(
        _message_label(update.message)
        for update in updates
        if update.message is not None
    )
    if labels != (
        "result:read-b:beta",
        "result:read-a:alpha",
        "result:write-c:write-done",
    ):
        errors.append(f"run_tools forwarded unexpected message order: {labels!r}")
    if updates[0].new_context is None or updates[0].new_context.state:
        errors.append(
            "concurrent batch leaked context mutation before batch completion"
        )
    if (
        updates[2].new_context is None
        or updates[2].message is not None
        or updates[2].new_context.state.get("order") != "AB"
    ):
        errors.append(
            "concurrent batch did not emit trailing context update in block order"
        )
    if (
        updates[3].new_context is None
        or updates[3].new_context.state.get("serial") != "applied"
    ):
        errors.append("serial batch did not apply write context modifier immediately")

    async def streaming_run_tool_use(
        tool_use: ToolCall,
        assistant_message: AssistantToolUseMessage,
        _can_use_tool: Any,
        _tool_use_context: ToolUseContext,
    ) -> AsyncIterable[MessageUpdateLazy]:
        if tool_use.id == "bash-fail":
            await asyncio.sleep(0.01)
            yield MessageUpdateLazy(
                message=create_tool_result_message(
                    tool_use.id,
                    "<tool_use_error>boom</tool_use_error>",
                    is_error=True,
                    tool_use_result="boom",
                    source_tool_assistant_uuid=assistant_message.uuid,
                )
            )
            return
        if tool_use.id == "read-sibling":
            yield MessageUpdateLazy(
                message=ProgressMessage(
                    tool_use_id=tool_use.id,
                    parent_tool_use_id=tool_use.id,
                    data={"step": "started"},
                )
            )
            await asyncio.sleep(0.03)
            yield MessageUpdateLazy(
                message=create_tool_result_message(
                    tool_use.id,
                    "ok",
                    tool_use_result="ok",
                    source_tool_assistant_uuid=assistant_message.uuid,
                )
            )
            return
        await asyncio.sleep(0.02)
        yield MessageUpdateLazy(
            message=create_tool_result_message(
                tool_use.id,
                "write",
                tool_use_result="write",
                source_tool_assistant_uuid=assistant_message.uuid,
            ),
            context_modifier=ContextModifier(
                tool_use_id=tool_use.id,
                modify_context=lambda context: context.with_state(write="done"),
            ),
        )

    streaming_assistants = (
        AssistantToolUseMessage(uuid="assistant-bash", tool_use_ids=("bash-fail",)),
        AssistantToolUseMessage(uuid="assistant-read", tool_use_ids=("read-sibling",)),
        AssistantToolUseMessage(uuid="assistant-write", tool_use_ids=("write-serial",)),
    )
    executor = StreamingToolExecutor(
        tools,
        None,
        base_context,
        streaming_run_tool_use,
    )
    executor.add_tool(
        ToolCall(id="bash-fail", name="Bash", input={"command": "false"}),
        streaming_assistants[0],
    )
    executor.add_tool(
        ToolCall(id="read-sibling", name="Read", input={"file_path": "alpha.txt"}),
        streaming_assistants[1],
    )
    executor.add_tool(
        ToolCall(id="write-serial", name="Write", input={"file_path": "beta.txt"}),
        streaming_assistants[2],
    )
    streamed_results: list[MessageUpdate] = []
    async for result in executor.get_remaining_results():
        streamed_results.append(result)
    stream_labels = tuple(
        _message_label(result.message)
        for result in streamed_results
        if result.message is not None
    )
    if stream_labels != (
        "progress:read-sibling:started",
        "error:bash-fail:boom",
        "error:read-sibling:Cancelled: parallel tool call Bash(false) errored",
        "error:write-serial:Cancelled: parallel tool call Bash(false) errored",
    ):
        errors.append(f"StreamingToolExecutor ordering mismatch: {stream_labels!r}")
    if executor.get_updated_context().state:
        errors.append("cancelled queued write should not mutate scheduler context")

    return (not errors, tuple(errors))


def validate_tool_scheduler_contract() -> tuple[bool, tuple[str, ...]]:
    return asyncio.run(_run_tool_scheduler_checks())
