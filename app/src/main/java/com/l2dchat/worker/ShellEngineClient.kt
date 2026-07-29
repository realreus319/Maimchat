package com.l2dchat.worker

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.Handler
import java.io.File
import android.os.HandlerThread
import android.os.IBinder
import android.os.Message
import android.os.Messenger
import android.util.Log
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.l2dchat.browser.BrowserOverlayHost
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Runtime LLM creds injected into the worker over IPC (never baked into the engine pkg). */
data class WorkerCreds(
        val apiKey: String,
        val baseUrl: String,
        val model: String,
        /** Claude-Code-format settings.json shipped to the worker (mcpServers/agent/env/…). */
        val settingsJson: String? = null,
)

/**
 * Binds the headless engine package's [ShellService] (cross-package, signature-permission protected)
 * and runs a single agent task, streaming progress and returning the worker's final output. One bind
 * per task — simple and robust for the planner's tool use.
 */
class ShellEngineClient(
    private val context: Context,
    /** Invoked (on a background thread) when the worker submits a file via the `submit_file` tool. */
    private val onFileSubmit: ((FileSubmission) -> Unit)? = null,
) {

    private val TAG = "ShellEngineClient"

    data class Outcome(val exitCode: Int, val text: String, val steps: List<String> = emptyList())

    /** Tell the engine we've taken the result for [taskId], so it drops its durable buffer copy. */
    private fun ackResult(svc: Messenger?, taskId: String) {
        svc ?: return
        runCatching {
            svc.send(Message.obtain(null, ShellEngineProtocol.MSG_RESULT_ACK).apply {
                data = Bundle().apply { putString(ShellEngineProtocol.KEY_TASK_ID, taskId) }
            })
        }.onFailure { Log.w(TAG, "ackResult failed: ${it.message}") }
    }

    suspend fun runTask(
        task: String,
        creds: WorkerCreds,
        timeoutMs: Long = 300_000,
        // Origin of the delegating turn — echoed through the engine's durable buffer so a result that
        // completes after this process is killed can be recovered and replayed into the right chat.
        originContext: String? = null,
        originAgent: String? = null,
        originMessage: String? = null,
        onProgress: (kind: String, text: String) -> Unit = { _, _ -> }
    ): Outcome = withContext(Dispatchers.IO) {
        val taskId = UUID.randomUUID().toString()
        suspendCancellableCoroutine { cont ->
            val ht = HandlerThread("worker-client").apply { start() }
            val resumed = AtomicBoolean(false)
            // Worker liveness (B): the engine pings via MSG_PROGRESS every ~8s while a task runs. If the
            // pings stop for too long the engine is frozen or dead, so fail fast instead of hanging on
            // the IPC forever (the per-task timeout lives in the engine, which freezes WITH it).
            val lastProgressMillis = java.util.concurrent.atomic.AtomicLong(System.currentTimeMillis())
            val workerSilenceTimeoutMs = 35_000L
            val watchdogIntervalMs = 10_000L
            var conn: ServiceConnection? = null
            // Reverse browser bridge: independent scope so MSG_BROWSER_ACTION handling never blocks the
            // IPC handler thread (BrowserController work hops to the main thread internally).
            val bridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
            // The bound service's Messenger, captured so the bridge can send MSG_BROWSER_RESULT back.
            val serviceMessenger = java.util.concurrent.atomic.AtomicReference<Messenger?>(null)

            fun cleanup() {
                runCatching { bridgeScope.cancel() }
                runCatching { conn?.let { context.unbindService(it) } }
                ht.quitSafely()
            }

            val handler = Handler(ht.looper) { msg ->
                when (msg.what) {
                    ShellEngineProtocol.MSG_PROGRESS -> {
                        lastProgressMillis.set(System.currentTimeMillis()) // includes "heartbeat" pings
                        val kind = msg.data.getString(ShellEngineProtocol.KEY_KIND) ?: "status"
                        val text = msg.data.getString(ShellEngineProtocol.KEY_LINE).orEmpty()
                        if (text.isNotEmpty()) onProgress(kind, text)
                    }
                    ShellEngineProtocol.MSG_BROWSER_ACTION -> {
                        val token = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_TOKEN)
                        val bTaskId = msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) ?: taskId
                        val name = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_NAME).orEmpty()
                        val argsJson = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_ARGS).orEmpty()
                        val svc = serviceMessenger.get()
                        if (token != null && svc != null) {
                            bridgeScope.launch { handleBrowserAction(svc, bTaskId, token, name, argsJson) }
                        }
                    }
                    // Only accept the result for OUR task: the engine flushes buffered results (from a
                    // prior killed task) to every registrant, so a non-matching taskId here belongs to
                    // the recovery path, not this coroutine — ignore it.
                    ShellEngineProtocol.MSG_RESULT ->
                        if (msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) == taskId &&
                            resumed.compareAndSet(false, true)) {
                            ackResult(serviceMessenger.get(), taskId) // drop the durable buffer copy
                            val outcome = Outcome(
                                msg.data.getInt(ShellEngineProtocol.KEY_EXIT),
                                msg.data.getString(ShellEngineProtocol.KEY_TEXT).orEmpty(),
                                msg.data.getStringArrayList(ShellEngineProtocol.KEY_STEPS) ?: emptyList()
                            )
                            cleanup(); cont.resume(outcome)
                        }
                    ShellEngineProtocol.MSG_ERROR ->
                        if (msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) == taskId &&
                            resumed.compareAndSet(false, true)) {
                            ackResult(serviceMessenger.get(), taskId)
                            val err = msg.data.getString(ShellEngineProtocol.KEY_ERROR) ?: "worker error"
                            cleanup(); cont.resumeWithException(RuntimeException(err))
                        }
                }
                true
            }
            val incoming = Messenger(handler)

            conn = object : ServiceConnection {
                override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                    val svc = Messenger(binder)
                    serviceMessenger.set(svc)
                    fun send(what: Int, data: Bundle) =
                        svc.send(Message.obtain(null, what).apply { this.data = data; replyTo = incoming })
                    runCatching {
                        send(ShellEngineProtocol.MSG_REGISTER, Bundle())
                        send(ShellEngineProtocol.MSG_RUN_TASK, Bundle().apply {
                            putString(ShellEngineProtocol.KEY_TASK_ID, taskId)
                            putString(ShellEngineProtocol.KEY_TASK, task)
                            putString(ShellEngineProtocol.KEY_API_KEY, creds.apiKey)
                            putString(ShellEngineProtocol.KEY_BASE_URL, creds.baseUrl)
                            putString(ShellEngineProtocol.KEY_MODEL, creds.model)
                            creds.settingsJson?.let {
                                putString(ShellEngineProtocol.KEY_SETTINGS_JSON, it)
                            }
                            putLong(ShellEngineProtocol.KEY_TIMEOUT_MS, timeoutMs)
                            originContext?.let { putString(ShellEngineProtocol.KEY_ORIGIN_CONTEXT, it) }
                            originAgent?.let { putString(ShellEngineProtocol.KEY_ORIGIN_AGENT, it) }
                            originMessage?.let { putString(ShellEngineProtocol.KEY_ORIGIN_MESSAGE, it) }
                        })
                    }.onFailure {
                        if (resumed.compareAndSet(false, true)) { cleanup(); cont.resumeWithException(it) }
                    }
                }

                override fun onServiceDisconnected(name: ComponentName?) {
                    if (resumed.compareAndSet(false, true)) {
                        cleanup(); cont.resumeWithException(RuntimeException("engine disconnected"))
                    }
                }
            }

            val intent = Intent()
                .setComponent(ComponentName(ShellEngineProtocol.ENGINE_PACKAGE, ShellEngineProtocol.SERVICE_CLASS))
                .setPackage(ShellEngineProtocol.ENGINE_PACKAGE)
            // Start the engine as a foreground service (not merely bind it) so it survives THIS process
            // (:chat) being reaped mid-task: the worker then runs to completion and its result is
            // durably buffered for recovery. Best-effort — background-start limits (Android 12+) can
            // reject it, in which case we still bind and degrade to the old bound-only behaviour.
            runCatching { context.startForegroundService(intent) }
                .onFailure { Log.w(TAG, "startForegroundService rejected (bg start limit?): ${it.message}") }
            val bound = runCatching { context.bindService(intent, conn!!, Context.BIND_AUTO_CREATE) }
                .getOrDefault(false)
            if (!bound && resumed.compareAndSet(false, true)) {
                Log.e(TAG, "bindService failed (engine installed? signature ok?)")
                cleanup()
                cont.resumeWithException(RuntimeException("worker engine not available (not installed?)"))
            }
            if (bound) {
                // Re-arming staleness check on the IPC handler thread (frozen WITH the app if ColorOS
                // freezes :chat, so it never false-fires during a freeze — only when the ENGINE goes
                // silent while we're alive).
                val watchdog =
                        object : Runnable {
                            override fun run() {
                                if (resumed.get()) return
                                if (System.currentTimeMillis() - lastProgressMillis.get() >
                                        workerSilenceTimeoutMs) {
                                    if (resumed.compareAndSet(false, true)) {
                                        Log.w(
                                                TAG,
                                                "worker unresponsive: no heartbeat for >${workerSilenceTimeoutMs}ms (engine frozen/dead)"
                                        )
                                        cleanup()
                                        cont.resumeWithException(
                                                RuntimeException("worker unresponsive (engine frozen or dead)")
                                        )
                                    }
                                } else {
                                    handler.postDelayed(this, watchdogIntervalMs)
                                }
                            }
                        }
                handler.postDelayed(watchdog, watchdogIntervalMs)
            }
            cont.invokeOnCancellation { if (resumed.compareAndSet(false, true)) cleanup() }
        }
    }

    /** Handle to a live persistent worker session (#5) — inject follow-up messages or close it. */
    inner class SessionHandle internal constructor(
        private val svcRef: java.util.concurrent.atomic.AtomicReference<Messenger?>,
        val taskId: String,
        private val cleanup: () -> Unit,
    ) {
        @Volatile private var closed = false
        val isClosed: Boolean get() = closed
        fun inject(text: String) {
            svcRef.get()?.let { svc ->
                runCatching {
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_INJECT).apply {
                        data = Bundle().apply {
                            putString(ShellEngineProtocol.KEY_TASK_ID, taskId)
                            putString(ShellEngineProtocol.KEY_TEXT, text)
                        }
                    })
                }.onFailure { Log.w(TAG, "inject failed: ${it.message}") }
            }
        }
        fun close() {
            if (closed) return
            closed = true
            svcRef.get()?.let { svc ->
                runCatching {
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_CLOSE_SESSION).apply {
                        data = Bundle().apply { putString(ShellEngineProtocol.KEY_TASK_ID, taskId) }
                    })
                }
            }
            cleanup()
        }
    }

    /**
     * Start a PERSISTENT injectable worker session. Stays bound; [onTurnResult] fires for EACH turn's
     * result (initial task + injected follow-ups); [onClosed] fires when the session/binding ends.
     * Returns a [SessionHandle] for inject/close, or null if the bind fails.
     */
    fun runSession(
        task: String,
        creds: WorkerCreds,
        originContext: String? = null,
        originAgent: String? = null,
        originMessage: String? = null,
        onProgress: (kind: String, text: String) -> Unit = { _, _ -> },
        onTurnResult: (Outcome) -> Unit,
        onClosed: () -> Unit = {},
    ): SessionHandle? {
        val taskId = UUID.randomUUID().toString()
        val ht = HandlerThread("worker-session").apply { start() }
        val svcRef = java.util.concurrent.atomic.AtomicReference<Messenger?>(null)
        var conn: ServiceConnection? = null
        val bridgeScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        val closedOnce = AtomicBoolean(false)
        fun cleanup() {
            if (!closedOnce.compareAndSet(false, true)) return
            runCatching { bridgeScope.cancel() }
            runCatching { conn?.let { context.unbindService(it) } }
            ht.quitSafely()
            runCatching { onClosed() }
        }
        val incoming = Messenger(Handler(ht.looper) { msg ->
            when (msg.what) {
                ShellEngineProtocol.MSG_PROGRESS -> {
                    val kind = msg.data.getString(ShellEngineProtocol.KEY_KIND) ?: "status"
                    val text = msg.data.getString(ShellEngineProtocol.KEY_LINE).orEmpty()
                    if (text.isNotEmpty()) onProgress(kind, text)
                }
                ShellEngineProtocol.MSG_BROWSER_ACTION -> {
                    val token = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_TOKEN)
                    val bTaskId = msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) ?: taskId
                    val name = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_NAME).orEmpty()
                    val argsJson = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_ARGS).orEmpty()
                    val svc = svcRef.get()
                    if (token != null && svc != null) {
                        bridgeScope.launch { handleBrowserAction(svc, bTaskId, token, name, argsJson) }
                    }
                }
                ShellEngineProtocol.MSG_RESULT ->
                    if (msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) == taskId) {
                        ackResult(svcRef.get(), taskId)
                        onTurnResult(Outcome(
                            msg.data.getInt(ShellEngineProtocol.KEY_EXIT),
                            msg.data.getString(ShellEngineProtocol.KEY_TEXT).orEmpty(),
                            msg.data.getStringArrayList(ShellEngineProtocol.KEY_STEPS) ?: emptyList()))
                    }
                ShellEngineProtocol.MSG_ERROR ->
                    if (msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) == taskId) {
                        ackResult(svcRef.get(), taskId)
                        onTurnResult(Outcome(-1, msg.data.getString(ShellEngineProtocol.KEY_ERROR) ?: "worker error", emptyList()))
                    }
                ShellEngineProtocol.MSG_SESSION_ENDED ->
                    if (msg.data.getString(ShellEngineProtocol.KEY_TASK_ID) == taskId) {
                        // Engine says the session is fully over — unbind and fire onClosed so the caller
                        // (runOne) stops awaiting and releases its concurrency permit (no leak).
                        cleanup()
                    }
            }
            true
        })
        conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                val svc = Messenger(binder); svcRef.set(svc)
                runCatching {
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_REGISTER).apply { replyTo = incoming })
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_RUN_SESSION).apply {
                        replyTo = incoming
                        data = Bundle().apply {
                            putString(ShellEngineProtocol.KEY_TASK_ID, taskId)
                            putString(ShellEngineProtocol.KEY_TASK, task)
                            putString(ShellEngineProtocol.KEY_API_KEY, creds.apiKey)
                            putString(ShellEngineProtocol.KEY_BASE_URL, creds.baseUrl)
                            putString(ShellEngineProtocol.KEY_MODEL, creds.model)
                            creds.settingsJson?.let { putString(ShellEngineProtocol.KEY_SETTINGS_JSON, it) }
                            originContext?.let { putString(ShellEngineProtocol.KEY_ORIGIN_CONTEXT, it) }
                            originAgent?.let { putString(ShellEngineProtocol.KEY_ORIGIN_AGENT, it) }
                            originMessage?.let { putString(ShellEngineProtocol.KEY_ORIGIN_MESSAGE, it) }
                        }
                    })
                }.onFailure { cleanup() }
            }
            override fun onServiceDisconnected(name: ComponentName?) { cleanup() }
        }
        val intent = Intent()
            .setComponent(ComponentName(ShellEngineProtocol.ENGINE_PACKAGE, ShellEngineProtocol.SERVICE_CLASS))
            .setPackage(ShellEngineProtocol.ENGINE_PACKAGE)
        runCatching { context.startForegroundService(intent) }
            .onFailure { Log.w(TAG, "session startForegroundService rejected: ${it.message}") }
        val bound = runCatching { context.bindService(intent, conn!!, Context.BIND_AUTO_CREATE) }.getOrDefault(false)
        if (!bound) { cleanup(); return null }
        return SessionHandle(svcRef, taskId, ::cleanup)
    }

    /**
     * Pre-warm the engine: bind it and trigger the proot rootfs extraction so the first real worker
     * task isn't slowed by a cold extraction. Idempotent + near-instant once the rootfs already
     * exists. (The engine must be allowed to start — on aggressive ROMs like ColorOS that means the
     * user has whitelisted the engine package in 自启动/关联启动 management.) Best-effort: returns true
     * iff the engine reported ready within [timeoutMs]; never throws.
     */
    suspend fun warmUp(timeoutMs: Long = 120_000): Boolean =
        kotlinx.coroutines.withTimeoutOrNull(timeoutMs) {
            withContext(Dispatchers.IO) {
                suspendCancellableCoroutine<Boolean> { cont ->
                    val ht = HandlerThread("worker-warmup").apply { start() }
                    val resumed = AtomicBoolean(false)
                    var conn: ServiceConnection? = null
                    fun cleanup() {
                        runCatching { conn?.let { context.unbindService(it) } }
                        ht.quitSafely()
                    }
                    fun finish(ok: Boolean) {
                        if (resumed.compareAndSet(false, true)) { cleanup(); cont.resume(ok) }
                    }
                    val incoming = Messenger(Handler(ht.looper) { msg ->
                        when (msg.what) {
                            ShellEngineProtocol.MSG_READY -> finish(true)
                            ShellEngineProtocol.MSG_ERROR -> finish(false)
                        }
                        true
                    })
                    conn = object : ServiceConnection {
                        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                            runCatching {
                                Messenger(binder).send(
                                    Message.obtain(null, ShellEngineProtocol.MSG_WARMUP).apply {
                                        replyTo = incoming
                                    }
                                )
                            }.onFailure { finish(false) }
                        }
                        override fun onServiceDisconnected(name: ComponentName?) = finish(false)
                    }
                    val intent = Intent()
                        .setComponent(ComponentName(ShellEngineProtocol.ENGINE_PACKAGE, ShellEngineProtocol.SERVICE_CLASS))
                        .setPackage(ShellEngineProtocol.ENGINE_PACKAGE)
                    val bound = runCatching { context.bindService(intent, conn!!, Context.BIND_AUTO_CREATE) }
                        .getOrDefault(false)
                    if (!bound) { Log.w(TAG, "warmUp bindService failed"); finish(false) }
                    cont.invokeOnCancellation { if (resumed.compareAndSet(false, true)) cleanup() }
                }
            }
        } ?: false

    /**
     * Reverse-bridge handler (app side): ensure the [BrowserOverlayHost] window is up, run the
     * Playwright-MCP tool against its [com.l2dchat.browser.BrowserController], and send the result
     * back to the engine over [svc] as [ShellEngineProtocol.MSG_BROWSER_RESULT]. Runs on the main
     * dispatcher (BrowserController hops to the UI thread for WebView work itself).
     *
     * Uses a WindowManager overlay rather than launching an Activity so the host comes up even when
     * the main app has no foreground Activity (wallpaper / widget chat paths), where background
     * `startActivity` would be blocked on Android 10+.
     */
    private suspend fun handleBrowserAction(svc: Messenger, taskId: String, token: String, name: String, argsJson: String) {
        val (text, isError) = if (name == "submit_file") {
            handleSubmitFile(argsJson)
        } else if (!BrowserOverlayHost.isOverlayGranted(context)) {
            // No overlay grant -> the host can never be shown. Return a clear, actionable error so the
            // worker fails fast instead of hanging until the bridge sub-timeout.
            "browser overlay permission not granted: enable 'Draw over other apps' for com.l2dchat" to true
        } else try {
            // Serialize (make-this-task's-context-current + execute) so concurrent workers each drive
            // their OWN isolated BrowserTabs without racing the single overlay holder (Option C).
            BrowserOverlayHost.browserMutex.withLock {
                val tabs = BrowserOverlayHost.ensureShown(context, taskId)
                val args: JsonObject = if (argsJson.isBlank()) JsonObject()
                else runCatching { JsonParser.parseString(argsJson).asJsonObject }.getOrDefault(JsonObject())
                val r = tabs.execute(name, args)
                r.text to r.isError
            }
        } catch (t: Throwable) {
            Log.w(TAG, "browser action '$name' failed", t)
            ("browser action '$name' failed: ${t.message}") to true
        }
        runCatching {
            svc.send(Message.obtain(null, ShellEngineProtocol.MSG_BROWSER_RESULT).apply {
                data = Bundle().apply {
                    putString(ShellEngineProtocol.KEY_BROWSER_TOKEN, token)
                    putString(ShellEngineProtocol.KEY_BROWSER_RESULT_TEXT, text)
                    putBoolean(ShellEngineProtocol.KEY_BROWSER_IS_ERROR, isError)
                }
            })
        }.onFailure { Log.w(TAG, "failed to send browser result: ${it.message}") }
    }

    /**
     * submit_file: the engine exposed the worker's rootfs file via a content:// URI (temporary read
     * grant). STREAM it into the app's `agent_files/` (no size limit) and notify [onFileSubmit] so it
     * surfaces as a chat attachment. Returns (replyText, isError) for the worker.
     */
    private fun handleSubmitFile(argsJson: String): Pair<String, Boolean> =
        try {
            val args =
                if (argsJson.isBlank()) JsonObject()
                else JsonParser.parseString(argsJson).asJsonObject
            fun str(k: String) =
                args.get(k)?.takeIf { it.isJsonPrimitive }?.asString?.takeIf { it.isNotBlank() }
            val filename = str("filename") ?: "file"
            val uriStr = str("content_uri")
            if (uriStr == null) {
                "submit_file error: no file uri received" to true
            } else {
                val description = str("description")
                val uri = android.net.Uri.parse(uriStr)
                val dir = File(context.filesDir, "agent_files").apply { mkdirs() }
                val safe = filename.replace(Regex("[^A-Za-z0-9._-]"), "_").takeLast(80).ifBlank { "file" }
                val out = File(dir, "${System.currentTimeMillis()}_$safe")
                val copied =
                    context.contentResolver.openInputStream(uri)?.use { input ->
                        out.outputStream().use { output -> input.copyTo(output) }
                    } ?: throw java.io.IOException("cannot open submitted file uri")
                val mime =
                    android.webkit.MimeTypeMap.getSingleton()
                        .getMimeTypeFromExtension(filename.substringAfterLast('.', "").lowercase())
                onFileSubmit?.invoke(
                    FileSubmission(
                        savedPath = out.absolutePath,
                        filename = filename,
                        mime = mime,
                        size = copied,
                        description = description,
                    )
                )
                "file delivered to the user's chat: $filename ($copied bytes)" to false
            }
        } catch (t: Throwable) {
            Log.w(TAG, "submit_file failed", t)
            "submit_file failed: ${t.message}" to true
        }
}

/** A file the worker submitted, after the app saved it locally. */
data class FileSubmission(
    val savedPath: String,
    val filename: String,
    val mime: String?,
    val size: Long,
    val description: String?,
)
