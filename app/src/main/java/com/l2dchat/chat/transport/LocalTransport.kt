package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.ChatWebSocketManager.RuntimeMode
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.LocalChatRuntime
import com.l2dchat.core.reply.ReplySink
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
        private val runtime: LocalChatRuntime = LocalChatRuntime(scope = scope)
) : ChatTransport {
    override val mode: RuntimeMode = RuntimeMode.LOCAL

    private val logger = L2DLogger.module(LogModule.CHAT)
    private var running = false
    private val replySink =
            object : ReplySink {
                override suspend fun send(message: MessageBase) {
                    callbacks.onIncomingText(message.toJsonString())
                }
            }

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
        scope.launch {
            try {
                delay(120L)
                runtime.handleMessage(
                                inbound = message,
                                fallbackPlatform = platformProvider(),
                                fallbackAgentName = agentNameProvider(),
                                replySink = replySink
                        )
            } catch (e: Exception) {
                callbacks.onError("本地运行时处理消息失败：${e.message ?: "未知错误"}", e)
            }
        }
        return true
    }
}
