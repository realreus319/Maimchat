package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PlannerPromptBuilderTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `builder turns trigger into system and user llm messages`() {
        val messages =
                PlannerPromptBuilder(systemPrompt = "system").buildMessages(
                        context = turnContext(trigger("msg-1", text = "hello"))
                )

        assertEquals(LlmMessageRole.SYSTEM, messages[0].role)
        assertEquals("system", messages[0].textContent())
        assertEquals(LlmMessageRole.USER, messages[1].role)
        assertTrue(messages[1].textContent().contains("[msg_trigger]"))
        assertTrue(messages[1].textContent().contains("hello"))
        assertEquals("msg", messages[1].metadata["trigger_type"])
        assertEquals("msg-1", messages[1].metadata["message_id"])
    }

    @Test
    fun `builder keeps multimodal image blocks`() {
        val messages =
                PlannerPromptBuilder(systemPrompt = "system").buildMessages(
                        context =
                                turnContext(
                                        trigger(
                                                "msg-1",
                                                text = "[image1] describe this",
                                                contentBlocks =
                                                        listOf(
                                                                mapOf(
                                                                        "type" to "image_url",
                                                                        "image_url" to
                                                                                mapOf(
                                                                                        "url" to
                                                                                                "https://example.test/a.png"
                                                                                )
                                                                )
                                                        )
                                        )
                                )
                )
        val parts = messages[1].content

        assertEquals(2, parts.size)
        assertTrue(parts[0] is LlmTextPart)
        assertEquals("https://example.test/a.png", (parts[1] as LlmImageUrlPart).url)
    }

    private fun turnContext(trigger: Trigger): PlannerTurnContext =
            PlannerTurnContext(
                    loopId = routingKey.toString(),
                    routingKey = routingKey,
                    trigger = trigger,
                    foregroundEpoch = 1,
                    sendReplyDelegate = { _, _, _ -> ReplySendResult(ReplySendStatus.SENT, true) }
            )

    private fun trigger(
            messageId: String,
            text: String,
            contentBlocks: List<Map<String, Any?>>? = null
    ): Trigger {
        val payload = linkedMapOf<String, Any?>("text" to text)
        contentBlocks?.let { payload["content_blocks"] = it }
        return Trigger(
                contextId = routingKey.contextId,
                agentId = routingKey.agentId,
                messageId = messageId,
                triggerType = TriggerType.MSG,
                priority = TriggerPriority.NORMAL,
                timestampSeconds = 1.0,
                payload = payload
        )
    }
}
