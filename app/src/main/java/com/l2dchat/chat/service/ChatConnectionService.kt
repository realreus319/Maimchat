package com.l2dchat.chat.service

import android.app.Service
import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Message
import android.os.Messenger
import android.os.Process
import android.os.RemoteException
import com.l2dchat.chat.ChatEnvironmentInteraction
import com.l2dchat.chat.ChatEnvironmentUpdate
import com.l2dchat.chat.ChatEnvironmentVisualSnapshot
import com.l2dchat.chat.ChatWebSocketManager
import com.l2dchat.chat.ChatWebSocketManager.ChatMessage
import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.wallpaper.WallpaperComm
import java.lang.ref.WeakReference
import java.util.concurrent.CopyOnWriteArraySet
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch

class ChatConnectionService : Service() {

    private val logger = L2DLogger.module(LogModule.CHAT)

    private val clients = CopyOnWriteArraySet<Messenger>()
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val incomingHandler = IncomingHandler(this)
    private val messenger = Messenger(incomingHandler)

    private lateinit var manager: ChatWebSocketManager

    private var lastKnownUrl: String? = null
    private var lastKnownPlatform: String? = null
    private var lastKnownAuth: String? = null
    private var lastKnownNickname: String? = null
    private var lastKnownReceiverId: String? = null
    private var lastKnownReceiverNickname: String? = null
    private var localLlmSettings = LocalLlmSettings()

    override fun onCreate() {
        super.onCreate()
        manager = ChatWebSocketManager()
        manager.setActiveModel(applicationContext, restoreModelName())
        applyStoredConfiguration()
        startObservers()
        logger.info("ChatConnectionService created (pid=${Process.myPid()})")
    }

    override fun onBind(intent: Intent?): IBinder = messenger.binder

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 保持粘性，便于在进程被系统回收后自动重启
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        logger.info("ChatConnectionService destroyed")
        serviceScope.cancel()
        clients.clear()
        manager.disconnect()
    }

    private fun restoreModelName(): String? {
        val prefs = getSharedPreferences(WallpaperComm.PREF_WALLPAPER, MODE_PRIVATE)
        val folder = prefs.getString(WallpaperComm.PREF_WALLPAPER_MODEL_FOLDER, null)
        return folder?.substringAfterLast('/')?.ifBlank { null }
    }

    private fun applyStoredConfiguration() {
        val prefs = getSharedPreferences(CHAT_PREFS, MODE_PRIVATE)
        lastKnownUrl = prefs.getString(KEY_LAST_URL, null)?.takeUnless { it.isNullOrBlank() }
        lastKnownPlatform = prefs.getString(KEY_PLATFORM, null)?.takeUnless { it.isNullOrBlank() }
        lastKnownAuth = prefs.getString(KEY_AUTH_TOKEN, null)?.takeUnless { it.isNullOrBlank() }
        lastKnownNickname = prefs.getString(KEY_NICKNAME, null)
        lastKnownReceiverId = prefs.getString(KEY_RECEIVER_ID, null)?.ifBlank { null }
        lastKnownReceiverNickname = prefs.getString(KEY_RECEIVER_NICKNAME, null)?.ifBlank { null }

        lastKnownPlatform?.let { manager.updatePlatformPreference(it) }
        manager.setConnectionConfig(manager.getPlatform(), lastKnownAuth)
        lastKnownNickname?.takeUnless { it.isNullOrBlank() }?.let { manager.setUserProfile(it) }
        if (!lastKnownReceiverId.isNullOrBlank() || !lastKnownReceiverNickname.isNullOrBlank()) {
            manager.setReceiverInfo(lastKnownReceiverId, lastKnownReceiverNickname)
        }
        localLlmSettings = readLocalLlmSettings(prefs)
        manager.setLocalLlmSettings(localLlmSettings)
        manager.startLocalRuntime()
    }

    private fun startObservers() {
        serviceScope.launch {
            manager.connectionState.collect { state -> broadcastConnectionState(state) }
        }
        serviceScope.launch {
            var lastBroadcastId: String? = null
            manager.messages.collect { list ->
                val last = list.lastOrNull() ?: return@collect
                if (last.id == lastBroadcastId) return@collect
                lastBroadcastId = last.id
                broadcastChatMessage(last)
            }
        }
        serviceScope.launch {
            var lastStandardId: String? = null
            manager.standardMessages.collect { list ->
                val last = list.lastOrNull() ?: return@collect
                val id = last.messageInfo.messageId ?: return@collect
                if (id == lastStandardId) return@collect
                lastStandardId = id
                broadcastStandardMessage(last)
            }
        }
        serviceScope.launch { manager.errors.collect { message -> notifyError(message) } }
    }

    private fun broadcastConnectionState(state: ConnectionState) {
        val bundle =
                Bundle().apply {
                    putInt(ChatServiceProtocol.EXTRA_CONNECTION_STATE, state.ordinal)
                    putString(
                            ChatServiceProtocol.EXTRA_CONNECTION_LABEL,
                            when (state) {
                                ConnectionState.DISCONNECTED -> "未连接"
                                ConnectionState.CONNECTING -> "连接中"
                                ConnectionState.CONNECTED -> manager.getConnectionStateDescription()
                                ConnectionState.ERROR -> "错误"
                            }
                    )
                }
        sendToClients(ChatServiceProtocol.MSG_EVENT_CONNECTION_STATE, bundle)
    }

    private fun broadcastChatMessage(message: ChatMessage) {
        val bundle =
                Bundle().apply {
                    putString(ChatServiceProtocol.EXTRA_MESSAGE_ID, message.id)
                    putString(ChatServiceProtocol.EXTRA_MESSAGE_CONTENT, message.content)
                    putBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FROM_USER, message.isFromUser)
                    putLong(ChatServiceProtocol.EXTRA_MESSAGE_TIMESTAMP, message.timestamp)
                }
        sendToClients(ChatServiceProtocol.MSG_EVENT_NEW_MESSAGE, bundle)
    }

    private fun sendSnapshot(target: Messenger? = null) {
        val snapshot =
                ArrayList<Bundle>(manager.messages.value.size).apply {
                    manager.messages.value.forEach { m ->
                        add(
                                Bundle().apply {
                                    putString(ChatServiceProtocol.EXTRA_MESSAGE_ID, m.id)
                                    putString(ChatServiceProtocol.EXTRA_MESSAGE_CONTENT, m.content)
                                    putBoolean(
                                            ChatServiceProtocol.EXTRA_MESSAGE_FROM_USER,
                                            m.isFromUser
                                    )
                                    putLong(
                                            ChatServiceProtocol.EXTRA_MESSAGE_TIMESTAMP,
                                            m.timestamp
                                    )
                                }
                        )
                    }
                }
        val standardSnapshot =
                ArrayList<String>(manager.standardMessages.value.size).apply {
                    manager.standardMessages.value.forEach { add(it.toJsonString()) }
                }
        val bundle =
                Bundle().apply {
                    putParcelableArrayList(ChatServiceProtocol.EXTRA_MESSAGE_BUNDLE_LIST, snapshot)
                    putStringArrayList(
                            ChatServiceProtocol.EXTRA_STANDARD_MESSAGE_LIST,
                            standardSnapshot
                    )
                }
        if (target != null) {
            sendToClient(target, ChatServiceProtocol.MSG_EVENT_SNAPSHOT, bundle)
        } else {
            sendToClients(ChatServiceProtocol.MSG_EVENT_SNAPSHOT, bundle)
        }
    }

    private fun broadcastStandardMessage(message: MessageBase) {
        val json = message.toJsonString()
        val bundle =
                Bundle().apply { putString(ChatServiceProtocol.EXTRA_STANDARD_MESSAGE_JSON, json) }
        sendToClients(ChatServiceProtocol.MSG_EVENT_STANDARD_MESSAGE, bundle)
    }

    private fun sendToClients(what: Int, data: Bundle) {
        val toRemove = mutableListOf<Messenger>()
        clients.forEach { client ->
            if (!sendToClient(client, what, data)) {
                toRemove.add(client)
            }
        }
        if (toRemove.isNotEmpty()) {
            clients.removeAll(toRemove.toSet())
        }
    }

    private fun sendToClient(client: Messenger, what: Int, data: Bundle): Boolean =
            try {
                val msg = Message.obtain(null, what).apply { this.data = data }
                client.send(msg)
                true
            } catch (e: RemoteException) {
                logger.warn("Client callback failed, removing target", e)
                false
            }

    private fun ensureConnected(triggerReconnect: Boolean = true) {
        val state = manager.connectionState.value
        if (state == ConnectionState.CONNECTED || state == ConnectionState.CONNECTING) return
        if (!triggerReconnect) return
        val url = lastKnownUrl
        if (url.isNullOrBlank()) {
            notifyError("未设置服务器地址，无法连接")
            return
        }
        logger.debug(
                "ensureConnected() with state=$state trigger=$triggerReconnect url=$url",
                throttleMs = 1_000L,
                throttleKey = "ensure_connected"
        )
        manager.connect(url, lastKnownPlatform, lastKnownAuth)
    }

    private fun handleSendMessage(data: Bundle) {
        val text = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_TEXT)?.trim()
        if (text.isNullOrEmpty()) {
            notifyError("发送内容不能为空")
            return
        }
        if (manager.isLocalMode()) {
            manager.startLocalRuntime()
        } else {
            ensureConnected()
        }
        manager.sendUserMessage(text)
    }

    private fun handleConnectRequest(data: Bundle) {
        val url =
                data.getString(ChatServiceProtocol.EXTRA_URL)?.takeIf { it.isNotBlank() }
                        ?: lastKnownUrl
        if (url.isNullOrBlank()) {
            notifyError("未提供有效的服务器地址")
            return
        }
        val platform =
                data.getString(ChatServiceProtocol.EXTRA_PLATFORM)?.takeUnless { it.isBlank() }
                        ?: lastKnownPlatform
        val auth =
                data.getString(ChatServiceProtocol.EXTRA_AUTH_TOKEN)?.takeUnless { it.isBlank() }
                        ?: lastKnownAuth

        logger.info(
                "handleConnectRequest url=$url platform=$platform " +
                        "authPresent=${auth != null} caller=${data.keySet()}"
        )

        lastKnownUrl = url
        lastKnownPlatform = platform
        lastKnownAuth = auth
        persistConnectionConfig()

        if (!platform.isNullOrBlank() || auth != null) {
            manager.setConnectionConfig(platform ?: manager.getPlatform(), auth)
        }
        logger.info("Invoking ChatWebSocketManager.connect url=$url platform=$platform")
        manager.connect(url, platform, auth)
    }

    private fun handleConfigUpdate(data: Bundle) {
        var needReconnect = false
        data.getString(ChatServiceProtocol.EXTRA_PLATFORM)?.let { platform ->
            val trimmed = platform.trim()
            lastKnownPlatform = trimmed.ifBlank { null }
            manager.updatePlatformPreference(lastKnownPlatform)
            needReconnect = true
        }
        data.getString(ChatServiceProtocol.EXTRA_AUTH_TOKEN)?.let { token ->
            lastKnownAuth = token.takeUnless { it.isBlank() }
            manager.setConnectionConfig(lastKnownPlatform ?: manager.getPlatform(), lastKnownAuth)
            needReconnect = true
        }
        data.getString(ChatServiceProtocol.EXTRA_NICKNAME)?.let { name ->
            lastKnownNickname = name
            if (name.isNotBlank()) {
                manager.setUserProfile(name)
            }
        }
        if (data.containsKey(ChatServiceProtocol.EXTRA_RECEIVER_ID) ||
                        data.containsKey(ChatServiceProtocol.EXTRA_RECEIVER_NICKNAME)
        ) {
            lastKnownReceiverId =
                    data.getString(ChatServiceProtocol.EXTRA_RECEIVER_ID)?.ifBlank { null }
            lastKnownReceiverNickname =
                    data.getString(ChatServiceProtocol.EXTRA_RECEIVER_NICKNAME)?.ifBlank { null }
            manager.setReceiverInfo(lastKnownReceiverId, lastKnownReceiverNickname)
        }
        if (data.containsLocalLlmSettings()) {
            localLlmSettings = localLlmSettings.updatedFrom(data)
            manager.setLocalLlmSettings(localLlmSettings)
            if (localLlmSettings.enabled) {
                manager.startLocalRuntime()
            }
        }
        data.getString(ChatServiceProtocol.EXTRA_URL)?.let { url ->
            val trimmed = url.trim()
            lastKnownUrl = trimmed.ifBlank { null }
            needReconnect = true
        }
        persistConnectionConfig()
        if (needReconnect) {
            ensureConnected(triggerReconnect = true)
        }
    }

    private fun handleClearMessages(persist: Boolean) {
        if (persist) manager.clearMessages() else manager.clearMessagesEphemeral()
        sendSnapshot()
    }

    private fun handleSetActiveModel(data: Bundle) {
        val modelName = data.getString(ChatServiceProtocol.EXTRA_MODEL_NAME)
        manager.setActiveModel(applicationContext, modelName)
        sendSnapshot()
    }

    private fun handleEnvironmentStateUpdate(data: Bundle) {
        manager.updateEnvironmentState(
                ChatEnvironmentUpdate(
                        modelKey =
                                data.optionalString(ChatServiceProtocol.EXTRA_ENV_MODEL_KEY),
                        modelName =
                                data.optionalString(ChatServiceProtocol.EXTRA_ENV_MODEL_NAME),
                        modelFolderPath =
                                data.optionalString(
                                        ChatServiceProtocol.EXTRA_ENV_MODEL_FOLDER_PATH
                                ),
                        modelFile =
                                data.optionalString(ChatServiceProtocol.EXTRA_ENV_MODEL_FILE),
                        lifecycleState =
                                data.optionalString(
                                        ChatServiceProtocol.EXTRA_ENV_MODEL_LIFECYCLE_STATE
                                ),
                        motionFiles =
                                if (data.containsKey(ChatServiceProtocol.EXTRA_ENV_MOTION_FILES)) {
                                    data.getStringArrayList(
                                                    ChatServiceProtocol.EXTRA_ENV_MOTION_FILES
                                            )
                                            ?: emptyList()
                                } else {
                                    null
                                },
                        appVisible =
                                if (data.containsKey(ChatServiceProtocol.EXTRA_ENV_APP_VISIBLE)) {
                                    data.getBoolean(ChatServiceProtocol.EXTRA_ENV_APP_VISIBLE)
                                } else {
                                    null
                                },
                        hasAppVisible =
                                data.containsKey(ChatServiceProtocol.EXTRA_ENV_APP_VISIBLE),
                        wallpaperVisible =
                                if (
                                        data.containsKey(
                                                ChatServiceProtocol.EXTRA_ENV_WALLPAPER_VISIBLE
                                        )
                                ) {
                                    data.getBoolean(
                                            ChatServiceProtocol.EXTRA_ENV_WALLPAPER_VISIBLE
                                    )
                                } else {
                                    null
                                },
                        hasWallpaperVisible =
                                data.containsKey(ChatServiceProtocol.EXTRA_ENV_WALLPAPER_VISIBLE),
                        backgroundPath =
                                data.optionalString(
                                        ChatServiceProtocol.EXTRA_ENV_BACKGROUND_PATH
                                ),
                        hasBackgroundPath =
                                data.containsKey(ChatServiceProtocol.EXTRA_ENV_BACKGROUND_PATH),
                        interaction =
                                data.optionalString(ChatServiceProtocol.EXTRA_ENV_INTERACTION_TYPE)
                                        ?.let { type ->
                                            ChatEnvironmentInteraction(
                                                    type = type,
                                                    x =
                                                            data.optionalFloat(
                                                                    ChatServiceProtocol.EXTRA_ENV_INTERACTION_X
                                                            ),
                                                    y =
                                                            data.optionalFloat(
                                                                    ChatServiceProtocol.EXTRA_ENV_INTERACTION_Y
                                                            ),
                                                    timestampMillis =
                                                            data.optionalLong(
                                                                    ChatServiceProtocol
                                                                            .EXTRA_ENV_INTERACTION_TIMESTAMP_MILLIS
                                                            )
                                            )
                                        },
                        visualSnapshot =
                                data.optionalString(
                                                ChatServiceProtocol
                                                        .EXTRA_ENV_VISUAL_SNAPSHOT_REFERENCE
                                        )
                                        ?.let { reference ->
                                            ChatEnvironmentVisualSnapshot(
                                                    reference = reference,
                                                    mimeType =
                                                            data.optionalString(
                                                                    ChatServiceProtocol
                                                                            .EXTRA_ENV_VISUAL_SNAPSHOT_MIME_TYPE
                                                            ),
                                                    width =
                                                            data.optionalPositiveInt(
                                                                    ChatServiceProtocol
                                                                            .EXTRA_ENV_VISUAL_SNAPSHOT_WIDTH
                                                            ),
                                                    height =
                                                            data.optionalPositiveInt(
                                                                    ChatServiceProtocol
                                                                            .EXTRA_ENV_VISUAL_SNAPSHOT_HEIGHT
                                                            ),
                                                    capturedAtMillis =
                                                            data.optionalLong(
                                                                    ChatServiceProtocol
                                                                            .EXTRA_ENV_VISUAL_SNAPSHOT_CAPTURED_AT_MILLIS
                                                            )
                                            )
                                        },
                        hasVisualSnapshot =
                                data.containsKey(
                                        ChatServiceProtocol.EXTRA_ENV_VISUAL_SNAPSHOT_REFERENCE
                                )
                )
        )
    }

    private fun persistConnectionConfig() {
        val editor = getSharedPreferences(CHAT_PREFS, MODE_PRIVATE).edit()
        if (lastKnownUrl != null) editor.putString(KEY_LAST_URL, lastKnownUrl)
        else editor.remove(KEY_LAST_URL)
        if (lastKnownPlatform != null) editor.putString(KEY_PLATFORM, lastKnownPlatform)
        else editor.remove(KEY_PLATFORM)
        if (lastKnownAuth != null) editor.putString(KEY_AUTH_TOKEN, lastKnownAuth)
        else editor.remove(KEY_AUTH_TOKEN)
        if (!lastKnownNickname.isNullOrEmpty()) editor.putString(KEY_NICKNAME, lastKnownNickname)
        else editor.remove(KEY_NICKNAME)
        if (lastKnownReceiverId != null) editor.putString(KEY_RECEIVER_ID, lastKnownReceiverId)
        else editor.remove(KEY_RECEIVER_ID)
        if (lastKnownReceiverNickname != null)
                editor.putString(KEY_RECEIVER_NICKNAME, lastKnownReceiverNickname)
        else editor.remove(KEY_RECEIVER_NICKNAME)
        editor.putBoolean(KEY_LOCAL_LLM_ENABLED, localLlmSettings.enabled)
        editor.putBoolean(KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, localLlmSettings.nativeToolCalling)
        editor.putOptionalString(KEY_LOCAL_LLM_BASE_URL, localLlmSettings.baseUrl)
        editor.putOptionalString(KEY_LOCAL_LLM_API_KEY, localLlmSettings.apiKey)
        editor.putOptionalString(KEY_LOCAL_LLM_PLANNER_MODEL, localLlmSettings.plannerModel)
        editor.putOptionalString(KEY_LOCAL_LLM_REPLIER_MODEL, localLlmSettings.replierModel)
        editor.putOptionalDouble(KEY_LOCAL_LLM_TEMPERATURE, localLlmSettings.temperature)
        editor.putOptionalInt(KEY_LOCAL_LLM_MAX_TOKENS, localLlmSettings.maxTokens)
        editor.putOptionalLong(KEY_LOCAL_LLM_TIMEOUT_MILLIS, localLlmSettings.timeoutMillis)
        editor.apply()
    }

    private fun readLocalLlmSettings(prefs: SharedPreferences): LocalLlmSettings =
            LocalLlmSettings(
                    enabled = prefs.getBoolean(KEY_LOCAL_LLM_ENABLED, false),
                    baseUrl = prefs.getString(KEY_LOCAL_LLM_BASE_URL, null),
                    apiKey = prefs.getString(KEY_LOCAL_LLM_API_KEY, null),
                    plannerModel = prefs.getString(KEY_LOCAL_LLM_PLANNER_MODEL, null),
                    replierModel = prefs.getString(KEY_LOCAL_LLM_REPLIER_MODEL, null),
                    nativeToolCalling =
                            prefs.getBoolean(KEY_LOCAL_LLM_NATIVE_TOOL_CALLING, true),
                    temperature =
                            prefs.getString(KEY_LOCAL_LLM_TEMPERATURE, null)?.toDoubleOrNull(),
                    maxTokens =
                            if (prefs.contains(KEY_LOCAL_LLM_MAX_TOKENS)) {
                                prefs.getInt(KEY_LOCAL_LLM_MAX_TOKENS, 0).takeIf { it > 0 }
                            } else {
                                null
                            },
                    timeoutMillis =
                            if (prefs.contains(KEY_LOCAL_LLM_TIMEOUT_MILLIS)) {
                                prefs.getLong(KEY_LOCAL_LLM_TIMEOUT_MILLIS, 0L)
                                        .takeIf { it > 0L }
                            } else {
                                LocalLlmSettings.DEFAULT_TIMEOUT_MILLIS
                            }
            )

    private fun Bundle.containsLocalLlmSettings(): Boolean =
            containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_ENABLED) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_BASE_URL) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_API_KEY) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_PLANNER_MODEL) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_REPLIER_MODEL) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_NATIVE_TOOL_CALLING) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_TEMPERATURE) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_MAX_TOKENS) ||
                    containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_TIMEOUT_MILLIS)

    private fun LocalLlmSettings.updatedFrom(data: Bundle): LocalLlmSettings =
            copy(
                    enabled =
                            if (data.containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_ENABLED)) {
                                data.getBoolean(ChatServiceProtocol.EXTRA_LOCAL_LLM_ENABLED)
                            } else {
                                enabled
                            },
                    baseUrl =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_LOCAL_LLM_BASE_URL,
                                    baseUrl
                            ),
                    apiKey =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_LOCAL_LLM_API_KEY,
                                    apiKey
                            ),
                    plannerModel =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_LOCAL_LLM_PLANNER_MODEL,
                                    plannerModel
                            ),
                    replierModel =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_LOCAL_LLM_REPLIER_MODEL,
                                    replierModel
                            ),
                    nativeToolCalling =
                            if (
                                    data.containsKey(
                                            ChatServiceProtocol
                                                    .EXTRA_LOCAL_LLM_NATIVE_TOOL_CALLING
                                    )
                            ) {
                                data.getBoolean(
                                        ChatServiceProtocol.EXTRA_LOCAL_LLM_NATIVE_TOOL_CALLING
                                )
                            } else {
                                nativeToolCalling
                            },
                    temperature =
                            if (data.containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_TEMPERATURE)) {
                                data.getDouble(
                                                ChatServiceProtocol.EXTRA_LOCAL_LLM_TEMPERATURE,
                                                Double.NaN
                                        )
                                        .takeIf { it.isFinite() }
                            } else {
                                temperature
                            },
                    maxTokens =
                            if (data.containsKey(ChatServiceProtocol.EXTRA_LOCAL_LLM_MAX_TOKENS)) {
                                data.getInt(ChatServiceProtocol.EXTRA_LOCAL_LLM_MAX_TOKENS, 0)
                                        .takeIf { it > 0 }
                            } else {
                                maxTokens
                            },
                    timeoutMillis =
                            if (
                                    data.containsKey(
                                            ChatServiceProtocol.EXTRA_LOCAL_LLM_TIMEOUT_MILLIS
                                    )
                            ) {
                                data.getLong(
                                                ChatServiceProtocol
                                                        .EXTRA_LOCAL_LLM_TIMEOUT_MILLIS,
                                                0L
                                        )
                                        .takeIf { it > 0L }
                            } else {
                                timeoutMillis
                            }
            )

    private fun Bundle.optionalStringOrExisting(key: String, current: String?): String? =
            if (containsKey(key)) getString(key)?.trim()?.takeIf { it.isNotEmpty() } else current

    private fun SharedPreferences.Editor.putOptionalString(
            key: String,
            value: String?
    ): SharedPreferences.Editor =
            if (value.isNullOrBlank()) remove(key) else putString(key, value)

    private fun SharedPreferences.Editor.putOptionalDouble(
            key: String,
            value: Double?
    ): SharedPreferences.Editor =
            if (value == null) remove(key) else putString(key, value.toString())

    private fun SharedPreferences.Editor.putOptionalInt(
            key: String,
            value: Int?
    ): SharedPreferences.Editor = if (value == null) remove(key) else putInt(key, value)

    private fun SharedPreferences.Editor.putOptionalLong(
            key: String,
            value: Long?
    ): SharedPreferences.Editor = if (value == null) remove(key) else putLong(key, value)

    private fun notifyError(message: String) {
        val data = Bundle().apply { putString(ChatServiceProtocol.EXTRA_ERROR_MESSAGE, message) }
        sendToClients(ChatServiceProtocol.MSG_EVENT_ERROR, data)
    }

    private class IncomingHandler(service: ChatConnectionService) :
            Handler(Looper.getMainLooper()) {
        private val serviceRef = WeakReference(service)

        override fun handleMessage(msg: Message) {
            val service = serviceRef.get() ?: return
            when (msg.what) {
                ChatServiceProtocol.MSG_REGISTER_CLIENT -> {
                    msg.replyTo?.let {
                        service.clients.add(it)
                        service.sendSnapshot(it)
                        service.broadcastConnectionState(service.manager.connectionState.value)
                    }
                }
                ChatServiceProtocol.MSG_UNREGISTER_CLIENT -> {
                    msg.replyTo?.let { service.clients.remove(it) }
                }
                ChatServiceProtocol.MSG_CONNECT -> service.handleConnectRequest(msg.data)
                ChatServiceProtocol.MSG_DISCONNECT -> service.manager.disconnect()
                ChatServiceProtocol.MSG_SEND_MESSAGE -> service.handleSendMessage(msg.data)
                ChatServiceProtocol.MSG_UPDATE_CONFIG -> service.handleConfigUpdate(msg.data)
                ChatServiceProtocol.MSG_START_LOCAL_RUNTIME -> service.manager.startLocalRuntime()
                ChatServiceProtocol.MSG_REQUEST_SNAPSHOT -> {
                    val target = msg.replyTo
                    if (target != null) service.sendSnapshot(target) else service.sendSnapshot()
                }
                ChatServiceProtocol.MSG_CLEAR_MESSAGES -> service.handleClearMessages(true)
                ChatServiceProtocol.MSG_CLEAR_MESSAGES_EPHEMERAL ->
                        service.handleClearMessages(false)
                ChatServiceProtocol.MSG_SET_ACTIVE_MODEL -> service.handleSetActiveModel(msg.data)
                ChatServiceProtocol.MSG_UPDATE_ENVIRONMENT_STATE ->
                        service.handleEnvironmentStateUpdate(msg.data)
                else -> super.handleMessage(msg)
            }
        }
    }

    companion object {
        private const val CHAT_PREFS = "chat_prefs"
        private const val KEY_LAST_URL = "last_url"
        private const val KEY_PLATFORM = "platform"
        private const val KEY_AUTH_TOKEN = "auth_token"
        private const val KEY_NICKNAME = "nickname"
        private const val KEY_RECEIVER_ID = "receiver_user_id"
        private const val KEY_RECEIVER_NICKNAME = "receiver_user_nickname"
        private const val KEY_LOCAL_LLM_ENABLED = "local_llm_enabled"
        private const val KEY_LOCAL_LLM_BASE_URL = "local_llm_base_url"
        private const val KEY_LOCAL_LLM_API_KEY = "local_llm_api_key"
        private const val KEY_LOCAL_LLM_PLANNER_MODEL = "local_llm_planner_model"
        private const val KEY_LOCAL_LLM_REPLIER_MODEL = "local_llm_replier_model"
        private const val KEY_LOCAL_LLM_NATIVE_TOOL_CALLING = "local_llm_native_tool_calling"
        private const val KEY_LOCAL_LLM_TEMPERATURE = "local_llm_temperature"
        private const val KEY_LOCAL_LLM_MAX_TOKENS = "local_llm_max_tokens"
        private const val KEY_LOCAL_LLM_TIMEOUT_MILLIS = "local_llm_timeout_millis"
    }
}

private fun Bundle.optionalString(key: String): String? =
        getString(key)?.trim()?.takeIf { it.isNotEmpty() }

private fun Bundle.optionalFloat(key: String): Float? =
        if (containsKey(key)) getFloat(key).takeIf { it.isFinite() } else null

private fun Bundle.optionalLong(key: String): Long? =
        if (containsKey(key)) getLong(key).takeIf { it >= 0L } else null

private fun Bundle.optionalPositiveInt(key: String): Int? =
        if (containsKey(key)) getInt(key).takeIf { it > 0 } else null
