package com.l2dchat.chat

import android.content.Context
import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.l2dchat.chat.transport.ChatTransportCallbacks
import com.l2dchat.chat.transport.LocalTransport
import com.l2dchat.core.config.AgentLlmSettingsOverride
import com.l2dchat.core.config.DefaultAgentProfileSeeder
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.PersonaRegistry
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.worker.WorkerResultRecovery
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.RuntimeMessageMapper
import com.l2dchat.core.message.VisibleMessageRecord
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerSessionStore
import com.l2dchat.core.reply.EmptyPlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerTurnContext
import com.l2dchat.core.storage.ChatDatabase
import com.l2dchat.core.storage.ChatHistoryStore
import com.l2dchat.core.storage.RoomChatHistoryStore
import com.l2dchat.core.storage.RoomPlannerSessionStore
import com.l2dchat.core.storage.RoomPlannerSystemPromptProvider
import com.l2dchat.core.storage.RoomPerceptionStore
import com.l2dchat.core.storage.RoomReplierPromptContextProvider
import com.l2dchat.core.storage.RoomReplierTaskStore
import com.l2dchat.core.storage.RuntimeStateDao
import com.l2dchat.core.tools.EmptyReplierPromptContextProvider
import com.l2dchat.core.tools.NoopReplierTaskStore
import com.l2dchat.core.tools.ReplierPromptContext
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskStore
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.preferences.ChatPreferenceKeys
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class ChatWebSocketManager {
    companion object {
        const val DEFAULT_USER_NICKNAME = "我"
        private const val HISTORY_PREFS = "chat_history"
        private const val HISTORY_LIMIT = 200
        private const val ROOM_IMPORT_PREFIX = "room_imported_"
        // Reply reconcile/poll backstop cadence + look-back window.
        private const val REPLY_RECONCILE_INTERVAL_MS = 5_000L
        private const val REPLY_RECONCILE_WINDOW_MS = 15 * 60_000L
    }
    private val logger = L2DLogger.module(LogModule.CHAT)
    private val gson = Gson()
    private var localLlmSettings = LocalLlmSettings()
    private var workerLlmSettings = com.l2dchat.core.config.WorkerLlmSettings()
    private val messageHandler = Live2DChatMessageHandler()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val _runtimeState = MutableStateFlow(RuntimeState.STOPPED)
    val runtimeState: StateFlow<RuntimeState> = _runtimeState.asStateFlow()
    private val _errors =
            MutableSharedFlow<String>(
                    extraBufferCapacity = 8,
                    onBufferOverflow = BufferOverflow.DROP_OLDEST
            )
    val errors: SharedFlow<String> = _errors.asSharedFlow()
    private val environmentStateProvider = ChatEnvironmentStateProvider()
    private val environmentTriggerEmitter = ChatEnvironmentTriggerEmitter()
    private val localMotionController =
            ChatMotionController { command -> emitMotionMessage(command) }
    private val transportCallbacks =
            object : ChatTransportCallbacks {
                override fun onStateChanged(state: RuntimeState) {
                    _runtimeState.value = state
                }

                override fun onIncomingText(text: String) {
                    scope.launch { handleIncomingMessage(text) }
                }

                override fun onError(message: String, throwable: Throwable?) {
                    reportConnectionError(message, throwable)
                }

                override fun onProcessingChanged(processing: Boolean) {
                    val count =
                            if (processing) processingCount.incrementAndGet()
                            else processingCount.decrementAndGet()
                    _processing.value = count > 0
                    // Agent activity is now an inline conversation message (see startAgentActivity), so
                    // there's no separate floating indicator to clear here.
                }

                override fun onMessageFailed(
                        messageId: String?,
                        message: String,
                        throwable: Throwable?
                ) {
                    markMessageFailed(messageId)
                    reportConnectionError(message, throwable)
                }
            }
    private val localTransport =
            LocalTransport(
                    scope = scope,
                    callbacks = transportCallbacks,
                    agentNameProvider = { receiverModelName },
                    perceptionStoreFactory = { localPerceptionStoreFor() },
                    plannerSessionStoreFactory = { localPlannerSessionStoreFor() },
                    plannerSystemPromptProvider = {
                        localPlannerSystemPromptProviderFor(
                                RoomPlannerSystemPromptProvider.PLANNER_SYSTEM_TEMPLATE
                        )
                    },
                    decisionPlannerSystemPromptProvider = {
                        localPlannerSystemPromptProviderFor(
                                RoomPlannerSystemPromptProvider.DECISION_SYSTEM_TEMPLATE
                        )
                    },
                    replierPromptContextProvider = { localReplierPromptContextProviderFor() },
                    replierTaskStoreProvider = { localReplierTaskStoreFor() },
                    localRuntimeLlmConfigProvider = {
                        localLlmSettings.toRuntimeConfig(
                                agentOverride =
                                        AgentLlmSettingsOverride.fromAgentConfig(activeAgentConfig)
                        )
                    },
                    environmentStateProvider = environmentStateProvider,
                    motionController = localMotionController,
                    runtimeStateDaoProvider = { localRuntimeStateDaoForTools() },
                    // Give the planner an on-device worker sub-agent (proot/Alpine/python Claude-Code
                    // port in the headless engine pkg). Context + creds resolve lazily at call time.
                    extraNormalTools =
                            listOf(
                                    com.l2dchat.core.tools.AskAiAgentTool(
                                            contextProvider = { appContext },
                                            // Detached dispatch: hand the task to the background worker
                                            // manager and return at once; the turn ends, the worker runs
                                            // off-turn, its result returns later as a SYS trigger.
                                            dispatch = { task, goal, origin ->
                                                backgroundWorkerManager.dispatch(task, goal, origin)
                                            },
                                    )
                            ),
                    // Every planner turn sees the live goals+status of this conversation's detached
                    // background workers, so it always knows what's running off-turn.
                    backgroundStatusProvider =
                            com.l2dchat.core.reply.BackgroundWorkerContextProvider { cid, aid ->
                                backgroundWorkerManager.statusBlock(cid, aid)
                            },
            )
    /** Worker LLM creds: its own config if set, else fall back to the chat LLM planner config. */
    private fun workerCredsOrNull(): com.l2dchat.worker.WorkerCreds? {
        val w = workerLlmSettings
        val effective =
                if (w.hasConnection()) w
                else {
                    val l = localLlmSettings
                    if (l.enabled && !l.baseUrl.isNullOrBlank() && !l.plannerModel.isNullOrBlank())
                            com.l2dchat.core.config.WorkerLlmSettings(
                                    baseUrl = l.baseUrl,
                                    apiKey = l.apiKey,
                                    model = l.plannerModel,
                                    settingsJson = null,
                            )
                    else null
                }
        return effective?.let {
            com.l2dchat.worker.WorkerCreds(
                    apiKey = it.apiKey.orEmpty(),
                    baseUrl = it.baseUrl.orEmpty(),
                    model = it.model.orEmpty(),
                    settingsJson = it.effectiveSettingsJson(),
            )
        }
    }

    /**
     * Compress the recent conversation for [origin] into a short background block via a cheap compact
     * LLM call, so a freshly-dispatched worker knows what was said/produced before (e.g. a file path
     * from an earlier task). Best-effort — returns null on any miss.
     */
    private suspend fun compactPlannerContext(origin: com.l2dchat.worker.WorkerOrigin): String? {
        val settings = localLlmSettings
        val baseUrl = settings.baseUrl?.takeIf { it.isNotBlank() }
                ?: run { logger.info("compact: no baseUrl (localLlmSettings empty?)"); return null }
        // Compact model: a fast, non-thinking model — reuse the replier model, else the planner model.
        val model =
                (settings.replierModel ?: settings.plannerModel)?.takeIf { it.isNotBlank() }
                        ?: run { logger.info("compact: no model"); return null }
        val ctx = appContext ?: return null
        val dao =
                com.l2dchat.core.storage.ChatDatabase.getInstance(ctx.applicationContext)
                        .runtimeMessageDao()
        // History is stored under the persona/history context (= activeModelKey), NOT the trigger's
        // user-scoped contextId. Fall back to the origin agent if the active key is missing.
        val historyContextId = activeModelKey ?: origin.agentId
        val rows =
                runCatching { dao.queryRecentStandardMessages(historyContextId, null, 16) }
                        .getOrNull()
                        ?.reversed()
                        ?: return null
        val transcript =
                rows.mapNotNull { m ->
                            val text = m.rawText?.trim()?.takeIf { it.isNotBlank() } ?: return@mapNotNull null
                            val who = if (m.senderUserId == activeModelKey) "助手" else "用户"
                            "$who：${text.take(400)}"
                        }
                        .joinToString("\n")
        if (transcript.isBlank()) { logger.info("compact: empty transcript"); return null }
        logger.info("compact: model=$model transcript=${transcript.length} chars")
        val client =
                com.l2dchat.core.llm.OpenAiCompatibleClient(
                        baseUrl = baseUrl,
                        apiKeyProvider = { settings.apiKey }
                )
        val resp =
                runCatching {
                            client.chatCompletion(
                                    messages =
                                            listOf(
                                                    com.l2dchat.core.llm.LlmMessage.system(
                                                            "把下面这段用户与助手的对话压缩成一段简短的“背景摘要”，供接下来一个后台任务参考。" +
                                                                    "尽量保留一切具体信息：用户提到的事实/偏好/数据（如喜欢的数字、名字、参数、要求）、" +
                                                                    "已经产出过的东西（尤其是文件路径/文件名、之前算出的结果、之前做过的事），以及正在进行或提到的任务。" +
                                                                    "宁可多留也不要漏掉具体信息。只有当整段对话都只是寒暄、没有任何具体内容时，才回复“无”。" +
                                                                    "直接给摘要，不要评论客套，不超过 200 字。"
                                                    ),
                                                    com.l2dchat.core.llm.LlmMessage.user(
                                                            "对话（旧→新）：\n$transcript"
                                                    )
                                            ),
                                    config =
                                            com.l2dchat.core.llm.LlmGenerationConfig(
                                                    model = model,
                                                    temperature = 0.3,
                                                    maxTokens = 400,
                                                    enableThinking = false
                                            )
                            )
                        }
                        .onFailure { logger.warn("compact: LLM call failed", it) }
                        .getOrNull()
                        ?: return null
        val out = resp.text.trim()
        logger.info("compact: got ${out.length} chars: ${out.take(60)}")
        return if (out.isBlank() || out == "无") null else out
    }

    /**
     * Detached background-worker manager: `ask_ai_agent` dispatches off-turn (≤3 concurrent), and each
     * worker's result returns as a SYS trigger the planner delivers. Lazy so appContext is set first.
     */
    private val backgroundWorkerManager: com.l2dchat.worker.BackgroundWorkerManager by lazy {
        com.l2dchat.worker.BackgroundWorkerManager(
                context = appContext?.applicationContext
                                ?: throw IllegalStateException("BackgroundWorkerManager: no app context"),
                scope = scope,
                credsProvider = { workerCredsOrNull() },
                fileSink = { sub -> emitFileMessage(sub) },
                onProgress = { _, _, kind, text ->
                    when (kind) {
                        "agent_start" -> {
                            runBrowserUsed = false
                            startAgentActivity(com.l2dchat.worker.WorkerActivity.running(text).toJson())
                        }
                        "tool", "status" -> {
                            if (text.contains("browser", ignoreCase = true)) runBrowserUsed = true
                            updateAgentActivity(
                                    com.l2dchat.worker.WorkerActivity.running(text, runBrowserUsed).toJson()
                            )
                        }
                        "agent_done" -> finishAgentActivity(text)
                    }
                },
                onComplete = { origin, goal, result, isError ->
                    val text =
                            if (isError)
                                    "【后台任务失败】目标：$goal。失败信息：$result。请如实、简洁地告诉用户这个任务没能完成。"
                            else
                                    "【后台任务已完成】目标：$goal。结果如下：\n$result\n\n请把这个结果整理后正常回复给用户一次。"
                    com.l2dchat.logging.L2DLogger.module(com.l2dchat.logging.LogModule.CHAT)
                            .info("[worker] onComplete goal='${goal.take(30)}' isError=$isError -> submit SYS")
                    localTransport.submitBackgroundTrigger(
                            com.l2dchat.core.trigger.Trigger(
                                    contextId = origin.contextId,
                                    agentId = origin.agentId,
                                    messageId = "worker_done_" + java.util.UUID.randomUUID(),
                                    triggerType = com.l2dchat.core.trigger.TriggerType.SYS,
                                    priority = com.l2dchat.core.trigger.TriggerPriority.HIGH,
                                    timestampSeconds = System.currentTimeMillis() / 1000.0,
                                    payload = mapOf("text" to text),
                            )
                    )
                },
                compactContext = { origin -> compactPlannerContext(origin) },
        )
    }

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()

    // The on-device AI agent's activity is an INLINE message in the conversation flow (one per
    // ask_ai_agent call), inserted on agent_start and updated in place through agent_done. It is
    // session-only (not persisted to history, never fed to the model). `runBrowserUsed` lights the
    // bubble's browser icon; `currentAgentMsgId` is the id of the in-flight agent bubble being updated.
    @Volatile private var runBrowserUsed = false
    @Volatile private var currentAgentMsgId: String? = null

    /** Insert a new inline agent-activity bubble (ephemeral) at the end of the conversation. */
    @Synchronized
    private fun startAgentActivity(json: String) {
        val id = "agent_" + java.util.UUID.randomUUID().toString()
        currentAgentMsgId = id
        val list = _messages.value.toMutableList()
        list.add(ChatMessage(id = id, content = "", isFromUser = false, agentActivityJson = json))
        _messages.value = list
    }

    /** Surface a worker-submitted file as a tappable attachment bubble in the conversation (ephemeral). */
    @Synchronized
    private fun emitFileMessage(sub: com.l2dchat.worker.FileSubmission) {
        val json =
                com.google.gson.JsonObject()
                        .apply {
                            addProperty("name", sub.filename)
                            addProperty("path", sub.savedPath)
                            sub.mime?.let { addProperty("mime", it) }
                            addProperty("size", sub.size)
                            sub.description?.let { addProperty("description", it) }
                        }
                        .toString()
        val msg =
                ChatMessage(
                        id = "file_" + java.util.UUID.randomUUID().toString(),
                        content = "",
                        isFromUser = false,
                        fileInfoJson = json,
                )
        _messages.value = _messages.value + msg
        appendVisibleHistory(msg) // persist so the attachment survives a history reload
    }

    /** Update the in-flight agent bubble in place (running→done); re-creates if it was cleared. */
    @Synchronized
    private fun updateAgentActivity(json: String) {
        val id = currentAgentMsgId
        if (id == null) {
            startAgentActivity(json)
            return
        }
        val list = _messages.value.toMutableList()
        val idx = list.indexOfFirst { it.id == id }
        if (idx < 0) {
            startAgentActivity(json)
            return
        }
        list[idx] = list[idx].copy(agentActivityJson = json)
        _messages.value = list
    }

    /** Finalize the agent bubble to its done-summary AND persist it (survives a history reload). */
    @Synchronized
    private fun finishAgentActivity(json: String) {
        updateAgentActivity(json)
        val id = currentAgentMsgId
        val msg = _messages.value.firstOrNull { it.id == id }
        currentAgentMsgId = null
        if (msg != null) appendVisibleHistory(msg)
    }
    private val _standardMessages = MutableStateFlow<List<MessageBase>>(emptyList())
    val standardMessages: StateFlow<List<MessageBase>> = _standardMessages.asStateFlow()
    // True while the runtime is actively processing a turn (planner/replier running). Drives the
    // "thinking" hint in the UI. Tracked with a counter so overlapping turns balance correctly.
    private val _processing = MutableStateFlow(false)
    val processing: StateFlow<Boolean> = _processing.asStateFlow()
    private val processingCount = java.util.concurrent.atomic.AtomicInteger(0)
    // Ids of messages whose turn failed (network/LLM error). Kept out of persisted history so a
    // transient failure does not stick across restarts; surfaced to the UI as a red "!".
    private val failedMessageIds: MutableSet<String> =
            java.util.concurrent.ConcurrentHashMap.newKeySet()
    private val _messageFailures =
            MutableSharedFlow<String>(extraBufferCapacity = 8, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    val messageFailures: SharedFlow<String> = _messageFailures.asSharedFlow()
    // Emitted after a bulk history reload so the service re-pushes a full snapshot to clients.
    private val _historyReloaded =
            MutableSharedFlow<Unit>(extraBufferCapacity = 4, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    val historyReloaded: SharedFlow<Unit> = _historyReloaded.asSharedFlow()

    fun isMessageFailed(id: String): Boolean = failedMessageIds.contains(id)

    private fun markMessageFailed(id: String?) {
        if (id.isNullOrBlank()) return
        if (failedMessageIds.add(id)) {
            scope.launch { _messageFailures.emit(id) }
        }
    }
    private var lastServerMessageTime: Long = 0L
    private var onMotionTrigger: ((String, Int, Boolean) -> Unit)? = null
    private var userId: String = generateUserId()
    private var userNickname: String = DEFAULT_USER_NICKNAME
    private var userCardName: String? = null
    private var receiverModelName: String? = null
    private var activeModelKey: String? = null
    private var appContext: Context? = null
    private var historyStore: ChatHistoryStore? = null
    private var historyLoadGeneration: Long = 0L
    @Volatile private var activeAgentConfig: AgentConfigEntity? = null
    private var activeAgentConfigLoadGeneration: Long = 0L

    enum class RuntimeState {
        STOPPED,
        STARTING,
        RUNNING,
        ERROR
    }

    data class ChatMessage(
            val id: String,
            val content: String,
            val isFromUser: Boolean,
            val timestamp: Long = System.currentTimeMillis(),
            val motionData: MotionData? = null,
            // When set, this message is an INLINE AI-agent activity bubble (running/done summary, JSON of
            // WorkerActivity) rendered in the conversation flow instead of a text bubble. One is inserted
            // per ask_ai_agent call and updated in place. Session-only (not persisted to history).
            val agentActivityJson: String? = null,
            // When set, this message is a FILE the worker submitted (JSON: name/path/mime/size/description),
            // rendered as a tappable attachment bubble. Session-only.
            val fileInfoJson: String? = null
    )

    data class MotionData(val group: String, val index: Int, val loop: Boolean = false)

    fun startLocalRuntime() {
        reapOrphanedPlannerRounds() // one-shot: fail rounds orphaned by a prior process kill
        startReplyReconciler() // poll-deliver any produced-but-undelivered reply (timeout/process death)
        refreshActiveAgentConfig(appContext, activeModelKey, rebuildRuntime = true)
        localTransport.start()
        recoverBufferedWorkerResults() // replay worker results that finished while :chat was dead
    }

    /**
     * A worker task delegated via ask_ai_agent may finish while this (:chat) process is dead — the OS
     * reaped the app while it was backgrounded mid-task, but the engine (separate process, foreground
     * service) kept running the task and durably buffered its result. On startup, pull those buffered
     * results and deliver each straight into the chat as an agent reply, so the user gets the answer
     * they were waiting for. We deliver directly (not via the planner) because the originating turn is
     * gone, and an ambient env-trigger would be dropped when environment replies are off. See
     * [WorkerResultRecovery] + [buildReconciledReply].
     */
    private fun recoverBufferedWorkerResults() {
        val ctx = appContext ?: return
        WorkerResultRecovery(ctx) { contextId, agentId, _, text, isError ->
            // A worker that finished while :chat was dead — deliver its result the SAME way as a live
            // completion: a SYS trigger the planner turns into a persona-voice reply. (Same path as
            // BackgroundWorkerManager.onComplete; the goal is unknown post-restart, so it's omitted.)
            val body = text.trim().ifBlank {
                if (isError) "没有产生结果。" else "没有输出内容。"
            }
            val sysText =
                    if (isError)
                            "【后台任务失败】你之前派到后台的一个任务没能完成：$body。请如实、简洁地告诉用户。"
                    else
                            "【后台任务已完成】你之前派到后台的一个任务跑完了，结果如下：\n$body\n\n请把结果整理后正常回复给用户一次。"
            localTransport.submitBackgroundTrigger(
                    com.l2dchat.core.trigger.Trigger(
                            contextId = contextId,
                            agentId = agentId,
                            messageId = "worker_recovered_" + java.util.UUID.randomUUID(),
                            triggerType = com.l2dchat.core.trigger.TriggerType.SYS,
                            priority = com.l2dchat.core.trigger.TriggerPriority.HIGH,
                            timestampSeconds = System.currentTimeMillis() / 1000.0,
                            payload = mapOf("text" to sysText),
                    )
            )
        }.recover()
    }

    private fun reportConnectionError(message: String, throwable: Throwable? = null) {
        logger.error(message, throwable)
        if (_runtimeState.value != RuntimeState.RUNNING) {
            _runtimeState.value = RuntimeState.ERROR
        }
        scope.launch { _errors.emit(message) }
    }

    fun setUserProfile(nickname: String, cardName: String? = null, userId: String? = null) {
        val sanitizedNickname = nickname.trim()
        userNickname = sanitizedNickname.ifBlank { DEFAULT_USER_NICKNAME }
        userCardName = cardName?.ifBlank { null }

        val resolvedId =
                when {
                    sanitizedNickname.isNotEmpty() -> sanitizedNickname
                    !userId.isNullOrBlank() -> userId.trim()
                    else -> null
                }
        resolvedId?.let { this.userId = it }
    }
    fun setActiveModel(context: Context, modelName: String?) {
        // modelName is the Live2D AVATAR (loaded by the UI). The persona/agent scope is INDEPENDENT —
        // driven by the saved active persona — so switching avatars never changes the persona, and
        // personas keep their own history. Apply the active persona to the agent scope here.
        applyActivePersona(context, readSelectedPersona(context))
    }

    /**
     * Switch the active PERSONA (小千 / 温柔助手). Persists the choice and loads that persona's own
     * history / memory / mood / prompts (each persona is its own routing agentId). The Live2D avatar
     * is unchanged.
     */
    fun setActivePersona(context: Context, personaId: String) {
        val resolved = PersonaRegistry.normalize(personaId)
        writeSelectedPersona(context, resolved)
        clearMessagesEphemeral() // drop the old persona's in-memory view before the new one loads
        applyActivePersona(context, resolved)
    }

    private fun applyActivePersona(context: Context, personaId: String) {
        val resolved = PersonaRegistry.normalize(personaId)
        val displayName = PersonaRegistry.displayNameFor(resolved) ?: resolved
        activeModelKey = resolved
        receiverModelName = displayName
        if (activeAgentConfig?.agentId != activeModelKey) {
            activeAgentConfig = null
        }
        val app = context.applicationContext
        appContext = app
        historyStoreFor(app)
        environmentStateProvider.setApplicationContext(context)
        environmentStateProvider.update(
                ChatEnvironmentUpdate(modelKey = activeModelKey, modelName = receiverModelName)
        )
        seedDefaultAgentProfile(app, activeModelKey)
        refreshActiveAgentConfig(app, activeModelKey, rebuildRuntime = true)
        activeModelKey?.let { loadHistory(app, it) }
        emitModelChangedEnvironmentTrigger()
    }

    private fun readSelectedPersona(context: Context): String =
            PersonaRegistry.normalize(
                    context.applicationContext
                            .getSharedPreferences(
                                    ChatPreferenceKeys.PREFS_NAME,
                                    Context.MODE_PRIVATE
                            )
                            .getString(ChatPreferenceKeys.SELECTED_PERSONA, null)
            )

    private fun writeSelectedPersona(context: Context, personaId: String) {
        context.applicationContext
                .getSharedPreferences(ChatPreferenceKeys.PREFS_NAME, Context.MODE_PRIVATE)
                .edit()
                .putString(ChatPreferenceKeys.SELECTED_PERSONA, personaId)
                .apply()
    }

    fun updateEnvironmentState(update: ChatEnvironmentUpdate) {
        environmentStateProvider.update(update)
        emitEnvironmentUpdateTriggers(update)
    }
    fun reportMotionFinished(
            group: String? = null,
            index: Int? = null,
            filePath: String? = null,
            loop: Boolean = false,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        val transport = localEnvironmentTransport()
        submitEnvironmentTriggers(
                transport = transport,
                submissions =
                        environmentTriggerEmitter.onMotionFinished(
                                motion =
                                        ChatEnvironmentMotionFinished(
                                                group = group,
                                                index = index,
                                                filePath = filePath,
                                                loop = loop,
                                                timestampMillis = timestampMillis
                                        ),
                                context = environmentTriggerContext()
                        )
        )    }
    fun setLocalLlmSettings(settings: LocalLlmSettings) {
        if (localLlmSettings == settings) return
        localLlmSettings = settings
        localTransport.rebuildRuntime("本地 LLM 配置更新")
    }
    /** Worker (cc_research) config — read lazily by the AskAiAgentTool credsProvider at task time. */
    fun setWorkerLlmSettings(settings: com.l2dchat.core.config.WorkerLlmSettings) {
        workerLlmSettings = settings
    }
    fun hasUserNickname(): Boolean = true
    fun getUserNickname(): String = userNickname
    private fun isSenderMe(sender: SenderInfo?): Boolean {
        if (sender == null) return false
        val sid = sender.userInfo?.userId
        if (!sid.isNullOrBlank() && sid == this.userId) return true
        val snick = sender.userInfo?.userNickname
        if (!snick.isNullOrBlank() && snick == userNickname) return true
        return false
    }

    private fun buildStandardMessage(
            segments: List<Seg>,
            messageType: String,
            raw: String? = null,
            additional: Map<String, Any> = emptyMap()
    ): MessageBase {
        val allTypes = segments.map { it.type }
        val rootSeg =
                if (segments.size == 1 && segments[0].type == "seglist") segments[0]
                else Seg("seglist", segments)
        val senderInfo =
                SenderInfo(
                        userInfo =
                                UserInfo(
                                        userId = userId,
                                        userNickname = userNickname,
                                        userCardname = userCardName
                                )
                )
        val receiverInfo =
                ReceiverInfo(
                        userInfo =
                                UserInfo(
                                        // userId must be the normalized agent key (== routing agentId)
                                        // so the assistant's own replies are recognized as assistant
                                        // turns by `senderUserId == agentId`; the human-readable model
                                        // name lives in the nickname for display only.
                                        userId = activeModelKey,
                                        userNickname = receiverModelName
                                )
                )
        val msgInfo =
                BaseMessageInfo(
                        messageId = generateMessageId(),
                        time = System.currentTimeMillis() / 1000.0,
                        senderInfo = senderInfo,
                        receiverInfo = receiverInfo,
                        groupInfo = senderInfo.groupInfo,
                        userInfo = senderInfo.userInfo,
                        formatInfo =
                                FormatInfo(
                                        contentFormat = allTypes.distinct(),
                                        acceptFormat = listOf("text", "image", "emoji", "voice")
                                ),
                        templateInfo = null,
                        additionalConfig =
                                if (additional.isEmpty()) mapOf("message_type" to messageType)
                                else additional + mapOf("message_type" to messageType)
                )
        return MessageBase(msgInfo, rootSeg, raw)
    }

    private fun handleIncomingMessage(text: String) {
        try {
            val standard = MessageBase.fromJsonString(text)
            // Environment-trigger replies (model switched, app foregrounded, snapshot updated,
            // idle timer, ...) drive runtime state but are not user-facing chat turns. Keep them
            // out of the visible message list and persisted history so they don't spam the UI.
            if (standard.messageInfo.additionalConfig?.get("migration_phase") == "env_trigger") {
                return
            }
            addStandardMessage(standard)
            when (val result = messageHandler.handleStandardMessage(standard)) {
                is Live2DChatMessageHandler.ChatMessageResult.Success -> {
                    val fromUser = isSenderMe(standard.messageInfo.senderInfo)
                    val srvTs = ((standard.messageInfo.time ?: 0.0) * 1000).toLong()
                    val isHistorical =
                            srvTs > 0 &&
                                    lastServerMessageTime > 0 &&
                                    srvTs + 1000 < lastServerMessageTime
                    if (!isHistorical) {
                        val adjusted =
                                result.message.copy(
                                        isFromUser = fromUser,
                                        timestamp =
                                                if (srvTs > 0) srvTs else result.message.timestamp
                                )
                        addMessage(adjusted)
                        // The character just replied → play a mood-driven Live2D motion.
                        if (!fromUser) playMoodMotionForReply()
                        if (srvTs > 0 && srvTs > lastServerMessageTime)
                                lastServerMessageTime = srvTs
                    }
                }
                is Live2DChatMessageHandler.ChatMessageResult.VoiceProcessed -> {}
                is Live2DChatMessageHandler.ChatMessageResult.EmojiProcessed -> {}
                is Live2DChatMessageHandler.ChatMessageResult.Error ->
                        logger.error("消息处理错误: ${result.message}")
            }
        } catch (e: Exception) {
            logger.error("处理消息失败", e)
        }
    }

    fun sendUserMessage(content: String) {
        val message = buildStandardMessage(listOf(Seg("text", content)), "chat", raw = content)
        addMessage(
                ChatMessage(
                        id = message.messageInfo.messageId!!,
                        content = content,
                        isFromUser = true
                )
        )
        sendStandardMessage(message)
    }
    fun sendStandardMessage(message: MessageBase) {
        addStandardMessage(message)
        if (!localTransport.send(message)) {
            logger.warn("发送消息失败：本地运行时")
        }
    }

    fun sendMotionMessage(group: String, index: Int, loop: Boolean = false) {
        sendStandardMessage(
                buildMotionMessage(MotionCommand(group = group, index = index, loop = loop))
        )
    }
    fun triggerModelMotion(group: String, index: Int, loop: Boolean = false) {
        onMotionTrigger?.invoke(group, index, loop)
        sendMotionMessage(group, index, loop)
    }
    fun setMotionTriggerCallback(callback: (String, Int, Boolean) -> Unit) {
        onMotionTrigger = callback
    }
    fun getMessageEvents() = messageHandler.messageEvents
    private fun emitMotionMessage(command: MotionCommand): Boolean {
        if (command.group != null && command.index != null) {
            onMotionTrigger?.invoke(command.group, command.index, command.loop)
        }
        addStandardMessage(buildMotionMessage(command))
        return true
    }

    /**
     * Play a mood-driven Live2D motion for the character's reply: one-to-many — the current mood maps to
     * a group of candidate motions and one is picked at random (see [MoodMotionMap]). Real moods use
     * deliberate non-idle motions; a weak/absent mood plays a calm motion. The mood is read through the
     * same Room connection the LLM writes it to. Called on each assistant reply.
     */
    private fun playMoodMotionForReply() {
        val modelKey = activeModelKey
        if (!MoodMotionMap.supports(modelKey)) return
        val dao = localRuntimeStateDaoForTools() ?: return
        val contextId = historyContextId(modelKey)
        val agentId = historyAgentId(modelKey) ?: return
        scope.launch {
            // Dev hook (inert in normal use): files/debug_mood.txt = "valence,arousal" forces the mood
            // used for selection, so the mapping can be tuned without waiting for the LLM.
            val debugMood = runCatching {
                val f = java.io.File(appContext?.filesDir, "debug_mood.txt")
                if (f.exists()) f.readText().trim().split(",")
                        .let { it[0].trim().toDouble() to it[1].trim().toDouble() }
                else null
            }.getOrNull()
            val category: MoodMotionMap.MoodCategory
            val where: String
            if (debugMood != null) {
                category = MoodMotionMap.categoryOf(debugMood.first, debugMood.second)
                where = "debug v=${debugMood.first} a=${debugMood.second}"
            } else {
                val mood = runCatching { dao.queryMoodState(contextId, agentId) }.getOrNull()
                if (mood == null) {
                    category = MoodMotionMap.NO_MOOD
                    where = "no-mood"
                } else {
                    val (v, a) =
                            com.l2dchat.core.tools.decayedMood(
                                    mood.valence,
                                    mood.arousal,
                                    mood.updatedAtMillis,
                                    System.currentTimeMillis(),
                            )
                    category = MoodMotionMap.categoryOf(v, a)
                    where = "v=${"%.2f".format(v)},a=${"%.2f".format(a)}"
                }
            }
            MoodMotionMap.pick(modelKey, category)?.let { m ->
                android.util.Log.i(
                        "MoodMotion",
                        "reply -> $category ($where) play ${m.group}/${m.index} model=$modelKey",
                )
                emitMotionMessage(MotionCommand(group = m.group, index = m.index))
            }
        }
    }

    private fun buildMotionMessage(command: MotionCommand): MessageBase =
            buildStandardMessage(
                    listOf(Seg("text", MotionMessage.displayText(command))),
                    MotionMessage.TYPE,
                    raw = MotionMessage.displayText(command),
                    additional = MotionMessage.additionalConfig(command)
            )

    @Synchronized
    private fun addMessage(message: ChatMessage) {
        val list = _messages.value.toMutableList()
        if (list.any { it.id == message.id }) return
        list.add(message)
        _messages.value = list
        syncEnvironmentRecentMessages()
        appendVisibleHistory(message)
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) saveHistory(ctx, key)    }

    private fun addStandardMessage(message: MessageBase) {
        val list = _standardMessages.value.toMutableList()
        val mid = message.messageInfo.messageId
        if (!mid.isNullOrBlank() && list.any { it.messageInfo.messageId == mid }) return
        list.add(message)
        _standardMessages.value = list
        appendStandardHistory(message)
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) saveHistory(ctx, key)
    }

    fun clearMessages() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        failedMessageIds.clear()
        environmentStateProvider.clearRecentMessages()
        lastServerMessageTime = 0L
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) {
            saveHistory(ctx, key) // overwrite the legacy SharedPreferences history with the empty list
        }
        clearAllPersistentChatData()
    }

    /**
     * Wipe ALL persisted chat data so "清空聊天记录" yields a genuinely EMPTY context: the conversation
     * (messages / standard_messages / media_blocks), the persona STATE that is re-injected into every
     * planner+replier prompt (memories / impressions / mood_state — without this the persona keeps
     * remembering the user and carries its mood across a clear), and the planner session logs
     * (planner_rounds / planner_messages / tool_tasks). Unscoped on purpose: the same conversation's
     * rows are spread across inconsistent context/agent ids (user id vs agent self-id), so a
     * per-context delete misses some. Agent CONFIG (agent_configs / prompt_templates) is preserved —
     * that's the persona definition, not chat data.
     */
    private fun clearAllPersistentChatData() {
        val context = appContext ?: return
        scope.launch {
            runCatching {
                val db = ChatDatabase.getInstance(context.applicationContext)
                db.runtimeMessageDao().apply {
                    deleteAllMessages()
                    deleteAllStandardMessages()
                }
                db.runtimeStateDao().apply {
                    deleteAllMemories()
                    deleteAllImpressions()
                    deleteAllMoodState()
                    deleteAllMediaBlocks()
                }
                db.plannerStateDao().apply {
                    deleteAllPlannerRounds()
                    deleteAllPlannerMessages()
                    deleteAllToolTasks()
                }
            }
                    .onFailure { logger.error("彻底清空聊天数据失败", it) }
        }
    }

    fun clearMessagesEphemeral() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        failedMessageIds.clear()
        environmentStateProvider.clearRecentMessages()
        lastServerMessageTime = 0L    }
    fun loadHistory(context: Context, modelKey: String) {
        val app = context.applicationContext
        appContext = app
        historyStoreFor(app)
        val generation = ++historyLoadGeneration
        val legacy = readLegacyHistory(app, modelKey)
        applyLoadedHistory(legacy.visibleMessages, legacy.standardMessages)
        loadRoomHistory(app, modelKey, legacy, generation)
    }
    fun saveHistory(context: Context, modelKey: String) {
        try {
            val editor = historyPreferences(context).edit()
            val msgs = _messages.value.takeLast(HISTORY_LIMIT)
            val arrMsgs = JsonArray()
            msgs.forEach { m ->
                val o = JsonObject()
                o.addProperty("id", m.id)
                o.addProperty("content", m.content)
                o.addProperty("isFromUser", m.isFromUser)
                o.addProperty("timestamp", m.timestamp)
                arrMsgs.add(o)
            }
            val stds = _standardMessages.value.takeLast(HISTORY_LIMIT)
            val arrStd = JsonArray()
            stds.forEach { s -> arrStd.add(s.toJsonString()) }
            editor.putString("messages_" + modelKey, gson.toJson(arrMsgs))
            editor.putString("standard_" + modelKey, gson.toJson(arrStd))
            editor.apply()
        } catch (e: Exception) {
            logger.error("保存历史失败", e)
        }
    }

    private data class StoredHistory(
            val visibleMessages: List<ChatMessage>,
            val standardMessages: List<MessageBase>
    )

    private fun readLegacyHistory(context: Context, modelKey: String): StoredHistory {
        try {
            val sp = historyPreferences(context)
            val msgsStr = sp.getString("messages_" + modelKey, null)
            val stdStr = sp.getString("standard_" + modelKey, null)
            val loadedMsgs = mutableListOf<ChatMessage>()
            val loadedStd = mutableListOf<MessageBase>()
            if (!msgsStr.isNullOrBlank()) {
                try {
                    val arr = gson.fromJson(msgsStr, JsonArray::class.java)
                    arr?.forEach { el ->
                        if (el.isJsonObject) {
                            val o = el.asJsonObject
                            val id = o.get("id")?.asString ?: return@forEach
                            val content = o.get("content")?.asString ?: ""
                            val isFromUser = o.get("isFromUser")?.asBoolean ?: false
                            val ts = o.get("timestamp")?.asLong ?: System.currentTimeMillis()
                            loadedMsgs.add(ChatMessage(id, content, isFromUser, ts))
                        }
                    }
                } catch (_: Exception) {}
            }
            if (!stdStr.isNullOrBlank()) {
                try {
                    val arr = gson.fromJson(stdStr, JsonArray::class.java)
                    arr?.forEach { el ->
                        if (el.isJsonPrimitive && el.asJsonPrimitive.isString) {
                            try {
                                loadedStd.add(MessageBase.fromJsonString(el.asString))
                            } catch (_: Exception) {}
                        }
                    }
                } catch (_: Exception) {}
            }
            return StoredHistory(loadedMsgs, loadedStd)
        } catch (e: Exception) {
            logger.error("加载历史失败", e)
        }
        return StoredHistory(emptyList(), emptyList())
    }

    private fun applyLoadedHistory(
            visibleMessages: List<ChatMessage>,
            standardMessages: List<MessageBase>
    ) {
        _messages.value = visibleMessages
        _standardMessages.value = standardMessages
        syncEnvironmentRecentMessages()
        lastServerMessageTime =
                _standardMessages.value
                        .maxOfOrNull { (((it.messageInfo.time) ?: 0.0) * 1000).toLong() }
                        ?.takeIf { it > 0L }
                        ?: 0L
        // A bulk reload (legacy then Room) REPLACES the whole list; the per-message broadcast only
        // re-sends the last message, so signal clients to pull a fresh full snapshot — otherwise
        // reloaded agent/file bubbles (which carry JSON only in the Room copy) show as empty.
        scope.launch { _historyReloaded.emit(Unit) }
    }

    private fun loadRoomHistory(
            context: Context,
            modelKey: String,
            legacy: StoredHistory,
            generation: Long
    ) {
        val store = historyStoreFor(context)
        val contextId = historyContextId(modelKey)
        val agentId = historyAgentId(modelKey)
        val baselineVisibleIds = legacy.visibleMessages.map { it.id }.toSet()
        val baselineStandardIds = legacy.standardMessages.map { it.historyIdentity() }.toSet()
        scope.launch {
            try {
                importLegacyHistoryIfNeeded(context, store, modelKey, contextId, agentId, legacy)
                val roomVisible =
                        store.queryRecentVisibleMessages(contextId, agentId, HISTORY_LIMIT)
                                .asReversed()
                                .map { it.toChatMessage() }
                val roomStandard =
                        store.queryRecentStandardMessages(contextId, agentId, HISTORY_LIMIT)
                                .asReversed()
                if (
                        canApplyRoomHistory(
                                modelKey,
                                generation,
                                baselineVisibleIds,
                                baselineStandardIds
                        )
                ) {
                    applyLoadedHistory(roomVisible, roomStandard)
                    saveHistory(context, modelKey)
                }
            } catch (e: Exception) {
                logger.error("加载 Room 历史失败", e)
            }
        }
    }

    private suspend fun importLegacyHistoryIfNeeded(
            context: Context,
            store: ChatHistoryStore,
            modelKey: String,
            contextId: String,
            agentId: String?,
            legacy: StoredHistory
    ) {
        val sp = historyPreferences(context)
        val importKey = roomImportKey(modelKey)
        if (sp.getBoolean(importKey, false)) return

        legacy.visibleMessages.forEach { message ->
            store.appendVisibleMessage(contextId, agentId, message.toVisibleRecord())
        }
        legacy.standardMessages.forEach { message ->
            store.appendStandardMessage(
                    contextId = contextId,
                    agentId = agentId,
                    message = message,
                    fallbackTimestampMillis = fallbackTimestampMillis(message)
            )
        }
        sp.edit().putBoolean(importKey, true).apply()
    }

    private fun canApplyRoomHistory(
            modelKey: String,
            generation: Long,
            baselineVisibleIds: Set<String>,
            baselineStandardIds: Set<String>
    ): Boolean {
        if (generation != historyLoadGeneration || activeModelKey != modelKey) return false
        val currentVisibleIds = _messages.value.map { it.id }.toSet()
        val currentStandardIds = _standardMessages.value.map { it.historyIdentity() }.toSet()
        return currentVisibleIds == baselineVisibleIds && currentStandardIds == baselineStandardIds
    }

    private fun appendVisibleHistory(message: ChatMessage) {
        val key = activeModelKey ?: return
        val context = appContext ?: return
        val store = historyStoreFor(context)
        val contextId = historyContextId(key)
        val agentId = historyAgentId(key)
        scope.launch {
            try {
                store.appendVisibleMessage(contextId, agentId, message.toVisibleRecord())
            } catch (e: Exception) {
                logger.error("保存可见消息到 Room 失败", e)
            }
        }
    }

    private fun appendStandardHistory(message: MessageBase) {
        val key = activeModelKey ?: return
        val context = appContext ?: return
        val store = historyStoreFor(context)
        val contextId = historyContextId(key)
        val agentId = historyAgentId(key)
        scope.launch {
            try {
                store.appendStandardMessage(
                        contextId = contextId,
                        agentId = agentId,
                        message = message,
                        fallbackTimestampMillis = fallbackTimestampMillis(message)
                )
            } catch (e: Exception) {
                logger.error("保存标准消息到 Room 失败", e)
            }
        }
    }

    private fun historyStoreFor(context: Context): ChatHistoryStore =
            historyStore
                    ?: RoomChatHistoryStore(
                                    ChatDatabase.getInstance(context.applicationContext)
                                            .runtimeMessageDao()
                            )
                            .also { historyStore = it }

    private fun localPerceptionStoreFor(): PerceptionStore? {
        val context = appContext ?: return null
        val database = ChatDatabase.getInstance(context.applicationContext)
        return RoomPerceptionStore(
                messageDao = database.runtimeMessageDao(),
                stateDao = database.runtimeStateDao()
        )
    }

    private fun localPlannerSessionStoreFor(): PlannerSessionStore {
        val context = appContext ?: return NoopPlannerSessionStore
        val database = ChatDatabase.getInstance(context.applicationContext)
        return RoomPlannerSessionStore(database.plannerStateDao())
    }

    @Volatile private var orphanedRoundsReaped = false

    /**
     * Startup reconciliation (A): a planner round only leaves 'generating' via its coroutine
     * finalizer, which a hard process kill (ColorOS LMK / freeze→kill / reinstall) skips. On a fresh
     * `:chat` process every such row is an orphan that can never resume — so fail it and surface its
     * originating message as failed, instead of letting the UI wait forever on a reply that will never
     * come. The cutoff = this process's start, so a round created by THIS process is never reaped.
     */
    private fun reapOrphanedPlannerRounds() {
        if (orphanedRoundsReaped) return
        val context = appContext ?: return // not ready yet — a later call will retry
        orphanedRoundsReaped = true
        val cutoffMillis = System.currentTimeMillis()
        scope.launch {
            runCatching {
                val dao = ChatDatabase.getInstance(context.applicationContext).plannerStateDao()
                val triggerIds = dao.queryGeneratingRoundTriggerIds(cutoffMillis).filterNotNull()
                val reaped = dao.failGeneratingRounds(cutoffMillis, cutoffMillis)
                if (reaped > 0) {
                    logger.info("startup: reaped $reaped orphaned planner round(s)")
                    triggerIds.forEach { id ->
                        failedMessageIds.add(id)
                        _messageFailures.emit(id)
                    }
                }
            }
                    .onFailure { logger.warn("orphaned-round reconciliation failed", it) }
        }
    }

    // Reconcile/poll reply delivery: a planner reply is ALWAYS durably saved (planner_messages), but
    // the one-shot chat push can be lost to delivery timing (await timeout / process death). This
    // backstop re-delivers any completed user-message round whose reply never reached the chat. Dedup
    // is by the STABLE id "reply_<triggerMessageId>" (addMessage + addStandardMessage both dedup by id),
    // so the fast push and this reconciler can never double-show a reply.
    private val deliveredReplyRoundIds =
            java.util.Collections.synchronizedSet(mutableSetOf<String>())
    @Volatile private var replyReconcilerStarted = false

    private fun startReplyReconciler() {
        reconcileUndeliveredReplies() // immediate catch-up on startup
        if (replyReconcilerStarted) return
        replyReconcilerStarted = true
        scope.launch {
            while (isActive) {
                delay(REPLY_RECONCILE_INTERVAL_MS)
                reconcileUndeliveredReplies()
            }
        }
    }

    private fun reconcileUndeliveredReplies() {
        val context = appContext ?: return
        scope.launch {
            runCatching {
                        val dao =
                                ChatDatabase.getInstance(context.applicationContext).plannerStateDao()
                        val sinceMs = System.currentTimeMillis() - REPLY_RECONCILE_WINDOW_MS
                        val rows = dao.queryRecentCompletedReplies(sinceMs)
                        for (row in rows) {
                            if (row.roundId in deliveredReplyRoundIds) continue
                            val tid = row.triggerMessageId ?: continue
                            val replyId = "reply_$tid"
                            if (_standardMessages.value.any {
                                        it.messageInfo.messageId == replyId
                                    }
                            ) {
                                // already shown (fast push delivered it) — remember and skip
                                deliveredReplyRoundIds.add(row.roundId)
                                continue
                            }
                            logger.info(
                                    "reconciler: delivering undelivered reply for round ${row.roundId}"
                            )
                            val reply = buildReconciledReply(replyId, row.replyText)
                            handleIncomingMessage(reply.toJsonString())
                            deliveredReplyRoundIds.add(row.roundId)
                        }
                    }
                    .onFailure { logger.warn("reply reconciliation failed", it) }
        }
    }

    /**
     * Build a chat-visible agent reply MessageBase with a caller-supplied STABLE id. Mirrors the
     * runtime's agent-reply shape: sender = the agent/persona (so isSenderMe() is false → renders as a
     * received bubble), receiver = the local user. Tagged message_type=chat / migration_phase=local_reply
     * (NOT env_trigger, which handleIncomingMessage filters out).
     */
    private fun buildReconciledReply(messageId: String, text: String): MessageBase {
        val agentUser =
                UserInfo(
                        userId = activeModelKey ?: "local_agent",
                        userNickname = receiverModelName ?: "Maimchat"
                )
        val userUser =
                UserInfo(
                        userId = userId,
                        userNickname = userNickname,
                        userCardname = userCardName
                )
        val msgInfo =
                BaseMessageInfo(
                        messageId = messageId,
                        time = System.currentTimeMillis() / 1000.0,
                        senderInfo = SenderInfo(userInfo = agentUser),
                        receiverInfo = ReceiverInfo(userInfo = userUser),
                        userInfo = agentUser,
                        formatInfo =
                                FormatInfo(
                                        contentFormat = listOf("text"),
                                        acceptFormat = listOf("text", "image", "emoji", "voice")
                                ),
                        additionalConfig =
                                mapOf(
                                        "message_type" to "chat",
                                        "runtime" to "local",
                                        "migration_phase" to "local_reply"
                                )
                )
        return MessageBase(msgInfo, Seg("text", text), text)
    }

    private fun localReplierTaskStoreFor(): ReplierTaskStore {
        val context = appContext ?: return NoopReplierTaskStore
        val database = ChatDatabase.getInstance(context.applicationContext)
        return RoomReplierTaskStore(database.plannerStateDao())
    }

    private fun localRuntimeStateDaoForTools(): RuntimeStateDao? {
        val context = appContext ?: return null
        return ChatDatabase.getInstance(context.applicationContext).runtimeStateDao()
    }

    private fun localPlannerSystemPromptProviderFor(
            templateName: String
    ): PlannerSystemPromptProvider {
        val context = appContext ?: return EmptyPlannerSystemPromptProvider
        val app = context.applicationContext
        val database = ChatDatabase.getInstance(app)
        val stateDao = database.runtimeStateDao()
        val roomProvider =
                RoomPlannerSystemPromptProvider(
                        stateDao = stateDao,
                        templateName = templateName
                )
        return object : PlannerSystemPromptProvider {
            override suspend fun systemPromptFor(context: PlannerTurnContext): String? {
                DefaultAgentProfileSeeder.seed(
                        context = app,
                        stateDao = stateDao,
                        agentId = context.routingKey.agentId,
                        displayName = receiverModelName
                )
                return roomProvider.systemPromptFor(context)
            }
        }
    }

    private fun localReplierPromptContextProviderFor(): ReplierPromptContextProvider {
        val context = appContext ?: return EmptyReplierPromptContextProvider
        val database = ChatDatabase.getInstance(context.applicationContext)
        val stateDao = database.runtimeStateDao()
        val roomProvider =
                RoomReplierPromptContextProvider(
                        historyStore = historyStoreFor(context),
                        stateDao = stateDao
                )
        return object : ReplierPromptContextProvider {
            override suspend fun contextFor(request: ReplierTaskRequest): ReplierPromptContext {
                DefaultAgentProfileSeeder.seed(
                        context = context.applicationContext,
                        stateDao = stateDao,
                        agentId = request.routingKey.agentId,
                        displayName = receiverModelName
                )
                return roomProvider.contextFor(request)
            }
        }
    }

    private fun seedDefaultAgentProfile(context: Context, modelKey: String?) {
        val agentId = historyAgentId(modelKey) ?: return
        val database = ChatDatabase.getInstance(context.applicationContext)
        scope.launch {
            try {
                DefaultAgentProfileSeeder.seed(
                        context = context.applicationContext,
                        stateDao = database.runtimeStateDao(),
                        agentId = agentId,
                        displayName = receiverModelName
                )
            } catch (e: Exception) {
                logger.error("写入默认本地角色配置失败", e)
            }
        }
    }

    private fun refreshActiveAgentConfig(
            context: Context?,
            modelKey: String?,
            rebuildRuntime: Boolean
    ) {
        val agentId = historyAgentId(modelKey)
        val app = context?.applicationContext
        val generation = ++activeAgentConfigLoadGeneration
        if (agentId == null || app == null) {
            val hadConfig = activeAgentConfig != null
            activeAgentConfig = null
            if (hadConfig && rebuildRuntime) {
                localTransport.rebuildRuntime("角色 LLM 配置清空")
            }
            return
        }

        scope.launch(Dispatchers.IO) {
            val config =
                    try {
                        ChatDatabase.getInstance(app).runtimeStateDao().queryAgentConfig(agentId)
                    } catch (e: Exception) {
                        logger.error("读取本地角色 LLM 配置失败", e)
                        null
                    }
            withContext(Dispatchers.Main) {
                if (generation != activeAgentConfigLoadGeneration || activeModelKey != agentId) {
                    return@withContext
                }
                if (activeAgentConfig == config) {
                    return@withContext
                }
                activeAgentConfig = config
                if (rebuildRuntime) {
                    localTransport.rebuildRuntime("角色 LLM 配置更新")
                }
            }
        }
    }

    private fun historyContextId(modelKey: String?): String =
            RuntimeMessageMapper.contextIdForModel(modelKey)

    private fun historyAgentId(modelKey: String?): String? = modelKey?.takeIf { it.isNotBlank() }

    private fun historyPreferences(context: Context) =
            context.applicationContext.getSharedPreferences(HISTORY_PREFS, Context.MODE_PRIVATE)

    private fun roomImportKey(modelKey: String): String = ROOM_IMPORT_PREFIX + modelKey

    private fun fallbackTimestampMillis(message: MessageBase): Long =
            message.messageInfo.time?.takeIf { it > 0.0 }?.let { (it * 1000).toLong() }
                    ?: System.currentTimeMillis()

    private fun ChatMessage.toVisibleRecord(): VisibleMessageRecord =
            VisibleMessageRecord(
                    messageId = id,
                    content = content,
                    isFromUser = isFromUser,
                    timestampMillis = timestamp,
                    motionGroup = motionData?.group,
                    motionIndex = motionData?.index,
                    motionLoop = motionData?.loop ?: false,
                    agentActivityJson = agentActivityJson,
                    fileInfoJson = fileInfoJson
            )

    private fun VisibleMessageRecord.toChatMessage(): ChatMessage =
            ChatMessage(
                    id = messageId,
                    content = content,
                    isFromUser = isFromUser,
                    timestamp = timestampMillis,
                    motionData =
                            if (motionGroup != null && motionIndex != null) {
                                MotionData(motionGroup, motionIndex, motionLoop)
                            } else {
                                null
                            },
                    agentActivityJson = agentActivityJson,
                    fileInfoJson = fileInfoJson
            )

    private fun syncEnvironmentRecentMessages() {
        environmentStateProvider.updateRecentMessages(
                _messages.value.map { message ->
                    ChatEnvironmentMessage(
                            text = message.content,
                            fromUser = message.isFromUser,
                            timestampMillis = message.timestamp
                    )
                }
        )
    }

    private fun emitModelChangedEnvironmentTrigger() {
        val transport = localEnvironmentTransport()
        submitEnvironmentTriggers(
                transport = transport,
                submissions = environmentTriggerEmitter.onModelChanged(environmentTriggerContext())
        )
    }

    private fun emitEnvironmentUpdateTriggers(update: ChatEnvironmentUpdate) {
        val transport = localEnvironmentTransport()
        submitEnvironmentTriggers(
                transport = transport,
                submissions =
                        environmentTriggerEmitter.onEnvironmentUpdate(
                                update = update,
                                context = environmentTriggerContext()
                        )
        )
    }

    private fun localEnvironmentTransport(): LocalTransport = localTransport

    private fun submitEnvironmentTriggers(
            transport: LocalTransport,
            submissions: List<EnvironmentTriggerSubmission>
    ) {
        submissions.forEach { submission ->
            if (!transport.submitEnvironmentTrigger(submission)) {
                logger.warn("提交环境触发失败：${submission.messageId}")
            }
        }
    }

    private fun environmentTriggerContext(): ChatEnvironmentTriggerContext {
        val modelKey = activeModelKey
        val agentId = historyAgentId(modelKey) ?: RoutingKey.DEFAULT_AGENT_ID
        val modelName = receiverModelName
        return ChatEnvironmentTriggerContext(
                routingKey = RoutingKey(contextId = historyContextId(modelKey), agentId = agentId),
                modelKey = modelKey,
                modelName = modelName,
                userId = userId,
                userName = userNickname,
                agentUserId = agentId,
                agentName = modelName ?: modelKey ?: "Maimchat"
        )
    }

    private fun MessageBase.historyIdentity(): String =
            messageInfo.messageId?.takeIf { it.isNotBlank() }
                    ?: "standard_${Integer.toHexString(toJsonString().hashCode())}"

    private fun generateMessageId(): String =
            "msg_${System.currentTimeMillis()}_${(Math.random()*1000).toInt()}"
    private fun generateUserId(): String =
            "u_${System.currentTimeMillis()}_${(Math.random()*1000).toInt()}"
    fun disconnect() {        localTransport.stop("用户断开", userInitiated = true)
    }

    fun shutdown() {        localTransport.stop("管理器销毁", userInitiated = false)
        scope.cancel()
    }

    fun getRuntimeStateDescription(): String =
            when (_runtimeState.value) {
                RuntimeState.STOPPED -> "本地运行时: stopped"
                RuntimeState.STARTING -> "本地运行时: starting"
                RuntimeState.RUNNING -> "本地运行时: ready"
                RuntimeState.ERROR -> "本地运行时: error"
            }
}

private fun ChatEnvironmentUpdate.isIdleActivity(): Boolean =
        hasAppVisible ||
                hasWallpaperVisible ||
                hasBackgroundPath ||
                hasVisualSnapshot ||
                interaction != null ||
                modelKey.isMeaningful() ||
                modelName.isMeaningful() ||
                modelFolderPath.isMeaningful() ||
                modelFile.isMeaningful() ||
                lifecycleState.isMeaningful() ||
                motionFiles != null

private fun String?.isMeaningful(): Boolean = !isNullOrBlank()
