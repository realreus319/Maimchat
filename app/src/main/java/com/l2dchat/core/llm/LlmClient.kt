package com.l2dchat.core.llm

import kotlinx.coroutines.flow.Flow

data class LlmGenerationConfig(
        val model: String,
        val temperature: Double? = null,
        val maxTokens: Int? = null,
        val timeoutMillis: Long? = null,
        val toolChoice: LlmToolChoice = LlmToolChoice.AUTO,
        val maxToolRounds: Int = 4,
        // Controls the DashScope/Qwen `enable_thinking` request flag. null = don't send it (use the
        // provider default); false = disable chain-of-thought for much lower latency.
        val enableThinking: Boolean? = null,
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(model.isNotBlank()) { "LLM model must not be blank" }
        require(temperature == null || temperature in 0.0..2.0) {
            "LLM temperature must be between 0 and 2"
        }
        require(maxTokens == null || maxTokens > 0) { "LLM maxTokens must be positive" }
        require(timeoutMillis == null || timeoutMillis > 0) {
            "LLM timeoutMillis must be positive"
        }
        require(maxToolRounds >= 0) { "LLM maxToolRounds must be non-negative" }
    }
}

data class LlmTokenUsage(
        val inputTokens: Int,
        val outputTokens: Int,
        val cachedInputTokens: Int = 0
) {
    init {
        require(inputTokens >= 0) { "LLM inputTokens must be non-negative" }
        require(outputTokens >= 0) { "LLM outputTokens must be non-negative" }
        require(cachedInputTokens >= 0) { "LLM cachedInputTokens must be non-negative" }
    }

    val totalTokens: Int
        get() = inputTokens + outputTokens
}

enum class LlmFinishReason {
    STOP,
    TOOL_CALLS,
    LENGTH,
    CANCELLED,
    ERROR
}

data class LlmResponse(
        val message: LlmMessage,
        val model: String,
        val id: String? = null,
        val usage: LlmTokenUsage? = null,
        val finishReason: LlmFinishReason = LlmFinishReason.STOP,
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(model.isNotBlank()) { "LLM response model must not be blank" }
        require(id == null || id.isNotBlank()) { "LLM response id must not be blank" }
    }

    val text: String
        get() = message.textContent()

    val toolCalls: List<LlmToolCall>
        get() = message.toolCalls
}

sealed interface LlmStreamEvent {
    data class TextDelta(val text: String) : LlmStreamEvent

    data class ToolCallStarted(val toolCall: LlmToolCall) : LlmStreamEvent

    data class ToolCallArgumentsDelta(
            val toolCallId: String,
            val argumentsJsonDelta: String
    ) : LlmStreamEvent

    data class Completed(val response: LlmResponse) : LlmStreamEvent
}

fun interface LlmToolExecutor {
    suspend fun execute(toolCall: LlmToolCall): LlmToolResult
}

interface LlmClient {
    suspend fun chatCompletion(
            messages: List<LlmMessage>,
            config: LlmGenerationConfig
    ): LlmResponse

    fun chatCompletionStream(
            messages: List<LlmMessage>,
            config: LlmGenerationConfig
    ): Flow<LlmStreamEvent>

    suspend fun chatCompletionWithTools(
            messages: List<LlmMessage>,
            tools: List<LlmToolDefinition>,
            config: LlmGenerationConfig,
            toolExecutor: LlmToolExecutor
    ): LlmResponse
}
