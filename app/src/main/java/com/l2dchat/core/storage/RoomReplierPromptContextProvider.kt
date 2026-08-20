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
        val impressionText =
                chatContext
                        .getImpression(subjectId)
                        ?.content
                        ?.trim()
                        ?.takeIf { content -> content.isNotBlank() }
        val moodText = chatContext.getMoodState()?.toPromptText(clockMillis())

        return ReplierPromptContext(
                systemPrompt = systemTemplate?.body?.trim()?.takeIf { it.isNotBlank() },
                userPromptTemplate = userTemplate?.body?.trim()?.takeIf { it.isNotBlank() },
                personaPrompt = agentConfig.toPersonaPrompt(),
                moodState = moodText,
                impressionText = impressionText,
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

    private fun MoodStateEntity.toPromptText(now: Long): String? {
        val (decayedValence, decayedArousal) =
                com.l2dchat.core.tools.decayedMood(valence, arousal, updatedAtMillis, now)
        // A faded mood (low arousal after decay) carries no useful signal.
        if (stateJson.isNullOrBlank() && kotlin.math.abs(decayedValence) < 0.05 && decayedArousal < 0.05) {
            return null
        }
        return stateJson?.trim()?.takeIf { it.isNotBlank() }
                ?: "valence=${decayedValence.formatMoodValue()}, arousal=${decayedArousal.formatMoodValue()}"
    }

    private fun Double.formatMoodValue(): String = String.format(Locale.ROOT, "%.2f", this)

    companion object {
        private const val DEFAULT_HISTORY_LIMIT = 12
        private const val REPLIER_SYSTEM_TEMPLATE = "replier_system"
        private const val REPLIER_USER_TEMPLATE = "replier_user"

        // Impressions are keyed on the single canonical local-user subject (matching the
        // write path in StateTools), not the transport-level sender_id channel constant.
        private fun subjectIdFor(request: ReplierTaskRequest): String =
                com.l2dchat.core.tools.CANONICAL_SUBJECT_ID

        private fun defaultTimeText(millis: Long): String =
                SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.ROOT).format(Date(millis))
    }
}
