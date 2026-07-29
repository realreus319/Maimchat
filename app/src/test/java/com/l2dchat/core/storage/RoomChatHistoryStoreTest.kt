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

    @Test
    fun `clears only scoped room history`() {
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
            store.appendVisibleMessage(
                    contextId = "ctx",
                    agentId = "other",
                    message =
                            VisibleMessageRecord(
                                    messageId = "visible-2",
                                    content = "keep",
                                    isFromUser = false,
                                    timestampMillis = 200L
                            )
            )
            store.appendStandardMessage(
                    contextId = "ctx",
                    agentId = "agent",
                    message = message("msg-1", 1.0, "first")
            )

            store.clearHistory(contextId = "ctx", agentId = "agent")
        }

        val clearedVisible =
                runBlocking {
                    store.queryRecentVisibleMessages(contextId = "ctx", agentId = "agent", limit = 10)
                }
        val preservedVisible =
                runBlocking {
                    store.queryRecentVisibleMessages(contextId = "ctx", agentId = "other", limit = 10)
                }
        val clearedStandard =
                runBlocking {
                    store.queryRecentStandardMessages(contextId = "ctx", agentId = "agent", limit = 10)
                }

        assertEquals(emptyList<VisibleMessageRecord>(), clearedVisible)
        assertEquals(listOf("visible-2"), preservedVisible.map { it.messageId })
        assertEquals(emptyList<MessageBase>(), clearedStandard)
    }

    private fun message(id: String, timeSeconds: Double, raw: String): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
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

        override suspend fun deleteMessages(contextId: String, agentId: String?) {
            visibleMessages.removeAll {
                it.contextId == contextId && (agentId == null || it.agentId == agentId)
            }
        }

        override suspend fun deleteStandardMessages(contextId: String, agentId: String?) {
            standardMessages.removeAll {
                it.contextId == contextId && (agentId == null || it.agentId == agentId)
            }
        }

        override suspend fun deleteAllMessages() {
            visibleMessages.clear()
        }

        override suspend fun deleteAllStandardMessages() {
            standardMessages.clear()
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
