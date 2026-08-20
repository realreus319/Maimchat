package com.l2dchat.core.tools

data class ReplierPromptContext(
        val systemPrompt: String? = null,
        val userPromptTemplate: String? = null,
        val personaPrompt: String? = null,
        val moodState: String? = null,
        val impressionText: String? = null,
        val historyMessages: List<ReplierPromptHistoryMessage> = emptyList(),
        val currentTimeText: String? = null,
        val agentDisplayName: String? = null
)

data class ReplierPromptHistoryMessage(
        val text: String,
        val senderName: String? = null,
        val isAssistant: Boolean = false
)

interface ReplierPromptContextProvider {
    suspend fun contextFor(request: ReplierTaskRequest): ReplierPromptContext
}

object EmptyReplierPromptContextProvider : ReplierPromptContextProvider {
    override suspend fun contextFor(request: ReplierTaskRequest): ReplierPromptContext =
            ReplierPromptContext()
}
