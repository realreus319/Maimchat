package com.l2dchat.core.message

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RuntimeMessageMapperTest {
    @Test
    fun `standard entity preserves query fields and json roundtrip`() {
        val message = buildMessage()

        val entity =
                RuntimeMessageMapper.toStandardEntity(
                        message = message,
                        contextId = "ctx-1",
                        agentId = "agent-1",
                        fallbackTimestampMillis = 999L
                )

        assertEquals("msg-1", entity.messageId)
        assertEquals("ctx-1", entity.contextId)
        assertEquals("agent-1", entity.agentId)
        assertEquals("test_platform", entity.platform)
        assertEquals("user-id", entity.senderUserId)
        assertEquals("bot-id", entity.receiverUserId)
        assertEquals(1234L, entity.timestampMillis)
        assertEquals("hello", entity.rawText)
        val restored = RuntimeMessageMapper.toStandardMessage(entity)
        assertEquals("msg-1", restored.messageInfo.messageId)
        assertEquals("test_platform", restored.messageInfo.platform)
        assertEquals("user-id", restored.messageInfo.senderInfo?.userInfo?.userId)
        assertEquals("bot-id", restored.messageInfo.receiverInfo?.userInfo?.userId)
        assertEquals("text", restored.messageSegment.type)
        assertEquals("hello", restored.messageSegment.data)
        assertEquals("hello", restored.rawMessage)
    }

    @Test
    fun `standard entity falls back to stable id and timestamp`() {
        val message =
                buildMessage(
                        messageId = null,
                        timeSeconds = null,
                )

        val first =
                RuntimeMessageMapper.toStandardEntity(
                        message,
                        contextId = "ctx",
                        agentId = null,
                        fallbackTimestampMillis = 42L
                )
        val second =
                RuntimeMessageMapper.toStandardEntity(
                        message,
                        contextId = "ctx",
                        agentId = null,
                        fallbackTimestampMillis = 43L
                )

        assertTrue(first.messageId.startsWith("standard_"))
        assertEquals(first.messageId, second.messageId)
        assertEquals(42L, first.timestampMillis)
    }

    @Test
    fun `visible record roundtrips through entity`() {
        val record =
                VisibleMessageRecord(
                        messageId = "visible-1",
                        content = "wave",
                        isFromUser = false,
                        timestampMillis = 100L,
                        motionGroup = "TapBody",
                        motionIndex = 1,
                        motionLoop = true
                )

        val entity = RuntimeMessageMapper.toVisibleEntity(record, contextId = "ctx", agentId = "agent")
        val restored = RuntimeMessageMapper.toVisibleRecord(entity)

        assertEquals("ctx", entity.contextId)
        assertEquals("agent", entity.agentId)
        assertEquals(record, restored)
    }

    private fun buildMessage(
            messageId: String? = "msg-1",
            timeSeconds: Double? = 1.234
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test_platform",
                                    messageId = messageId,
                                    time = timeSeconds,
                                    senderInfo =
                                            SenderInfo(
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
                    messageSegment = Seg("text", "hello"),
                    rawMessage = "hello"
            )
}
