package com.l2dchat.core.tools

import com.google.gson.JsonObject
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.trigger.Trigger

data class ToolExecutionContext(
        val loopId: String,
        val routingKey: RoutingKey,
        val trigger: Trigger,
        val foregroundEpoch: Int,
        val roundId: String = "round_${trigger.messageId}",
        val mode: ToolExecutionMode = ToolExecutionMode.NORMAL
)

enum class ToolExecutionMode {
    NORMAL,
    DECISION
}

data class ToolExecutionResult(
        val llmContent: String,
        val replyText: String? = null,
        val sent: Boolean = false,
        val isError: Boolean = false,
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(llmContent.isNotBlank()) { "Tool result content must not be blank" }
        require(replyText == null || replyText.isNotBlank()) {
            "Tool replyText must not be blank"
        }
    }
}

interface Tool {
    val definition: LlmToolDefinition

    val allowedModes: Set<ToolExecutionMode>
        get() = setOf(ToolExecutionMode.NORMAL)

    suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult
}
