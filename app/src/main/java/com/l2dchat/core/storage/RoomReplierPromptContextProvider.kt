package com.l2dchat.core.storage

import com.l2dchat.core.context.ChatContext
import com.l2dchat.core.context.ChatContextMessage
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.tools.ReplierPromptContext
import com.l2dchat.core.tools.ReplierPromptContextProvider
import com.l2dchat.core.tools.ReplierPromptHistoryMessage
import com.l2dchat.core.tools.ReplierTaskRequest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class RoomReplierPromptContextProvider(
        private val historyStore: ChatHistoryStore,
        private val stateDao: RuntimeStateDao,
        private val historyLimit: Int = DEFAULT_HISTORY_LIMIT,
        private val memoryLimit: Int = DEFAULT_MEMORY_LIMIT,
        private val clockMillis: () -> Long = { System.currentTimeMillis() },
        private val timeFormatter: (Long) -> String = ::defaultTimeText
) : ReplierPromptContextProvider {
    override suspend fun contextFor(request: ReplierTaskRequest): ReplierPromptContext {
        val routingKey = request.routingKey
        val agentId = routingKey.agentId
        val chatContext = ChatContext(routingKey, historyStore, stateDao)
        val agentConfig = chatContext.getAgentConfig()
        val systemTemplate = chatContext.getPromptTemplate(REPLIER_SYSTEM_TEMPLATE)
        val userTemplate = chatContext.getPromptTemplate(REPLIER_USER_TEMPLATE)
        val subjectId = subjectIdFor(request)

        val historyMessages =
                chatContext
                        .getConversationHistoryForTrigger(
                                triggerMessageId = request.trigger.messageId,
                                limit = historyLimit
                        )
                        .filterNot { it.messageId == request.trigger.messageId }
                        .mapNotNull { it.toPromptHistoryMessage(agentId, agentConfig) }
        val memoryText =
                chatContext
                        .getMemories(limit = memoryLimit)
                        .mapNotNull { it.content.trim().takeIf { content -> content.isNotBlank() } }
                        .joinToString("\n") { "- $it" }
                        .takeIf { it.isNotBlank() }
        val impressionText =
                subjectId?.let {
                    chatContext
                            .getImpression(it)
                            ?.content
                            ?.trim()
                            ?.takeIf { content -> content.isNotBlank() }
                }
        val moodText = chatContext.getMoodState()?.toPromptText()

        return ReplierPromptContext(
                systemPrompt = systemTemplate?.body?.trim()?.takeIf { it.isNotBlank() },
                userPromptTemplate = userTemplate?.body?.trim()?.takeIf { it.isNotBlank() },
                personaPrompt = agentConfig.toPersonaPrompt(),
                moodState = moodText,
                impressionText = impressionText,
                memoryText = memoryText,
                historyMessages = historyMessages,
                currentTimeText = timeFormatter(clockMillis()),
                agentDisplayName = agentConfig?.displayName?.trim()?.takeIf { it.isNotBlank() }
        )
    }

    private fun ChatContextMessage.toPromptHistoryMessage(
            agentId: String,
            agentConfig: AgentConfigEntity?
    ): ReplierPromptHistoryMessage? {
        val text = text.takeIf { it.isNotBlank() } ?: return null
        val assistant = isAssistant || senderUserId == agentId
        return ReplierPromptHistoryMessage(
                text = text,
                senderName =
                        if (assistant) {
                            senderDisplayName
                                    ?: agentConfig?.displayName?.trim()?.takeIf { it.isNotBlank() }
                        } else {
                            senderDisplayName
                        },
                isAssistant = assistant
        )
    }

    private fun AgentConfigEntity?.toPersonaPrompt(): String? {
        if (this == null) return null
        return persona?.trim()?.takeIf { it.isNotBlank() }
                ?: displayName.trim().takeIf { it.isNotBlank() }?.let { "你是$it。" }
    }

    private fun MoodStateEntity.toPromptText(): String? =
            stateJson?.trim()?.takeIf { it.isNotBlank() }
                    ?: "valence=${valence.formatMoodValue()}, arousal=${arousal.formatMoodValue()}"

    private fun Double.formatMoodValue(): String = String.format(Locale.ROOT, "%.2f", this)

    companion object {
        private const val DEFAULT_HISTORY_LIMIT = 12
        private const val DEFAULT_MEMORY_LIMIT = 5
        private const val REPLIER_SYSTEM_TEMPLATE = "replier_system"
        private const val REPLIER_USER_TEMPLATE = "replier_user"

        private fun subjectIdFor(request: ReplierTaskRequest): String? =
                request.trigger.payload["sender_id"]?.toString()?.trim()?.takeIf { it.isNotBlank() }

        private fun defaultTimeText(millis: Long): String =
                SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.ROOT).format(Date(millis))
    }
}
