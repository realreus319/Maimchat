package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.ChatWebSocketManager.RuntimeMode
import com.l2dchat.chat.MessageBase
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

class RemoteWebSocketTransport(
        private val scope: CoroutineScope,
        private val callbacks: ChatTransportCallbacks,
        private val maxRetries: Int = 3,
        private val client: OkHttpClient =
                OkHttpClient.Builder()
                        .connectTimeout(20, TimeUnit.SECONDS)
                        .readTimeout(0, TimeUnit.SECONDS)
                        .writeTimeout(20, TimeUnit.SECONDS)
                        .pingInterval(30, TimeUnit.SECONDS)
                        .build()
) : ChatTransport {
    override val mode: RuntimeMode = RuntimeMode.REMOTE

    private val logger = L2DLogger.module(LogModule.CHAT)
    private var webSocket: WebSocket? = null
    private var currentState = ConnectionState.DISCONNECTED
    private var lastConfig: RemoteWebSocketConfig? = null
    private var retryCount = 0
    private var userInitiatedDisconnect = false
    private var reconnectJobActive = false

    fun connect(config: RemoteWebSocketConfig) {
        if (currentState == ConnectionState.CONNECTED ||
                        currentState == ConnectionState.CONNECTING
        ) {
            return
        }

        lastConfig = config
        userInitiatedDisconnect = false
        updateState(ConnectionState.CONNECTING)

        val requestBuilder =
                Request.Builder()
                        .url(config.url)
                        .addHeader("platform", config.platform)
                        .addHeader("Sec-WebSocket-Protocol", "chat")
        config.authToken?.takeIf { it.isNotBlank() }?.let {
            requestBuilder.addHeader("Authorization", "Bearer $it")
        }
        val request = requestBuilder.build()

        val authStatus =
                if (config.authToken.isNullOrBlank()) "未提供鉴权信息"
                else "已附带 Bearer Token（长度=${config.authToken.length}）"
        val headerPreview =
                request.headers.names().sorted().joinToString(separator = "; ") { name ->
                    val value =
                            if (name.equals("Authorization", ignoreCase = true)) "******"
                            else request.header(name).orEmpty()
                    "$name=$value"
                }
        logger.info(
                "准备建立 WebSocket 连接：目标地址=${config.url}，当前重试序号=$retryCount，" +
                        "平台=${config.platform}，$authStatus"
        )
        if (headerPreview.isNotBlank()) {
            logger.debug(
                    "连接请求头：$headerPreview",
                    throttleMs = 2_000L,
                    throttleKey = "ws_request_headers"
            )
        }

        val listener =
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        logger.info("服务器握手成功：${summarizeResponse(response)}")
                        retryCount = 0
                        reconnectJobActive = false
                        updateState(ConnectionState.CONNECTED)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        callbacks.onIncomingText(text)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        try {
                            webSocket.close(1000, null)
                        } catch (_: Exception) {}
                        this@RemoteWebSocketTransport.webSocket = null
                        updateState(ConnectionState.DISCONNECTED)
                        val readableReason = if (reason.isBlank()) "无" else reason
                        logger.warn(
                                "服务器请求关闭连接：状态码=$code，原因=$readableReason，" +
                                        "是否用户主动断开=$userInitiatedDisconnect"
                        )
                        if (!userInitiatedDisconnect && code !in setOf(1000, 1001)) {
                            reportConnectionError("服务器异常断开：状态码=$code，原因=$readableReason")
                        }
                        attemptScheduleReconnect()
                    }

                    override fun onFailure(
                            webSocket: WebSocket,
                            t: Throwable,
                            response: Response?
                    ) {
                        this@RemoteWebSocketTransport.webSocket = null
                        val baseMsg = t.message ?: "未知错误"
                        val summary = summarizeResponse(response)
                        reportConnectionError("建立连接失败：$baseMsg；服务器响应概览：$summary", t)
                        attemptScheduleReconnect()
                    }
                }

        try {
            webSocket = client.newWebSocket(request, listener)
        } catch (e: Exception) {
            reportConnectionError("创建 WebSocket 失败：${e.message ?: "未知错误"}", e)
        }
    }

    override fun stop(reason: String, userInitiated: Boolean) {
        userInitiatedDisconnect = userInitiated
        reconnectJobActive = false
        try {
            webSocket?.close(1000, reason)
        } catch (_: Exception) {}
        webSocket = null
        updateState(ConnectionState.DISCONNECTED)
    }

    override fun send(message: MessageBase): Boolean {
        val text = message.toJsonString()
        if (currentState == ConnectionState.CONNECTED) {
            val accepted = webSocket?.send(text) ?: false
            if (accepted) {
                logger.debug(
                        "发送: ${sanitizeForLog(text)}",
                        throttleMs = 200L,
                        throttleKey = "send_preview"
                )
            } else {
                logger.warn("WebSocket 未接受发送内容", throttleMs = 1_000L, throttleKey = "send_rejected")
            }
            return accepted
        }
        logger.warn("未连接, 发送失败", throttleMs = 1_000L, throttleKey = "send_without_connection")
        return false
    }

    private fun updateState(state: ConnectionState) {
        currentState = state
        callbacks.onStateChanged(state)
    }

    private fun reportConnectionError(message: String, throwable: Throwable? = null) {
        logger.error(message, throwable)
        if (currentState != ConnectionState.CONNECTED) {
            updateState(ConnectionState.ERROR)
        }
        callbacks.onError(message, throwable)
    }

    private fun attemptScheduleReconnect() {
        if (userInitiatedDisconnect) {
            logger.info("用户主动断开，不自动重连")
            return
        }
        val config = lastConfig
        if (config == null) {
            reportConnectionError("无法自动重连：缺少上次连接的服务器地址，请重新配置连接信息")
            return
        }
        if (retryCount >= maxRetries) {
            reportConnectionError("达到最大重试次数($maxRetries)，已停止自动重连，请检查服务器状态或网络")
            return
        }
        if (reconnectJobActive) {
            logger.debug("已有重连任务，跳过重复调度", throttleMs = 2_000L, throttleKey = "reconnect_skip")
            return
        }
        val delayMs = 1500L * (retryCount + 1)
        reconnectJobActive = true
        retryCount += 1
        logger.info("计划 ${delayMs}ms 后进行第 $retryCount 次重连 ...")
        scope.launch {
            try {
                delay(delayMs)
                reconnectJobActive = false
                if (!userInitiatedDisconnect && currentState != ConnectionState.CONNECTED) {
                    connect(config)
                }
            } catch (e: Exception) {
                reconnectJobActive = false
                reportConnectionError("重连调度失败：${e.message ?: "未知错误"}", e)
            }
        }
    }

    private fun summarizeResponse(response: Response?, previewLimit: Long = 512L): String {
        if (response == null) return "无可用响应（response=null）"
        val statusLine = "HTTP ${response.code} ${response.message.ifBlank { "(无状态描述)" }}"
        val protocol = response.header("Sec-WebSocket-Protocol")?.let { "，协商协议=$it" } ?: ""
        val server = response.header("Server")?.let { "，Server=$it" } ?: ""
        val errorCode = response.header("X-Error-Code")?.let { "，服务器错误码=$it" } ?: ""
        val headersPreview =
                response.headers
                        .names()
                        .filterNot { it.equals("Set-Cookie", true) }
                        .sorted()
                        .take(5)
                        .joinToString(separator = "; ") { name ->
                            val value = response.header(name).orEmpty()
                            "$name=$value"
                        }
        val headerText = if (headersPreview.isBlank()) "" else "，首部信息={$headersPreview}"
        val bodyPreview =
                runCatching { response.peekBody(previewLimit).string().trim() }
                        .getOrNull()
                        ?.takeIf { it.isNotEmpty() }
                        ?.let { "，响应体预览=${sanitizeForLog(it)}" }
                        ?: ""
        return statusLine + protocol + server + errorCode + headerText + bodyPreview
    }

    private fun sanitizeForLog(raw: String): String {
        return raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    }
}
