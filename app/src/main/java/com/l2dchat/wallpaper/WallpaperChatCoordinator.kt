package com.l2dchat.wallpaper

import android.content.Context
import com.l2dchat.chat.service.ChatServiceClient
import com.l2dchat.chat.service.ChatServiceClient.ChatMessageSnapshot
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import com.l2dchat.preferences.ChatPreferenceKeys
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.min
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull

/** 管理桌面小部件与壁纸后台的聊天调度，复用独立进程的聊天服务以共享 WebSocket 连接，并将最新对话同步到小部件。 */
object WallpaperChatCoordinator {
    private val logger = L2DLogger.module(LogModule.WALLPAPER)
    private const val ENVIRONMENT_VISUAL_SNAPSHOT_MIME_TYPE =
            "application/vnd.l2dchat.environment-snapshot+json"
    private const val CHAT_PREFS = "chat_prefs"
    private const val KEY_NICKNAME = "nickname"
    private const val CONNECTION_TIMEOUT_MS = 4_000L
    private const val PREVIEW_MAX_CHARS = 80

    private val initMutex = Mutex()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val clientRef = AtomicReference<ChatServiceClient?>()
    private val listeners = CopyOnWriteArraySet<Listener>()

    interface Listener {
        fun onMessageAppended(message: ChatMessageSnapshot)

        /** The full shared chat history (same source as the main UI), for seeding/aligning state. */
        fun onHistoryLoaded(messages: List<ChatMessageSnapshot>) {}
    }

    suspend fun sendMessage(context: Context, message: String): Boolean {
        val trimmed = message.trim()
        if (trimmed.isEmpty()) {
            logger.warn("忽略空白消息", throttleMs = 1_000L, throttleKey = "empty_message")
            return false
        }
        val appContext = context.applicationContext
        val client = ensureClient(appContext)
        client.ensureBound()
        updateWidgetPreview(appContext, trimmed, fromUser = true)
        val connected = ensureConnection(client)
        if (!connected) {
            logger.warn("无法在超时时间内连接到服务器，发送失败", throttleMs = 2_000L, throttleKey = "send_timeout")
            return false
        }
        client.sendUserMessage(trimmed)
        return true
    }

    /**
     * Wipe the active conversation (same as the in-app "清空聊天记录" button). Used by the debug
     * TestControlReceiver to give E2E tests a clean slate without the flaky UI/IME path.
     */
    suspend fun clearChat(context: Context): Boolean {
        val appContext = context.applicationContext
        val client = ensureClient(appContext)
        client.ensureBound()
        if (!ensureConnection(client)) {
            logger.warn("清空失败：无法连接服务", throttleMs = 2_000L, throttleKey = "clear_timeout")
            return false
        }
        client.clearMessages()
        return true
    }

    suspend fun warmUp(context: Context) {
        ensureClient(context.applicationContext)
    }

    fun reportEnvironmentState(
            context: Context,
            wallpaperVisible: Boolean? = null,
            surfaceWidth: Int? = null,
            surfaceHeight: Int? = null,
            backgroundPath: String? = null,
            hasBackgroundPath: Boolean = false,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        val appContext = context.applicationContext
        val surfaceReported = surfaceWidth != null || surfaceHeight != null
        if (wallpaperVisible == null && !surfaceReported && !hasBackgroundPath) return
        scope.launch {
            try {
                val client = ensureClient(appContext)
                client.ensureBound()
                val width = surfaceWidth?.takeIf { it > 0 }
                val height = surfaceHeight?.takeIf { it > 0 }
                val snapshotReference =
                        if (width != null && height != null) {
                            buildWallpaperVisualSnapshotReference(
                                    context = appContext,
                                    visible = wallpaperVisible,
                                    width = width,
                                    height = height
                            )
                        } else {
                            null
                        }
                client.updateEnvironmentState(
                        wallpaperVisible = wallpaperVisible,
                        backgroundPath = backgroundPath,
                        hasBackgroundPath = hasBackgroundPath,
                        visualSnapshotReference = snapshotReference,
                        visualSnapshotMimeType =
                                if (snapshotReference != null) {
                                    ENVIRONMENT_VISUAL_SNAPSHOT_MIME_TYPE
                                } else {
                                    null
                                },
                        visualSnapshotWidth = width,
                        visualSnapshotHeight = height,
                        visualSnapshotCapturedAtMillis =
                                if (snapshotReference != null) timestampMillis else null,
                        hasVisualSnapshot = surfaceReported
                )
            } catch (t: Throwable) {
                logger.warn("壁纸环境状态上报失败", t)
            }
        }
    }

    fun reportEnvironmentInteraction(
            context: Context,
            type: String,
            x: Float?,
            y: Float?,
            timestampMillis: Long = System.currentTimeMillis()
    ) {
        val cleanType = type.trim()
        if (cleanType.isEmpty()) return
        val appContext = context.applicationContext
        scope.launch {
            try {
                val client = ensureClient(appContext)
                client.ensureBound()
                client.updateEnvironmentInteraction(
                        type = cleanType,
                        x = x?.takeIf { it.isFinite() },
                        y = y?.takeIf { it.isFinite() },
                        timestampMillis = timestampMillis
                )
            } catch (t: Throwable) {
                logger.warn("壁纸环境交互上报失败", t)
            }
        }
    }

    fun updateWidgetPreview(context: Context, message: String, fromUser: Boolean) {
        val label = if (fromUser) "我" else "TA"
        val preview = formatPreview(label, message)
        val prefs =
                context.applicationContext.getSharedPreferences(
                        WallpaperComm.PREF_WIDGET_INPUT,
                        Context.MODE_PRIVATE
                )
        prefs.edit().putString(WallpaperComm.PREF_WIDGET_LAST_INPUT_KEY, preview).apply()
        Live2DChatWidgetProvider.updateAllWidgets(context.applicationContext)
    }

    private suspend fun ensureClient(context: Context): ChatServiceClient {
        val existing = clientRef.get()
        if (existing != null) return existing
        return initMutex.withLock {
            clientRef.get() ?: createClient(context.applicationContext).also { clientRef.set(it) }
        }
    }

    private fun createClient(context: Context): ChatServiceClient {
        val client = ChatServiceClient(context)
        client.bindService()
        applyUserConfig(context, client)
        startMessageCollection(context, client)
        return client
    }

    fun addListener(listener: Listener) {
        listeners.add(listener)
        // Seed a freshly-attached listener (e.g. a recreated wallpaper engine) with the history
        // that is already loaded, so it shows the same recent conversation as the main UI rather
        // than waiting for the next message.
        clientRef.get()?.messages?.value?.takeIf { it.isNotEmpty() }?.let {
            try {
                listener.onHistoryLoaded(it)
            } catch (t: Throwable) {
                logger.warn("Listener history seed failed", t)
            }
        }
    }

    fun removeListener(listener: Listener) {
        listeners.remove(listener)
    }

    private fun applyUserConfig(context: Context, client: ChatServiceClient) {
        client.ensureBound()
        val prefs = context.getSharedPreferences(CHAT_PREFS, Context.MODE_PRIVATE)
        prefs.getString(KEY_NICKNAME, null)?.takeUnless { it.isBlank() }?.let {
            client.setUserProfile(it)
        }
        // 以"更换模型"选定的模型（chat_prefs/selected_model_folder）为唯一真相源，沿用 app 内行为
        prefs.getString(ChatPreferenceKeys.SELECTED_MODEL_FOLDER, null)?.let { folder ->
            val modelName = folder.substringAfterLast('/')
            client.setActiveModel(modelName)
        }
        client.startLocalRuntime()
        client.requestSnapshot()
    }

    private fun startMessageCollection(context: Context, client: ChatServiceClient) {
        // Full-history snapshots (register / requestSnapshot / clear / model switch) — the same
        // source the main UI renders. Listeners use this to align their state with the real history
        // instead of accumulating only newly-arriving messages.
        scope.launch {
            client.snapshot.collect { messages ->
                // Only real conversation bubbles reach the wallpaper/widget — agent-activity and
                // file-attachment bubbles (empty content) would render as black boxes on the wallpaper.
                val convo = messages.filter { it.isConversationBubble() }
                convo.lastOrNull()?.let {
                    updateWidgetPreview(context, it.content, fromUser = it.isFromUser)
                }
                notifyHistoryLoaded(convo)
            }
        }
        // Incremental appends.
        scope.launch {
            client.newMessages.collect { message ->
                if (!message.isConversationBubble()) return@collect
                updateWidgetPreview(context, message.content, fromUser = message.isFromUser)
                notifyListeners(message)
                if (!message.isFromUser) {
                    logger.info(
                            "收到回复 -> ${message.content.take(120)}${if (message.content.length > 120) "…" else ""}"
                    )
                }
            }
        }
    }

    /** A real chat bubble (vs an inline agent-activity / file-attachment bubble, which the wallpaper
     *  must not render). */
    private fun ChatMessageSnapshot.isConversationBubble(): Boolean =
            agentActivityJson == null && fileInfoJson == null

    private fun notifyHistoryLoaded(messages: List<ChatMessageSnapshot>) {
        listeners.forEach { listener ->
            try {
                listener.onHistoryLoaded(messages)
            } catch (t: Throwable) {
                logger.warn("Listener history dispatch failed", t)
            }
        }
    }

    private fun notifyListeners(message: ChatMessageSnapshot) {
        listeners.forEach { listener ->
            try {
                listener.onMessageAppended(message)
            } catch (t: Throwable) {
                logger.warn("Listener dispatch failed", t)
            }
        }
    }

    private fun buildWallpaperVisualSnapshotReference(
            context: Context,
            visible: Boolean?,
            width: Int,
            height: Int
    ): String {
        val prefs = context.getSharedPreferences(WallpaperComm.PREF_WALLPAPER, Context.MODE_PRIVATE)
        val chatPrefs =
                context.getSharedPreferences(ChatPreferenceKeys.PREFS_NAME, Context.MODE_PRIVATE)
        val fingerprint =
                listOf(
                                "android_wallpaper",
                                chatPrefs.getString(ChatPreferenceKeys.SELECTED_MODEL_FOLDER, null)
                                        .orEmpty(),
                                prefs.getString(WallpaperComm.PREF_WALLPAPER_BG_PATH, null)
                                        .orEmpty(),
                                visible?.toString().orEmpty(),
                                width.toString(),
                                height.toString()
                        )
                        .joinToString("|")
                        .hashCode()
        return "android_wallpaper:${Integer.toHexString(fingerprint)}"
    }

    private suspend fun ensureConnection(client: ChatServiceClient): Boolean {
        return when (
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeState = client.runtimeState.value
                )
        ) {
            WallpaperChatConnectionPolicy.Action.READY -> true
            WallpaperChatConnectionPolicy.Action.WAIT_FOR_READY -> waitForConnection(client)
            WallpaperChatConnectionPolicy.Action.START_LOCAL_RUNTIME -> {
                client.startLocalRuntime()
                waitForConnection(client)
            }
        }
    }

    private suspend fun waitForConnection(client: ChatServiceClient): Boolean {
        if (client.runtimeState.value == ChatServiceClient.RuntimeState.RUNNING) {
            return true
        }
        return withTimeoutOrNull(CONNECTION_TIMEOUT_MS) {
            client.runtimeState
                    .filter { it == ChatServiceClient.RuntimeState.RUNNING }
                    .first()
            true
        }
                ?: false
    }

    private fun formatPreview(label: String, content: String): String {
        val cleaned = content.trim().replace("\n", " ")
        if (cleaned.isEmpty()) return label
        val previewContent =
                if (cleaned.length > PREVIEW_MAX_CHARS) {
                    cleaned.substring(0, min(PREVIEW_MAX_CHARS, cleaned.length)).trimEnd() + "…"
                } else {
                    cleaned
                }
        return "$label: $previewContent"
    }
}
