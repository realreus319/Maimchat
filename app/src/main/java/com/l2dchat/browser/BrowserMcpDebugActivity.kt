package com.l2dchat.browser

import android.app.Activity
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
import android.util.Log
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.l2dchat.worker.ShellEngineProtocol
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * DEBUG: exercises the P3 chain — engine's Playwright-MCP server + Messenger reverse bridge +
 * in-process [BrowserController] — WITHOUT the worker / LLM.
 *
 * Flow: bind the engine [com.l2dchat.shell.ShellService], register (which makes this Activity the
 * reverse-bridge target), ask for the MCP server port (MSG_GET_MCP_PORT -> MSG_MCP_PORT), then POST
 * JSON-RPC directly to http://127.0.0.1:<port>/mcp: initialize, tools/list, tools/call
 * browser_navigate{example.com}, tools/call browser_snapshot. Each step is logged to the TextView and
 * logcat (tag "BrowserMcpDebug"). The tools/call requests round-trip back through the bridge into the
 * Activity's own BrowserController (published to BrowserHost below).
 *
 * Start: adb shell am start -n com.l2dchat/com.l2dchat.browser.BrowserMcpDebugActivity
 */
class BrowserMcpDebugActivity : Activity() {

    private val tag = "BrowserMcpDebug"
    private val scope = CoroutineScope(Dispatchers.Main)
    private lateinit var controller: BrowserController
    private lateinit var out: TextView

    private var serviceMessenger: Messenger? = null
    private var conn: ServiceConnection? = null

    // Incoming Messenger (main looper): receives MSG_READY / MSG_MCP_PORT / MSG_BROWSER_ACTION.
    private val incoming = Messenger(Handler(Looper.getMainLooper()) { msg ->
        when (msg.what) {
            ShellEngineProtocol.MSG_READY -> log("engine: READY")
            ShellEngineProtocol.MSG_MCP_PORT -> {
                val port = msg.data.getInt(ShellEngineProtocol.KEY_MCP_PORT)
                log("engine: MCP port = $port")
                if (port > 0) runMcpTest(port) else log("ERROR: invalid MCP port")
            }
            ShellEngineProtocol.MSG_BROWSER_ACTION -> {
                val token = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_TOKEN)
                val name = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_NAME).orEmpty()
                val argsJson = msg.data.getString(ShellEngineProtocol.KEY_BROWSER_ARGS).orEmpty()
                if (token != null) handleBrowserAction(token, name, argsJson)
            }
        }
        true
    })

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        controller = BrowserController(this)
        BrowserHost.publish(controller)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        out = TextView(this).apply { textSize = 10f; setPadding(12, 12, 12, 12) }
        root.addView(ScrollView(this).apply {
            addView(out)
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0).apply { weight = 1f }
        })
        root.addView(controller.view, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0).apply { weight = 2f })
        setContentView(root)
        log("== P3 MCP debug ==")

        bindEngine()
    }

    private fun bindEngine() {
        val c = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                val svc = Messenger(binder)
                serviceMessenger = svc
                log("engine: bound, registering…")
                runCatching {
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_REGISTER).apply { replyTo = incoming })
                    svc.send(Message.obtain(null, ShellEngineProtocol.MSG_GET_MCP_PORT).apply { replyTo = incoming })
                }.onFailure { log("ERROR sending register/get-port: ${it.message}") }
            }

            override fun onServiceDisconnected(name: ComponentName?) {
                log("engine: disconnected")
            }
        }
        conn = c
        val intent = Intent()
            .setComponent(ComponentName(ShellEngineProtocol.ENGINE_PACKAGE, ShellEngineProtocol.SERVICE_CLASS))
            .setPackage(ShellEngineProtocol.ENGINE_PACKAGE)
        val bound = runCatching { bindService(intent, c, Context.BIND_AUTO_CREATE) }.getOrDefault(false)
        if (!bound) log("ERROR: bindService failed (engine installed / signed correctly?)")
    }

    /** Reverse-bridge handler: drive the local BrowserController and reply MSG_BROWSER_RESULT. */
    private fun handleBrowserAction(token: String, name: String, argsJson: String) {
        log("bridge <- $name $argsJson")
        scope.launch {
            val (text, isError) = try {
                val controllerReady = BrowserHost.awaitReady()
                val args: JsonObject = if (argsJson.isBlank()) JsonObject()
                else runCatching { JsonParser.parseString(argsJson).asJsonObject }.getOrDefault(JsonObject())
                val r = controllerReady.execute(name, args)
                r.text to r.isError
            } catch (t: Throwable) {
                Log.w(tag, "browser action '$name' failed", t)
                ("browser action '$name' failed: ${t.message}") to true
            }
            runCatching {
                serviceMessenger?.send(Message.obtain(null, ShellEngineProtocol.MSG_BROWSER_RESULT).apply {
                    data = Bundle().apply {
                        putString(ShellEngineProtocol.KEY_BROWSER_TOKEN, token)
                        putString(ShellEngineProtocol.KEY_BROWSER_RESULT_TEXT, text)
                        putBoolean(ShellEngineProtocol.KEY_BROWSER_IS_ERROR, isError)
                    }
                })
            }
            log("bridge -> ${text.take(120)}")
        }
    }

    /** POST the standard JSON-RPC handshake + two tool calls to the MCP server. */
    private fun runMcpTest(port: Int) {
        val base = "http://127.0.0.1:$port/mcp"
        scope.launch {
            val r1 = post(base, rpc(1, "initialize", JSONObject().put("protocolVersion", "2025-06-18")))
            log("initialize -> $r1")
            // Notification (no id): server returns 200 empty, no body expected.
            post(base, JSONObject().put("jsonrpc", "2.0").put("method", "notifications/initialized").toString())
            val r2 = post(base, rpc(2, "tools/list", JSONObject()))
            log("tools/list -> $r2")
            val navArgs = JSONObject().put("name", "browser_navigate")
                .put("arguments", JSONObject().put("url", "https://example.com"))
            val r3 = post(base, rpc(3, "tools/call", navArgs))
            log("tools/call browser_navigate -> $r3")
            val snapArgs = JSONObject().put("name", "browser_snapshot").put("arguments", JSONObject())
            val r4 = post(base, rpc(4, "tools/call", snapArgs))
            log("tools/call browser_snapshot -> $r4")
            log("== done ==")
        }
    }

    private fun rpc(id: Int, method: String, params: JSONObject): String =
        JSONObject().put("jsonrpc", "2.0").put("id", id).put("method", method).put("params", params).toString()

    private suspend fun post(url: String, body: String): String = withContext(Dispatchers.IO) {
        val conn = URL(url).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 10_000
            conn.readTimeout = 70_000
            conn.setRequestProperty("Content-Type", "application/json")
            val bytes = body.toByteArray(Charsets.UTF_8)
            conn.setRequestProperty("Content-Length", bytes.size.toString())
            conn.outputStream.use { os: OutputStream -> os.write(bytes) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            "[$code] $text"
        } finally {
            conn.disconnect()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        runCatching { conn?.let { unbindService(it) } }
        BrowserHost.clear(controller)
        controller.close()
    }

    private fun log(line: String) {
        Log.i(tag, line)
        runOnUiThread { out.append(line + "\n") }
    }
}
