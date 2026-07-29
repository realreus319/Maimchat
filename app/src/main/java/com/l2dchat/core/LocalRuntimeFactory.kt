package com.l2dchat.core

import com.google.gson.Gson
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.NoopMotionController
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.BackgroundReplierPromptContextProvider
import com.l2dchat.core.reply.CompositePlannerPromptContextProvider
import com.l2dchat.core.reply.EmptyPlannerPromptContextProvider
import com.l2dchat.core.reply.EmptyPlannerSystemPromptProvider
import com.l2dchat.core.reply.JsonFallbackPlannerTriggerProcessor
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerPromptBuilder
import com.l2dchat.core.reply.PlannerSessionStore
import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerTriggerProcessor
import com.l2dchat.core.reply.ToolCallingPlannerTriggerProcessor
import com.l2dchat.core.reply.PlannerPromptContextProvider
import com.l2dchat.core.storage.RoomPlannerPromptContextProvider
import com.l2dchat.core.storage.RuntimeStateDao
import com.l2dchat.core.tools.EmptyReplierPromptContextProvider
import com.l2dchat.core.tools.LlmReplierTaskGenerator
import com.l2dchat.core.tools.LocalToolRegistryFactory
import com.l2dchat.core.tools.NoopReplierTaskStore
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierTaskManager
import com.l2dchat.core.tools.ReplierTaskStore
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

data class LocalRuntimeLlmConfig(
        val plannerClient: LlmClient,
        val plannerConfig: LlmGenerationConfig,
        val replierClient: LlmClient = plannerClient,
        val replierConfig: LlmGenerationConfig = plannerConfig,
        val nativeToolCalling: Boolean = true,
        val environmentRepliesEnabled: Boolean = true
)

object LocalRuntimeFactory {
    fun create(
            scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
            llmConfig: LocalRuntimeLlmConfig? = null,
            perceptionStoreFactory: (RoutingKey) -> PerceptionStore? = { null },
            plannerSessionStoreFactory: (RoutingKey) -> PlannerSessionStore = {
                NoopPlannerSessionStore
            },
            plannerSystemPromptProvider: PlannerSystemPromptProvider =
                    EmptyPlannerSystemPromptProvider,
            decisionPlannerSystemPromptProvider: PlannerSystemPromptProvider =
                    EmptyPlannerSystemPromptProvider,
            replierPromptContextProvider: ReplierPromptContextProvider =
                    EmptyReplierPromptContextProvider,
            replierTaskStore: ReplierTaskStore = NoopReplierTaskStore,
            environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
            motionController: MotionController = NoopMotionController,
            runtimeStateDaoProvider: () -> RuntimeStateDao? = { null },
            extraNormalTools: List<com.l2dchat.core.tools.Tool> = emptyList(),
            backgroundStatusProvider: PlannerPromptContextProvider = EmptyPlannerPromptContextProvider,
            gson: Gson = Gson()
    ): LocalChatRuntime {
        if (llmConfig == null) {
            return LocalChatRuntime(
                    scope = scope,
                    perceptionStoreFactory = perceptionStoreFactory,
                    plannerSessionStoreFactory = plannerSessionStoreFactory
            )
        }

        val taskSequence = AtomicInteger()
        val taskManager =
                ReplierTaskManager(
                        scope = scope,
                        generator =
                                LlmReplierTaskGenerator(
                                        llmClient = llmConfig.replierClient,
                                        config = llmConfig.replierConfig,
                                        contextProvider = replierPromptContextProvider
                                ),
                        taskStore = replierTaskStore
                )
        val taskIdFactory: (ToolExecutionContext) -> String = { context ->
            "replier_" +
                    listOf(
                                    context.routingKey.contextId,
                                    context.routingKey.agentId,
                                    context.trigger.messageId,
                                    context.foregroundEpoch.toString(),
                                    taskSequence.incrementAndGet().toString()
                            )
                            .joinToString("_") { it.taskIdPart() }
        }

        return LocalChatRuntime(
                scope = scope,
                environmentRepliesEnabled = llmConfig.environmentRepliesEnabled,
                perceptionStoreFactory = perceptionStoreFactory,
                plannerSessionStoreFactory = plannerSessionStoreFactory,
                plannerProcessorFactory = {
                    llmConfig.buildProcessor(
                            registry =
                                    LocalToolRegistryFactory.normalRegistry(
                                            gson = gson,
                                            environmentStateProvider = environmentStateProvider,
                                            motionController = motionController,
                                            runtimeStateDao = runtimeStateDaoProvider(),
                                            replierTaskManager = taskManager,
                                            replierTaskIdFactory = taskIdFactory,
                                            extraTools = extraNormalTools
                                    ),
                            mode = ToolExecutionMode.NORMAL,
                            promptBuilder =
                                    PlannerPromptBuilder(
                                            contextProvider =
                                                    CompositePlannerPromptContextProvider(
                                                            listOf(
                                                                    backgroundStatusProvider,
                                                                    memoryPlannerContextProvider(
                                                                            runtimeStateDaoProvider()
                                                                    )
                                                            )
                                                    )
                                    ),
                            systemPromptProvider = plannerSystemPromptProvider
                    )
                },
                decisionPlannerProcessorFactory = {
                    val memoryProvider = memoryPlannerContextProvider(runtimeStateDaoProvider())
                    llmConfig.buildProcessor(
                            registry =
                                    LocalToolRegistryFactory.decisionRegistry(
                                            taskManager = taskManager,
                                            gson = gson
                                    ),
                            mode = ToolExecutionMode.DECISION,
                            promptBuilder =
                                    PlannerPromptBuilder(
                                            contextProvider =
                                                    CompositePlannerPromptContextProvider(
                                                            listOf(
                                                                    backgroundStatusProvider,
                                                                    BackgroundReplierPromptContextProvider(
                                                                            taskManager
                                                                    ),
                                                                    memoryProvider
                                                            )
                                                    )
                                    ),
                            systemPromptProvider = decisionPlannerSystemPromptProvider
                    )
                }
        )
    }

    private fun LocalRuntimeLlmConfig.buildProcessor(
            registry: ToolRegistry,
            mode: ToolExecutionMode,
            promptBuilder: PlannerPromptBuilder,
            systemPromptProvider: PlannerSystemPromptProvider
    ): PlannerTriggerProcessor =
            if (nativeToolCalling) {
                ToolCallingPlannerTriggerProcessor(
                        llmClient = plannerClient,
                        config = plannerConfig,
                        toolRegistry = registry,
                        promptBuilder = promptBuilder,
                        systemPromptProvider = systemPromptProvider,
                        toolMode = mode
                )
            } else {
                JsonFallbackPlannerTriggerProcessor(
                        llmClient = plannerClient,
                        config = plannerConfig,
                        toolRegistry = registry,
                        promptBuilder = promptBuilder,
                        systemPromptProvider = systemPromptProvider,
                        toolMode = mode
                )
            }
}

private fun memoryPlannerContextProvider(
        stateDao: RuntimeStateDao?
): PlannerPromptContextProvider =
        stateDao?.let { RoomPlannerPromptContextProvider(it) } ?: EmptyPlannerPromptContextProvider

private fun String.taskIdPart(): String =
        trim()
                .ifBlank { "unknown" }
                .replace(Regex("[^A-Za-z0-9_.:-]+"), "_")
