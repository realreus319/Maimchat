package com.l2dchat.core.reply

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig

class LlmPlannerTriggerProcessor(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val promptBuilder: PlannerPromptBuilder = PlannerPromptBuilder(),
        private val systemPromptProvider: PlannerSystemPromptProvider =
                EmptyPlannerSystemPromptProvider
) : PlannerTriggerProcessor {
    override suspend fun process(context: PlannerTurnContext) {
        val response =
                llmClient.chatCompletion(
                        messages =
                                promptBuilder.buildMessages(
                                        context = context,
                                        systemPromptOverride =
                                                systemPromptProvider.systemPromptFor(context)
                                ),
                        config = config
                )
        if (response.toolCalls.isNotEmpty()) {
            throw UnsupportedOperationException(
                    "LLM tool calls require the native tool-calling planner processor"
            )
        }
        val result = context.sendReply(response.text)
        if (result.status == ReplySendStatus.BLANK_REJECTED) {
            throw IllegalStateException("LLM response did not contain final assistant text")
        }
    }
}
