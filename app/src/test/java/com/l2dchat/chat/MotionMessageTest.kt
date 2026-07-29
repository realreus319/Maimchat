package com.l2dchat.chat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MotionMessageTest {
    @Test
    fun `motion command round trips through message json`() {
        val command = MotionCommand(group = "TapBody", index = 1, loop = true)
        val message =
                MessageBase(
                        messageInfo =
                                BaseMessageInfo(
                                        messageId = "motion-1",
                                        additionalConfig = MotionMessage.additionalConfig(command)
                                ),
                        messageSegment = Seg("text", MotionMessage.displayText(command)),
                        rawMessage = MotionMessage.displayText(command)
                )

        val restored = MessageBase.fromJsonString(message.toJsonString())
        val parsed = MotionMessage.parse(restored)

        assertEquals(command, parsed)
        assertEquals("motion", restored.messageInfo.additionalConfig?.get("message_type"))
        assertEquals(1, (restored.messageInfo.additionalConfig?.get("motion_index") as Number).toInt())
    }

    @Test
    fun `file path motion command is parsed from primitive config`() {
        val command = MotionCommand(filePath = "mao/wave.motion3.json", loop = false)
        val message =
                MessageBase(
                        messageInfo =
                                BaseMessageInfo(
                                        additionalConfig = MotionMessage.additionalConfig(command)
                                ),
                        messageSegment = Seg("text", MotionMessage.displayText(command))
                )

        assertEquals(command, MotionMessage.parse(MessageBase.fromJsonString(message.toJsonString())))
    }

    @Test
    fun `non motion message returns null`() {
        val message =
                MessageBase(
                        messageInfo =
                                BaseMessageInfo(additionalConfig = mapOf("message_type" to "chat")),
                        messageSegment = Seg("text", "hello")
                )

        assertNull(MotionMessage.parse(message))
    }
}
