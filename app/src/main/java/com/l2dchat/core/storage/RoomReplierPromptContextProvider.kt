package com.l2dchat.core.storage

import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.chat.UserInfo
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
        val contextId = routingKey.contextId
        val agentConfig = stateDao.queryAgentConfig(agentId)
        val systemTemplate = stateDao.queryPromptTemplate(REPLIER_SYSTEM_TEMPLATE, agentId)
        val userTemplate = stateDao.queryPromptTemplate(REPLIER_USER_TEMPLATE, agentId)
        val subjectId = subjectIdFor(request)

        val historyMessages =
                queryHistory(request)
                        .asReversed()
                        .filterNot { it.messageInfo.messageId == request.trigger.messageId }
                        .mapNotNull { it.toPromptHistoryMessage(agentId, agentConfig) }
        val memoryText =
                stateDao.queryMemories(contextId = contextId, agentId = agentId, limit = memoryLimit)
                        .mapNotNull { it.content.trim().takeIf { content -> content.isNotBlank() } }
                        .joinToString("\n") { "- $it" }
                        .takeIf { it.isNotBlank() }
        val impressionText =
                subjectId?.let {
                    stateDao.queryImpression(
                                    contextId = contextId,
                                    agentId = agentId,
                                    subjectId = it
                            )
                            ?.content
                            ?.trim()
                            ?.takeIf { content -> content.isNotBlank() }
                }
        val moodText =
                stateDao.queryMoodState(contextId = contextId, agentId = agentId)
                        ?.toPromptText()

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

    private suspend fun queryHistory(request: ReplierTaskRequest): List<MessageBase> {
        val contextId = request.routingKey.contextId
        val agentId = request.routingKey.agentId
        val byAnchor =
                historyStore.queryStandardHistoryUntilMessage(
                        contextId = contextId,
                        agentId = agentId,
                        messageId = request.trigger.messageId,
                        limit = historyLimit
                )
        return byAnchor.ifEmpty {
            historyStore.queryRecentStandardMessages(
                    contextId = contextId,
                    agentId = agentId,
                    limit = historyLimit
            )
        }
    }

    private fun MessageBase.toPromptHistoryMessage(
            agentId: String,
            agentConfig: AgentConfigEntity?
    ): ReplierPromptHistoryMessage? {
        val text = promptText().takeIf { it.isNotBlank() } ?: return null
        val sender = messageInfo.senderInfo?.userInfo
        val assistant = sender?.userId?.takeIf { it.isNotBlank() } == agentId
        return ReplierPromptHistoryMessage(
                text = text,
                senderName =
                        if (assistant) {
                            sender.displayName()
                                    ?: agentConfig?.displayName?.trim()?.takeIf { it.isNotBlank() }
                        } else {
                            sender.displayName()
                        },
                isAssistant = assistant
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
