package com.l2dchat.core.storage

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.GroupInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.inbound.InboundBuilder
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.StandardMessageEntity
import com.l2dchat.core.message.VisibleMessageEntity
import com.l2dchat.core.perception.PerceptionProcessor
import com.l2dchat.core.trigger.TriggerPriority
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test

class RoomPerceptionStoreTest {
    @Test
    fun `processAndPersist stores standard message and ordered media blocks`() {
        val messageDao = FakeRuntimeMessageDao()
        val mediaStore = FakeMediaBlockStore()
        val store = RoomPerceptionStore(messageDao, mediaStore) { 999L }
        val inbound =
                InboundBuilder()
                        .fromMessageBase(
                                buildMessage(
                                        rawText = "看这里 [image1]",
                                        segment =
                                                Seg(
                                                        "seglist",
                                                        listOf(
                                                                Seg("text", "看这里 [image1]"),
                                                                Seg("image", "file:///tmp/a.png"),
                                                                Seg("emoji", "smile"),
                                                                Seg("voice", "voice-bytes")
                                                        )
                                                )
                                )
                        )

        val result =
                runBlocking {
                    PerceptionProcessor().processAndPersist(message = inbound, store = store)
                }

        assertEquals(TriggerPriority.NORMAL, result.trigger.priority)
        assertEquals(listOf("message-id"), messageDao.standardMessages.map { it.messageId })
        assertEquals("group-id", messageDao.standardMessages.single().contextId)
        assertEquals("bot-id", messageDao.standardMessages.single().agentId)
        assertEquals("看这里 [image1]", messageDao.standardMessages.single().rawText)
        assertEquals(listOf("image_url", "emoji", "voice"), mediaStore.mediaBlocks.map { it.type })
        assertEquals(listOf(1, 2, 3), mediaStore.mediaBlocks.map { it.sequence })
        assertEquals("file:///tmp/a.png", mediaStore.mediaBlocks[0].uri)
        assertEquals("smile", mediaStore.mediaBlocks[1].uri)
        assertEquals("voice-bytes", mediaStore.mediaBlocks[2].uri)
        assertEquals(listOf("image/*", "image/*", "audio/*"), mediaStore.mediaBlocks.map { it.mimeType })
    }

    private fun buildMessage(rawText: String, segment: Seg): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test_platform",
                                    messageId = "message-id",
                                    time = 1000.0,
                                    senderInfo =
                                            SenderInfo(
                                                    groupInfo =
                                                            GroupInfo(
                                                                    platform = "test_platform",
                                                                    groupId = "group-id"
                                                            ),
                                                    userInfo =
                                                            UserInfo(
                                                                    platform = "test_platform",
                                                                    userId = "user-id",
                                                                    userNickname = "Alice"
                                                            )
                                            ),
                                    receiverInfo =
                                            ReceiverInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform = "test_platform",
                                                                    userId = "bot-id",
                                                                    userNickname = "Bot"
                                                            )
                                            )
                            ),
                    messageSegment = segment,
                    rawMessage = rawText
            )

    private class FakeRuntimeMessageDao : RuntimeMessageDao {
        val standardMessages = mutableListOf<StandardMessageEntity>()

        override suspend fun appendMessage(message: VisibleMessageEntity) = Unit

        override suspend fun appendStandardMessage(message: StandardMessageEntity) {
            if (standardMessages.none { it.messageId == message.messageId }) {
                standardMessages.add(message)
            }
        }

        override suspend fun deleteMessages(contextId: String, agentId: String?) = Unit

        override suspend fun deleteStandardMessages(contextId: String, agentId: String?) = Unit

        override suspend fun queryRecentMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<VisibleMessageEntity> = emptyList()

        override suspend fun queryRecentStandardMessages(
                contextId: String,
                agentId: String?,
                limit: Int
        ): List<StandardMessageEntity> = standardMessages.take(limit)

        override suspend fun queryStandardHistoryUntilMessage(
                contextId: String,
                agentId: String?,
                messageId: String,
                limit: Int
        ): List<StandardMessageEntity> = standardMessages.filter { it.messageId == messageId }.take(limit)
    }

    private class FakeMediaBlockStore : MediaBlockStore {
        val mediaBlocks = mutableListOf<MediaBlockEntity>()

        override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) {
            mediaBlocks.removeAll { it.mediaId == mediaBlock.mediaId }
            mediaBlocks.add(mediaBlock)
        }
    }
}
