package com.l2dchat.core.storage

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.message.VisibleMessageRecord
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RoomReplierPromptContextProviderTest {
    @Test
    fun `provider builds replier prompt context from room state and history`() {
        val historyStore =
                FakeHistoryStore(
                        historyUntil =
                                listOf(
                                        message(
                                                id = "current",
                                                text = "现在的问题",
                                                senderId = "user-1",
                                                senderName = "Alice",
                                                timeSeconds = 3.0
                                        ),
                                        message(
                                                id = "assistant-1",
                                                text = "你好，我是小倩。",
                                                senderId = "agent-a",
                                                senderName = "小倩",
                                                timeSeconds = 2.0
                                        ),
                                        message(
                                                id = "user-1",
                                                text = "你好",
                                                senderId = "user-1",
                                                senderName = "Alice",
                                                timeSeconds = 1.0
                                        )
                                )
                )
        val stateDao =
                FakeRuntimeStateDao(
                        agentConfig =
                                AgentConfigEntity(
                                        agentId = "agent-a",
                                        displayName = "小倩",
                                        persona = "你是小倩。",
                                        provider = null,
                                        model = null,
                                        settingsJson = null,
                                        updatedAtMillis = 1L
                                ),
                        templates =
                                listOf(
                                        PromptTemplateEntity(
                                                templateId = "global",
                                                agentId = null,
                                                name = "replier_user",
                                                body = "global template",
                                                updatedAtMillis = 1L
                                        ),
                                        PromptTemplateEntity(
                                                templateId = "agent",
                                                agentId = "agent-a",
                                                name = "replier_user",
                                                body = "agent template",
                                                updatedAtMillis = 1L
                                        ),
                                        PromptTemplateEntity(
                                                templateId = "system",
                                                agentId = "agent-a",
                                                name = "replier_system",
                                                body = "system template",
                                                updatedAtMillis = 1L
                                        )
                                ),
                        impressions =
                                listOf(
                                        ImpressionEntity(
                                                impressionId = "imp-1",
                                                contextId = "room-a",
                                                agentId = "agent-a",
                                                // Provider now reads the canonical local-user subject.
                                                subjectId = com.l2dchat.core.tools.CANONICAL_SUBJECT_ID,
                                                content = "Alice 是熟悉的用户。",
                                                updatedAtMillis = 1L
                                        )
                                ),
                        moodState =
                                MoodStateEntity(
                                        contextId = "room-a",
                                        agentId = "agent-a",
                                        valence = 0.25,
                                        arousal = 0.5,
                                        stateJson = null,
                                        // Fresh (matches the test clock) so recency-decay keeps it intact.
                                        updatedAtMillis = 1_812_340_800_000L
                                )
                )
        val provider =
                RoomReplierPromptContextProvider(
                        historyStore = historyStore,
                        stateDao = stateDao,
                        clockMillis = { 1_812_340_800_000L },
                        timeFormatter = { "2027-06-10 12:00:00" }
                )

        val context = runBlocking { provider.contextFor(request()) }

        assertEquals("system template", context.systemPrompt)
        assertEquals("agent template", context.userPromptTemplate)
        assertEquals("你是小倩。", context.personaPrompt)
        assertEquals("valence=0.25, arousal=0.50", context.moodState)
        assertEquals("Alice 是熟悉的用户。", context.impressionText)
        assertEquals("2027-06-10 12:00:00", context.currentTimeText)
        assertEquals(listOf("你好", "你好，我是小倩。"), context.historyMessages.map { it.text })
        assertFalse(context.historyMessages.first().isAssistant)
        assertTrue(context.historyMessages.last().isAssistant)
        assertEquals("小倩", context.agentDisplayName)
    }

    private fun request(): ReplierTaskRequest =
            ReplierTaskRequest(
                    taskId = "task-1",
                    routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a"),
                    trigger =
                            Trigger(
                                    contextId = "room-a",
                                    agentId = "agent-a",
                                    messageId = "current",
                                    triggerType = TriggerType.MSG,
                                    priority = TriggerPriority.NORMAL,
                                    timestampSeconds = 3.0,
                                    payload = mapOf("text" to "现在的问题", "sender_id" to "user-1")
                            ),
                    thinking = "planner thoughts"
            )

    private fun message(
            id: String,
            text: String,
            senderId: String,
            senderName: String,
            timeSeconds: Double
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    messageId = id,
                                    time = timeSeconds,
                                    senderInfo =
                                            SenderInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    userId = senderId,
                                                                    userNickname = senderName
                                                            )
                                            )
                            ),
                    messageSegment = Seg("text", text),
                    rawMessage = text
            )

    private class FakeHistoryStore(
            private val historyUntil: List<MessageBase> = emptyList(),
            private val recentMessages: List<MessageBase> = emptyList()
    ) : ChatHistoryStore {
        override suspend fun appendVisibleMessage(
                contextId: String,
                agentId: String?,
                message: VisibleMessageRecord
        ) = Unit

        override suspend fun appendStandardMessage(
                contextId: String,
                agentId: String?,
                message: MessageBase,
                fallbackTimestampMillis: Long
        ) = Unit

        override suspend fun clearHistory(contextId: String, agentId: String?) = Unit

        override suspend fun queryRecentVisibleMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<VisibleMessageRecord> = emptyList()

        override suspend fun queryRecentStandardMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<MessageBase> = recentMessages.take(limit)

        override suspend fun queryStandardHistoryUntilMessage(
                contextId: String,
                agentId: String?,
                messageId: String,
                limit: Int
        ): List<MessageBase> = historyUntil.take(limit)
    }

    private class FakeRuntimeStateDao(
            private val agentConfig: AgentConfigEntity? = null,
            private val templates: List<PromptTemplateEntity> = emptyList(),
            impressions: List<ImpressionEntity> = emptyList(),
            moodState: MoodStateEntity? = null
    ) : RuntimeStateDao {
        private val impressions = impressions.toMutableList()
        private var moodState = moodState

        override suspend fun upsertAgentConfig(config: AgentConfigEntity) = Unit

        override suspend fun upsertPromptTemplate(template: PromptTemplateEntity) = Unit

        override suspend fun upsertImpression(impression: ImpressionEntity) = Unit

        override suspend fun upsertMoodState(moodState: MoodStateEntity) = Unit

        override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) = Unit

        override suspend fun queryMediaBlocksForMessage(messageId: String): List<MediaBlockEntity> =
                emptyList()

        override suspend fun queryAgentConfig(agentId: String): AgentConfigEntity? =
                agentConfig?.takeIf { it.agentId == agentId }

        override suspend fun queryPromptTemplate(
                name: String,
                agentId: String
        ): PromptTemplateEntity? =
                templates
                        .filter { it.name == name && (it.agentId == agentId || it.agentId == null) }
                        .sortedWith(
                                compareBy<PromptTemplateEntity> {
                                                if (it.agentId == agentId) 0 else 1
                                            }
                                            .thenByDescending { it.updatedAtMillis }
                        )
                        .firstOrNull()

        override suspend fun queryPromptTemplateById(templateId: String): PromptTemplateEntity? =
                templates.firstOrNull { it.templateId == templateId }

        override suspend fun deleteAllImpressions() {
            impressions.clear()
        }

        override suspend fun deleteAllMoodState() {
            moodState = null
        }

        override suspend fun deleteAllMediaBlocks() = Unit

        override suspend fun queryImpression(
                contextId: String,
                agentId: String,
                subjectId: String
        ): ImpressionEntity? =
                impressions.firstOrNull {
                    it.contextId == contextId && it.agentId == agentId && it.subjectId == subjectId
                }

        override suspend fun queryImpressions(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<ImpressionEntity> =
                impressions
                        .filter { it.contextId == contextId && it.agentId == agentId }
                        .sortedByDescending { it.updatedAtMillis }
                        .take(limit)

        override suspend fun queryMoodState(contextId: String, agentId: String): MoodStateEntity? =
                moodState?.takeIf { it.contextId == contextId && it.agentId == agentId }

        override fun observeMoodState(
                contextId: String,
                agentId: String
        ): kotlinx.coroutines.flow.Flow<MoodStateEntity?> =
                kotlinx.coroutines.flow.flowOf(
                        moodState?.takeIf { it.contextId == contextId && it.agentId == agentId }
                )
    }
}
