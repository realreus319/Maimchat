package com.l2dchat.core.inbound

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.GroupInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class InboundBuilderTest {
    private val builder = InboundBuilder()

    @Test
    fun `fromMessageBase derives routing and normalizes seglist blocks`() {
        val message =
                buildMessage(
                        segment =
                                Seg(
                                        "seglist",
                                        listOf(
                                                Seg("text", "看这里 [image1]"),
                                                Seg("image", "file:///tmp/a.png"),
                                                Seg("emoji", "smile"),
                                                Seg("voice", "voice-bytes")
                                        )
                                ),
                        rawMessage = "看这里 [image1]"
                )

        val inbound = builder.fromMessageBase(message)

        assertEquals("group-id", inbound.routingKey.contextId)
        assertEquals("bot-id", inbound.routingKey.agentId)
        assertEquals("user-id", inbound.senderId)
        assertEquals("Alice", inbound.senderName)
        assertEquals("看这里 [image1]", inbound.text)
        assertTrue(inbound.isGroupChat)
        assertEquals(listOf("text", "image_url", "emoji", "voice"), inbound.contentBlocks.map { it.type })
        assertEquals("file:///tmp/a.png", inbound.contentBlocks[1].imageUrl)
        assertEquals("smile", inbound.contentBlocks[2].data)
        assertEquals("voice-bytes", inbound.contentBlocks[3].data)
        assertTrue(builder.validateMultimodalMirror(inbound.text, inbound.contentBlocks).isValid)
    }

    @Test
    fun `parseRoom follows backend private and group room rules`() {
        val privateRoute = builder.parseRoom("private:user-1:agent-1")
        val groupRoute = builder.parseRoom("group:room-1")

        assertEquals("user-1", privateRoute.contextId)
        assertEquals("agent-1", privateRoute.agentId)
        assertFalse(privateRoute.isGroupChat)
        assertEquals("room-1", groupRoute.contextId)
        assertNull(groupRoute.agentId)
        assertTrue(groupRoute.isGroupChat)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `parseRoom rejects unknown room format`() {
        builder.parseRoom("room-1")
    }

    private fun buildMessage(
            segment: Seg = Seg("text", "hello"),
            rawMessage: String = "hello"
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    messageId = "message-id",
                                    time = 1000.0,
                                    senderInfo =
                                            SenderInfo(
                                                    groupInfo =
                                                            GroupInfo(
                                                                    groupId = "group-id"
                                                            ),
                                                    userInfo =
                                                            UserInfo(
                                                                    userId = "user-id",
                                                                    userNickname = "Alice"
                                                            )
                                            ),
                                    receiverInfo =
                                            ReceiverInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    userId = "bot-id",
                                                                    userNickname = "Bot"
                                                            )
                                            )
                            ),
                    messageSegment = segment,
                    rawMessage = rawMessage
            )
}
