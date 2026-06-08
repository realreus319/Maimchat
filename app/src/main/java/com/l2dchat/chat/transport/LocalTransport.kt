package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.ChatWebSocketManager.RuntimeMode
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.LocalChatRuntime
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class LocalTransport(
        private val scope: CoroutineScope,
        private val callbacks: ChatTransportCallbacks,
        private val platformProvider: () -> String,
        private val agentNameProvider: () -> String?,
        private val runtime: LocalChatRuntime = LocalChatRuntime()
) : ChatTransport {
    override val mode: RuntimeMode = RuntimeMode.LOCAL

    private val logger = L2DLogger.module(LogModule.CHAT)
    private var running = false

    fun start() {
        val wasRunning = running
        running = true
        callbacks.onStateChanged(ConnectionState.CONNECTED)
        if (!wasRunning) {
            logger.info("本地聊天运行时已启动")
        }
    }

    override fun stop(reason: String, userInitiated: Boolean) {
        if (running) {
            logger.info("本地聊天运行时已停止：$reason")
        }
        running = false
        callbacks.onStateChanged(ConnectionState.DISCONNECTED)
    }

    override fun send(message: MessageBase): Boolean {
        if (!running) {
            start()
        }
        if (!runtime.shouldReply(message)) {
            return true
        }
        scope.launch {
            try {
                delay(120L)
                val reply =
                        runtime.createReply(
                                inbound = message,
                                fallbackPlatform = platformProvider(),
                                fallbackAgentName = agentNameProvider()
                        )
                callbacks.onIncomingText(reply.toJsonString())
            } catch (e: Exception) {
                callbacks.onError("本地运行时处理消息失败：${e.message ?: "未知错误"}", e)
            }
        }
        return true
    }
}
