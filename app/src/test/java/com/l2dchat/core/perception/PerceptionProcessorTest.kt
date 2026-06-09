package com.l2dchat.core.perception

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.inbound.InboundBuilder
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PerceptionProcessorTest {
    private val inboundBuilder = InboundBuilder()
    private val processor = PerceptionProcessor()

    @Test
    fun `process parses command mention and creates high priority message trigger`() {
        val inbound =
                inboundBuilder.fromMessageBase(
                        buildMessage("/draw style=\"ink wash\" size=large @Maimai")
                )

        val result = processor.process(inbound)

        assertEquals("message-id", result.parsedMessage.messageId)
        assertEquals("user-id", result.parsedMessage.senderId)
        assertTrue(result.parsedMessage.isCommand)
        assertEquals("draw", result.parsedMessage.commandName)
        assertEquals("ink wash", result.parsedMessage.commandArgs["style"])
        assertEquals("large", result.parsedMessage.commandArgs["size"])
        assertTrue(result.parsedMessage.isMentioned)
        assertEquals(TriggerType.MSG, result.trigger.triggerType)
        assertEquals(TriggerPriority.HIGH, result.trigger.priority)
        assertEquals("group-id:bot-id", result.trigger.loopId())
        assertEquals("Maimai", result.parsedMessage.mentionedNames.single())
    }

    @Test
    fun `process carries content blocks into trigger payload`() {
        val inbound =
                inboundBuilder.fromMessageBase(
                        buildMessage(
                                "看图 [image1]",
                                Seg(
                                        "seglist",
                                        listOf(
                                                Seg("text", "看图 [image1]"),
                                                Seg("image", "file:///tmp/p.png")
                                        )
                                )
                        )
                )

        val result = processor.process(inbound)

        @Suppress("UNCHECKED_CAST")
        val blocks = result.trigger.payload["content_blocks"] as List<Map<String, Any>>
        assertEquals(2, blocks.size)
        assertEquals("text", blocks[0]["type"])
        assertEquals("image_url", blocks[1]["type"])
    }

    private fun buildMessage(
            text: String,
            segment: Seg = Seg("text", text)
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test_platform",
                                    messageId = "message-id",
                                    time = 1000.0,
                                    senderInfo =
                                            SenderInfo(
                                                    groupInfo =
                                                            com.l2dchat.chat.GroupInfo(
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
                                                                    userNickname = "Maimai"
                                                            )
                                            )
                            ),
                    messageSegment = segment,
                    rawMessage = text
            )
}
