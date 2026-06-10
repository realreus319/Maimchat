package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.ChatWebSocketManager.RuntimeMode
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.LocalChatRuntime
import com.l2dchat.core.LocalRuntimeFactory
import com.l2dchat.core.LocalRuntimeLlmConfig
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.NoopMotionController
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerSessionStore
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
        perceptionStoreFactory: (RoutingKey) -> PerceptionStore? = { null },
        plannerSessionStoreFactory: (RoutingKey) -> PlannerSessionStore = {
            NoopPlannerSessionStore
        },
        localRuntimeLlmConfigProvider: () -> LocalRuntimeLlmConfig? = { null },
        environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
        motionController: MotionController = NoopMotionController,
        private val runtimeFactory: () -> LocalChatRuntime = {
            LocalRuntimeFactory.create(
                    scope = scope,
                    llmConfig = localRuntimeLlmConfigProvider(),
                    perceptionStoreFactory = perceptionStoreFactory,
                    plannerSessionStoreFactory = plannerSessionStoreFactory,
                    environmentStateProvider = environmentStateProvider,
                    motionController = motionController
            )
        }
) : ChatTransport {
    override val mode: RuntimeMode = RuntimeMode.LOCAL

    private val logger = L2DLogger.module(LogModule.CHAT)
    private var running = false
    private var runtime: LocalChatRuntime = runtimeFactory()
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

    fun rebuildRuntime(reason: String = "runtime configuration changed") {
        val previous = runtime
        runtime = runtimeFactory()
        previous.cancel()
        logger.info("本地聊天运行时已重建：$reason")
        if (running) {
            callbacks.onStateChanged(ConnectionState.CONNECTED)
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
