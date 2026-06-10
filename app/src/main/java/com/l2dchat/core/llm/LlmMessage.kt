package com.l2dchat.core.llm

enum class LlmMessageRole(val wireValue: String) {
    SYSTEM("system"),
    USER("user"),
    ASSISTANT("assistant"),
    TOOL("tool")
}

sealed interface LlmContentPart {
    val type: String
}

data class LlmTextPart(val text: String) : LlmContentPart {
    override val type: String = "text"
}

data class LlmImageUrlPart(
        val url: String,
        val detail: String? = null
) : LlmContentPart {
    override val type: String = "image_url"
}

data class LlmMessage(
        val role: LlmMessageRole,
        val content: List<LlmContentPart> = emptyList(),
        val name: String? = null,
        val toolCallId: String? = null,
        val toolCalls: List<LlmToolCall> = emptyList(),
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(name == null || name.isNotBlank()) { "LLM message name must not be blank" }
        require(toolCallId == null || toolCallId.isNotBlank()) {
            "LLM tool message id must not be blank"
        }
        require(toolCalls.isEmpty() || role == LlmMessageRole.ASSISTANT) {
            "Only assistant messages can contain tool calls"
        }
        require(toolCallId == null || role == LlmMessageRole.TOOL) {
            "Only tool messages can contain a tool call id"
        }
    }

    fun textContent(): String =
            content.filterIsInstance<LlmTextPart>().joinToString(separator = "") { it.text }

    fun isBlankAssistantResponse(): Boolean =
            role == LlmMessageRole.ASSISTANT && textContent().isBlank() && toolCalls.isEmpty()

    companion object {
        fun system(text: String, metadata: Map<String, Any?> = emptyMap()): LlmMessage =
                text(role = LlmMessageRole.SYSTEM, text = text, metadata = metadata)

        fun user(text: String, metadata: Map<String, Any?> = emptyMap()): LlmMessage =
                text(role = LlmMessageRole.USER, text = text, metadata = metadata)

        fun assistant(text: String, metadata: Map<String, Any?> = emptyMap()): LlmMessage =
                text(role = LlmMessageRole.ASSISTANT, text = text, metadata = metadata)

        fun assistantToolCalls(
                toolCalls: List<LlmToolCall>,
                text: String? = null,
                metadata: Map<String, Any?> = emptyMap()
        ): LlmMessage =
                LlmMessage(
                        role = LlmMessageRole.ASSISTANT,
                        content =
                                text?.takeIf { it.isNotBlank() }?.let { listOf(LlmTextPart(it)) }
                                        ?: emptyList(),
                        toolCalls = toolCalls,
                        metadata = metadata
                )

        fun toolResult(result: LlmToolResult): LlmMessage =
                LlmMessage(
                        role = LlmMessageRole.TOOL,
                        content = listOf(LlmTextPart(result.content)),
                        name = result.name,
                        toolCallId = result.toolCallId,
                        metadata = mapOf("is_error" to result.isError)
                )

        fun text(
                role: LlmMessageRole,
                text: String,
                name: String? = null,
                metadata: Map<String, Any?> = emptyMap()
        ): LlmMessage =
                LlmMessage(
                        role = role,
                        content = listOf(LlmTextPart(text)),
                        name = name,
                        metadata = metadata
                )
    }
}
