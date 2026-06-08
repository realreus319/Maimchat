package com.l2dchat.core.storage

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.core.message.StandardMessageEntity
import com.l2dchat.core.message.VisibleMessageEntity
import com.l2dchat.core.message.VisibleMessageRecord
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test

class RoomChatHistoryStoreTest {
    @Test
    fun `appends and queries visible messages through dao`() {
        val dao = FakeRuntimeMessageDao()
        val store = RoomChatHistoryStore(dao)

        runBlocking {
            store.appendVisibleMessage(
                    contextId = "ctx",
                    agentId = "agent",
                    message =
                            VisibleMessageRecord(
                                    messageId = "visible-1",
                                    content = "hello",
                                    isFromUser = true,
                                    timestampMillis = 100L
                            )
            )
        }

        val recent =
                runBlocking {
                    store.queryRecentVisibleMessages(contextId = "ctx", agentId = "agent", limit = 10)
                }

        assertEquals(listOf("visible-1"), recent.map { it.messageId })
        assertEquals("hello", recent.single().content)
    }

    @Test
    fun `appends and queries standard messages through dao`() {
        val dao = FakeRuntimeMessageDao()
        val store = RoomChatHistoryStore(dao)

        runBlocking {
            store.appendStandardMessage(
                    contextId = "ctx",
                    agentId = "agent",
                    message = message("msg-1", 1.0, "first")
            )
            store.appendStandardMessage(
                    contextId = "ctx",
                    agentId = "agent",
                    message = message("msg-2", 2.0, "second")
            )
        }

        val recent =
                runBlocking {
                    store.queryRecentStandardMessages(contextId = "ctx", agentId = "agent", limit = 1)
                }
        val untilSecond =
                runBlocking {
                    store.queryStandardHistoryUntilMessage(
                            contextId = "ctx",
                            agentId = "agent",
                            messageId = "msg-2",
                            limit = 10
                    )
                }

        assertEquals(listOf("msg-2"), recent.map { it.messageInfo.messageId })
        assertEquals(listOf("msg-2", "msg-1"), untilSecond.map { it.messageInfo.messageId })
    }

    private fun message(id: String, timeSeconds: Double, raw: String): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test",
                                    messageId = id,
                                    time = timeSeconds
                            ),
                    messageSegment = Seg("text", raw),
                    rawMessage = raw
            )

    private class FakeRuntimeMessageDao : RuntimeMessageDao {
        private val visibleMessages = mutableListOf<VisibleMessageEntity>()
        private val standardMessages = mutableListOf<StandardMessageEntity>()

        override suspend fun appendMessage(message: VisibleMessageEntity) {
            if (visibleMessages.none { it.messageId == message.messageId }) {
                visibleMessages.add(message)
            }
        }

        override suspend fun appendStandardMessage(message: StandardMessageEntity) {
            if (standardMessages.none { it.messageId == message.messageId }) {
                standardMessages.add(message)
            }
        }

        override suspend fun queryRecentMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<VisibleMessageEntity> =
                visibleMessages
                        .filter { it.contextId == contextId && (agentId == null || it.agentId == agentId) }
                        .sortedByDescending { it.timestampMillis }
                        .take(limit)

        override suspend fun queryRecentStandardMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<StandardMessageEntity> =
                standardMessages
                        .filter { it.contextId == contextId && (agentId == null || it.agentId == agentId) }
                        .sortedByDescending { it.timestampMillis }
                        .take(limit)

        override suspend fun queryStandardHistoryUntilMessage(
                contextId: String,
                agentId: String?,
                messageId: String,
                limit: Int
        ): List<StandardMessageEntity> {
            val anchor =
                    standardMessages.firstOrNull {
                        it.contextId == contextId &&
                                (agentId == null || it.agentId == agentId) &&
                                it.messageId == messageId
                    }
                            ?: return emptyList()
            return standardMessages
                    .filter {
                        it.contextId == contextId &&
                                (agentId == null || it.agentId == agentId) &&
                                it.timestampMillis <= anchor.timestampMillis
                    }
                    .sortedByDescending { it.timestampMillis }
                    .take(limit)
        }
    }
}
