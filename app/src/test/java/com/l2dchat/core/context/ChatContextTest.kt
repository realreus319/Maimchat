package com.l2dchat.core.context

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.message.VisibleMessageRecord
import com.l2dchat.core.storage.ChatHistoryStore
import com.l2dchat.core.storage.RuntimeStateDao
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatContextTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `conversation history is chronological and marks assistant messages`() = runBlocking {
        val context =
                ChatContext(
                        routingKey = routingKey,
                        historyStore =
                                FakeHistoryStore(
                                        recentStandardMessages =
                                                listOf(
                                                        message(
                                                                id = "msg-2",
                                                                timeSeconds = 2.0,
                                                                senderId = "agent-a",
                                                                senderName = "Mai",
                                                                text = "hello Alice"
                                                        ),
                                                        message(
                                                                id = "msg-1",
                                                                timeSeconds = 1.0,
                                                                senderId = "alice",
                                                                senderName = "Alice",
                                                                text = "hi"
                                                        )
                                                )
                                ),
                        stateDao = FakeRuntimeStateDao()
                )

        val history = context.getConversationHistory(limit = 2)

        assertEquals(listOf("msg-1", "msg-2"), history.map { it.messageId })
        assertEquals("hi", history[0].text)
        assertEquals("Alice", history[0].senderDisplayName)
        assertFalse(history[0].isAssistant)
        assertTrue(history[1].isAssistant)
    }

    @Test
    fun `trigger history falls back to recent messages when anchor is missing`() = runBlocking {
        val context =
                ChatContext(
                        routingKey = routingKey,
                        historyStore =
                                FakeHistoryStore(
                                        historyUntilMessage = emptyList(),
                                        recentStandardMessages =
                                                listOf(
                                                        message(
                                                                id = "msg-2",
                                                                timeSeconds = 2.0,
                                                                senderId = "alice",
                                                                senderName = "Alice",
                                                                text = "second"
                                                        ),
                                                        message(
                                                                id = "msg-1",
                                                                timeSeconds = 1.0,
                                                                senderId = "alice",
                                                                senderName = "Alice",
                                                                text = "first"
                                                        )
                                                )
                                ),
                        stateDao = FakeRuntimeStateDao()
                )

        val history = context.getConversationHistoryForTrigger("missing", limit = 10)

        assertEquals(listOf("msg-1", "msg-2"), history.map { it.messageId })
    }

    @Test
    fun `queries memories impressions mood and templates in routing scope`() = runBlocking {
        val stateDao =
                FakeRuntimeStateDao(
                        agentConfig =
                                AgentConfigEntity(
                                        agentId = "agent-a",
                                        displayName = "Mai",
                                        persona = "local persona",
                                        provider = null,
                                        model = null,
                                        settingsJson = null,
                                        updatedAtMillis = 10L
                                ),
                        templates =
                                listOf(
                                        PromptTemplateEntity(
                                                templateId = "tpl-1",
                                                agentId = "agent-a",
                                                name = "planner_system",
                                                body = "system",
                                                updatedAtMillis = 20L
                                        )
                                ),
                        memories =
                                mutableListOf(
                                        memory("mem-1", "room-a", "agent-a", "Alice likes coffee", 0.7),
                                        memory("mem-2", "room-a", "agent-a", "Alice likes tea", 0.9),
                                        memory("mem-3", "other", "agent-a", "ignore", 1.0)
                                ),
                        impressions =
                                listOf(
                                        ImpressionEntity(
                                                impressionId = "imp-1",
                                                contextId = "room-a",
                                                agentId = "agent-a",
                                                subjectId = "alice",
                                                content = "trusted user",
                                                updatedAtMillis = 30L
                                        )
                                ),
                        moodState =
                                MoodStateEntity(
                                        contextId = "room-a",
                                        agentId = "agent-a",
                                        valence = 0.4,
                                        arousal = 0.2,
                                        stateJson = null,
                                        updatedAtMillis = 40L
                                )
                )
        val context =
                ChatContext(
                        routingKey = routingKey,
                        historyStore = FakeHistoryStore(),
                        stateDao = stateDao
                )

        assertEquals("Mai", context.getAgentConfig()?.displayName)
        assertEquals("system", context.getPromptTemplate("planner_system")?.body)
        assertEquals(listOf("mem-2", "mem-1"), context.getMemories(limit = 5).map { it.memoryId })
        assertEquals(listOf("mem-1"), context.searchMemories(" coffee ", limit = 5).map { it.memoryId })
        assertEquals("trusted user", context.getImpression("alice")?.content)
        assertEquals(listOf("imp-1"), context.getImpressions(limit = 5).map { it.impressionId })
        assertEquals(0.4, context.getMoodState()?.valence ?: 0.0, 0.0)
    }

    private fun message(
            id: String,
            timeSeconds: Double,
            senderId: String,
            senderName: String,
            text: String
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test",
                                    messageId = id,
                                    time = timeSeconds,
                                    senderInfo =
                                            SenderInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform = "test",
                                                                    userId = senderId,
                                                                    userNickname = senderName
                                                            )
                                            )
                            ),
                    messageSegment = Seg("seglist", listOf(Seg("text", text))),
                    rawMessage = null
            )

    private fun memory(
            id: String,
            contextId: String,
            agentId: String,
            content: String,
            importance: Double
    ): MemoryEntity =
            MemoryEntity(
                    memoryId = id,
                    contextId = contextId,
                    agentId = agentId,
                    content = content,
                    importance = importance,
                    createdAtMillis = 1L,
                    updatedAtMillis = id.takeLast(1).toLong()
            )

    private class FakeHistoryStore(
            private val recentStandardMessages: List<MessageBase> = emptyList(),
            private val historyUntilMessage: List<MessageBase> = recentStandardMessages,
            private val visibleMessages: List<VisibleMessageRecord> = emptyList()
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
        ): List<VisibleMessageRecord> = visibleMessages.take(limit)

        override suspend fun queryRecentStandardMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<MessageBase> = recentStandardMessages.take(limit)

        override suspend fun queryStandardHistoryUntilMessage(
                contextId: String,
                agentId: String?,
                messageId: String,
                limit: Int
        ): List<MessageBase> = historyUntilMessage.take(limit)
    }

    private class FakeRuntimeStateDao(
            private val agentConfig: AgentConfigEntity? = null,
            private val templates: List<PromptTemplateEntity> = emptyList(),
            private val memories: MutableList<MemoryEntity> = mutableListOf(),
            private val impressions: List<ImpressionEntity> = emptyList(),
            private val moodState: MoodStateEntity? = null
    ) : RuntimeStateDao {
        override suspend fun upsertAgentConfig(config: AgentConfigEntity) = Unit

        override suspend fun upsertPromptTemplate(template: PromptTemplateEntity) = Unit

        override suspend fun upsertMemory(memory: MemoryEntity) = Unit

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
                templates.firstOrNull { it.name == name && (it.agentId == agentId || it.agentId == null) }

        override suspend fun queryPromptTemplateById(templateId: String): PromptTemplateEntity? =
                templates.firstOrNull { it.templateId == templateId }

        override suspend fun queryMemories(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<MemoryEntity> =
                memories
                        .filter { it.contextId == contextId && it.agentId == agentId }
                        .sortedByMemoryRelevance()
                        .take(limit)

        override suspend fun countMemories(contextId: String, agentId: String): Int =
                memories.count { it.contextId == contextId && it.agentId == agentId }

        override suspend fun queryMemoryByContent(
                contextId: String,
                agentId: String,
                content: String
        ): MemoryEntity? =
                memories.firstOrNull {
                    it.contextId == contextId && it.agentId == agentId && it.content == content
                }

        override suspend fun reinforceMemory(
                memoryId: String,
                accessCount: Int,
                lastAccessMillis: Long
        ) {
            val index = memories.indexOfFirst { it.memoryId == memoryId }
            if (index >= 0) {
                memories[index] = memories[index].copy(accessCount = accessCount, lastAccessMillis = lastAccessMillis)
            }
        }

        override suspend fun deleteMemory(memoryId: String) {
            memories.removeAll { it.memoryId == memoryId }
        }

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
    }
}

private fun List<MemoryEntity>.sortedByMemoryRelevance(): List<MemoryEntity> =
        sortedWith(
                compareByDescending<MemoryEntity> { it.importance }
                        .thenByDescending { it.updatedAtMillis }
        )
