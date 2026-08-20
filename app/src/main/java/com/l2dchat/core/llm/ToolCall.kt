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
        val isError: Boolean = false,
        /**
         * Optional multimodal content parts that accompany this tool result. When non-null and
         * containing non-text parts (e.g. [LlmImageUrlPart]), the LLM client emits a synthetic
         * user message carrying these parts immediately after the tool message — the OpenAI tool
         * message wire format only supports a string `content`, so image bytes cannot ride on the
         * tool message itself. Text-only results leave this null (the common case).
         *
         * Used by the host planner's `ltm_read_media` post-processing (T5): a photo result's bytes
         * are injected as an `image_url` block so the VLM sees the picture the path points to.
         */
        val contentParts: List<LlmContentPart>? = null
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
