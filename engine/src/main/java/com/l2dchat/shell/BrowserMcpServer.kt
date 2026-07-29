package com.l2dchat.shell

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/**
 * A minimal **Playwright-MCP-compatible** HTTP JSON-RPC MCP server, hosted inside the engine process.
 *
 * The worker (Claude-Code python port, running under proot) is configured (via `--mcp-config`) to
 * POST single JSON-RPC requests to `http://127.0.0.1:<port>/mcp`. proot shares the engine's network
 * namespace, so 127.0.0.1 reaches this ServerSocket. Each `tools/call` is handed to [bridge] — a
 * synchronous, blocking call that crosses the Messenger reverse bridge to the main app's
 * BrowserController and returns the resulting text.
 *
 * Transport: bare HTTP/1.1, one request per connection, `Connection: close`. Only POST is handled
 * (path is ignored). This deliberately avoids any HTTP server dependency — the surface the worker's
 * MCP http client needs is tiny and fully specified here.
 */
class BrowserMcpServer(
    private val bridge: (name: String, argsJson: String) -> BridgeResult,
    private val bridgeTimeoutMs: Long = 60_000L,
) {

    /** Result of a single bridged browser tool call. */
    data class BridgeResult(val text: String, val isError: Boolean = false)

    private val TAG = "BrowserMcpServer"

    @Volatile
    private var server: ServerSocket? = null

    @Volatile
    private var running = false

    /** The bound port (0 until [start]). */
    val port: Int get() = server?.localPort ?: 0

    /**
     * Bind on the loopback interface and spin up the accept loop. MUST bind 127.0.0.1 explicitly:
     * getLoopbackAddress() resolves to ::1 on IPv6 devices, which the worker's `http://127.0.0.1`
     * client cannot reach.
     */
    fun start() {
        if (running) return
        val s = ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))
        server = s
        running = true
        Log.i(TAG, "MCP server listening on 127.0.0.1:${s.localPort}")
        thread(name = "mcp-accept", isDaemon = true) {
            while (running) {
                val client = try {
                    s.accept()
                } catch (t: Throwable) {
                    if (running) Log.w(TAG, "accept stopped: ${t.javaClass.simpleName}")
                    break
                }
                // One short-lived worker thread per connection (the worker POSTs serially, but be safe).
                thread(name = "mcp-conn", isDaemon = true) { handleConnection(client) }
            }
        }
    }

    fun stop() {
        running = false
        runCatching { server?.close() }
        server = null
    }

    // --- HTTP/1.1 request parsing (request line + headers + Content-Length body) ---

    private fun handleConnection(socket: Socket) {
        socket.use { sock ->
            val input = BufferedInputStream(sock.getInputStream())
            val output = sock.getOutputStream()
            val requestLine = readLine(input) ?: return
            // e.g. "POST /mcp HTTP/1.1" — we only support POST; path is irrelevant.
            var contentLength = 0
            while (true) {
                val header = readLine(input) ?: break
                if (header.isEmpty()) break // blank line terminates headers
                val idx = header.indexOf(':')
                if (idx > 0) {
                    val key = header.substring(0, idx).trim().lowercase()
                    val value = header.substring(idx + 1).trim()
                    if (key == "content-length") contentLength = value.toIntOrNull() ?: 0
                }
            }
            if (!requestLine.startsWith("POST")) {
                writeHttp(output, 405, "Method Not Allowed", null)
                return
            }
            val body = readBody(input, contentLength)
            val responseJson = handleRpc(body)
            // Notifications (no id) yield no JSON-RPC response -> 200 with empty body.
            if (responseJson == null) {
                writeHttp(output, 200, "OK", "")
            } else {
                writeHttp(output, 200, "OK", responseJson)
            }
        }
    }

    /** Read one CRLF/LF-terminated line as ASCII (header parsing). Returns null on EOF. */
    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder()
        var sawAny = false
        while (true) {
            val b = input.read()
            if (b < 0) return if (sawAny) sb.toString() else null
            sawAny = true
            if (b == '\n'.code) break
            if (b == '\r'.code) continue
            sb.append(b.toChar())
        }
        return sb.toString()
    }

    private fun readBody(input: InputStream, length: Int): String {
        if (length <= 0) return ""
        val buf = ByteArray(length)
        var got = 0
        while (got < length) {
            val r = input.read(buf, got, length - got)
            if (r < 0) break
            got += r
        }
        return String(buf, 0, got, Charsets.UTF_8)
    }

    private fun writeHttp(out: OutputStream, code: Int, reason: String, body: String?) {
        val bodyBytes = (body ?: "").toByteArray(Charsets.UTF_8)
        val sb = StringBuilder()
        sb.append("HTTP/1.1 ").append(code).append(' ').append(reason).append("\r\n")
        sb.append("Content-Type: application/json\r\n")
        sb.append("Content-Length: ").append(bodyBytes.size).append("\r\n")
        sb.append("Connection: close\r\n")
        sb.append("\r\n")
        out.write(sb.toString().toByteArray(Charsets.US_ASCII))
        out.write(bodyBytes)
        out.flush()
    }

    // --- JSON-RPC dispatch ---

    /**
     * Parse [body] as JSON-RPC and dispatch by method. Returns the JSON-RPC response string, or null
     * for notifications (no `id`) which require no response body.
     */
    private fun handleRpc(body: String): String? {
        val req = try {
            JSONObject(body)
        } catch (t: Throwable) {
            // Malformed body with no id we can echo: respond with a parse error (id null).
            return errorResponse(JSONObject.NULL, -32700, "Parse error")
        }
        val hasId = req.has("id") && !req.isNull("id")
        val id: Any = if (hasId) req.get("id") else JSONObject.NULL
        val method = req.optString("method")
        // Notifications (no id) -> no response. notifications/initialized etc.
        if (!hasId) {
            Log.d(TAG, "notification: $method")
            return null
        }
        val params = req.optJSONObject("params") ?: JSONObject()
        return when (method) {
            "initialize" -> resultResponse(id, initializeResult(params))
            "ping" -> resultResponse(id, JSONObject())
            "tools/list" -> resultResponse(id, toolsListResult())
            "tools/call" -> resultResponse(id, toolsCallResult(params))
            else -> errorResponse(id, -32601, "Method not found: $method")
        }
    }

    private fun initializeResult(params: JSONObject): JSONObject {
        val protocolVersion = params.optString("protocolVersion", "2025-06-18")
            .ifEmpty { "2025-06-18" }
        return JSONObject().apply {
            put("protocolVersion", protocolVersion)
            put("capabilities", JSONObject().apply { put("tools", JSONObject()) })
            put("serverInfo", JSONObject().apply {
                put("name", "l2d-browser")
                put("version", "1")
            })
        }
    }

    private fun toolsListResult(): JSONObject =
        JSONObject().put("tools", TOOLS)

    private fun toolsCallResult(params: JSONObject): JSONObject {
        val name = params.optString("name")
        val args = params.optJSONObject("arguments") ?: JSONObject()
        val result = try {
            bridge(name, args.toString())
        } catch (t: Throwable) {
            Log.w(TAG, "bridge threw for $name", t)
            BridgeResult("bridge error: ${t.message}", isError = true)
        }
        val content = JSONArray().put(
            JSONObject().apply {
                put("type", "text")
                put("text", result.text)
            }
        )
        return JSONObject().apply {
            put("content", content)
            if (result.isError) put("isError", true)
        }
    }

    private fun resultResponse(id: Any, result: JSONObject): String =
        JSONObject().apply {
            put("jsonrpc", "2.0")
            put("id", id)
            put("result", result)
        }.toString()

    private fun errorResponse(id: Any, code: Int, message: String): String =
        JSONObject().apply {
            put("jsonrpc", "2.0")
            put("id", id)
            put("error", JSONObject().apply {
                put("code", code)
                put("message", message)
            })
        }.toString()

    companion object {
        /** Playwright-MCP-compatible tool set (P3 core subset). Names/params 1:1 with Playwright MCP. */
        private val TOOLS: JSONArray = buildTools()

        private fun obj(vararg pairs: Pair<String, Any?>): JSONObject =
            JSONObject().apply { for ((k, v) in pairs) put(k, v) }

        private fun stringProp(): JSONObject = obj("type" to "string")
        private fun boolProp(): JSONObject = obj("type" to "boolean")
        private fun intProp(): JSONObject = obj("type" to "number")

        private fun tool(name: String, description: String, inputSchema: JSONObject): JSONObject =
            obj(
                "name" to name,
                "description" to description,
                "inputSchema" to inputSchema,
            )

        private fun schema(properties: JSONObject, required: List<String> = emptyList()): JSONObject =
            obj(
                "type" to "object",
                "properties" to properties,
                "required" to JSONArray(required),
            )

        private fun buildTools(): JSONArray {
            val arr = JSONArray()
            arr.put(
                tool(
                    "browser_navigate",
                    "Navigate the browser to a URL. Waits for the page to load, then returns the new " +
                        "accessibility snapshot of the page (interactive elements with [ref=eN] handles).",
                    schema(obj("url" to stringProp()), required = listOf("url")),
                )
            )
            arr.put(
                tool(
                    "browser_snapshot",
                    "Capture an accessibility snapshot of the current page. Returns the page url, title, " +
                        "and the list of interactive elements, each with a stable [ref=eN] handle used " +
                        "by browser_click / browser_type. Prefer this over a screenshot for acting.",
                    schema(JSONObject()),
                )
            )
            arr.put(
                tool(
                    "browser_click",
                    "Click an element on the page identified by its ref (from the latest browser_snapshot). " +
                        "`element` is a human-readable description (optional, for logging). After clicking, " +
                        "returns the new page snapshot.",
                    schema(
                        obj("element" to stringProp(), "ref" to stringProp()),
                        required = listOf("ref"),
                    ),
                )
            )
            arr.put(
                tool(
                    "browser_type",
                    "Type text into an editable element identified by its ref (from the latest " +
                        "browser_snapshot). Set `submit` true to press Enter / submit the form after typing. " +
                        "`element` is an optional human-readable description. Returns the new page snapshot.",
                    schema(
                        obj(
                            "element" to stringProp(),
                            "ref" to stringProp(),
                            "text" to stringProp(),
                            "submit" to boolProp(),
                        ),
                        required = listOf("ref", "text"),
                    ),
                )
            )
            arr.put(
                tool(
                    "browser_navigate_back",
                    "Go back to the previous page in history. Returns the new page snapshot.",
                    schema(JSONObject()),
                )
            )
            arr.put(
                tool(
                    "browser_take_screenshot",
                    "Take a screenshot of the current page (PNG). Returns metadata about the captured " +
                        "image. Use browser_snapshot instead when you need to act on elements.",
                    schema(JSONObject()),
                )
            )
            arr.put(
                tool(
                    "browser_tab_list",
                    "List all open browser tabs (index, title, url; the active tab is marked).",
                    schema(JSONObject()),
                )
            )
            arr.put(
                tool(
                    "browser_tab_new",
                    "Open a NEW browser tab and switch to it. Optionally navigate it to `url`. Returns " +
                        "the updated tab list.",
                    schema(obj("url" to stringProp())),
                )
            )
            arr.put(
                tool(
                    "browser_tab_select",
                    "Switch to the tab at `index` (from browser_tab_list). Returns that tab's snapshot.",
                    schema(obj("index" to intProp()), required = listOf("index")),
                )
            )
            arr.put(
                tool(
                    "browser_tab_close",
                    "Close the tab at `index` (or the current tab if omitted). Returns the updated tab list.",
                    schema(obj("index" to intProp())),
                )
            )
            arr.put(
                tool(
                    "submit_file",
                    "Submit a file you produced to the USER'S CHAT — it appears as a tappable attachment in the conversation. Use this to deliver outputs (reports, code, generated images, etc.) to the user. `path` is the absolute path to the file in your environment (e.g. /root/report.md).",
                    schema(
                        obj(
                            "path" to stringProp(),
                            "filename" to stringProp(),
                            "description" to stringProp(),
                        ),
                        required = listOf("path"),
                    ),
                )
            )
            return arr
        }
    }
}
