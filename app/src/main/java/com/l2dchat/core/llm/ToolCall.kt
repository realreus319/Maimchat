package com.l2dchat.core.llm

private val TOOL_NAME_PATTERN = Regex("^[A-Za-z_][A-Za-z0-9_\\-.]{0,63}$")

data class LlmToolDefinition(
        val name: String,
        val description: String,
        val parameters: Map<String, Any?> = OBJECT_PARAMETERS
) {
    init {
        require(name.matches(TOOL_NAME_PATTERN)) { "Invalid LLM tool name: $name" }
        require(description.isNotBlank()) { "LLM tool description must not be blank" }
        require(parameters.isNotEmpty()) { "LLM tool parameters schema must not be empty" }
    }

    companion object {
        val OBJECT_PARAMETERS: Map<String, Any?> =
                mapOf("type" to "object", "properties" to emptyMap<String, Any>())
    }
}

data class LlmToolCall(
        val id: String,
        val name: String,
        val argumentsJson: String = "{}"
) {
    init {
        require(id.isNotBlank()) { "LLM tool call id must not be blank" }
        require(name.matches(TOOL_NAME_PATTERN)) { "Invalid LLM tool call name: $name" }
        require(argumentsJson.isNotBlank()) { "LLM tool call arguments must not be blank" }
    }
}

data class LlmToolResult(
        val toolCallId: String,
        val name: String,
        val content: String,
        val isError: Boolean = false
) {
    init {
        require(toolCallId.isNotBlank()) { "LLM tool result id must not be blank" }
        require(name.matches(TOOL_NAME_PATTERN)) { "Invalid LLM tool result name: $name" }
    }
}

enum class LlmToolChoice {
    AUTO,
    NONE,
    REQUIRED
}
