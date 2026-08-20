package com.l2dchat.chat.service

import android.app.Service
import android.content.Intent
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
import com.l2dchat.chat.ChatWebSocketManager.RuntimeState
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.MemSettings
import com.l2dchat.core.config.MemSettingsStore
import com.l2dchat.core.config.WorkerLlmSettings
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.preferences.ChatPreferenceKeys
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
    private val secureStore by lazy { SecurePreferenceStore(applicationContext) }

    private val clients = CopyOnWriteArraySet<Messenger>()
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val incomingHandler = IncomingHandler(this)
    private val messenger = Messenger(incomingHandler)

    private lateinit var manager: ChatWebSocketManager

    private var lastKnownNickname: String? = null
    private var localLlmSettings = LocalLlmSettings()
    private var workerLlmSettings = WorkerLlmSettings()
    private var memSettings = MemSettings()
    private var lastConnectionError: String? = null

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
        manager.shutdown()
        serviceScope.cancel()
        clients.clear()
    }

    private fun restoreModelName(): String? {
        val prefs = getSharedPreferences(ChatPreferenceKeys.PREFS_NAME, MODE_PRIVATE)
        val folder = prefs.getString(ChatPreferenceKeys.SELECTED_MODEL_FOLDER, null)
        return folder?.substringAfterLast('/')?.ifBlank { null }
    }

    private fun applyStoredConfiguration() {
        val prefs = getSharedPreferences(CHAT_PREFS, MODE_PRIVATE)
        lastKnownNickname = prefs.getString(KEY_NICKNAME, null)

        lastKnownNickname?.takeUnless { it.isNullOrBlank() }?.let { manager.setUserProfile(it) }
        localLlmSettings = LocalLlmSettingsStore.read(prefs, secureStore)
        manager.setLocalLlmSettings(localLlmSettings)
        workerLlmSettings = WorkerLlmSettingsStore.read(prefs, secureStore)
        manager.setWorkerLlmSettings(workerLlmSettings)
        memSettings = MemSettingsStore.read(prefs, SecurePreferenceMemSecretStore(secureStore))
        manager.setMemSettings(memSettings)
        manager.startLocalRuntime()
    }

    private fun startObservers() {
        serviceScope.launch {
            manager.runtimeState.collect { state -> broadcastRuntimeState(state) }
        }
        serviceScope.launch {
            // Broadcast the last message when its id OR its agent-activity changes — the latter lets an
            // in-place inline agent bubble (running→done) propagate to clients (which replace by id).
            var lastBroadcast: Pair<String, String?>? = null
            manager.messages.collect { list ->
                val last = list.lastOrNull() ?: return@collect
                val key = last.id to last.agentActivityJson
                if (key == lastBroadcast) return@collect
                lastBroadcast = key
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
        serviceScope.launch {
            manager.errors.collect { message ->
                lastConnectionError = message
                notifyError(message)
                if (manager.runtimeState.value == RuntimeState.ERROR) {
                    broadcastRuntimeState(RuntimeState.ERROR)
                }
            }
        }
        serviceScope.launch {
            manager.processing.collect { processing -> broadcastProcessing(processing) }
        }
        serviceScope.launch {
            manager.messageFailures.collect { id -> broadcastMessageFailed(id) }
        }
        serviceScope.launch {
            // After a bulk history reload, push the FULL list (carries agent/file bubble JSON) — the
            // per-message broadcast only re-sends the last message.
            manager.historyReloaded.collect { sendSnapshot() }
        }
    }

    private fun broadcastRuntimeState(state: RuntimeState) {
        if (state != RuntimeState.ERROR) {
            lastConnectionError = null
        }
        val diagnostic =
                if (state == RuntimeState.ERROR) {
                    lastConnectionError?.let(::compactConnectionError)
                } else {
                    null
                }
        val bundle =
                Bundle().apply {
                    putInt(ChatServiceProtocol.EXTRA_CONNECTION_STATE, state.ordinal)
                    putString(
                            ChatServiceProtocol.EXTRA_CONNECTION_LABEL,
                            runtimeLabelFor(state, diagnostic)
                    )
                    diagnostic?.let {
                        putString(ChatServiceProtocol.EXTRA_ERROR_MESSAGE, it)
                    }
                }
        sendToClients(ChatServiceProtocol.MSG_EVENT_CONNECTION_STATE, bundle)
    }

    private fun runtimeLabelFor(state: RuntimeState, diagnostic: String?): String {
        val base =
                when (state) {
                    RuntimeState.STOPPED -> "本地运行时: stopped"
                    RuntimeState.STARTING -> "本地运行时: starting"
                    RuntimeState.RUNNING -> "本地运行时: ready"
                    RuntimeState.ERROR -> "本地运行时: error"
                }
        return diagnostic?.takeIf { it.isNotBlank() }?.let { "$base · $it" } ?: base
    }

    private fun compactConnectionError(message: String): String {
        val singleLine = message.lineSequence().firstOrNull()?.trim().orEmpty()
        return if (singleLine.length <= CONNECTION_ERROR_PREVIEW_LIMIT) {
            singleLine
        } else {
            singleLine.take(CONNECTION_ERROR_PREVIEW_LIMIT - 3) + "..."
        }
    }

    private fun broadcastChatMessage(message: ChatMessage) {
        val bundle =
                Bundle().apply {
                    putString(ChatServiceProtocol.EXTRA_MESSAGE_ID, message.id)
                    putString(ChatServiceProtocol.EXTRA_MESSAGE_CONTENT, message.content)
                    putBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FROM_USER, message.isFromUser)
                    putLong(ChatServiceProtocol.EXTRA_MESSAGE_TIMESTAMP, message.timestamp)
                    putBoolean(
                            ChatServiceProtocol.EXTRA_MESSAGE_FAILED,
                            manager.isMessageFailed(message.id)
                    )
                    message.agentActivityJson?.let {
                        putString(ChatServiceProtocol.EXTRA_MESSAGE_AGENT_ACTIVITY, it)
                    }
                    message.fileInfoJson?.let {
                        putString(ChatServiceProtocol.EXTRA_MESSAGE_FILE_INFO, it)
                    }
                }
        sendToClients(ChatServiceProtocol.MSG_EVENT_NEW_MESSAGE, bundle)
    }

    private fun broadcastProcessing(processing: Boolean) {
        val bundle =
                Bundle().apply { putBoolean(ChatServiceProtocol.EXTRA_PROCESSING, processing) }
        sendToClients(ChatServiceProtocol.MSG_EVENT_PROCESSING, bundle)
    }

    private fun broadcastMessageFailed(messageId: String) {
        val bundle =
                Bundle().apply { putString(ChatServiceProtocol.EXTRA_MESSAGE_ID, messageId) }
        sendToClients(ChatServiceProtocol.MSG_EVENT_MESSAGE_FAILED, bundle)
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
                                    putBoolean(
                                            ChatServiceProtocol.EXTRA_MESSAGE_FAILED,
                                            manager.isMessageFailed(m.id)
                                    )
                                    m.agentActivityJson?.let {
                                        putString(
                                                ChatServiceProtocol.EXTRA_MESSAGE_AGENT_ACTIVITY,
                                                it
                                        )
                                    }
                                    m.fileInfoJson?.let {
                                        putString(
                                                ChatServiceProtocol.EXTRA_MESSAGE_FILE_INFO,
                                                it
                                        )
                                    }
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

    private fun handleSendMessage(data: Bundle) {
        val text = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_TEXT)?.trim()
        if (text.isNullOrEmpty()) {
            notifyError("发送内容不能为空")
            return
        }
        manager.startLocalRuntime()
        manager.sendUserMessage(text)
    }

    private fun handleSendLifeCapture(data: Bundle) {
        val mediaPath = data.getString(ChatServiceProtocol.EXTRA_LIFE_CAPTURE_MEDIA_PATH)?.trim()
        if (mediaPath.isNullOrEmpty()) {
            notifyError("生活记录媒体路径不能为空")
            return
        }
        val modality = data.getString(ChatServiceProtocol.EXTRA_LIFE_CAPTURE_MODALITY).orEmpty()
        val caption = data.getString(ChatServiceProtocol.EXTRA_LIFE_CAPTURE_CAPTION).orEmpty()
        manager.startLocalRuntime()
        manager.sendLifeCapture(mediaPath, modality, caption)
    }

    private fun handleConfigUpdate(data: Bundle) {
        data.getString(ChatServiceProtocol.EXTRA_NICKNAME)?.let { name ->
            lastKnownNickname = name
            if (name.isNotBlank()) {
                manager.setUserProfile(name)
            }
        }
        if (data.containsLocalLlmSettings()) {
            localLlmSettings = localLlmSettings.updatedFrom(data)
            manager.setLocalLlmSettings(localLlmSettings)
        }
        if (data.containsWorkerLlmSettings()) {
            workerLlmSettings = workerLlmSettings.updatedFrom(data)
            manager.setWorkerLlmSettings(workerLlmSettings)
        }
        persistConnectionConfig()
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

    private fun handleSetActivePersona(data: Bundle) {
        val personaId = data.getString(ChatServiceProtocol.EXTRA_PERSONA_ID) ?: return
        manager.setActivePersona(applicationContext, personaId)
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

    private fun handleMotionFinished(data: Bundle) {
        manager.reportMotionFinished(
                group = data.optionalString(ChatServiceProtocol.EXTRA_ENV_MOTION_GROUP),
                index =
                        if (data.containsKey(ChatServiceProtocol.EXTRA_ENV_MOTION_INDEX)) {
                            data.getInt(ChatServiceProtocol.EXTRA_ENV_MOTION_INDEX)
                                    .takeIf { it >= 0 }
                        } else {
                            null
                        },
                filePath = data.optionalString(ChatServiceProtocol.EXTRA_ENV_MOTION_FILE_PATH),
                loop =
                        if (data.containsKey(ChatServiceProtocol.EXTRA_ENV_MOTION_LOOP)) {
                            data.getBoolean(ChatServiceProtocol.EXTRA_ENV_MOTION_LOOP)
                        } else {
                            false
                        },
                timestampMillis =
                        data.optionalLong(
                                        ChatServiceProtocol
                                                .EXTRA_ENV_MOTION_FINISHED_AT_MILLIS
                                )
                                ?: System.currentTimeMillis()
        )
    }

    private fun persistConnectionConfig() {
        val prefs = getSharedPreferences(CHAT_PREFS, MODE_PRIVATE)
        val editor = prefs.edit()
        if (!lastKnownNickname.isNullOrEmpty()) editor.putString(KEY_NICKNAME, lastKnownNickname)
        else editor.remove(KEY_NICKNAME)
        editor.apply()
        LocalLlmSettingsStore.persist(prefs, secureStore, localLlmSettings)
        WorkerLlmSettingsStore.persist(prefs, secureStore, workerLlmSettings)
        if (!MemSettingsStore.persist(
                        prefs,
                        SecurePreferenceMemSecretStore(secureStore),
                        memSettings
                )
        ) {
            logger.warn("Mem API key persistence failed; any legacy plaintext key was retained")
        }
    }

    private fun Bundle.containsWorkerLlmSettings(): Boolean =
            containsKey(ChatServiceProtocol.EXTRA_WORKER_BASE_URL) ||
                    containsKey(ChatServiceProtocol.EXTRA_WORKER_API_KEY) ||
                    containsKey(ChatServiceProtocol.EXTRA_WORKER_MODEL) ||
                    containsKey(ChatServiceProtocol.EXTRA_WORKER_SETTINGS_JSON)

    private fun WorkerLlmSettings.updatedFrom(data: Bundle): WorkerLlmSettings =
            copy(
                    baseUrl =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_WORKER_BASE_URL,
                                    baseUrl
                            ),
                    apiKey =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_WORKER_API_KEY,
                                    apiKey
                            ),
                    model =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_WORKER_MODEL,
                                    model
                            ),
                    settingsJson =
                            data.optionalStringOrExisting(
                                    ChatServiceProtocol.EXTRA_WORKER_SETTINGS_JSON,
                                    settingsJson
                            ),
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
                        service.broadcastRuntimeState(service.manager.runtimeState.value)
                        service.broadcastProcessing(service.manager.processing.value)
                    }
                }
                ChatServiceProtocol.MSG_UNREGISTER_CLIENT -> {
                    msg.replyTo?.let { service.clients.remove(it) }
                }
                ChatServiceProtocol.MSG_DISCONNECT -> service.manager.disconnect()
                ChatServiceProtocol.MSG_SEND_MESSAGE -> service.handleSendMessage(msg.data)
                ChatServiceProtocol.MSG_SEND_LIFE_CAPTURE ->
                        service.handleSendLifeCapture(msg.data)
                ChatServiceProtocol.MSG_UPDATE_CONFIG -> service.handleConfigUpdate(msg.data)
                ChatServiceProtocol.MSG_START_LOCAL_RUNTIME -> {
                    service.manager.startLocalRuntime()
                    service.persistConnectionConfig()
                }
                ChatServiceProtocol.MSG_REQUEST_SNAPSHOT -> {
                    val target = msg.replyTo
                    if (target != null) service.sendSnapshot(target) else service.sendSnapshot()
                }
                ChatServiceProtocol.MSG_CLEAR_MESSAGES -> service.handleClearMessages(true)
                ChatServiceProtocol.MSG_CLEAR_MESSAGES_EPHEMERAL ->
                        service.handleClearMessages(false)
                ChatServiceProtocol.MSG_SET_ACTIVE_MODEL -> service.handleSetActiveModel(msg.data)
                ChatServiceProtocol.MSG_SET_ACTIVE_PERSONA ->
                        service.handleSetActivePersona(msg.data)
                ChatServiceProtocol.MSG_UPDATE_ENVIRONMENT_STATE ->
                        service.handleEnvironmentStateUpdate(msg.data)
                ChatServiceProtocol.MSG_REPORT_MOTION_FINISHED ->
                        service.handleMotionFinished(msg.data)
                else -> super.handleMessage(msg)
            }
        }
    }

    companion object {
        private const val CHAT_PREFS = "chat_prefs"
        private const val KEY_NICKNAME = "nickname"
        private const val CONNECTION_ERROR_PREVIEW_LIMIT = 120
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
