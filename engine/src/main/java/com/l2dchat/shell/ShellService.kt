package com.l2dchat.shell

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Message
import android.os.Messenger
import android.os.Process
import android.os.RemoteException
import android.util.Log
import java.io.File
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Headless worker engine service. No launcher activity — the main app binds here (protected by a
 * signature-level permission) and asks it to run agent tasks. The actual Linux/python work is done
 * by [WorkerRuntime] under proot. Output is streamed back to the bound client over Messenger.
 */
class ShellService : Service() {

    private val TAG = "ShellService"
    private lateinit var runtime: WorkerRuntime
    // Up to 3 workers run in parallel (matches the app's BackgroundWorkerManager concurrency gate);
    // each gets its OWN BrowserMcpServer + reverse-bridge routing so concurrent browser use can't
    // cross-talk between tasks.
    private val pool = Executors.newFixedThreadPool(3)
    // Liveness heartbeat: while a task runs, ping the bound client every few seconds on a SEPARATE
    // thread so the app can tell "engine alive" from "engine frozen/dead" (no ping ⇒ frozen). If this
    // engine process is frozen by the ROM, this scheduler freezes with it and the pings stop.
    private val heartbeatScheduler = Executors.newSingleThreadScheduledExecutor()
    private val heartbeatIntervalMs = 8_000L
    private val jobs = ConcurrentHashMap<String, Future<*>>()
    // Live persistent sessions (#5) by taskId — targets of MSG_INJECT / MSG_CLOSE_SESSION.
    private val sessions = ConcurrentHashMap<String, WorkerRuntime.LiveSession>()

    // Durable result buffer. A worker task ALWAYS writes its result here on completion (success OR
    // error), so an app-process death (OS reap while backgrounded) can't lose it. The result is then
    // delivered to whatever client is bound; cleared on MSG_RESULT_ACK; and re-flushed to any client
    // that registers later (app relaunch) — see flushPendingResults / saveResult / ackResult below.
    private val pendingDir by lazy { File(filesDir, "pending").apply { mkdirs() } }

    private lateinit var handlerThread: HandlerThread
    private lateinit var incoming: Messenger

    // --- Playwright-MCP-compatible server + reverse browser bridge ---
    private lateinit var mcpServer: BrowserMcpServer
    private var mcpPort: Int = 0

    // The main app package — grantee of the temporary read URI for worker-submitted files.
    private val APP_PACKAGE = "com.l2dchat"

    /** The replyTo Messenger of the currently running task — the reverse bridge targets it. */
    @Volatile
    private var activeClient: Messenger? = null

    /**
     * token -> single-slot result queue. The bridge thread blocks on poll(); IncomingHandler offers
     * the result. A buffering (LinkedBlocking) queue — not SynchronousQueue — so a result that lands
     * a hair before the bridge thread reaches poll() is retained rather than dropped.
     */
    private val pendingBrowser = ConcurrentHashMap<String, LinkedBlockingQueue<BrowserMcpServer.BridgeResult>>()

    override fun onCreate() {
        super.onCreate()
        runtime = WorkerRuntime(this)
        handlerThread = HandlerThread("shell-ipc").also { it.start() }
        incoming = Messenger(IncomingHandler(handlerThread))
        // Shared server only for the standalone debug MCP client (MSG_GET_MCP_PORT). Real tasks each
        // spin up their OWN per-task BrowserMcpServer in startTask (routes to that task's client).
        mcpServer = BrowserMcpServer(bridge = { name, args -> browserBridgeCall(activeClient, "debug", name, args) }).also { it.start() }
        mcpPort = mcpServer.port
        Log.i(TAG, "ShellService created (pid=${Process.myPid()}) mcpPort=$mcpPort")
    }

    override fun onBind(intent: Intent?): IBinder = incoming.binder

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // The client calls startForegroundService(this) before binding so the engine is a STARTED
        // service, not merely bound. That's what lets it survive the client (:chat) being reaped
        // mid-task and run the worker to completion. startForegroundService demands a prompt
        // startForeground — satisfy it now; stopIfIdle() tears down once no task is running.
        startForegroundCompat()
        return START_NOT_STICKY
    }

    override fun onUnbind(intent: Intent?): Boolean {
        // Never cancel jobs on unbind — the client may have died mid-task. Keep running; the result is
        // buffered to disk and re-flushed when a client registers again. Tear down only if idle.
        stopIfIdle()
        return true // allow onRebind on app relaunch
    }

    override fun onDestroy() {
        pool.shutdownNow()
        heartbeatScheduler.shutdownNow()
        runCatching { mcpServer.stop() }
        handlerThread.quitSafely()
        super.onDestroy()
    }

    private inner class IncomingHandler(t: HandlerThread) : Handler(t.looper) {
        override fun handleMessage(msg: Message) {
            when (msg.what) {
                ShellProtocol.MSG_REGISTER -> {
                    // Target the registering client for the reverse browser bridge. In production a
                    // register is always followed by a run-task with the same client; the standalone
                    // MCP debug client registers but never runs a task, so this is what wires it up.
                    activeClient = msg.replyTo
                    reply(msg.replyTo, ShellProtocol.MSG_READY, Bundle())
                    // Deliver any result that completed while the app was dead/backgrounded (recovery).
                    flushPendingResults(msg.replyTo)
                }
                ShellProtocol.MSG_RESULT_ACK -> {
                    ackResult(msg.data.getString(ShellProtocol.KEY_TASK_ID))
                    stopIfIdle()
                }
                ShellProtocol.MSG_RUN_TASK -> startTask(msg.data, msg.replyTo)
                ShellProtocol.MSG_RUN_SESSION -> startSessionTask(msg.data, msg.replyTo)
                ShellProtocol.MSG_INJECT -> {
                    val id = msg.data.getString(ShellProtocol.KEY_TASK_ID)
                    val text = msg.data.getString(ShellProtocol.KEY_TEXT).orEmpty()
                    sessions[id]?.inject(text)?.also { Log.i(TAG, "injected into session $id") }
                }
                ShellProtocol.MSG_CLOSE_SESSION ->
                    sessions[msg.data.getString(ShellProtocol.KEY_TASK_ID)]?.close()
                ShellProtocol.MSG_WARMUP -> warmUp(msg.replyTo)
                ShellProtocol.MSG_CANCEL -> jobs.remove(msg.data.getString(ShellProtocol.KEY_TASK_ID))?.cancel(true)
                ShellProtocol.MSG_GET_MCP_PORT -> reply(msg.replyTo, ShellProtocol.MSG_MCP_PORT, Bundle().apply {
                    putInt(ShellProtocol.KEY_MCP_PORT, mcpPort)
                })
                ShellProtocol.MSG_BROWSER_RESULT -> deliverBrowserResult(msg.data)
                else -> super.handleMessage(msg)
            }
        }
    }

    /**
     * Reverse-bridge entry: called (off the IPC thread) by [BrowserMcpServer] for each `tools/call`.
     * Pushes a [ShellProtocol.MSG_BROWSER_ACTION] to the active task's client and blocks until the app
     * sends back a [ShellProtocol.MSG_BROWSER_RESULT] for the same token, or times out.
     */
    private fun browserBridgeCall(client: Messenger?, taskId: String, name: String, argsJson: String): BrowserMcpServer.BridgeResult =
        if (name == "submit_file") submitFileBridgeCall(client, taskId, argsJson)
        else bridgeToApp(client, taskId, name, argsJson)

    /**
     * submit_file: the worker hands a file (a path in its proot env) to the user's chat. The app can't
     * reach engine-private storage, so the engine exposes the rootfs file via a content:// URI (File
     * provider) with a temporary read grant to the main app and forwards the URI — the app streams it,
     * with NO size limit (unlike base64-over-Binder).
     */
    private fun submitFileBridgeCall(client: Messenger?, taskId: String, argsJson: String): BrowserMcpServer.BridgeResult {
        val args = try { org.json.JSONObject(argsJson) } catch (_: Throwable) { org.json.JSONObject() }
        val path = args.optString("path").trim()
        if (path.isBlank()) {
            return BrowserMcpServer.BridgeResult("submit_file error: missing 'path'", isError = true)
        }
        val file = runtime.resolveGuestFile(path)
        if (file == null || !file.isFile) {
            return BrowserMcpServer.BridgeResult("submit_file error: file not found: $path", isError = true)
        }
        val uri = try {
            androidx.core.content.FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        } catch (t: Throwable) {
            return BrowserMcpServer.BridgeResult("submit_file error: cannot share file: ${t.message}", isError = true)
        }
        grantUriPermission(APP_PACKAGE, uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        val forward = org.json.JSONObject().apply {
            put("filename", args.optString("filename").ifBlank { file.name })
            put("description", args.optString("description"))
            put("size", file.length())
            put("content_uri", uri.toString())
        }
        val result = bridgeToApp(client, taskId, "submit_file", forward.toString())
        // The app read the URI synchronously within the bridge round-trip; the grant is no longer needed.
        runCatching { revokeUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        return result
    }

    /** The generic reverse-bridge round-trip: send MSG_BROWSER_ACTION to the app, block for the result. */
    private fun bridgeToApp(client: Messenger?, taskId: String, name: String, argsJson: String): BrowserMcpServer.BridgeResult {
        val c = client
            ?: return BrowserMcpServer.BridgeResult("bridge unavailable: no task client", isError = true)
        val token = UUID.randomUUID().toString()
        val queue = LinkedBlockingQueue<BrowserMcpServer.BridgeResult>(1)
        pendingBrowser[token] = queue
        try {
            c.send(Message.obtain(null, ShellProtocol.MSG_BROWSER_ACTION).apply {
                data = Bundle().apply {
                    putString(ShellProtocol.KEY_BROWSER_TOKEN, token)
                    // taskId routes the action to THIS worker's own browser context in the app.
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putString(ShellProtocol.KEY_BROWSER_NAME, name)
                    putString(ShellProtocol.KEY_BROWSER_ARGS, argsJson)
                }
            })
        } catch (e: RemoteException) {
            pendingBrowser.remove(token)
            return BrowserMcpServer.BridgeResult("browser bridge send failed: ${e.message}", isError = true)
        }
        val result = queue.poll(60, TimeUnit.SECONDS)
        pendingBrowser.remove(token)
        return result
            ?: BrowserMcpServer.BridgeResult("browser action '$name' timed out after 60s", isError = true)
    }

    private fun deliverBrowserResult(data: Bundle) {
        val token = data.getString(ShellProtocol.KEY_BROWSER_TOKEN) ?: return
        val text = data.getString(ShellProtocol.KEY_BROWSER_RESULT_TEXT).orEmpty()
        val isError = data.getBoolean(ShellProtocol.KEY_BROWSER_IS_ERROR, false)
        pendingBrowser[token]?.offer(BrowserMcpServer.BridgeResult(text, isError))
    }

    private fun startTask(data: Bundle, client: Messenger?) {
        val taskId = data.getString(ShellProtocol.KEY_TASK_ID) ?: return
        val task = data.getString(ShellProtocol.KEY_TASK).orEmpty()
        val creds = WorkerCreds(
            apiKey = data.getString(ShellProtocol.KEY_API_KEY).orEmpty(),
            baseUrl = data.getString(ShellProtocol.KEY_BASE_URL).orEmpty(),
            model = data.getString(ShellProtocol.KEY_MODEL).orEmpty(),
            settingsJson = data.getString(ShellProtocol.KEY_SETTINGS_JSON)
        )
        val timeout = data.getLong(ShellProtocol.KEY_TIMEOUT_MS, 300_000L)
        val originContext = data.getString(ShellProtocol.KEY_ORIGIN_CONTEXT)
        val originAgent = data.getString(ShellProtocol.KEY_ORIGIN_AGENT)
        val originMessage = data.getString(ShellProtocol.KEY_ORIGIN_MESSAGE)
        val future = pool.submit {
            // Per-task browser MCP server: the worker's browser_* / submit_file tools route back to
            // THIS task's client + its own browser context, so parallel workers never cross-talk.
            val browserServer =
                    BrowserMcpServer(bridge = { name, args -> browserBridgeCall(client, taskId, name, args) })
                            .also { it.start() }
            val heartbeat =
                    heartbeatScheduler.scheduleWithFixedDelay(
                            { runCatching { progress(client, taskId, "heartbeat", "") } },
                            heartbeatIntervalMs,
                            heartbeatIntervalMs,
                            TimeUnit.MILLISECONDS
                    )
            try {
                startForegroundCompat()
                runtime.ensureInstalled { line -> progress(client, taskId, "status", line) }
                val r = runtime.runTask(task, creds, timeout, browserServer.port) { kind, text -> progress(client, taskId, kind, text) }
                // Persist FIRST (durable), then deliver. If the app died while we ran, the result is on
                // disk for re-flush on its next register; the client acks (MSG_RESULT_ACK) to drop it.
                saveResult(taskId, r.exitCode, r.text, r.steps, null, originContext, originAgent, originMessage)
                reply(client, ShellProtocol.MSG_RESULT, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putInt(ShellProtocol.KEY_EXIT, r.exitCode)
                    putString(ShellProtocol.KEY_TEXT, r.text)
                    putStringArrayList(ShellProtocol.KEY_STEPS, ArrayList(r.steps))
                })
            } catch (t: Throwable) {
                Log.e(TAG, "task failed", t)
                saveResult(taskId, -1, "", emptyList(), t.message ?: "worker failed", originContext, originAgent, originMessage)
                reply(client, ShellProtocol.MSG_ERROR, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putString(ShellProtocol.KEY_ERROR, t.message ?: "worker failed")
                })
            } finally {
                heartbeat.cancel(false)
                runCatching { browserServer.stop() }
                jobs.remove(taskId)
                stopIfIdle()
            }
        }
        jobs[taskId] = future
    }

    /**
     * Persistent injectable session variant of [startTask] (#5): starts a long-lived worker whose
     * initial turn is [KEY_TASK], delivers EACH turn's result as an MSG_RESULT (persisted via the T8
     * buffer), stays alive for MSG_INJECT-driven follow-up turns, and closes after a grace-idle window.
     */
    private fun startSessionTask(data: Bundle, client: Messenger?) {
        val taskId = data.getString(ShellProtocol.KEY_TASK_ID) ?: return
        val task = data.getString(ShellProtocol.KEY_TASK).orEmpty()
        val creds = WorkerCreds(
            apiKey = data.getString(ShellProtocol.KEY_API_KEY).orEmpty(),
            baseUrl = data.getString(ShellProtocol.KEY_BASE_URL).orEmpty(),
            model = data.getString(ShellProtocol.KEY_MODEL).orEmpty(),
            settingsJson = data.getString(ShellProtocol.KEY_SETTINGS_JSON)
        )
        val timeout = data.getLong(ShellProtocol.KEY_TIMEOUT_MS, 600_000L)
        val originContext = data.getString(ShellProtocol.KEY_ORIGIN_CONTEXT)
        val originAgent = data.getString(ShellProtocol.KEY_ORIGIN_AGENT)
        val originMessage = data.getString(ShellProtocol.KEY_ORIGIN_MESSAGE)
        val future = pool.submit {
            val browserServer =
                BrowserMcpServer(bridge = { name, args -> browserBridgeCall(client, taskId, name, args) })
                    .also { it.start() }
            val heartbeat = heartbeatScheduler.scheduleWithFixedDelay(
                { runCatching { progress(client, taskId, "heartbeat", "") } },
                heartbeatIntervalMs, heartbeatIntervalMs, TimeUnit.MILLISECONDS)
            val firstTurnDone = java.util.concurrent.atomic.AtomicBoolean(false)
            try {
                startForegroundCompat()
                runtime.ensureInstalled { line -> progress(client, taskId, "status", line) }
                val session = runtime.startSession(
                    task, creds, browserServer.port,
                    onProgress = { kind, text -> progress(client, taskId, kind, text) }
                ) { r ->
                    firstTurnDone.set(true)
                    saveResult(taskId, r.exitCode, r.text, r.steps, null, originContext, originAgent, originMessage)
                    reply(client, ShellProtocol.MSG_RESULT, Bundle().apply {
                        putString(ShellProtocol.KEY_TASK_ID, taskId)
                        putInt(ShellProtocol.KEY_EXIT, r.exitCode)
                        putString(ShellProtocol.KEY_TEXT, r.text)
                        putStringArrayList(ShellProtocol.KEY_STEPS, ArrayList(r.steps))
                    })
                }
                sessions[taskId] = session
                val graceMs = 60_000L
                val hardEnd = System.currentTimeMillis() + timeout
                // Idle-close only AFTER the first turn completes, and only when the worker has produced
                // NO output for graceMs (session.lastActivityMs updates on every stdout line, so a turn
                // that's actively streaming — even for minutes — is never mistaken for idle).
                while (session.alive && System.currentTimeMillis() < hardEnd &&
                    (!firstTurnDone.get() ||
                        System.currentTimeMillis() - session.lastActivityMs < graceMs)) {
                    Thread.sleep(250)
                }
                runCatching { session.close() }; Thread.sleep(600); runCatching { session.kill() }
            } catch (t: Throwable) {
                Log.e(TAG, "session task failed", t)
                saveResult(taskId, -1, "", emptyList(), t.message ?: "worker failed", originContext, originAgent, originMessage)
                reply(client, ShellProtocol.MSG_ERROR, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putString(ShellProtocol.KEY_ERROR, t.message ?: "worker failed")
                })
            } finally {
                heartbeat.cancel(false)
                runCatching { browserServer.stop() }
                sessions.remove(taskId)
                jobs.remove(taskId)
                // Tell the client the session is fully over so it can unbind — otherwise the client's
                // runSession stays bound forever, its onClosed never fires, and the caller's permit leaks.
                reply(client, ShellProtocol.MSG_SESSION_ENDED, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                })
                stopIfIdle()
            }
        }
        jobs[taskId] = future
    }

    /**
     * Pre-warm: extract the proot rootfs NOW (as a foreground service, while the app that bound us is
     * itself in the foreground), then reply [ShellProtocol.MSG_READY]. This makes the first real
     * `run_task` skip the cold extraction that some aggressive ROMs (e.g. ColorOS) kill when it runs
     * lazily in the background. Idempotent + near-instant once the rootfs already exists.
     */
    private fun warmUp(client: Messenger?) {
        pool.submit {
            try {
                startForegroundCompat()
                runtime.ensureInstalled { /* no per-line progress needed for a warm-up */ }
                reply(client, ShellProtocol.MSG_READY, Bundle())
            } catch (t: Throwable) {
                Log.e(TAG, "warmup failed", t)
                reply(client, ShellProtocol.MSG_ERROR, Bundle().apply {
                    putString(ShellProtocol.KEY_ERROR, t.message ?: "warmup failed")
                })
            } finally {
                if (jobs.isEmpty()) stopForegroundCompat()
            }
        }
    }

    private fun progress(client: Messenger?, taskId: String, kind: String, text: String) {
        reply(client, ShellProtocol.MSG_PROGRESS, Bundle().apply {
            putString(ShellProtocol.KEY_TASK_ID, taskId)
            putString(ShellProtocol.KEY_KIND, kind)
            putString(ShellProtocol.KEY_LINE, text)
        })
    }

    private fun reply(client: Messenger?, what: Int, data: Bundle) {
        client ?: return
        try {
            client.send(Message.obtain(null, what).apply { this.data = data })
        } catch (e: RemoteException) {
            Log.w(TAG, "client gone")
        }
    }

    // --- durable result buffer (survives :chat / app process death) ---
    private fun saveResult(
        taskId: String, exit: Int, text: String, steps: List<String>, error: String?,
        originContext: String?, originAgent: String?, originMessage: String?
    ) {
        runCatching {
            val json = org.json.JSONObject()
                .put("taskId", taskId)
                .put("exit", exit)
                .put("text", text)
                .put("error", error ?: org.json.JSONObject.NULL)
                .put("steps", org.json.JSONArray(steps))
                .put("originContext", originContext ?: org.json.JSONObject.NULL)
                .put("originAgent", originAgent ?: org.json.JSONObject.NULL)
                .put("originMessage", originMessage ?: org.json.JSONObject.NULL)
            File(pendingDir, "$taskId.json").writeText(json.toString())
        }.onFailure { Log.w(TAG, "saveResult failed: ${it.message}") }
    }

    private fun ackResult(taskId: String?) {
        taskId ?: return
        File(pendingDir, "$taskId.json").delete()
        Log.i(TAG, "result acked + dropped: $taskId")
    }

    /** Re-deliver every buffered (un-acked) result to [client]. Called when a client registers. */
    private fun flushPendingResults(client: Messenger?) {
        client ?: return
        val files = pendingDir.listFiles { f -> f.name.endsWith(".json") } ?: return
        for (f in files) {
            val json = runCatching { org.json.JSONObject(f.readText()) }.getOrNull() ?: continue
            val taskId = json.optString("taskId")
            fun Bundle.putOrigin() {
                if (!json.isNull("originContext")) putString(ShellProtocol.KEY_ORIGIN_CONTEXT, json.optString("originContext"))
                if (!json.isNull("originAgent")) putString(ShellProtocol.KEY_ORIGIN_AGENT, json.optString("originAgent"))
                if (!json.isNull("originMessage")) putString(ShellProtocol.KEY_ORIGIN_MESSAGE, json.optString("originMessage"))
            }
            val err = if (json.isNull("error")) null else json.optString("error")
            if (err != null) {
                reply(client, ShellProtocol.MSG_ERROR, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putString(ShellProtocol.KEY_ERROR, err)
                    putOrigin()
                })
            } else {
                val steps = ArrayList<String>()
                json.optJSONArray("steps")?.let { for (i in 0 until it.length()) steps.add(it.optString(i)) }
                reply(client, ShellProtocol.MSG_RESULT, Bundle().apply {
                    putString(ShellProtocol.KEY_TASK_ID, taskId)
                    putInt(ShellProtocol.KEY_EXIT, json.optInt("exit"))
                    putString(ShellProtocol.KEY_TEXT, json.optString("text"))
                    putStringArrayList(ShellProtocol.KEY_STEPS, steps)
                    putOrigin()
                })
            }
            Log.i(TAG, "flushed buffered result to re-registered client: $taskId")
        }
    }

    /** Tear down only when no task is running — buffered results live on disk, so it's safe to stop. */
    private fun stopIfIdle() {
        if (jobs.isEmpty()) {
            stopForegroundCompat()
            stopSelf()
        }
    }

    // --- foreground service (long worker runs must not be killed) ---
    private fun startForegroundCompat() {
        val chanId = "worker"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(chanId) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(chanId, "Worker", NotificationManager.IMPORTANCE_LOW)
                )
            }
        }
        val n: Notification =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                Notification.Builder(this, chanId)
                    .setContentTitle("本地 worker 运行中")
                    .setSmallIcon(android.R.drawable.stat_notify_sync)
                    .build()
            else
                @Suppress("DEPRECATION")
                Notification.Builder(this)
                    .setContentTitle("本地 worker 运行中")
                    .setSmallIcon(android.R.drawable.stat_notify_sync)
                    .build()
        startForeground(1, n)
    }

    private fun stopForegroundCompat() {
        @Suppress("DEPRECATION")
        stopForeground(true)
    }
}
