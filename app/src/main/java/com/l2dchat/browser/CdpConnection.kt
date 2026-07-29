package com.l2dchat.browser

import android.net.LocalSocket
import android.net.LocalSocketAddress
import android.util.Log
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * In-process CDP (Chrome DevTools Protocol) client for the app's own WebView.
 *
 * Chromium, once [android.webkit.WebView.setWebContentsDebuggingEnabled] is on, exposes its
 * DevTools endpoint on an ABSTRACT unix socket named `webview_devtools_remote_<pid>`. OkHttp can
 * only speak TCP host:port, so this class stands up a tiny localhost TCP <-> abstract-socket relay:
 * every TCP connection that lands on the relay port is bridged byte-for-byte to a fresh
 * [LocalSocket] connected to the devtools abstract socket of THIS process. Because we attach to
 * our own process there is no cross-app SELinux barrier and no chromedriver (so
 * `navigator.webdriver` stays false).
 *
 * Flow (see [connect]): start relay -> HTTP GET /json/list through the relay -> pick the page
 * target's `webSocketDebuggerUrl` and rewrite its host:port to the relay -> open the WebSocket.
 */
class CdpConnection private constructor(
    private val relay: Relay,
    private val httpClient: OkHttpClient,
) {
    private val tag = "CdpConnection"
    private val nextId = AtomicInteger(1)
    private val pending = ConcurrentHashMap<Int, CompletableDeferred<JsonObject>>()

    @Volatile
    private var webSocket: WebSocket? = null

    /** The `/devtools/page/<id>` path of the page this connection drives (unique per WebView page).
     *  Used by the multi-tab manager to bind each tab's CDP to a distinct page. */
    @Volatile
    var pagePath: String = ""
        private set

    /** Simple event sink for CDP events (messages without an `id`). P1: a single callback is enough. */
    @Volatile
    var onEvent: (method: String, params: JsonObject?) -> Unit = { _, _ -> }

    private val openDeferred = CompletableDeferred<Unit>()

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            openDeferred.complete(Unit)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val obj = JsonParser.parseString(text).asJsonObject
            if (obj.has("id")) {
                val id = obj.get("id").asInt
                val deferred = pending.remove(id) ?: return
                if (obj.has("error")) {
                    val err = obj.getAsJsonObject("error")
                    val message = if (err.has("message")) err.get("message").asString else err.toString()
                    deferred.completeExceptionally(CdpException("CDP error for id=$id: $message"))
                } else {
                    val result = if (obj.has("result")) obj.getAsJsonObject("result") else JsonObject()
                    deferred.complete(result)
                }
            } else if (obj.has("method")) {
                val method = obj.get("method").asString
                val params = if (obj.has("params")) obj.getAsJsonObject("params") else null
                onEvent(method, params)
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (!openDeferred.isCompleted) openDeferred.completeExceptionally(t)
            // Fail every awaiting request so callers don't hang forever.
            val snapshot = pending.toMap()
            pending.clear()
            snapshot.values.forEach { it.completeExceptionally(t) }
        }
    }

    private fun startWebSocket(httpWsUrl: String) {
        val request = Request.Builder().url(httpWsUrl).build()
        webSocket = httpClient.newWebSocket(request, listener)
    }

    private suspend fun awaitOpen() {
        withTimeout(CONNECT_TIMEOUT_MS) { openDeferred.await() }
    }

    /**
     * Send a CDP command and suspend until the matching `{"id":id,"result":...}` arrives.
     * A `{"id":id,"error":...}` reply throws [CdpException] carrying the error message.
     */
    suspend fun send(method: String, params: JsonObject? = null): JsonObject {
        val ws = webSocket ?: throw IllegalStateException("WebSocket not connected")
        val id = nextId.getAndIncrement()
        val deferred = CompletableDeferred<JsonObject>()
        pending[id] = deferred
        val msg = JsonObject().apply {
            addProperty("id", id)
            addProperty("method", method)
            if (params != null) add("params", params)
        }
        ws.send(msg.toString())
        return try {
            withTimeout(SEND_TIMEOUT_MS) { deferred.await() }
        } finally {
            pending.remove(id)
        }
    }

    /** Convenience for `<Domain>.enable`, e.g. enableDomain("Page"). */
    suspend fun enableDomain(name: String): JsonObject = send("$name.enable")

    fun close() {
        webSocket?.close(1000, "client closing")
        webSocket = null
        relay.close()
    }

    class CdpException(message: String) : RuntimeException(message)

    /**
     * localhost TCP <-> ABSTRACT devtools-socket relay. The [ServerSocket] is bound to the
     * loopback interface on an OS-chosen port; a daemon accept loop bridges each inbound TCP
     * connection to a fresh [LocalSocket] on the abstract devtools socket of this process.
     */
    class Relay {
        // Bind explicitly to the IPv4 loopback. getLoopbackAddress() can return ::1 on IPv6
        // hosts, which would leave the relay listening only on [::1] while the OkHttp client
        // connects to 127.0.0.1 -> ECONNREFUSED. The /json/list URL below uses 127.0.0.1.
        private val server: ServerSocket =
            ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        val port: Int get() = server.localPort
        private val abstractName = "webview_devtools_remote_${android.os.Process.myPid()}"

        @Volatile
        private var running = true

        init {
            val acceptThread = Thread({ acceptLoop() }, "cdp-relay-accept")
            acceptThread.isDaemon = true
            acceptThread.start()
        }

        private fun acceptLoop() {
            while (running) {
                val tcp: Socket = try {
                    server.accept()
                } catch (e: IOException) {
                    // server.close() unblocks accept() with an IOException; that is the stop signal.
                    if (running) Log.w("CdpRelay", "accept failed", e)
                    break
                }
                bridge(tcp)
            }
        }

        private fun bridge(tcp: Socket) {
            val local = LocalSocket()
            local.connect(LocalSocketAddress(abstractName, LocalSocketAddress.Namespace.ABSTRACT))
            // Two daemon threads: TCP->local and local->TCP. Either side closing tears down both.
            pump("cdp-relay-up", tcp.getInputStream(), local.outputStream) { closeBoth(tcp, local) }
            pump("cdp-relay-down", local.inputStream, tcp.getOutputStream()) { closeBoth(tcp, local) }
        }

        private fun pump(name: String, input: InputStream, output: OutputStream, onEnd: () -> Unit) {
            val t = Thread({
                val buf = ByteArray(16 * 1024)
                try {
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        output.flush()
                    }
                } catch (_: IOException) {
                    // Expected when the peer socket is closed; just unwind and tear down the pair.
                } finally {
                    onEnd()
                }
            }, name)
            t.isDaemon = true
            t.start()
        }

        private fun closeBoth(tcp: Socket, local: LocalSocket) {
            try { tcp.close() } catch (_: IOException) {}
            try { local.close() } catch (_: IOException) {}
        }

        fun close() {
            running = false
            try { server.close() } catch (_: IOException) {}
        }
    }

    companion object {
        private const val CONNECT_TIMEOUT_MS = 10_000L
        private const val SEND_TIMEOUT_MS = 10_000L
        private const val TAG = "CdpConnection"

        /**
         * Full connect: stand up the relay, discover the page target via /json/list, rewrite the
         * WebSocket debugger URL to point at the relay, and open the WebSocket.
         */
        suspend fun connect(claimedPaths: Set<String> = emptySet()): CdpConnection {
            val relay = Relay()
            val httpClient = OkHttpClient.Builder()
                .connectTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS) // 0 = no read timeout: required for the WebSocket
                .pingInterval(15, TimeUnit.SECONDS)
                .build()

            val resolved = withContext(Dispatchers.IO) {
                resolvePageWebSocketUrl(httpClient, relay.port, claimedPaths)
            }
            val conn = CdpConnection(relay, httpClient)
            conn.pagePath = resolved.path
            conn.startWebSocket(resolved.relayUrl)
            try {
                conn.awaitOpen()
            } catch (t: Throwable) {
                conn.close()
                throw t
            }
            return conn
        }

        private data class ResolvedTarget(val path: String, val relayUrl: String)

        /**
         * GET http://127.0.0.1:<relayPort>/json/list, find a `page` target whose `/devtools/page/<id>`
         * path is NOT in [claimedPaths] (so each tab's CDP binds to a distinct WebView page), and
         * rewrite that ws:// URL's host:port to the relay (scheme flipped to http:// — OkHttp's
         * Request.url only accepts http/https; the upgrade still produces a real WebSocket). Retries
         * briefly because a freshly created WebView's devtools page registers asynchronously.
         */
        private fun resolvePageWebSocketUrl(
            httpClient: OkHttpClient,
            relayPort: Int,
            claimedPaths: Set<String>,
        ): ResolvedTarget {
            val listUrl = "http://127.0.0.1:$relayPort/json/list"
            var lastError = "no DevTools page target available"
            // up to ~3s: a new WebView's page can take a beat to appear in /json/list.
            repeat(20) { attempt ->
                val body = runCatching {
                    httpClient.newCall(Request.Builder().url(listUrl).build()).execute().use { resp ->
                        if (!resp.isSuccessful) throw IOException("DevTools /json/list HTTP ${resp.code}")
                        resp.body?.string() ?: throw IOException("DevTools /json/list empty body")
                    }
                }.getOrElse { lastError = it.message ?: "json/list failed"; null }
                if (body != null) {
                    val arr = JsonParser.parseString(body).asJsonArray
                    var chosen: JsonObject? = null
                    for (el in arr) {
                        val obj = el.asJsonObject
                        if (!obj.has("webSocketDebuggerUrl")) continue
                        val type = if (obj.has("type")) obj.get("type").asString else ""
                        if (type != "page") continue
                        val path = pathOf(obj.get("webSocketDebuggerUrl").asString)
                        if (path in claimedPaths) continue
                        chosen = obj; break
                    }
                    if (chosen != null) {
                        val wsUrl = chosen.get("webSocketDebuggerUrl").asString
                        val path = pathOf(wsUrl)
                        Log.i(TAG, "page target ws url: $wsUrl (path=$path)")
                        return ResolvedTarget(path, rewriteToRelay(wsUrl, relayPort))
                    }
                    lastError = "all ${arr.size()} page target(s) already claimed"
                }
                Thread.sleep(150)
            }
            throw IllegalStateException("CDP: $lastError (claimed=${claimedPaths.size})")
        }

        private fun pathOf(wsUrl: String): String = URI(wsUrl).rawPath ?: "/"

        /** Keep the path, swap host:port for the relay, and use http:// (OkHttp upgrades it to ws). */
        private fun rewriteToRelay(wsUrl: String, relayPort: Int): String {
            val uri = URI(wsUrl)
            val path = uri.rawPath ?: "/"
            return "http://127.0.0.1:$relayPort$path"
        }
    }
}
