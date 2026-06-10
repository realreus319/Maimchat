package com.l2dchat.core.tools

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmStreamEvent
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

class LlmReplierTaskGenerator(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val promptBuilder: ReplierPromptBuilder = ReplierPromptBuilder()
) : ReplierTaskGenerator {
    override fun generate(request: ReplierTaskRequest): Flow<ReplierTaskUpdate> =
            flow {
                llmClient.chatCompletionStream(
                                messages = promptBuilder.buildMessages(request),
                                config = config
                        )
                        .collect { event ->
                            when (event) {
                                is LlmStreamEvent.TextDelta -> {
                                    if (event.text.isNotEmpty()) {
                                        emit(ReplierTaskUpdate.TextDelta(event.text))
                                    }
                                }
                                is LlmStreamEvent.Completed -> {
                                    event.response.text.trim().takeIf { it.isNotBlank() }?.let {
                                        emit(ReplierTaskUpdate.Completed(it))
                                    }
                                }
                                is LlmStreamEvent.ToolCallArgumentsDelta,
                                is LlmStreamEvent.ToolCallStarted -> {
                                    throw IllegalStateException(
                                            "Replier generation does not support tool calls"
                                    )
                                }
                            }
                        }
            }
}
