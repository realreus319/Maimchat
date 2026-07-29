package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.RuntimeState
import com.l2dchat.chat.MessageBase
import com.l2dchat.core.LocalChatRuntime
import com.l2dchat.core.LocalRuntimeFactory
import com.l2dchat.core.LocalRuntimeLlmConfig
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.NoopMotionController
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.EmptyPlannerSystemPromptProvider
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerSessionStore
import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerTurnContext
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.storage.RuntimeStateDao
import com.l2dchat.core.tools.EmptyReplierPromptContextProvider
import com.l2dchat.core.tools.NoopReplierTaskStore
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskStore
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class LocalTransport(
        private val scope: CoroutineScope,
        private val callbacks: ChatTransportCallbacks,
        private val agentNameProvider: () -> String?,
        perceptionStoreFactory: (RoutingKey) -> PerceptionStore? = { null },
        plannerSessionStoreFactory: (RoutingKey) -> PlannerSessionStore = {
            NoopPlannerSessionStore
        },
        plannerSystemPromptProvider: () -> PlannerSystemPromptProvider = {
            EmptyPlannerSystemPromptProvider
        },
        decisionPlannerSystemPromptProvider: () -> PlannerSystemPromptProvider = {
            EmptyPlannerSystemPromptProvider
        },
        replierPromptContextProvider: () -> ReplierPromptContextProvider = {
            EmptyReplierPromptContextProvider
        },
        replierTaskStoreProvider: () -> ReplierTaskStore = { NoopReplierTaskStore },
        localRuntimeLlmConfigProvider: () -> LocalRuntimeLlmConfig? = { null },
        environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
        motionController: MotionController = NoopMotionController,
        runtimeStateDaoProvider: () -> RuntimeStateDao? = { null },
        extraNormalTools: List<com.l2dchat.core.tools.Tool> = emptyList(),
        backgroundStatusProvider: com.l2dchat.core.reply.PlannerPromptContextProvider =
                com.l2dchat.core.reply.EmptyPlannerPromptContextProvider,
        private val runtimeFactory: (CoroutineScope) -> LocalChatRuntime = { runtimeScope ->
            LocalRuntimeFactory.create(
                    scope = runtimeScope,
                    llmConfig = localRuntimeLlmConfigProvider(),
                    perceptionStoreFactory = perceptionStoreFactory,
                    plannerSessionStoreFactory = plannerSessionStoreFactory,
                    plannerSystemPromptProvider =
                            object : PlannerSystemPromptProvider {
                                override suspend fun systemPromptFor(context: PlannerTurnContext) =
                                        plannerSystemPromptProvider().systemPromptFor(context)
                            },
                    decisionPlannerSystemPromptProvider =
                            object : PlannerSystemPromptProvider {
                                override suspend fun systemPromptFor(context: PlannerTurnContext) =
                                        decisionPlannerSystemPromptProvider().systemPromptFor(
                                                context
                                        )
                            },
                    replierPromptContextProvider =
                            object : ReplierPromptContextProvider {
                                override suspend fun contextFor(request: ReplierTaskRequest) =
                                        replierPromptContextProvider().contextFor(request)
                            },
                    replierTaskStore = replierTaskStoreProvider(),
                    environmentStateProvider = environmentStateProvider,
                    motionController = motionController,
                    runtimeStateDaoProvider = runtimeStateDaoProvider,
                    extraNormalTools = extraNormalTools,
                    backgroundStatusProvider = backgroundStatusProvider
            )
        }
) : ChatTransport {
    private val logger = L2DLogger.module(LogModule.CHAT)
    private val runtimeLock = Any()
    private var running = false
    @Volatile private var inErrorState = false
    private var runtimeJob: Job? = null
    private var runtimeScope: CoroutineScope? = null
    private var runtimeGeneration: Long = 0L
    private var runtime: LocalChatRuntime? = null
    private val replySink =
            object : ReplySink {
                override suspend fun send(message: MessageBase) {
                    callbacks.onIncomingText(message.toJsonString())
                }
            }

    fun start() {
        val shouldEmitStarting =
                synchronized(runtimeLock) {
                    !running || runtime == null || runtimeJob?.isActive != true
                }
        if (shouldEmitStarting) {
            callbacks.onStateChanged(RuntimeState.STARTING)
        }
        val wasRunning =
                try {
                    synchronized(runtimeLock) {
                        val previous = running
                        ensureRuntimeLocked()
                        running = true
                        previous
                    }
                } catch (e: Exception) {
                    markRuntimeFailed()
                    callbacks.onError("本地运行时启动失败：${e.message ?: "未知错误"}", e)
                    return
                }
        inErrorState = false
        callbacks.onStateChanged(RuntimeState.RUNNING)
        if (!wasRunning) {
            logger.info("本地聊天运行时已启动")
        }
    }

    fun rebuildRuntime(reason: String = "runtime configuration changed") {
        if (isRunning()) {
            callbacks.onStateChanged(RuntimeState.STARTING)
        }
        var previous: RuntimeSnapshot? = null
        try {
            synchronized(runtimeLock) {
                previous = clearRuntimeLocked()
                if (running) {
                    ensureRuntimeLocked()
                }
            }
        } catch (e: Exception) {
            previous?.runtime?.cancel()
            previous?.job?.cancel()
            markRuntimeFailed()
            callbacks.onError("本地运行时重建失败：${e.message ?: "未知错误"}", e)
            return
        }
        val rebuilt = previous ?: return
        rebuilt.runtime?.cancel()
        rebuilt.job?.cancel()
        logger.info("本地聊天运行时已重建：$reason")
        if (rebuilt.wasRunning) {
            inErrorState = false
            callbacks.onStateChanged(RuntimeState.RUNNING)
        }
    }

    override fun stop(reason: String, userInitiated: Boolean) {
        val previous =
                synchronized(runtimeLock) {
                    val wasRunning = running
                    val snapshot = clearRuntimeLocked()
                    running = false
                    snapshot.copy(wasRunning = wasRunning)
                }
        previous.runtime?.cancel()
        previous.job?.cancel()
        if (previous.wasRunning) {
            logger.info("本地聊天运行时已停止：$reason")
        }
        callbacks.onStateChanged(RuntimeState.STOPPED)
    }

    override fun send(message: MessageBase): Boolean {
        if (!isRunning()) {
            start()
        }
        val work = currentRuntimeWork()
        val messageId = message.messageInfo.messageId
        work.scope.launch {
            callbacks.onProcessingChanged(true)
            try {
                delay(120L)
                if (!isCurrentRuntimeWork(work.generation)) {
                    return@launch
                }
                work.runtime.handleMessage(
                                inbound = message,
                                fallbackAgentName = agentNameProvider(),
                                replySink = replySink
                        )
                // A successful turn proves the runtime is healthy again; clear any sticky
                // error state left by a previous failed message so the UI recovers without
                // requiring the user to re-save settings. Only re-emit when actually
                // recovering from an error so normal sends stay quiet.
                if (isCurrentRuntimeWork(work.generation) && inErrorState) {
                    inErrorState = false
                    callbacks.onStateChanged(RuntimeState.RUNNING)
                }
            } catch (_: CancellationException) {
                // Expected when local runtime is stopped, rebuilt, or the service is destroyed.
            } catch (e: Exception) {
                inErrorState = true
                callbacks.onStateChanged(RuntimeState.ERROR)
                // Flag the specific message that failed so the UI can mark it (red "!"), and still
                // surface the human-readable error for the connection banner.
                callbacks.onMessageFailed(
                        messageId,
                        "本地运行时处理消息失败：${e.message ?: "未知错误"}",
                        e
                )
            } finally {
                callbacks.onProcessingChanged(false)
            }
        }
        return true
    }

    fun submitEnvironmentTrigger(submission: EnvironmentTriggerSubmission): Boolean {
        if (!isRunning()) {
            start()
        }
        val work = currentRuntimeWork()
        work.scope.launch {
            try {
                if (!isCurrentRuntimeWork(work.generation)) {
                    return@launch
                }
                work.runtime.submitEnvironmentTrigger(
                        submission = submission,
                        fallbackAgentName = agentNameProvider(),
                        replySink = replySink
                )
            } catch (_: CancellationException) {
                // Expected when local runtime is stopped, rebuilt, or the service is destroyed.
            } catch (e: Exception) {
                inErrorState = true
                callbacks.onStateChanged(RuntimeState.ERROR)
                callbacks.onError("本地运行时处理环境触发失败：${e.message ?: "未知错误"}", e)
            }
        }
        return true
    }

    /** Submit a raw background trigger (SYS worker-completion) into the live runtime; reply → [replySink]. */
    fun submitBackgroundTrigger(trigger: com.l2dchat.core.trigger.Trigger): Boolean {
        if (!isRunning()) {
            start()
        }
        val work = currentRuntimeWork()
        work.scope.launch {
            try {
                if (!isCurrentRuntimeWork(work.generation)) {
                    return@launch
                }
                work.runtime.submitBackgroundTrigger(
                        trigger = trigger,
                        fallbackAgentName = agentNameProvider(),
                        replySink = replySink
                )
            } catch (_: CancellationException) {
                // Expected when local runtime is stopped/rebuilt/destroyed.
            } catch (e: Exception) {
                inErrorState = true
                callbacks.onStateChanged(RuntimeState.ERROR)
                callbacks.onError("本地运行时处理后台任务完成失败：${e.message ?: "未知错误"}", e)
            }
        }
        return true
    }

    private fun currentRuntimeWork(): RuntimeWork =
            synchronized(runtimeLock) { ensureRuntimeLocked() }

    private fun isRunning(): Boolean = synchronized(runtimeLock) { running }

    private fun isCurrentRuntimeWork(generation: Long): Boolean =
            synchronized(runtimeLock) { running && runtimeGeneration == generation }

    private fun ensureRuntimeLocked(): RuntimeWork {
        val existingRuntime = runtime
        val existingScope = runtimeScope
        val existingJob = runtimeJob
        if (existingRuntime != null && existingScope != null && existingJob?.isActive == true) {
            return RuntimeWork(
                    runtime = existingRuntime,
                    scope = existingScope,
                    generation = runtimeGeneration
            )
        }

        runtimeGeneration += 1
        val job = newRuntimeJob()
        val childScope = CoroutineScope(scope.coroutineContext + job)
        val newRuntime =
                try {
                    runtimeFactory(childScope)
                } catch (throwable: Throwable) {
                    job.cancel()
                    throw throwable
                }
        runtimeJob = job
        runtimeScope = childScope
        runtime = newRuntime
        return RuntimeWork(
                runtime = newRuntime,
                scope = childScope,
                generation = runtimeGeneration
        )
    }

    private fun clearRuntimeLocked(): RuntimeSnapshot {
        val previous =
                RuntimeSnapshot(
                        runtime = runtime,
                        job = runtimeJob,
                        wasRunning = running
                )
        runtime = null
        runtimeScope = null
        runtimeJob = null
        runtimeGeneration += 1
        return previous
    }

    private fun newRuntimeJob(): Job = SupervisorJob(scope.coroutineContext[Job])

    private fun markRuntimeFailed() {
        val snapshot =
                synchronized(runtimeLock) {
                    val cleared = clearRuntimeLocked()
                    running = false
                    cleared
                }
        snapshot.runtime?.cancel()
        snapshot.job?.cancel()
        inErrorState = true
        callbacks.onStateChanged(RuntimeState.ERROR)
    }

    private data class RuntimeWork(
            val runtime: LocalChatRuntime,
            val scope: CoroutineScope,
            val generation: Long
    )

    private data class RuntimeSnapshot(
            val runtime: LocalChatRuntime?,
            val job: Job?,
            val wasRunning: Boolean
    )
}
