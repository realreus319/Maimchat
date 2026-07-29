package com.l2dchat.chat.service

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Message
import android.os.Messenger
import android.os.RemoteException
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.MotionCommand
import com.l2dchat.chat.MotionMessage
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.PersonaRegistry
import com.l2dchat.core.config.WorkerLlmSettings
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.preferences.ChatPreferenceKeys
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

class ChatServiceClient(context: Context) : ServiceConnection {

    private val logger = L2DLogger.module(LogModule.CHAT)

    private val appContext = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val prefs = appContext.getSharedPreferences(CHAT_PREFS, Context.MODE_PRIVATE)
    private val secureStore = SecurePreferenceStore(appContext)

    private val incomingHandler =
            object : Handler(Looper.getMainLooper()) {
                override fun handleMessage(msg: Message) {
                    when (msg.what) {
                        ChatServiceProtocol.MSG_EVENT_CONNECTION_STATE -> {
                            val ordinal =
                                    msg.data.getInt(ChatServiceProtocol.EXTRA_CONNECTION_STATE, 0)
                            val state = RuntimeState.fromOrdinal(ordinal)
                            val label =
                                    msg.data.getString(ChatServiceProtocol.EXTRA_CONNECTION_LABEL)
                            val diagnostic =
                                    msg.data
                                            .getString(ChatServiceProtocol.EXTRA_ERROR_MESSAGE)
                                            .orEmpty()
                            _runtimeState.value = state
                            _runtimeLabel.value = label.orEmpty()
                            if (diagnostic.isNotBlank() || state != RuntimeState.ERROR) {
                                _runtimeDiagnostic.value = diagnostic
                            }
                        }
                        ChatServiceProtocol.MSG_EVENT_NEW_MESSAGE -> handleNewMessage(msg.data)
                        ChatServiceProtocol.MSG_EVENT_SNAPSHOT -> handleSnapshot(msg.data)
                        ChatServiceProtocol.MSG_EVENT_MESSAGE_FAILED ->
                                handleMessageFailed(msg.data)
                        ChatServiceProtocol.MSG_EVENT_PROCESSING -> {
                            _processing.value =
                                    msg.data.getBoolean(ChatServiceProtocol.EXTRA_PROCESSING)
                        }
                        ChatServiceProtocol.MSG_EVENT_STANDARD_MESSAGE ->
                                handleStandardMessage(msg.data)
                        ChatServiceProtocol.MSG_EVENT_ERROR -> {
                            msg.data
                                    .getString(ChatServiceProtocol.EXTRA_ERROR_MESSAGE)
                                    ?.takeIf { it.isNotBlank() }
                                    ?.let { error -> scope.launch { _errors.emit(error) } }
                        }
                        else -> super.handleMessage(msg)
                    }
                }
            }

    private val messenger = Messenger(incomingHandler)
    private var serviceMessenger: Messenger? = null
    private var isBound = false
    private val pendingCommands = mutableListOf<Pair<Int, Bundle?>>()

    private val _runtimeState = MutableStateFlow(RuntimeState.STOPPED)
    private val _runtimeLabel = MutableStateFlow("")
    private val _runtimeDiagnostic = MutableStateFlow("")
    private val _messages = MutableStateFlow<List<ChatMessageSnapshot>>(emptyList())
    private val _standardMessages = MutableStateFlow<List<MessageBase>>(emptyList())
    private val _newMessages =
            MutableSharedFlow<ChatMessageSnapshot>(
                    replay = 0,
                    extraBufferCapacity = 8,
                    onBufferOverflow = BufferOverflow.DROP_OLDEST
            )
    private val _snapshot =
            MutableSharedFlow<List<ChatMessageSnapshot>>(
                    replay = 0,
                    extraBufferCapacity = 1,
                    onBufferOverflow = BufferOverflow.DROP_OLDEST
            )
    private val _errors =
            MutableSharedFlow<String>(
                    replay = 0,
                    extraBufferCapacity = 4,
                    onBufferOverflow = BufferOverflow.DROP_OLDEST
            )
    private val _processing = MutableStateFlow(false)

    private val _userNickname =
            MutableStateFlow(prefs.getString(KEY_NICKNAME, null)?.takeIf { it.isNotBlank() })
    private val _localLlmSettings = MutableStateFlow(LocalLlmSettingsStore.read(prefs, secureStore))
    private val _workerLlmSettings = MutableStateFlow(WorkerLlmSettingsStore.read(prefs, secureStore))
    private val _activeModel = MutableStateFlow<String?>(null)
    // Active persona (== routing agentId in the service). Persisted client-side for the UI; pushed to
    // the service over IPC. The display name is derived locally from the registry.
    private val _selectedPersona =
            MutableStateFlow(
                    PersonaRegistry.normalize(
                            prefs.getString(ChatPreferenceKeys.SELECTED_PERSONA, null)
                    )
            )
    private val _personaDisplayName =
            MutableStateFlow(PersonaRegistry.displayNameFor(_selectedPersona.value) ?: "助手")
    private var motionCallback: ((MotionCommand) -> Unit)? = null

    val runtimeState: StateFlow<RuntimeState> = _runtimeState.asStateFlow()
    val runtimeLabel: StateFlow<String> = _runtimeLabel.asStateFlow()
    val runtimeDiagnostic: StateFlow<String> = _runtimeDiagnostic.asStateFlow()
    val messages: StateFlow<List<ChatMessageSnapshot>> = _messages.asStateFlow()
    val standardMessages: StateFlow<List<MessageBase>> = _standardMessages.asStateFlow()
    val newMessages: SharedFlow<ChatMessageSnapshot> = _newMessages.asSharedFlow()
    val snapshot: SharedFlow<List<ChatMessageSnapshot>> = _snapshot.asSharedFlow()
    val errors: SharedFlow<String> = _errors.asSharedFlow()
    /** True while the runtime is generating a reply; drives the UI "thinking" indicator. */
    val processing: StateFlow<Boolean> = _processing.asStateFlow()
    val userNickname: StateFlow<String?> = _userNickname.asStateFlow()
    val activeModel: StateFlow<String?> = _activeModel.asStateFlow()
    val selectedPersona: StateFlow<String> = _selectedPersona.asStateFlow()
    /** The active persona's display name (小千 / 温柔助手) — what the chat shows for the character. */
    val personaDisplayName: StateFlow<String> = _personaDisplayName.asStateFlow()
    val localLlmSettings: StateFlow<LocalLlmSettings> = _localLlmSettings.asStateFlow()
    val workerLlmSettings: StateFlow<WorkerLlmSettings> = _workerLlmSettings.asStateFlow()

    fun bindService() {
        if (isBound) return
        val intent = Intent(appContext, ChatConnectionService::class.java)
        isBound = appContext.bindService(intent, this, Context.BIND_AUTO_CREATE)
        if (!isBound) {
            logger.error("bindService failed")
        }
    }

    fun unbindService() {
        if (!isBound) return
        try {
            sendCommand(ChatServiceProtocol.MSG_UNREGISTER_CLIENT)
        } catch (_: Exception) {}
        appContext.unbindService(this)
        isBound = false
        serviceMessenger = null
    }

    override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
        serviceMessenger = Messenger(service)
        isBound = true
        logger.info("Service connected component=$name pending=${pendingCommands.size}")
        flushPendingCommands()
        sendCommand(ChatServiceProtocol.MSG_REGISTER_CLIENT)
        // Tell the service which persona to run as (the client persists the choice; the service's own
        // pref read is just a fallback for a :chat restart with no client attached).
        sendPersonaToService(_selectedPersona.value)
        requestSnapshot()
    }

    override fun onServiceDisconnected(name: ComponentName?) {
        isBound = false
        serviceMessenger = null
        logger.warn("Service disconnected component=$name")
        _runtimeState.value = RuntimeState.STOPPED
        _runtimeDiagnostic.value = ""
    }

    fun ensureBound() {
        if (!isBound) {
            logger.debug(
                    "ensureBound() -> binding to ChatConnectionService",
                    throttleMs = 2_000L,
                    throttleKey = "ensure_bound"
            )
            bindService()
        }
    }

    fun disconnect() {
        sendCommand(ChatServiceProtocol.MSG_DISCONNECT)
    }

    fun startLocalRuntime() {
        ensureBound()
        sendCommand(ChatServiceProtocol.MSG_START_LOCAL_RUNTIME)
    }

    fun sendUserMessage(text: String) {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return
        sendCommand(
                ChatServiceProtocol.MSG_SEND_MESSAGE,
                Bundle().apply { putString(ChatServiceProtocol.EXTRA_MESSAGE_TEXT, trimmed) }
        )
    }

    fun setUserProfile(nickname: String?) {
        val sanitized = nickname?.trim().takeUnless { it.isNullOrEmpty() }
        _userNickname.value = sanitized
        sendConfigUpdate { putString(ChatServiceProtocol.EXTRA_NICKNAME, sanitized ?: "") }
    }

    fun updateLocalLlmSettings(settings: LocalLlmSettings) {
        _localLlmSettings.value = settings
        LocalLlmSettingsStore.persist(prefs, secureStore, settings)
        sendConfigUpdate {
            putBoolean(ChatServiceProtocol.EXTRA_LOCAL_LLM_ENABLED, settings.enabled)
            putString(ChatServiceProtocol.EXTRA_LOCAL_LLM_BASE_URL, settings.baseUrl.orEmpty())
            putString(ChatServiceProtocol.EXTRA_LOCAL_LLM_API_KEY, settings.apiKey.orEmpty())
            putString(
                    ChatServiceProtocol.EXTRA_LOCAL_LLM_PLANNER_MODEL,
                    settings.plannerModel.orEmpty()
            )
            putString(
                    ChatServiceProtocol.EXTRA_LOCAL_LLM_REPLIER_MODEL,
                    settings.replierModel.orEmpty()
            )
            putBoolean(
                    ChatServiceProtocol.EXTRA_LOCAL_LLM_NATIVE_TOOL_CALLING,
                    settings.nativeToolCalling
            )
            putDouble(
                    ChatServiceProtocol.EXTRA_LOCAL_LLM_TEMPERATURE,
                    settings.temperature ?: Double.NaN
            )
            putInt(ChatServiceProtocol.EXTRA_LOCAL_LLM_MAX_TOKENS, settings.maxTokens ?: 0)
            putLong(
                    ChatServiceProtocol.EXTRA_LOCAL_LLM_TIMEOUT_MILLIS,
                    settings.timeoutMillis ?: 0L
            )
        }
    }

    fun updateWorkerLlmSettings(settings: WorkerLlmSettings) {
        _workerLlmSettings.value = settings
        WorkerLlmSettingsStore.persist(prefs, secureStore, settings)
        sendConfigUpdate {
            putString(ChatServiceProtocol.EXTRA_WORKER_BASE_URL, settings.baseUrl.orEmpty())
            putString(ChatServiceProtocol.EXTRA_WORKER_API_KEY, settings.apiKey.orEmpty())
            putString(ChatServiceProtocol.EXTRA_WORKER_MODEL, settings.model.orEmpty())
            putString(
                    ChatServiceProtocol.EXTRA_WORKER_SETTINGS_JSON,
                    settings.settingsJson.orEmpty()
            )
        }
    }

    fun getWorkerLlmSettings(): WorkerLlmSettings = _workerLlmSettings.value

    fun updateEnvironmentState(
            modelKey: String? = null,
            modelName: String? = null,
            modelFolderPath: String? = null,
            modelFile: String? = null,
            lifecycleState: String? = null,
            motionFiles: List<String>? = null,
            appVisible: Boolean? = null,
            wallpaperVisible: Boolean? = null,
            backgroundPath: String? = null,
            hasBackgroundPath: Boolean = backgroundPath != null,
            visualSnapshotReference: String? = null,
            visualSnapshotMimeType: String? = null,
            visualSnapshotWidth: Int? = null,
            visualSnapshotHeight: Int? = null,
            visualSnapshotCapturedAtMillis: Long? = null,
            hasVisualSnapshot: Boolean = visualSnapshotReference != null
    ) {
        sendCommand(
                ChatServiceProtocol.MSG_UPDATE_ENVIRONMENT_STATE,
                Bundle().apply {
                    putString(ChatServiceProtocol.EXTRA_ENV_MODEL_KEY, modelKey.orEmpty())
                    putString(ChatServiceProtocol.EXTRA_ENV_MODEL_NAME, modelName.orEmpty())
                    putString(
                            ChatServiceProtocol.EXTRA_ENV_MODEL_FOLDER_PATH,
                            modelFolderPath.orEmpty()
                    )
                    putString(ChatServiceProtocol.EXTRA_ENV_MODEL_FILE, modelFile.orEmpty())
                    putString(
                            ChatServiceProtocol.EXTRA_ENV_MODEL_LIFECYCLE_STATE,
                            lifecycleState.orEmpty()
                    )
                    motionFiles?.let {
                        putStringArrayList(
                                ChatServiceProtocol.EXTRA_ENV_MOTION_FILES,
                                ArrayList(it)
                        )
                    }
                    appVisible?.let {
                        putBoolean(ChatServiceProtocol.EXTRA_ENV_APP_VISIBLE, it)
                    }
                    wallpaperVisible?.let {
                        putBoolean(ChatServiceProtocol.EXTRA_ENV_WALLPAPER_VISIBLE, it)
                    }
                    if (hasBackgroundPath) {
                        putString(
                                ChatServiceProtocol.EXTRA_ENV_BACKGROUND_PATH,
                                backgroundPath.orEmpty()
                        )
                    }
                    if (hasVisualSnapshot) {
                        putString(
                                ChatServiceProtocol.EXTRA_ENV_VISUAL_SNAPSHOT_REFERENCE,
                                visualSnapshotReference.orEmpty()
                        )
                        visualSnapshotMimeType?.trim()?.takeIf { it.isNotEmpty() }
                                ?.let {
                                    putString(
                                            ChatServiceProtocol.EXTRA_ENV_VISUAL_SNAPSHOT_MIME_TYPE,
                                            it
                                    )
                                }
                        visualSnapshotWidth?.takeIf { it > 0 }
                                ?.let {
                                    putInt(
                                            ChatServiceProtocol.EXTRA_ENV_VISUAL_SNAPSHOT_WIDTH,
                                            it
                                    )
                                }
                        visualSnapshotHeight?.takeIf { it > 0 }
                                ?.let {
                                    putInt(
                                            ChatServiceProtocol.EXTRA_ENV_VISUAL_SNAPSHOT_HEIGHT,
                                            it
                                    )
                                }
                        visualSnapshotCapturedAtMillis?.takeIf { it >= 0L }
                                ?.let {
                                    putLong(
                                            ChatServiceProtocol
                                                    .EXTRA_ENV_VISUAL_SNAPSHOT_CAPTURED_AT_MILLIS,
                                            it
                                    )
                                }
                    }
                }
        )
    }

    fun updateEnvironmentInteraction(
            type: String,
            x: Float? = null,
            y: Float? = null,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        val cleanType = type.trim()
        if (cleanType.isBlank()) return
        sendCommand(
                ChatServiceProtocol.MSG_UPDATE_ENVIRONMENT_STATE,
                Bundle().apply {
                    putString(ChatServiceProtocol.EXTRA_ENV_INTERACTION_TYPE, cleanType)
                    x?.takeIf { it.isFinite() }
                            ?.let { putFloat(ChatServiceProtocol.EXTRA_ENV_INTERACTION_X, it) }
                    y?.takeIf { it.isFinite() }
                            ?.let { putFloat(ChatServiceProtocol.EXTRA_ENV_INTERACTION_Y, it) }
                    putLong(
                            ChatServiceProtocol.EXTRA_ENV_INTERACTION_TIMESTAMP_MILLIS,
                            timestampMillis.coerceAtLeast(0L)
                    )
                }
        )
    }

    fun reportMotionFinished(
            group: String? = null,
            index: Int? = null,
            filePath: String? = null,
            loop: Boolean = false,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        if (group.isNullOrBlank() && filePath.isNullOrBlank()) return
        sendCommand(
                ChatServiceProtocol.MSG_REPORT_MOTION_FINISHED,
                Bundle().apply {
                    group?.trim()?.takeIf { it.isNotEmpty() }
                            ?.let { putString(ChatServiceProtocol.EXTRA_ENV_MOTION_GROUP, it) }
                    index?.takeIf { it >= 0 }
                            ?.let { putInt(ChatServiceProtocol.EXTRA_ENV_MOTION_INDEX, it) }
                    filePath?.trim()?.takeIf { it.isNotEmpty() }
                            ?.let {
                                putString(ChatServiceProtocol.EXTRA_ENV_MOTION_FILE_PATH, it)
                            }
                    putBoolean(ChatServiceProtocol.EXTRA_ENV_MOTION_LOOP, loop)
                    putLong(
                            ChatServiceProtocol.EXTRA_ENV_MOTION_FINISHED_AT_MILLIS,
                            timestampMillis.coerceAtLeast(0L)
                    )
                }
        )
    }

    fun setActiveModel(modelName: String?) {
        val trimmed = modelName?.trim().takeUnless { it.isNullOrEmpty() }
        _activeModel.value = trimmed
        sendCommand(
                ChatServiceProtocol.MSG_SET_ACTIVE_MODEL,
                Bundle().apply { putString(ChatServiceProtocol.EXTRA_MODEL_NAME, trimmed) }
        )
    }

    /** Switch the active persona; persists it client-side and pushes it to the service. */
    fun setActivePersona(personaId: String) {
        val resolved = PersonaRegistry.normalize(personaId)
        _selectedPersona.value = resolved
        _personaDisplayName.value = PersonaRegistry.displayNameFor(resolved) ?: resolved
        prefs.edit().putString(ChatPreferenceKeys.SELECTED_PERSONA, resolved).apply()
        sendPersonaToService(resolved)
    }

    private fun sendPersonaToService(personaId: String) {
        sendCommand(
                ChatServiceProtocol.MSG_SET_ACTIVE_PERSONA,
                Bundle().apply { putString(ChatServiceProtocol.EXTRA_PERSONA_ID, personaId) }
        )
    }

    fun clearMessages() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        sendCommand(ChatServiceProtocol.MSG_CLEAR_MESSAGES)
    }

    fun clearMessagesEphemeral() {
        _messages.value = emptyList()
        _standardMessages.value = emptyList()
        sendCommand(ChatServiceProtocol.MSG_CLEAR_MESSAGES_EPHEMERAL)
    }

    fun setMotionTriggerCallback(callback: (String, Int, Boolean) -> Unit) {
        motionCallback = { command ->
            val group = command.group
            val index = command.index
            if (group != null && index != null) {
                callback(group, index, command.loop)
            }
        }
    }

    fun setMotionCommandCallback(callback: (MotionCommand) -> Unit) {
        motionCallback = callback
    }

    fun requestSnapshot() {
        sendCommand(ChatServiceProtocol.MSG_REQUEST_SNAPSHOT)
    }

    fun hasUserNickname(): Boolean = !_userNickname.value.isNullOrBlank()

    fun getUserNickname(): String? = _userNickname.value

    fun getLocalLlmSettings(): LocalLlmSettings = _localLlmSettings.value

    fun getRuntimeStateDescription(): String =
            runtimeLabel.value.ifBlank { fallbackRuntimeLabel(runtimeState.value) }

    fun release() {
        unbindService()
        scope.cancel()
    }

    private fun sendCommand(what: Int, data: Bundle? = null) {
        val target = serviceMessenger
        if (target == null) {
            queueCommand(what, data)
            if (!isBound) bindService()
            return
        }
        dispatchCommand(target, what, data)
    }

    private fun sendConfigUpdate(builder: Bundle.() -> Unit) {
        val bundle = Bundle().apply(builder)
        sendCommand(ChatServiceProtocol.MSG_UPDATE_CONFIG, bundle)
    }

    private fun handleNewMessage(data: Bundle) {
        val content = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_CONTENT) ?: return
        val id = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_ID) ?: return
        val isFromUser = data.getBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FROM_USER)
        val timestamp = data.getLong(ChatServiceProtocol.EXTRA_MESSAGE_TIMESTAMP)
        val failed = data.getBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FAILED)
        val agentActivity = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_AGENT_ACTIVITY)
        val fileInfo = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_FILE_INFO)
        val snapshot =
                ChatMessageSnapshot(
                        id,
                        content,
                        isFromUser,
                        timestamp,
                        failed,
                        agentActivity,
                        fileInfo
                )
        _messages.update { current ->
            // Replace-or-add by id, so an inline agent bubble's in-place updates (running→done) apply.
            val idx = current.indexOfFirst { it.id == id }
            if (idx >= 0) current.toMutableList().also { it[idx] = snapshot }
            else current + snapshot
        }
        scope.launch { _newMessages.emit(snapshot) }
    }

    private fun handleMessageFailed(data: Bundle) {
        val id = data.getString(ChatServiceProtocol.EXTRA_MESSAGE_ID) ?: return
        _messages.update { current ->
            current.map { if (it.id == id && !it.failed) it.copy(failed = true) else it }
        }
    }

    private fun handleStandardMessage(data: Bundle) {
        val json = data.getString(ChatServiceProtocol.EXTRA_STANDARD_MESSAGE_JSON) ?: return
        val message = runCatching { MessageBase.fromJsonString(json) }.getOrNull() ?: return
        val messageId = message.messageInfo.messageId
        val isDuplicate =
                messageId != null &&
                        _standardMessages.value.any { it.messageInfo.messageId == messageId }
        if (!isDuplicate) {
            _standardMessages.update { current -> current + message }
            MotionMessage.parse(message)?.let { command -> motionCallback?.invoke(command) }
        }
    }

    private fun handleSnapshot(data: Bundle) {
        val messageBundles =
                data.getParcelableArrayList<Bundle>(ChatServiceProtocol.EXTRA_MESSAGE_BUNDLE_LIST)
                        ?: emptyList()
        val messages =
                messageBundles.mapNotNull { bundle ->
                    val id =
                            bundle.getString(ChatServiceProtocol.EXTRA_MESSAGE_ID)
                                    ?: return@mapNotNull null
                    val content =
                            bundle.getString(ChatServiceProtocol.EXTRA_MESSAGE_CONTENT)
                                    ?: return@mapNotNull null
                    val isFromUser = bundle.getBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FROM_USER)
                    val timestamp = bundle.getLong(ChatServiceProtocol.EXTRA_MESSAGE_TIMESTAMP)
                    val failed = bundle.getBoolean(ChatServiceProtocol.EXTRA_MESSAGE_FAILED)
                    val agentActivity =
                            bundle.getString(ChatServiceProtocol.EXTRA_MESSAGE_AGENT_ACTIVITY)
                    val fileInfo =
                            bundle.getString(ChatServiceProtocol.EXTRA_MESSAGE_FILE_INFO)
                    ChatMessageSnapshot(
                            id,
                            content,
                            isFromUser,
                            timestamp,
                            failed,
                            agentActivity,
                            fileInfo
                    )
                }
        val standardJson =
                data.getStringArrayList(ChatServiceProtocol.EXTRA_STANDARD_MESSAGE_LIST)
                        ?: arrayListOf()
        val standardMessages =
                standardJson.mapNotNull { json ->
                    runCatching { MessageBase.fromJsonString(json) }.getOrNull()
                }
        _messages.value = messages
        _standardMessages.value = standardMessages
        scope.launch { _snapshot.emit(messages) }
    }

    private fun flushPendingCommands() {
        if (pendingCommands.isEmpty()) return
        val target = serviceMessenger ?: return
        logger.debug(
                "Flushing ${pendingCommands.size} pending commands",
                throttleMs = 1_000L,
                throttleKey = "flush_pending"
        )
        val snapshot = ArrayList(pendingCommands)
        pendingCommands.clear()
        snapshot.forEach { (pendingWhat, pendingData) ->
            dispatchCommand(target, pendingWhat, pendingData)
        }
    }

    private fun queueCommand(what: Int, data: Bundle?) {
        val copy = data?.let { Bundle(it) }
        pendingCommands.add(what to copy)
        logger.debug(
                "queueCommand ${commandName(what)} pending=${pendingCommands.size} bound=$isBound messengerReady=${serviceMessenger != null}",
                throttleMs = 500L,
                throttleKey = "queue_${commandName(what)}"
        )
    }

    private fun dispatchCommand(target: Messenger, what: Int, data: Bundle?) {
        try {
            val msg =
                    Message.obtain(null, what).apply {
                        replyTo = messenger
                        data?.let { this.data = it }
                    }
            logger.debug(
                    "dispatchCommand ${commandName(what)} -> binder=$target",
                    throttleMs = 500L,
                    throttleKey = "dispatch_${commandName(what)}"
            )
            target.send(msg)
        } catch (e: RemoteException) {
            logger.error("sendMessage failed", e)
        }
    }

    private fun commandName(what: Int): String =
            when (what) {
                ChatServiceProtocol.MSG_REGISTER_CLIENT -> "MSG_REGISTER_CLIENT"
                ChatServiceProtocol.MSG_UNREGISTER_CLIENT -> "MSG_UNREGISTER_CLIENT"
                ChatServiceProtocol.MSG_DISCONNECT -> "MSG_DISCONNECT"
                ChatServiceProtocol.MSG_SEND_MESSAGE -> "MSG_SEND_MESSAGE"
                ChatServiceProtocol.MSG_UPDATE_CONFIG -> "MSG_UPDATE_CONFIG"
                ChatServiceProtocol.MSG_REQUEST_SNAPSHOT -> "MSG_REQUEST_SNAPSHOT"
                ChatServiceProtocol.MSG_CLEAR_MESSAGES -> "MSG_CLEAR_MESSAGES"
                ChatServiceProtocol.MSG_CLEAR_MESSAGES_EPHEMERAL -> "MSG_CLEAR_MESSAGES_EPHEMERAL"
                ChatServiceProtocol.MSG_START_LOCAL_RUNTIME -> "MSG_START_LOCAL_RUNTIME"
                ChatServiceProtocol.MSG_UPDATE_ENVIRONMENT_STATE ->
                        "MSG_UPDATE_ENVIRONMENT_STATE"
                ChatServiceProtocol.MSG_REPORT_MOTION_FINISHED ->
                        "MSG_REPORT_MOTION_FINISHED"
                ChatServiceProtocol.MSG_EVENT_CONNECTION_STATE -> "MSG_EVENT_CONNECTION_STATE"
                ChatServiceProtocol.MSG_EVENT_NEW_MESSAGE -> "MSG_EVENT_NEW_MESSAGE"
                ChatServiceProtocol.MSG_EVENT_SNAPSHOT -> "MSG_EVENT_SNAPSHOT"
                ChatServiceProtocol.MSG_EVENT_ERROR -> "MSG_EVENT_ERROR"
                ChatServiceProtocol.MSG_EVENT_STANDARD_MESSAGE -> "MSG_EVENT_STANDARD_MESSAGE"
                ChatServiceProtocol.MSG_SET_ACTIVE_MODEL -> "MSG_SET_ACTIVE_MODEL"
                else -> "MSG_UNKNOWN_$what"
            }

    data class ChatMessageSnapshot(
            val id: String,
            val content: String,
            val isFromUser: Boolean,
            val timestamp: Long,
            val failed: Boolean = false,
            /** JSON of WorkerActivity when this is an inline AI-agent activity bubble (else null). */
            val agentActivityJson: String? = null,
            /** JSON (name/path/mime/size/description) when this is a worker-submitted file (else null). */
            val fileInfoJson: String? = null
    )

    enum class RuntimeState {
        STOPPED,
        STARTING,
        RUNNING,
        ERROR;

        companion object {
            fun fromOrdinal(ordinal: Int): RuntimeState = values().getOrNull(ordinal) ?: STOPPED
        }
    }

    private fun fallbackRuntimeLabel(state: RuntimeState): String =
            when (state) {
                RuntimeState.STOPPED -> "本地运行时: stopped"
                RuntimeState.STARTING -> "本地运行时: starting"
                RuntimeState.RUNNING -> "本地运行时: ready"
                RuntimeState.ERROR -> "本地运行时: error"
            }

    companion object {
        private const val CHAT_PREFS = "chat_prefs"
        private const val KEY_NICKNAME = "nickname"
    }
}
