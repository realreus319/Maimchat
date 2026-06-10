package com.l2dchat.chat

import android.content.Context
import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.l2dchat.chat.transport.ChatTransport
import com.l2dchat.chat.transport.ChatTransportCallbacks
import com.l2dchat.chat.transport.LocalTransport
import com.l2dchat.chat.transport.RemoteWebSocketConfig
import com.l2dchat.chat.transport.RemoteWebSocketTransport
import com.l2dchat.core.config.DefaultAgentProfileSeeder
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
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
import com.l2dchat.core.storage.RuntimeStateDao
import com.l2dchat.core.tools.EmptyReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierPromptContext
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class ChatWebSocketManager {
    companion object {
        private const val DEFAULT_PLATFORM = "live2d_chat"
        private const val HISTORY_PREFS = "chat_history"
        private const val HISTORY_LIMIT = 200
        private const val ROOM_IMPORT_PREFIX = "room_imported_"
        private const val IDLE_TRIGGER_DELAY_MILLIS = 5 * 60 * 1000L
    }
    private val logger = L2DLogger.module(LogModule.CHAT)
    private val gson = Gson()
    private var platform: String = DEFAULT_PLATFORM
    private var authToken: String? = null
    private var localLlmSettings = LocalLlmSettings()
    private val messageHandler = Live2DChatMessageHandler()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val _connectionState = MutableStateFlow(ConnectionState.DISCONNECTED)
    val connectionState: StateFlow<ConnectionState> = _connectionState.asStateFlow()
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
                override fun onStateChanged(state: ConnectionState) {
                    _connectionState.value = state
                }

                override fun onIncomingText(text: String) {
                    scope.launch { handleIncomingMessage(text) }
                }

                override fun onError(message: String, throwable: Throwable?) {
                    reportConnectionError(message, throwable)
                }
            }
    private val localTransport =
            LocalTransport(
                    scope = scope,
                    callbacks = transportCallbacks,
                    platformProvider = { platform },
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
                    localRuntimeLlmConfigProvider = { localLlmSettings.toRuntimeConfig() },
                    environmentStateProvider = environmentStateProvider,
                    motionController = localMotionController,
                    runtimeStateDaoProvider = { localRuntimeStateDaoForTools() }
            )
    private val remoteTransport =
            RemoteWebSocketTransport(scope = scope, callbacks = transportCallbacks)
    private var activeTransport: ChatTransport = localTransport
    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()
    private val _standardMessages = MutableStateFlow<List<MessageBase>>(emptyList())
    val standardMessages: StateFlow<List<MessageBase>> = _standardMessages.asStateFlow()
    private var lastServerMessageTime: Long = 0L
    private var onMotionTrigger: ((String, Int, Boolean) -> Unit)? = null
    private var userId: String = generateUserId()
    private var userNickname: String? = null
    private var userCardName: String? = null
    private var receiverModelName: String? = null
    private var activeModelKey: String? = null
    private var appContext: Context? = null
    private var historyStore: ChatHistoryStore? = null
    private var historyLoadGeneration: Long = 0L
    private var receiverUserIdOverride: String? = null
    private var receiverUserNicknameOverride: String? = null
    private var idleTimerJob: Job? = null
    private var lastActivityMillis: Long = System.currentTimeMillis()
    private var idleAppVisible: Boolean = false
    private var idleWallpaperVisible: Boolean = false

    enum class ConnectionState {
        DISCONNECTED,
        CONNECTING,
        CONNECTED,
        ERROR
    }

    enum class RuntimeMode {
        LOCAL,
        REMOTE
    }

    data class ChatMessage(
            val id: String,
            val content: String,
            val isFromUser: Boolean,
            val timestamp: Long = System.currentTimeMillis(),
            val motionData: MotionData? = null
    )

    data class MotionData(val group: String, val index: Int, val loop: Boolean = false)

    fun setConnectionConfig(platform: String, authToken: String? = null) {
        applyPlatformPreference(platform)
        this.authToken = authToken
    }

    fun updatePlatformPreference(platform: String?) {
        applyPlatformPreference(platform)
    }

    private fun applyPlatformPreference(platformInput: String?) {
        val resolvedPlatform = resolvePlatform(platformInput)
        if (this.platform != resolvedPlatform) {
            this.platform = resolvedPlatform
            synchronizeCachedMessagePlatforms(resolvedPlatform)
        }
    }

    private fun resolvePlatform(input: String?): String {
        val trimmed = input?.trim().orEmpty()
        return if (trimmed.isEmpty()) DEFAULT_PLATFORM else trimmed
    }
    fun connect(url: String, platform: String? = null, authToken: String? = null) {
        cancelIdleEnvironmentTimer()
        if (activeTransport.mode != RuntimeMode.REMOTE) {
            activeTransport.stop("切换到远程 WebSocket", userInitiated = false)
            activeTransport = remoteTransport
        }
        if (platform != null) updatePlatformPreference(platform)
        if (authToken != null) this.authToken = authToken.takeIf { it.isNotBlank() }
        remoteTransport.connect(RemoteWebSocketConfig(url, this.platform, this.authToken))
    }

    fun startLocalRuntime() {
        if (activeTransport.mode != RuntimeMode.LOCAL) {
            activeTransport.stop("切换到本地运行时", userInitiated = true)
            activeTransport = localTransport
        }
        localTransport.start()
        scheduleIdleEnvironmentTimer()
    }

    fun isLocalMode(): Boolean = activeTransport.mode == RuntimeMode.LOCAL

    private fun reportConnectionError(message: String, throwable: Throwable? = null) {
        logger.error(message, throwable)
        if (_connectionState.value != ConnectionState.CONNECTED) {
            _connectionState.value = ConnectionState.ERROR
        }
        scope.launch { _errors.emit(message) }
    }

    fun setUserProfile(nickname: String, cardName: String? = null, userId: String? = null) {
        val sanitizedNickname = nickname.trim()
        userNickname = sanitizedNickname.ifBlank { null }
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
        receiverModelName = modelName?.ifBlank { null }
        activeModelKey = modelName?.lowercase()?.replace(Regex("[^a-z0-9_-]+"), "_")
        val app = context.applicationContext
        appContext = app
        historyStoreFor(app)
        environmentStateProvider.setApplicationContext(context)
        environmentStateProvider.update(
                ChatEnvironmentUpdate(modelKey = activeModelKey, modelName = receiverModelName)
        )
        seedDefaultAgentProfile(app, activeModelKey)
        activeModelKey?.let { loadHistory(app, it) }
        emitModelChangedEnvironmentTrigger()
        noteIdleActivity()
    }

    fun updateEnvironmentState(update: ChatEnvironmentUpdate) {
        updateIdleVisibility(update)
        environmentStateProvider.update(update)
        emitEnvironmentUpdateTriggers(update)
        if (update.isIdleActivity()) {
            noteIdleActivity()
        } else {
            scheduleIdleEnvironmentTimer()
        }
    }
    fun reportMotionFinished(
            group: String? = null,
            index: Int? = null,
            filePath: String? = null,
            loop: Boolean = false,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        val transport = localEnvironmentTransport() ?: return
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
        )
        noteIdleActivity()
    }
    fun setReceiverInfo(userId: String?, userNickname: String?) {
        receiverUserIdOverride = userId?.ifBlank { null }
        receiverUserNicknameOverride = userNickname?.ifBlank { null }
    }
    fun setLocalLlmSettings(settings: LocalLlmSettings) {
        if (localLlmSettings == settings) return
        localLlmSettings = settings
        localTransport.rebuildRuntime("本地 LLM 配置更新")
    }
    fun hasUserNickname(): Boolean = !userNickname.isNullOrBlank()
    fun getUserNickname(): String? = userNickname
    private fun isSenderMe(sender: SenderInfo?): Boolean {
        if (sender == null) return false
        val sid = sender.userInfo?.userId
        if (!sid.isNullOrBlank() && sid == this.userId) return true
        val snick = sender.userInfo?.userNickname
        if (!snick.isNullOrBlank() && !userNickname.isNullOrBlank() && snick == userNickname)
                return true
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
                                        platform = platform,
                                        userId = userId,
                                        userNickname = userNickname,
                                        userCardname = userCardName
                                )
                )
        val receiverInfo =
                ReceiverInfo(
                        userInfo =
                                UserInfo(
                                        platform = platform,
                                        userId = receiverUserIdOverride ?: receiverModelName,
                                        userNickname = receiverUserNicknameOverride
                                                        ?: receiverModelName
                                )
                )
        val msgInfo =
                BaseMessageInfo(
                        platform = platform,
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
        val transport = activeTransport
        if (transport.mode == RuntimeMode.LOCAL) {
            addStandardMessage(message)
        }
        if (!transport.send(message)) {
            logger.warn("发送消息失败：当前传输=${transport.mode}")
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

    private fun buildMotionMessage(command: MotionCommand): MessageBase =
            buildStandardMessage(
                    listOf(Seg("text", MotionMessage.displayText(command))),
                    MotionMessage.TYPE,
                    raw = MotionMessage.displayText(command),
                    additional = MotionMessage.additionalConfig(command)
            )

    private fun addMessage(message: ChatMessage) {
        val list = _messages.value.toMutableList()
        if (list.any { it.id == message.id }) return
        list.add(message)
        _messages.value = list
        syncEnvironmentRecentMessages()
        appendVisibleHistory(message)
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) saveHistory(ctx, key)
        noteIdleActivity()
    }
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

    private fun synchronizeCachedMessagePlatforms(newPlatform: String) {
        val current = _standardMessages.value
        if (current.isEmpty()) return
        val updated = current.map { it.withPlatform(newPlatform) }
        _standardMessages.value = updated
    }

    private fun MessageBase.withPlatform(newPlatform: String): MessageBase {
        val updatedInfo = messageInfo.withPlatform(newPlatform)
        return if (updatedInfo === messageInfo) this else copy(messageInfo = updatedInfo)
    }

    private fun BaseMessageInfo.withPlatform(newPlatform: String): BaseMessageInfo {
        val updatedSender = senderInfo?.withPlatform(newPlatform)
        val updatedReceiver = receiverInfo?.withPlatform(newPlatform)
        val updatedGroup = groupInfo?.withPlatform(newPlatform)
        val updatedUser = userInfo?.withPlatform(newPlatform)
        if (platform == newPlatform &&
                        updatedSender === senderInfo &&
                        updatedReceiver === receiverInfo &&
                        updatedGroup === groupInfo &&
                        updatedUser === userInfo
        )
                return this
        return copy(
                platform = newPlatform,
                senderInfo = updatedSender,
                receiverInfo = updatedReceiver,
                groupInfo = updatedGroup,
                userInfo = updatedUser
        )
    }

    private fun SenderInfo.withPlatform(newPlatform: String): SenderInfo {
        val updatedGroup = groupInfo?.withPlatform(newPlatform)
        val updatedUser = userInfo?.withPlatform(newPlatform)
        if (updatedGroup === groupInfo && updatedUser === userInfo) return this
        return copy(groupInfo = updatedGroup, userInfo = updatedUser)
    }

    private fun ReceiverInfo.withPlatform(newPlatform: String): ReceiverInfo {
        val updatedGroup = groupInfo?.withPlatform(newPlatform)
        val updatedUser = userInfo?.withPlatform(newPlatform)
        if (updatedGroup === groupInfo && updatedUser === userInfo) return this
        return copy(groupInfo = updatedGroup, userInfo = updatedUser)
    }

    private fun GroupInfo.withPlatform(newPlatform: String): GroupInfo {
        if (platform == newPlatform) return this
        return copy(platform = newPlatform)
    }

    private fun UserInfo.withPlatform(newPlatform: String): UserInfo {
        if (platform == newPlatform) return this
        return copy(platform = newPlatform)
    }
    fun clearMessages() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        environmentStateProvider.clearRecentMessages()
        lastServerMessageTime = 0L
        noteIdleActivity()
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) {
            saveHistory(ctx, key)
            clearRoomHistory(key)
        }
    }
    fun clearMessagesEphemeral() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        environmentStateProvider.clearRecentMessages()
        lastServerMessageTime = 0L
        noteIdleActivity()
    }
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
        synchronizeCachedMessagePlatforms(this.platform)
        lastServerMessageTime =
                _standardMessages.value
                        .maxOfOrNull { (((it.messageInfo.time) ?: 0.0) * 1000).toLong() }
                        ?.takeIf { it > 0L }
                        ?: 0L
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

    private fun clearRoomHistory(modelKey: String) {
        val context = appContext ?: return
        val store = historyStoreFor(context)
        val contextId = historyContextId(modelKey)
        val agentId = historyAgentId(modelKey)
        scope.launch {
            try {
                store.clearHistory(contextId, agentId)
            } catch (e: Exception) {
                logger.error("清空 Room 历史失败", e)
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
                    motionLoop = motionData?.loop ?: false
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
                            }
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
        val transport = localEnvironmentTransport() ?: return
        submitEnvironmentTriggers(
                transport = transport,
                submissions = environmentTriggerEmitter.onModelChanged(environmentTriggerContext())
        )
    }

    private fun emitEnvironmentUpdateTriggers(update: ChatEnvironmentUpdate) {
        val transport = localEnvironmentTransport() ?: return
        submitEnvironmentTriggers(
                transport = transport,
                submissions =
                        environmentTriggerEmitter.onEnvironmentUpdate(
                                update = update,
                                context = environmentTriggerContext()
                        )
        )
    }

    private fun emitIdleTimerEnvironmentTrigger(idleMillis: Long, timestampMillis: Long) {
        val transport = localEnvironmentTransport() ?: return
        submitEnvironmentTriggers(
                transport = transport,
                submissions =
                        environmentTriggerEmitter.onIdleTimer(
                                idle =
                                        ChatEnvironmentIdleTimer(
                                                idleMillis = idleMillis,
                                                appVisible = idleAppVisible,
                                                wallpaperVisible = idleWallpaperVisible,
                                                timestampMillis = timestampMillis
                                        ),
                                context = environmentTriggerContext()
                        )
        )
    }

    private fun noteIdleActivity() {
        lastActivityMillis = System.currentTimeMillis()
        scheduleIdleEnvironmentTimer()
    }

    private fun updateIdleVisibility(update: ChatEnvironmentUpdate) {
        if (update.hasAppVisible) idleAppVisible = update.appVisible == true
        if (update.hasWallpaperVisible) idleWallpaperVisible = update.wallpaperVisible == true
    }

    private fun scheduleIdleEnvironmentTimer() {
        idleTimerJob?.cancel()
        idleTimerJob = null
        if (!isLocalMode() || !hasVisibleIdleSurface()) return
        val scheduledActivityMillis = lastActivityMillis
        idleTimerJob =
                scope.launch {
                    delay(IDLE_TRIGGER_DELAY_MILLIS)
                    if (!isLocalMode() || !hasVisibleIdleSurface()) return@launch
                    if (lastActivityMillis != scheduledActivityMillis) return@launch
                    val firedAtMillis = System.currentTimeMillis()
                    idleTimerJob = null
                    emitIdleTimerEnvironmentTrigger(
                            idleMillis = firedAtMillis - scheduledActivityMillis,
                            timestampMillis = firedAtMillis
                    )
                    lastActivityMillis = firedAtMillis
                    scheduleIdleEnvironmentTimer()
                }
    }

    private fun cancelIdleEnvironmentTimer() {
        idleTimerJob?.cancel()
        idleTimerJob = null
    }

    private fun hasVisibleIdleSurface(): Boolean = idleAppVisible || idleWallpaperVisible

    private fun localEnvironmentTransport(): LocalTransport? =
            if (activeTransport.mode == RuntimeMode.LOCAL) activeTransport as? LocalTransport
            else null

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
                userName = userNickname ?: userCardName ?: "用户",
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
    fun disconnect() {
        cancelIdleEnvironmentTimer()
        activeTransport.stop("用户断开", userInitiated = true)
    }

    fun getConnectionStateDescription(): String =
            when (_connectionState.value) {
                ConnectionState.DISCONNECTED -> "未连接"
                ConnectionState.CONNECTING -> "连接中..."
                ConnectionState.CONNECTED ->
                        if (activeTransport.mode == RuntimeMode.LOCAL) "本地运行中" else "已连接"
                ConnectionState.ERROR -> "连接错误"
            }

    fun getPlatform(): String = platform
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
