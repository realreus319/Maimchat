package com.l2dchat.core.reply

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry

class ToolCallingPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val toolRegistry: ToolRegistry,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder(),
        private val systemPromptProvider: PlannerSystemPromptProvider =
                EmptyPlannerSystemPromptProvider,
        private val toolMode: ToolExecutionMode = ToolExecutionMode.NORMAL
) : PlannerTriggerProcessor {
    init {
        require(toolRegistry.definitionsFor(toolMode).isNotEmpty()) {
            "ToolCallingPlannerTriggerProcessor requires at least one ${toolMode.name.lowercase()} tool"
        }
    }

    override suspend fun process(context: PlannerTurnContext) {
        var replySent = false
        val toolDefinitions = toolRegistry.definitionsFor(toolMode)
        val toolContext =
                ToolExecutionContext(
                        loopId = context.loopId,
                        routingKey = context.routingKey,
                        trigger = context.trigger,
                        foregroundEpoch = context.foregroundEpoch,
                        roundId = context.roundId,
                        mode = toolMode
                )
        val response =
                llmClient.chatCompletionWithTools(
                        messages =
                                promptBuilder.buildMessages(
                                        context = context,
                                        systemPromptOverride =
                                                systemPromptProvider.systemPromptFor(context)
                                ),
                        tools = toolDefinitions,
                        config = config,
                        toolExecutor =
                                LlmToolExecutor { toolCall ->
                                    val execution = toolRegistry.execute(toolContext, toolCall)
                                    if (!execution.result.isError && execution.result.replyText != null) {
                                        val sendResult =
                                                context.sendReply(execution.result.replyText)
                                        replySent = replySent || sendResult.sent
                                    }
                                    execution.toLlmToolResult()
                                }
                )

        if (!replySent) {
            var finalText = response.text
            if (finalText.isBlank()) {
                // The planner ended its tool loop without calling the replier or emitting any
                // text. This is far more likely with chain-of-thought disabled, where the model
                // sometimes stops after a non-replier tool call. Force one tool-free completion so
                // the user still gets a reply instead of the runtime erroring out and locking input.
                finalText =
                        llmClient.chatCompletion(
                                        messages =
                                                promptBuilder.buildMessages(
                                                        context = context,
                                                        systemPromptOverride =
                                                                systemPromptProvider.systemPromptFor(
                                                                        context
                                                                )
                                                ) +
                                                        LlmMessage.system(
                                                                "现在直接以当前角色的口吻回复用户，不要再调用任何工具，只输出一条回复正文。"
                                                        ),
                                        config = config
                                )
                                .text
            }
            val result = context.sendReply(finalText)
            if (result.status == ReplySendStatus.BLANK_REJECTED) {
                throw IllegalStateException(
                        "LLM planner produced no reply even after a forced direct completion"
                )
            }
        }
    }
}
