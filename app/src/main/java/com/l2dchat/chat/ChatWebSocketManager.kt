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
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.BufferOverflow
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
    }
    private val logger = L2DLogger.module(LogModule.CHAT)
    private val gson = Gson()
    private var platform: String = DEFAULT_PLATFORM
    private var authToken: String? = null
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
                    agentNameProvider = { receiverModelName }
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
    private var receiverUserIdOverride: String? = null
    private var receiverUserNicknameOverride: String? = null

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
        appContext = context.applicationContext
        activeModelKey?.let { loadHistory(context, it) }
    }
    fun setReceiverInfo(userId: String?, userNickname: String?) {
        receiverUserIdOverride = userId?.ifBlank { null }
        receiverUserNicknameOverride = userNickname?.ifBlank { null }
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
        val m =
                buildStandardMessage(
                        listOf(Seg("text", "播放动作: $group[$index]")),
                        "motion",
                        additional =
                                mapOf(
                                        "motion" to
                                                mapOf(
                                                        "group" to group,
                                                        "index" to index,
                                                        "loop" to loop
                                                )
                                )
                )
        sendStandardMessage(m)
    }
    fun triggerModelMotion(group: String, index: Int, loop: Boolean = false) {
        onMotionTrigger?.invoke(group, index, loop)
        sendMotionMessage(group, index, loop)
    }
    fun setMotionTriggerCallback(callback: (String, Int, Boolean) -> Unit) {
        onMotionTrigger = callback
    }
    fun getMessageEvents() = messageHandler.messageEvents
    private fun addMessage(message: ChatMessage) {
        val list = _messages.value.toMutableList()
        if (list.any { it.id == message.id }) return
        list.add(message)
        _messages.value = list
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) saveHistory(ctx, key)
    }
    private fun addStandardMessage(message: MessageBase) {
        val list = _standardMessages.value.toMutableList()
        val mid = message.messageInfo.messageId
        if (!mid.isNullOrBlank() && list.any { it.messageInfo.messageId == mid }) return
        list.add(message)
        _standardMessages.value = list
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
        lastServerMessageTime = 0L
        val key = activeModelKey
        val ctx = appContext
        if (key != null && ctx != null) saveHistory(ctx, key)
    }
    fun clearMessagesEphemeral() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        lastServerMessageTime = 0L
    }
    fun loadHistory(context: Context, modelKey: String) {
        try {
            val sp = context.getSharedPreferences("chat_history", Context.MODE_PRIVATE)
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
            _messages.value = loadedMsgs
            _standardMessages.value = loadedStd
            synchronizeCachedMessagePlatforms(this.platform)
            val lastTs = loadedStd.maxOfOrNull { (((it.messageInfo.time) ?: 0.0) * 1000).toLong() }
            if (lastTs != null && lastTs > 0) lastServerMessageTime = lastTs
        } catch (e: Exception) {
            logger.error("加载历史失败", e)
        }
    }
    fun saveHistory(context: Context, modelKey: String) {
        try {
            val sp = context.getSharedPreferences("chat_history", Context.MODE_PRIVATE)
            val editor = sp.edit()
            val msgs = _messages.value.takeLast(200)
            val arrMsgs = JsonArray()
            msgs.forEach { m ->
                val o = JsonObject()
                o.addProperty("id", m.id)
                o.addProperty("content", m.content)
                o.addProperty("isFromUser", m.isFromUser)
                o.addProperty("timestamp", m.timestamp)
                arrMsgs.add(o)
            }
            val stds = _standardMessages.value.takeLast(200)
            val arrStd = JsonArray()
            stds.forEach { s -> arrStd.add(s.toJsonString()) }
            editor.putString("messages_" + modelKey, gson.toJson(arrMsgs))
            editor.putString("standard_" + modelKey, gson.toJson(arrStd))
            editor.apply()
        } catch (e: Exception) {
            logger.error("保存历史失败", e)
        }
    }
    private fun generateMessageId(): String =
            "msg_${System.currentTimeMillis()}_${(Math.random()*1000).toInt()}"
    private fun generateUserId(): String =
            "u_${System.currentTimeMillis()}_${(Math.random()*1000).toInt()}"
    fun disconnect() {
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
