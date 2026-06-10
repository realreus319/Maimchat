package com.l2dchat.core.reply

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolRegistry

class ToolCallingPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val toolRegistry: ToolRegistry,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder()
) : PlannerTriggerProcessor {
    init {
        require(toolRegistry.definitions.isNotEmpty()) {
            "ToolCallingPlannerTriggerProcessor requires at least one tool"
        }
    }

    override suspend fun process(context: PlannerTurnContext) {
        var replySent = false
        val toolContext =
                ToolExecutionContext(
                        loopId = context.loopId,
                        routingKey = context.routingKey,
                        trigger = context.trigger,
                        foregroundEpoch = context.foregroundEpoch
                )
        val response =
                llmClient.chatCompletionWithTools(
                        messages = promptBuilder.buildMessages(context),
                        tools = toolRegistry.definitions,
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
            val result = context.sendReply(response.text)
            if (result.status == ReplySendStatus.BLANK_REJECTED) {
                throw IllegalStateException(
                        "LLM tool loop did not send a replier result or final assistant text"
                )
            }
        }
    }
}
