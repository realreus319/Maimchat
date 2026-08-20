package com.l2dchat.core.context

import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.message.VisibleMessageRecord
import com.l2dchat.core.storage.ChatHistoryStore
import com.l2dchat.core.storage.RuntimeStateDao

class ChatContext(
        val routingKey: RoutingKey,
        private val historyStore: ChatHistoryStore,
        private val stateDao: RuntimeStateDao
) {
    suspend fun getConversationHistory(limit: Int = DEFAULT_HISTORY_LIMIT): List<ChatContextMessage> =
            historyStore
                    .queryRecentStandardMessages(
                            contextId = routingKey.contextId,
                            agentId = routingKey.agentId,
                            limit = positiveLimit(limit)
                    )
                    .toChronologicalContextMessages(routingKey.agentId)

    suspend fun getConversationHistoryUntilMessage(
            messageId: String?,
            limit: Int = DEFAULT_HISTORY_LIMIT
    ): List<ChatContextMessage> {
        val anchorMessageId = messageId?.trim().orEmpty()
        require(anchorMessageId.isNotBlank()) { "messageId must not be blank" }
        val messages =
                historyStore.queryStandardHistoryUntilMessage(
                        contextId = routingKey.contextId,
                        agentId = routingKey.agentId,
                        messageId = anchorMessageId,
                        limit = positiveLimit(limit)
                )
        return messages.toChronologicalContextMessages(routingKey.agentId)
    }

    suspend fun getConversationHistoryForTrigger(
            triggerMessageId: String?,
            limit: Int = DEFAULT_HISTORY_LIMIT
    ): List<ChatContextMessage> {
        val anchorMessageId = triggerMessageId?.trim()
        val messages =
                if (anchorMessageId.isNullOrBlank()) {
                    historyStore.queryRecentStandardMessages(
                            contextId = routingKey.contextId,
                            agentId = routingKey.agentId,
                            limit = positiveLimit(limit)
                    )
                } else {
                    historyStore
                            .queryStandardHistoryUntilMessage(
                                    contextId = routingKey.contextId,
                                    agentId = routingKey.agentId,
                                    messageId = anchorMessageId,
                                    limit = positiveLimit(limit)
                            )
                            .ifEmpty {
                                historyStore.queryRecentStandardMessages(
                                        contextId = routingKey.contextId,
                                        agentId = routingKey.agentId,
                                        limit = positiveLimit(limit)
                                )
                            }
                }
        return messages.toChronologicalContextMessages(routingKey.agentId)
    }

    suspend fun getRecentVisibleMessages(limit: Int = DEFAULT_HISTORY_LIMIT): List<VisibleMessageRecord> =
            historyStore
                    .queryRecentVisibleMessages(
                            contextId = routingKey.contextId,
                            agentId = routingKey.agentId,
                            limit = positiveLimit(limit)
                    )
                    .asReversed()

    suspend fun getAgentConfig(): AgentConfigEntity? =
            stateDao.queryAgentConfig(routingKey.agentId)

    suspend fun getPromptTemplate(name: String): PromptTemplateEntity? {
        val normalizedName = name.trim()
        require(normalizedName.isNotBlank()) { "prompt template name must not be blank" }
        return stateDao.queryPromptTemplate(normalizedName, routingKey.agentId)
    }

    suspend fun getImpression(subjectId: String): ImpressionEntity? {
        val normalizedSubjectId = subjectId.trim()
        require(normalizedSubjectId.isNotBlank()) { "subjectId must not be blank" }
        return stateDao.queryImpression(
                contextId = routingKey.contextId,
                agentId = routingKey.agentId,
                subjectId = normalizedSubjectId
        )
    }

    suspend fun getImpressions(limit: Int = DEFAULT_IMPRESSION_LIMIT): List<ImpressionEntity> =
            stateDao.queryImpressions(
                    contextId = routingKey.contextId,
                    agentId = routingKey.agentId,
                    limit = positiveLimit(limit)
            )

    suspend fun getMoodState(): MoodStateEntity? =
            stateDao.queryMoodState(
                    contextId = routingKey.contextId,
                    agentId = routingKey.agentId
            )

    private fun positiveLimit(limit: Int): Int {
        require(limit > 0) { "limit must be positive" }
        return limit
    }

    private fun List<MessageBase>.toChronologicalContextMessages(
            agentId: String
    ): List<ChatContextMessage> =
            asReversed().map { it.toContextMessage(agentId) }

    companion object {
        const val DEFAULT_HISTORY_LIMIT: Int = 20
        const val DEFAULT_IMPRESSION_LIMIT: Int = 5
    }
}

data class ChatContextMessage(
        val messageId: String?,
        val timestampMillis: Long?,
        val senderUserId: String?,
        val senderDisplayName: String?,
        val text: String,
        val isAssistant: Boolean,
        val message: MessageBase
)

private fun MessageBase.toContextMessage(agentId: String): ChatContextMessage {
    val info = messageInfo
    val sender = info.senderInfo?.userInfo ?: info.userInfo
    val senderUserId = sender?.userId?.trim()?.takeIf { it.isNotBlank() }
    return ChatContextMessage(
            messageId = info.messageId?.trim()?.takeIf { it.isNotBlank() },
            timestampMillis = info.time?.takeIf { it > 0.0 }?.let { (it * 1000).toLong() },
            senderUserId = senderUserId,
            senderDisplayName = sender.displayName(),
            text = promptText(),
            isAssistant = senderUserId == agentId,
            message = this
    )
}

private fun MessageBase.promptText(): String =
        rawMessage?.trim()?.takeIf { it.isNotBlank() }
                ?: messageSegment.flattenText().trim()

private fun Seg.flattenText(): String =
        when (type) {
            "seglist" -> {
                @Suppress("UNCHECKED_CAST")
                val segments = data as? List<Seg> ?: return ""
                segments.joinToString("\n") { it.flattenText() }
            }
            "text" -> data.toString()
            else -> data.toString()
        }

private fun UserInfo?.displayName(): String? =
        this?.userCardname?.trim()?.takeIf { it.isNotBlank() }
                ?: this?.userNickname?.trim()?.takeIf { it.isNotBlank() }
                ?: this?.userId?.trim()?.takeIf { it.isNotBlank() }
