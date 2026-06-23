package com.l2dchat.core.tools

import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmStreamEvent
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

class LlmReplierTaskGenerator(
        private val llmClient: LlmClient,
        private val config: LlmGenerationConfig,
        private val promptBuilder: ReplierPromptBuilder = ReplierPromptBuilder(),
        private val contextProvider: ReplierPromptContextProvider =
                EmptyReplierPromptContextProvider
) : ReplierTaskGenerator {
    override fun generate(request: ReplierTaskRequest): Flow<ReplierTaskUpdate> =
            flow {
                val promptContext = contextProvider.contextFor(request)
                llmClient.chatCompletionStream(
                                messages = promptBuilder.buildMessages(request, promptContext),
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
                                    // The replier is a text-only stage. If the model emits a
                                    // stray tool call, ignore it rather than failing the whole
                                    // reply; any accompanying text deltas still produce a reply.
                                }
                            }
                        }
            }
}
