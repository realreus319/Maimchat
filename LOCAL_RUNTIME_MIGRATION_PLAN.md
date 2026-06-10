# Maimchat Local Runtime Migration Plan

## Purpose

Maimchat is currently shaped as a Live2D client that sends chat messages to a backend over WebSocket and renders the backend response in the app, wallpaper, and widget surfaces. The migration goal is to make Maimchat a self-contained chat runtime app: user input, perception, planning, tool execution, reply generation, memory, Live2D action selection, and reply delivery should all run inside the Android app.

The backend being migrated is `/home/tcmofashi/chatbot/l2d_backend`. Its main reply path is:

```text
Inbound payload
  -> TransportBridge / InboundBuilder
  -> ChatManager
  -> PerceptionProcessor
  -> Trigger
  -> ReplyLayerFactory
  -> PlannerLoop
  -> ToolRegistry
  -> ReplierTool / ReplierTask
  -> ReplySender
```

The Android target path should become:

```text
UI / wallpaper / widget input
  -> LocalChatRuntime
  -> InboundBuilder
  -> PerceptionProcessor
  -> Trigger
  -> PlannerLoop
  -> ToolRegistry
  -> ReplierTool / ReplierTask
  -> ReplySink
  -> existing messages / standardMessages flows
```

The migration should preserve the backend logic first, then adapt details to Android where needed.

## Current Maimchat Boundaries

Important current files:

- `app/src/main/java/com/l2dchat/chat/ChatWebSocketManager.kt`
  - Owns WebSocket connection, message history, current user/receiver config, `messages`, `standardMessages`, and outbound message construction.
- `app/src/main/java/com/l2dchat/chat/service/ChatConnectionService.kt`
  - Android service boundary used by UI, wallpaper, and widget clients.
- `app/src/main/java/com/l2dchat/chat/service/ChatServiceClient.kt`
  - Client-side Messenger facade for UI/wallpaper/widget surfaces.
- `app/src/main/java/com/l2dchat/chat/MessageBase.kt`
  - Existing standard message compatibility model.
- `app/src/main/java/com/l2dchat/chat/Live2DChatMessageHandler.kt`
  - Parses received standard messages into visible chat events, voice events, and emoji events.
- `app/src/main/java/com/l2dchat/live2d/Live2DModelLifecycleManager.kt`
  - Live2D model lifecycle and motion control.
- `app/src/main/java/com/l2dchat/wallpaper/*`
  - Wallpaper-facing chat and Live2D integration.

The existing UI and wallpaper consumers should remain stable as much as possible. The main internal change is replacing "WebSocket as the chat engine" with "local runtime as the chat engine", while keeping WebSocket as an optional remote compatibility transport.

## Migration Principles

1. Do not embed the Python backend into Android.
   - Reimplement the backend semantics in Kotlin.
   - Keep the conceptual modules: inbound, perception, trigger, planner loop, tools, replier, reply sender.

2. Preserve the service/client surface first.
   - `ChatConnectionService` and `ChatServiceClient` should remain the main app boundary.
   - UI, wallpaper, and widget should continue observing `messages` and `standardMessages`.

3. Introduce a transport abstraction.
   - `LocalTransport` becomes the default.
   - Existing WebSocket behavior becomes `RemoteWebSocketTransport`.

4. Use Room for runtime state.
   - SharedPreferences history is too weak for planner sessions, task state, memory, and message queries.

5. Build a minimal local loop before adding full intelligence.
   - First fixed local reply.
   - Then fake LLM planner tests.
   - Then real LLM.
   - Then interruption, background tasks, environment tools, and memory.

6. Preserve multimodal payloads end to end.
   - The backend currently supports image/content blocks in some paths but can lose them in normal chat perception.
   - The Android migration should fix this and carry content blocks through `Trigger`.

## Backend-To-Android Mapping

| Backend concept | Backend file | Android target |
| --- | --- | --- |
| Runtime startup | `src/chat_v1/runtime.py` | `com.l2dchat.core.ChatRuntime` |
| Transport bridge | `src/adapters/chat_v1/bridge.py` | `com.l2dchat.core.inbound.TransportBridge` |
| Inbound validation | `src/adapters/chat_v1/inbound_builder.py` | `com.l2dchat.core.inbound.InboundBuilder` |
| Chat context | `src/chat_v1/base/context/chat_context.py` | `com.l2dchat.core.context.ChatContext` |
| Chat manager | `src/chat_v1/orchestrator/chat_manager.py` | `com.l2dchat.core.orchestrator.ChatManager` |
| Perception factory | `src/chat_v1/orchestrator/perception/factory.py` | `com.l2dchat.core.perception.PerceptionFactory` |
| Perception processor | `src/chat_v1/orchestrator/perception/perception_processor.py` | `com.l2dchat.core.perception.PerceptionProcessor` |
| Parser factory | `src/chat_v1/orchestrator/perception/parser_factory.py` | `com.l2dchat.core.perception.MessageParserFactory` |
| Trigger | `src/chat_v1/orchestrator/common/trigger.py` | `com.l2dchat.core.trigger.Trigger` |
| Reply layer factory | `src/chat_v1/orchestrator/reply_generation/factory.py` | `com.l2dchat.core.reply.ReplyLayerFactory` |
| Planner loop | `src/chat_v1/orchestrator/reply_generation/planner_loop.py` | `com.l2dchat.core.reply.PlannerLoop` |
| Planner session | `src/chat_v1/orchestrator/reply_generation/planner_loop_session.py` | `com.l2dchat.core.reply.PlannerSessionStore` |
| Planner prompting | `src/chat_v1/orchestrator/reply_generation/planner_loop_prompting.py` | `com.l2dchat.core.reply.PlannerPromptBuilder` |
| Tool registry | `src/chat_v1/orchestrator/tools/tool_registry.py` | `com.l2dchat.core.tools.ToolRegistry` |
| Replier tool | `src/chat_v1/orchestrator/tools/replier_tool.py` | `com.l2dchat.core.tools.ReplierTool` |
| Replier task | `src/chat_v1/orchestrator/tools/replier_task.py` | `com.l2dchat.core.tools.ReplierTask` |
| Decision tools | `src/chat_v1/orchestrator/tools/decision_tools.py` | `com.l2dchat.core.tools.DecisionTools` |
| Reply sender | `src/adapters/chat_v1/reply_sender.py` | `com.l2dchat.core.reply.ReplySink` |

## Target Package Layout

Proposed new Android package layout:

```text
app/src/main/java/com/l2dchat/core/
  ChatRuntime.kt
  ChatRuntimeConfig.kt
  context/
    ChatContext.kt
    RoutingKey.kt
  inbound/
    InboundBuilder.kt
    TransportBridge.kt
    ContentBlock.kt
  message/
    ChatMessageEntity.kt
    InternalMessage.kt
    MessageMapper.kt
  perception/
    MessageParser.kt
    MessageParserFactory.kt
    PerceptionFactory.kt
    PerceptionProcessor.kt
  trigger/
    Trigger.kt
    TriggerPriority.kt
    TriggerType.kt
  reply/
    PlannerLoop.kt
    PlannerLoopState.kt
    PlannerPromptBuilder.kt
    PlannerSessionStore.kt
    ReplyLayerFactory.kt
    ReplySink.kt
  tools/
    Tool.kt
    ToolRegistry.kt
    ToolExecutionContext.kt
    ReplierTool.kt
    ReplierTask.kt
    DecisionTools.kt
    WaitForTool.kt
    EnvironmentTools.kt
  llm/
    LlmClient.kt
    OpenAiCompatibleClient.kt
    ToolCall.kt
    LlmMessage.kt
  memory/
    MemoryStore.kt
    ImpressionStore.kt
    MoodStore.kt
  storage/
    ChatDatabase.kt
    dao/
  config/
    AgentConfig.kt
    PromptConfig.kt
```

Existing chat package should become a thin facade:

```text
app/src/main/java/com/l2dchat/chat/
  ChatWebSocketManager.kt      -> eventually split/rename to ChatSessionManager
  MessageBase.kt               -> keep as compatibility message format
  Live2DChatMessageHandler.kt  -> keep for visible message parsing
```

## Phase 0: Baseline And Guardrails

Goal: make future refactors measurable and reversible.

Tasks:

- Add this migration plan to the repo.
- Add a small architecture note to `README.md` later after implementation begins.
- Capture current `assembleDebug` success as baseline.
- Add unit test scaffolding if not already available for JVM tests.
- Avoid changing UI behavior in this phase.
- Keep existing WebSocket mode working until local mode is stable.

Acceptance:

- `./gradlew assembleDebug` still succeeds.
- Existing WebSocket chat path still works.
- No UI changes are required.

## Phase 1: Local Runtime Skeleton

Goal: replace backend dependency with a local fixed-reply runtime path, without implementing LLM logic yet.

Tasks:

- Introduce `ChatTransport`.
  - `sendUserMessage(message: MessageBase)`
  - `start()`
  - `stop()`
  - `connectionState`
  - `events`

- Implement `RemoteWebSocketTransport`.
  - Move the current OkHttp WebSocket logic from `ChatWebSocketManager` behind this transport.
  - Preserve reconnect, auth token, platform header, and error reporting.

- Implement `LocalChatRuntime`.
  - Receives `MessageBase`.
  - Emits a fixed assistant reply through `ReplySink`.
  - Uses the same `MessageBase` format for assistant output.

- Implement `LocalTransport`.
  - Wraps `LocalChatRuntime`.
  - Reports `CONNECTED` when runtime is ready.
  - No server URL needed.

- Update `ChatConnectionService`.
  - Add mode selection: `local` or `remote`.
  - Default to `local`.
  - Keep remote mode available for comparison.

Acceptance:

- User sends a message in app UI.
- App shows user message immediately.
- App shows a fixed local assistant reply.
- Wallpaper/widget consumers see the same message events.
- No backend process is required.

## Phase 2: Storage And Runtime State

Goal: replace SharedPreferences chat history with queryable runtime storage.

Tasks:

- Add Room dependencies.
- Create `ChatDatabase`.
- Tables:
  - `messages`
  - `standard_messages`
  - `planner_rounds`
  - `planner_messages`
  - `agent_configs`
  - `prompt_templates`
  - `tool_tasks`
  - `memories`
  - `impressions`
  - `mood_state`
  - `media_blocks`

- Implement DAOs:
  - append message
  - query recent messages by `(contextId, agentId)`
  - query history until message id
  - append planner round
  - append planner LLM messages
  - update tool task state

- Migrate existing SharedPreferences history.
  - Keep a one-time import path.
  - Keep export to `MessageBase` for compatibility.

Acceptance:

- Existing histories still load.
- New messages persist through process death.
- `ChatContext.getConversationHistory()` can be implemented on top of Room.

Progress:

- Done: Room-backed `messages` and `standard_messages` storage.
- Done: one-time import from legacy SharedPreferences history.
- Done: compatibility mirror from Room-backed flow state back to existing visible/standard message APIs.
- Done: scoped history clearing by model context and optional agent id.
- Pending: planner/session/tool/memory tables and higher-level `ChatContext` query API.

## Phase 3: Inbound And Perception

Goal: implement the backend `Bridge -> InboundBuilder -> PerceptionProcessor -> Trigger` path in Kotlin.

Tasks:

- Implement internal models:
  - `RoutingKey(contextId, agentId)`
  - `ChatContext`
  - `InternalMessage`
  - `ContentBlock`
  - `ParsedMessageResult`
  - `Trigger`

- Implement `InboundBuilder`.
  - Convert UI text into `MessageBase`.
  - Convert `MessageBase` into `InternalMessage`.
  - Preserve sender, receiver, group, platform, raw text, message id, timestamp.
  - Preserve content blocks for image, text, emoji, voice.

- Implement `TransportBridge`.
  - Determine local context id.
  - Determine agent id from selected Live2D model or configured agent.
  - Build `ChatContext`.
  - Submit to `ChatManager`.

- Implement `MessageParser`.
  - raw text extraction
  - command parsing
  - mention parsing
  - multimodal block parsing
  - sender metadata extraction

- Implement `PerceptionProcessor`.
  - One coroutine actor per `(contextId, agentId)`.
  - Queue inbound messages.
  - Parse.
  - Persist.
  - Create `MSG` trigger.
  - Submit trigger to reply layer.

Acceptance:

- A user message becomes one persisted message and one `MSG` trigger.
- Mentioned messages get high priority.
- Normal text, emoji, voice, and image blocks are not dropped.

Progress:

- Done: `RoutingKey`, `InboundMessage`, and `ContentBlock` core models.
- Done: `InboundBuilder` for existing `MessageBase` input, room parsing, seglist text/image normalization, and image marker validation.
- Done: default parser for text, command prefix, command args, and mentions.
- Done: `Trigger` and `PerceptionProcessor` conversion from parsed message to `MSG` trigger.
- Done: `LocalChatRuntime` now reads inbound text through the inbound/perception path while keeping fixed local reply behavior.
- Done: `processAndPersist()` can persist parsed standard messages and ordered media blocks into Room.
- Done: image, emoji, and voice `seglist` blocks are preserved through inbound, trigger payload, and media block storage.
- Done: `PerceptionDispatcher` and `PerceptionWorker` queue inbound messages per `(contextId, agentId)`, optionally persist parsed messages, and submit triggers through `TriggerSink`.
- Done: `LocalChatRuntime.handleMessage()` now routes chat input through `PerceptionDispatcher` and waits on `TriggerSink` before emitting the current fixed local reply.
- Done: `LocalChatRuntime.handleMessage()` now routes perception triggers into `ReplyLayerFactory` and `PlannerLoop`.
- Done: live local runtime can inject `RoomPerceptionStore`, so parsed standard messages and media blocks are persisted before trigger submission.
- Pending: planner/session/tool/memory tables and higher-level `ChatContext` query API.

## Phase 4: PlannerLoop Core

Goal: port the backend's central reply state machine.

Required behavior:

- One `PlannerLoop` per `(contextId, agentId)`.
- States:
  - `IDLE`
  - `GENERATING`
  - `DECIDING`
- Priority trigger queue.
- Trigger ordering by priority and timestamp.
- `MSG` triggers can interrupt.
- `ENV` and `SYS` triggers queue normally.
- Foreground epoch prevents stale tool calls from sending replies.
- Planner session persists trigger/user/tool/assistant messages.

Tasks:

- Implement `ReplyLayerFactory`.
  - `getOrCreateLoop(routingKey, context)`
  - `submitTrigger(trigger, context)`

- Implement `PlannerLoop`.
  - `submitTrigger()`
  - `run()`
  - `_processTriggerNative()`
  - `_handleFork()`
  - `_processDecisionTrigger()`
  - `_continueForegroundAfterDecision()`
  - `_sendReply()`

- Implement native tool-calling loop.
  - Build system prompt.
  - Build planner session messages.
  - Call `LlmClient.chatCompletionWithTools()`.
  - Execute tool calls.
  - Persist all LLM and tool messages.
  - Send final assistant content only if no tool already sent a reply.

- Implement fallback if provider does not support native tools.
  - Strict JSON action format.
  - Parse action.
  - Execute tool.
  - Feed observation back.

Acceptance:

- Fake LLM can force a `replier` call.
- Fake LLM final assistant content can be sent when no tool is called.
- Duplicate foreground `replier` calls in the same turn are rejected.
- Stale foreground tool calls cannot send.

Progress:

- Done: `PlannerLoopState`, `PlannerLoop`, `PlannerTurnContext`, `PlannerReplySink`, and `ReplyLayerFactory` skeletons are in place.
- Done: `ReplyLayerFactory` creates one running loop per `(contextId, agentId)` and implements `TriggerSink`.
- Done: `PlannerLoop` processes queued triggers by priority and timestamp, supports `MSG` interruption through foreground epoch advancement and job cancellation, rejects duplicate foreground replies, and rejects stale foreground sends.
- Done: `LocalChatRuntime` now routes perception triggers into `ReplyLayerFactory`/`PlannerLoop`; the temporary fixed reply is emitted by a planner processor and converted back through `PlannerReplySink`.
- Done: `PlannerSessionStore` and `RoomPlannerSessionStore` persist planner rounds plus trigger/user/assistant session messages for each processed turn.
- Done: `PlannerPromptBuilder` and `LlmPlannerTriggerProcessor` can build a trigger prompt, call a fake/provider-neutral LLM client, and send final assistant content when no tool call is returned.
- Done: `ToolCallingPlannerTriggerProcessor` calls `LlmClient.chatCompletionWithTools()`,
  executes registered tools, sends planner-managed `replier` output through the
  existing `ReplySink`, and falls back to final assistant text when no tool reply
  was sent.
- Pending: tool task/session message persistence and decision/fork replier adoption.
- Done: JSON fallback planner processor executes tool calls through plain
  `chatCompletion` responses for providers without native tool calling.

## Phase 5: LLM Client

Goal: provide a provider-neutral LLM layer.

Tasks:

- Define `LlmClient`.
  - `chatCompletion(messages, config)`
  - `chatCompletionStream(messages, config)`
  - `chatCompletionWithTools(messages, tools, config, toolExecutor)`

- Implement `OpenAiCompatibleClient`.
  - base URL
  - API key
  - model
  - temperature
  - max tokens
  - streaming support
  - tool calls
  - cancellation
  - timeout
  - retry

- Add provider settings UI later.
  - API endpoint
  - API key
  - planner model
  - replier model
  - tool support toggle

- Store secrets safely.
  - Prefer AndroidX Security `EncryptedSharedPreferences`.
  - Do not log API keys or raw Authorization headers.

Acceptance:

- App can call an OpenAI-compatible endpoint directly.
- Planner and replier can use different models/configs.
- Cancellation works when a new message interrupts generation.

Progress:

- Done: provider-neutral `core.llm` message, content part, tool definition, tool call,
  response, stream event, generation config, and client contracts are in place.
- Done: fake-client contract tests cover assistant tool calls, tool result messages,
  streaming completion events, and tool executor wiring shape.
- Done: `OpenAiCompatibleClient` supports OpenAI-compatible chat completions,
  SSE text streaming, native tool-call request/response conversion, timeout,
  retry, and coroutine cancellation through OkHttp call cancellation.
- Done: `LocalLlmSettings` can translate provider-neutral local LLM settings into
  planner/replier runtime configs with separate model selection and native tool
  calling fallback control.
- Pending: encrypted provider settings and manual real-provider verification.

## Phase 6: ReplierTool And ReplierTask

Goal: port the actual reply-generation behavior.

Tasks:

- Implement `ReplierTool`.
  - Tool schema:
    - `content`
    - `reply_guidance`
    - `style_override`
    - `emotion_hint`
    - `is_progress_update`
    - `include_action`
    - `live_image`
  - Validate content.
  - Create `ReplierTask`.
  - Return `replyText` to planner in planner-managed mode.
  - Do not directly send in planner-managed mode.

- Implement `ReplierTask`.
  - States:
    - `PENDING`
    - `GENERATING`
    - `BACKGROUND`
    - `COMPLETED`
    - `CANCELLED`
    - `FAILED`
  - Streaming generation.
  - `moveToBackground()`.
  - `waitForCompletion()`.
  - `cancel()`.
  - progress preview.
  - background delay behavior if needed.

- Implement replier prompt builder.
  - persona
  - current user message
  - recent conversation history
  - mood
  - user impression
  - style override
  - emotion hint
  - live image blocks

Acceptance:

- Planner-managed `replier` returns `replyText` and `sent=false`.
- Planner sends the returned text through `ReplySink`.
- Streaming task can be moved to background by interruption.

Progress:

- Done: minimal planner-managed `ReplierTool` schema covers `content`,
  `reply_guidance`, `style_override`, `emotion_hint`, `is_progress_update`,
  `include_action`, and `live_image`.
- Done: `ReplierTool` validates nonblank content, returns structured tool JSON
  containing `replyText` and `sent=false`, and never sends directly.
- Done: `ReplierTask` and `ReplierTaskManager` cover `PENDING`, `GENERATING`,
  `BACKGROUND`, `COMPLETED`, `CANCELLED`, and `FAILED` lifecycle states,
  streaming preview updates, wait, cancel, and background transitions.
- Done: `ReplierTool` can optionally create a `ReplierTask`, wait for completion,
  return the generated task reply text to the planner, and convert task failures
  into LLM tool error results.
- Done: `ReplierPromptBuilder` builds a replier-specific prompt from the planner
  request, current trigger, style/guidance/emotion flags, trigger images, and
  optional live image input.
- Done: `LlmReplierTaskGenerator` streams `LlmClient.chatCompletionStream()` output
  into `ReplierTaskUpdate` events and rejects accidental tool calls from the
  replier model.
- Done: `ReplierTool` moves an unfinished task to `BACKGROUND` when planner-side
  waiting is cancelled, allowing a later decision turn to adopt or kill it.
- Pending: full persona/history/mood/impression context injection.

## Phase 7: Decision Tools And Tool Registry

Goal: complete the minimum tool system required for planner parity.

Minimum tools:

- `replier`
- `wait_for`
- `adopt_background_reply`
- `kill_background_reply`

Important follow-up tools:

- `get_context`
- `update_impression`
- `delegate_task`
- `get_world_state`
- `look_at`
- `trigger_motion`
- `set_expression`

Tasks:

- Implement `Tool`.
  - name
  - description
  - JSON schema
  - execute(context, args)

- Implement `ToolRegistry`.
  - one registry per routing key
  - default tools
  - decision tools gated by decision mode

- Implement decision mode restrictions.
  - In decision mode, only `adopt_background_reply` and `kill_background_reply` are allowed.
  - In normal mode, decision tools are not allowed.

- Implement `AdoptBackgroundReplyTool`.
  - Match task id.
  - Wait for background replier completion.
  - Return reply text.
  - Planner sends reply.

- Implement `KillBackgroundReplyTool`.
  - Match task id.
  - Cancel/kill background replier.
  - Clear background task.
  - Continue foreground with new trigger.

Acceptance:

- If a new user message arrives during reply generation, current reply can be backgrounded.
- Planner can adopt old background reply.
- Planner can kill old background reply and respond to the new message.

Progress:

- Done: core `Tool`, `ToolExecutionContext`, `ToolExecutionResult`, and
  `ToolRegistry` are in place for normal planner tool execution.
- Done: `ToolRegistry` exposes provider-neutral `LlmToolDefinition`s and converts
  unknown tools, malformed JSON, and argument validation failures into LLM tool
  error results.
- Done: `ToolExecutionMode` and `ToolRegistry.definitionsFor()` gate normal and
  decision-mode tools, rejecting disallowed calls before execution.
- Done: `LocalToolRegistryFactory` centralizes the default normal-mode tool set
  (`replier`, optional `wait_for`, `get_world_state`, `look_at`,
  `trigger_motion`) and exposes only decision-mode tools for decision turns.
- Done: `wait_for`, `adopt_background_reply`, and `kill_background_reply` operate
  on `ReplierTaskManager`; adopt returns planner-managed `replyText`, while kill
  cancels running background tasks.
- Done: `ToolCallingPlannerTriggerProcessor` can run in normal or decision tool
  mode, exposing only the matching tool definitions and passing the mode into
  tool execution.
- Done: `PlannerLoop` marks an interrupted next `MSG` as a decision turn when a
  decision processor is configured, sets `DECIDING` state for that turn, and
  returns later triggers to normal processing.
- Done: `ReplyLayerFactory` and `LocalChatRuntime` can wire a dedicated decision
  processor into planner loops, and decision prompts can include per-routing-key
  background replier task ids, states, trigger text, and previews.
- Done: interrupted planner turns notify `LocalChatRuntime`, so the cancelled
  inbound `handleMessage()` completes `false` instead of hanging while the next
  decision turn continues.
- Pending: richer fork-continuation behavior after kill/adopt.

## Phase 8: Live2D And Environment Integration

Goal: adapt backend environment logic to Android-native state.

Environment sources:

- current Live2D model
- current model motions
- current expression state if available
- wallpaper foreground/background state
- last touch/drag event
- selected background image
- app visibility
- optional screenshot/render snapshot
- recent chat bubbles

Tasks:

- Implement `EnvironmentStateProvider`.
  - Expose current model info.
  - Expose available motions.
  - Expose last user interaction.
  - Expose wallpaper/app surface state.

- Implement environment triggers.
  - model changed
  - app opened
  - wallpaper interaction
  - idle timer
  - motion finished
  - optional visual snapshot ready

- Implement `trigger_motion` tool.
  - Use existing `Live2DModelLifecycleManager` motion APIs.
  - Replace message-based manual motion trigger where possible.

- Implement `get_world_state`.
  - Return summarized environment state to planner.

- Implement `look_at`.
  - First version returns environment metadata only.
  - Later version can include image content blocks if screenshot/capture is available and permission-safe.

Acceptance:

- Planner can choose a Live2D motion as part of a reply.
- Environment triggers can initiate planner turns without user text.
- Live image/multimodal blocks are preserved when available.

Progress:

- Done: core `EnvironmentStateProvider` and `EnvironmentState` models expose
  routing identity, selected model metadata, motions, expression, surface state,
  last interaction, recent bubbles, optional visual snapshot metadata, and
  extension metadata without coupling to Android UI classes.
- Done: `get_world_state` returns a bounded, planner-readable JSON summary of
  that environment state and is exposed only in normal tool mode.
- Done: core `trigger_motion` tool validates group/index or file path requests,
  calls a `MotionController`, reports accepted/rejected motion queue state as
  planner-readable JSON, and includes a Live2D adapter over
  `Live2DModelLifecycleManager.playMotionByGroup()` / `playMotionByFile()`.
- Done: metadata-only `look_at` tool returns model, surface, last interaction,
  visual snapshot metadata, and optional recent chat bubbles without attaching
  screenshot pixels or image content blocks.
- Done: `LocalRuntimeFactory` can build `LocalChatRuntime` with a shared
  `ReplierTaskManager`, normal and decision registries from
  `LocalToolRegistryFactory`, native tool-calling or JSON fallback planner
  processors, and LLM-backed replier task generation.
- Done: `ChatServiceClient`, `ChatConnectionService`, `ChatWebSocketManager`,
  and `LocalTransport` can persist local LLM settings, pass them into
  `LocalRuntimeFactory`, and rebuild the local runtime when settings change.
- Done: the Android chat configuration dialog exposes local LLM provider
  settings and can switch the service back to the local runtime when enabled.
- Pending: wire real app, wallpaper, Live2D lifecycle, touch, chat bubble, and
  snapshot producers into an Android-backed provider.
- Pending: wire real environment/motion adapters into `LocalRuntimeFactory`.
- Pending: image/content-block capable `look_at` and environment triggers.

## Phase 9: Agent Config, Prompts, Memory, And Mood

Goal: port important agent behavior and prompts from backend config.

Tasks:

- Store default agent config in app assets.
  - `assets/agents/default/agent.json`
  - `assets/agents/default/prompts/planner_system.md`
  - `assets/agents/default/prompts/decision_system.md`
  - `assets/agents/default/prompts/replier_user.md`
  - `assets/agents/default/parser.json`

- Copy or convert existing backend prompt templates.
  - Keep prompt keys aligned with backend:
    - `planner_system`
    - `decision_system`
    - `replier_user`

- Implement user-editable agent config.
  - Import/export config.
  - Per Live2D model agent profile.
  - Model/provider selection per planner/replier.

- Implement memory stores.
  - recent chat history
  - user impression
  - mood state
  - long-term memory entries

- Implement memory update tools gradually.

Acceptance:

- Default agent works without external config.
- Prompt templates can be overridden.
- Replier prompt includes history, mood, and impression when available.

## Phase 10: UI And Service Migration

Goal: make local runtime the normal product experience.

Tasks:

- Replace "connect to server" as the main UI concept.
  - Local engine state:
    - stopped
    - starting
    - ready
    - error
  - Provider settings:
    - endpoint
    - API key
    - planner model
    - replier model
    - tool-calling support

- Keep remote mode under advanced settings.
  - Useful for debugging against backend.
  - Useful during parity testing.

- Update `ChatConnectionService`.
  - Start local runtime automatically.
  - No server URL required in local mode.
  - Preserve Messenger protocol.

- Update `ChatServiceClient`.
  - Add local/remote mode config methods if needed.
  - Keep existing `sendUserMessage()`.

- Update UI labels.
  - Avoid requiring URL for local mode.
  - Show LLM provider status instead.

Acceptance:

- Fresh install can chat after provider config is present.
- Existing UI, wallpaper, and widget still use the same service client.
- Remote backend mode remains available but is not required.

## Phase 11: Tests And Parity Verification

Goal: prove the local runtime covers backend behavior before removing old dependency paths.

Unit tests:

- `MessageBase` JSON compatibility.
- `InboundBuilder` text/image/emoji/voice conversion.
- parser command parsing.
- parser mention parsing.
- trigger priority ordering.
- trigger interrupt eligibility.
- `PerceptionProcessor` queue behavior.
- `PlannerLoop` fixed fake LLM flow.
- duplicate `replier` rejection.
- stale foreground epoch rejection.
- `ReplierTask` cancellation.
- background/adopt/kill decision flow.
- Room DAO history queries.

Integration tests:

- UI sends text -> local runtime replies.
- Wallpaper sends text -> local runtime replies.
- Message history persists across process restart.
- Local runtime starts without WebSocket URL.
- Remote mode still connects to backend.

Manual checks:

- `./gradlew assembleDebug`
- install debug APK
- send one message in app
- send one message from wallpaper widget/input
- switch Live2D model
- verify history separation per model/agent
- interrupt a long reply with a new message

## Implementation Order

Recommended first implementation slices:

1. Transport split.
   - Extract current WebSocket code behind `RemoteWebSocketTransport`.
   - Add `LocalTransport` with fixed reply.

2. Local runtime skeleton.
   - `ChatRuntime`
   - `ReplySink`
   - fixed assistant response
   - local mode default

3. Room storage.
   - messages
   - standard messages
   - migration from SharedPreferences history

4. Inbound and perception.
   - `InboundBuilder`
   - `PerceptionProcessor`
   - `Trigger`

5. Simplified planner.
   - fake LLM planner
   - fixed `replier` tool result
   - final reply send

6. Real LLM client.
   - OpenAI-compatible provider
   - planner and replier configs

7. Full `ReplierTool`.
   - prompt builder
   - streaming
   - cancellation

8. Interruption and decision tools.
   - background task
   - adopt
   - kill

9. Environment and Live2D tools.
   - world state
   - motion trigger
   - optional visual context

10. Prompt/config/memory parity.
    - default agent config
    - user-editable profiles
    - mood/impression/memory

## Logic Coverage Checklist

Core routing:

- [x] User input becomes standard message.
- [x] Standard message becomes internal message.
- [x] Internal message has context id and agent id.
- [x] Message is persisted before reply generation.
- [x] Message parser extracts text, command, mention, sender, timestamp.
- [x] Multimodal blocks are retained.
- [x] Trigger is created with correct type and priority.
- [x] Trigger is routed to the correct planner loop.

Planner:

- [x] One loop per `(contextId, agentId)`.
- [x] Priority queue sorts by priority and timestamp.
- [x] `MSG` triggers can interrupt.
- [x] `ENV` and `SYS` triggers queue.
- [x] Foreground epoch blocks stale sends.
- [x] Planner session is persisted.
- [x] Native tool calling works.
- [x] JSON fallback works for providers without tool calling.
- [x] Final assistant text fallback sends only when no tool reply was sent.

Replier:

- [x] `replier` validates content.
- [ ] Replier prompt includes persona.
- [ ] Replier prompt includes current message.
- [ ] Replier prompt includes history.
- [ ] Replier prompt includes mood.
- [ ] Replier prompt includes impression.
- [ ] Replier prompt includes live image blocks when requested.
- [x] Streaming updates task preview.
- [x] Cancellation works.
- [x] Backgrounding works.
- [x] Planner-managed mode returns text but does not send directly.

Decision mode:

- [x] New user message during generation moves current task to background.
- [x] Decision mode restricts tools.
- [x] Adopt waits for old background task and sends its reply.
- [x] Kill cancels old background task.
- [ ] Foreground continues correctly after kill or adopt failure.

Reply delivery:

- [ ] `ReplySink` creates assistant `MessageBase`.
- [ ] Assistant message is added to `standardMessages`.
- [ ] Assistant visible message is added to `messages`.
- [ ] UI receives update.
- [ ] Wallpaper receives update.
- [ ] Widget receives update if active.
- [ ] History persists.

Tools:

- [x] `replier`
- [x] `wait_for`
- [x] `adopt_background_reply`
- [x] `kill_background_reply`
- [x] `get_world_state`
- [x] `look_at`
- [x] `trigger_motion`
- [ ] memory/impression tools

Android integration:

- [ ] Local mode starts without server URL.
- [ ] Remote WebSocket mode still works.
- [ ] API key is stored securely.
- [ ] No API key appears in logs.
- [ ] Runtime survives service restarts where possible.
- [ ] Long-running generation handles Android lifecycle cancellation.

## Risks And Decisions

Open decisions:

- Whether the first LLM provider is strictly OpenAI-compatible HTTP or also includes a local on-device model.
- Whether planner and replier should always be separate models or default to one model with separate configs.
- Whether remote backend compatibility remains long-term or only during migration.
- How much visual context `look_at` can access without unsafe screenshot permissions.
- How to represent Live2D expression state if a model lacks explicit expression metadata.

Risks:

- Android process death can kill in-flight planner/replier tasks.
- Tool-calling support differs across providers.
- Long prompts may exceed mobile network/provider limits.
- Streaming + interruption must be carefully serialized to avoid duplicate sends.
- Room migrations must be stable before replacing SharedPreferences history fully.

Mitigations:

- Persist planner rounds and task state.
- Keep foreground epoch checks.
- Add fake LLM tests for state-machine behavior.
- Keep remote mode until local parity is proven.
- Start with minimal tools, then expand.

## Done Definition

The migration is considered complete when:

- Maimchat can run a full chat session without `/home/tcmofashi/chatbot/l2d_backend`.
- User input, planner decision, replier generation, interruption, background decision, and final send all happen in app.
- UI, wallpaper, and widget all receive local runtime replies.
- WebSocket backend mode is optional, not required.
- `./gradlew assembleDebug` succeeds.
- Core planner/replier behavior has fake LLM tests and at least one real-provider manual test.
