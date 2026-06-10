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
import com.l2dchat.core.reply.EmptyPlannerSystemPromptProvider
import com.l2dchat.core.reply.JsonFallbackPlannerTriggerProcessor
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerPromptBuilder
import com.l2dchat.core.reply.PlannerSessionStore
import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.PlannerTriggerProcessor
import com.l2dchat.core.reply.ToolCallingPlannerTriggerProcessor
import com.l2dchat.core.tools.EmptyReplierPromptContextProvider
import com.l2dchat.core.tools.LlmReplierTaskGenerator
import com.l2dchat.core.tools.LocalToolRegistryFactory
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierTaskManager
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
        val nativeToolCalling: Boolean = true
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
            environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
            motionController: MotionController = NoopMotionController,
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
                                )
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
                perceptionStoreFactory = perceptionStoreFactory,
                plannerSessionStoreFactory = plannerSessionStoreFactory,
                plannerProcessorFactory = {
                    llmConfig.buildProcessor(
                            registry =
                                    LocalToolRegistryFactory.normalRegistry(
                                            gson = gson,
                                            environmentStateProvider = environmentStateProvider,
                                            motionController = motionController,
                                            replierTaskManager = taskManager,
                                            replierTaskIdFactory = taskIdFactory
                                    ),
                            mode = ToolExecutionMode.NORMAL,
                            promptBuilder = PlannerPromptBuilder(),
                            systemPromptProvider = plannerSystemPromptProvider
                    )
                },
                decisionPlannerProcessorFactory = {
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
                                                    BackgroundReplierPromptContextProvider(
                                                            taskManager
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

private fun String.taskIdPart(): String =
        trim()
                .ifBlank { "unknown" }
                .replace(Regex("[^A-Za-z0-9_.:-]+"), "_")
