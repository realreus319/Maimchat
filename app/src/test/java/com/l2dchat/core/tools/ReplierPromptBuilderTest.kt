package com.l2dchat.core.tools

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

class ReplierPromptBuilderTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `builder turns replier request into system and user llm messages`() {
        val messages =
                ReplierPromptBuilder(systemPrompt = "system").buildMessages(
                        request(
                                content = "answer the user",
                                replyGuidance = "short",
                                styleOverride = "casual",
                                emotionHint = "warm",
                                isProgressUpdate = true,
                                includeAction = true
                        )
                )

        assertEquals(LlmMessageRole.SYSTEM, messages[0].role)
        assertEquals("system", messages[0].textContent())
        assertEquals(LlmMessageRole.USER, messages[1].role)
        assertTrue(messages[1].textContent().contains("[current_trigger]"))
        assertTrue(messages[1].textContent().contains("[planner_content]"))
        assertTrue(messages[1].textContent().contains("hello"))
        assertTrue(messages[1].textContent().contains("answer the user"))
        assertTrue(messages[1].textContent().contains("[reply_guidance]"))
        assertTrue(messages[1].textContent().contains("[style_override]"))
        assertTrue(messages[1].textContent().contains("[emotion_hint]"))
        assertTrue(messages[1].textContent().contains("[progress_update]"))
        assertTrue(messages[1].textContent().contains("[include_action]"))
        assertEquals("task-1", messages[1].metadata["task_id"])
        assertEquals("msg-1", messages[1].metadata["message_id"])
    }

    @Test
    fun `builder keeps trigger image and live image inputs`() {
        val messages =
                ReplierPromptBuilder(systemPrompt = "system").buildMessages(
                        request(
                                content = "describe it",
                                liveImage = "https://example.test/live.png",
                                contentBlocks =
                                        listOf(
                                                mapOf(
                                                        "type" to "image_url",
                                                        "image_url" to
                                                                mapOf(
                                                                        "url" to
                                                                                "https://example.test/input.png",
                                                                        "detail" to "high"
                                                                )
                                                )
                                        )
                        )
                )
        val parts = messages[1].content

        assertEquals(3, parts.size)
        assertTrue(parts[0] is LlmTextPart)
        val triggerImage = parts[1] as LlmImageUrlPart
        assertEquals("https://example.test/input.png", triggerImage.url)
        assertEquals("high", triggerImage.detail)
        assertEquals("https://example.test/live.png", (parts[2] as LlmImageUrlPart).url)
    }

    @Test
    fun `builder injects persona mood impression memory and history context`() {
        val messages =
                ReplierPromptBuilder(systemPrompt = "system").buildMessages(
                        request(content = "answer the user"),
                        ReplierPromptContext(
                                personaPrompt = "你是小倩。",
                                moodState = "开心",
                                impressionText = "Alice：熟悉的用户。",
                                memoryText = "- Alice 喜欢咖啡",
                                historyMessages =
                                        listOf(
                                                ReplierPromptHistoryMessage(
                                                        text = "你好",
                                                        senderName = "Alice"
                                                ),
                                                ReplierPromptHistoryMessage(
                                                        text = "你好，我是小倩。",
                                                        senderName = "小倩",
                                                        isAssistant = true
                                                )
                                        ),
                                currentTimeText = "2026-06-10 12:00:00",
                                agentDisplayName = "小倩"
                        )
                )

        val text = messages[1].textContent()

        assertTrue(text.contains("[persona]"))
        assertTrue(text.contains("你是小倩。"))
        assertTrue(text.contains("[mood]"))
        assertTrue(text.contains("开心"))
        assertTrue(text.contains("[impression]"))
        assertTrue(text.contains("Alice：熟悉的用户。"))
        assertTrue(text.contains("[memory]"))
        assertTrue(text.contains("- Alice 喜欢咖啡"))
        assertTrue(text.contains("[history]"))
        assertTrue(text.contains("Alice: 你好"))
        assertTrue(text.contains("小倩(你): 你好，我是小倩。"))
        assertTrue(text.contains("[current_trigger]"))
    }

    @Test
    fun `builder renders replier user template placeholders`() {
        val messages =
                ReplierPromptBuilder(systemPrompt = "system").buildMessages(
                        request(content = "planner thoughts", replyGuidance = "keep it short"),
                        ReplierPromptContext(
                                userPromptTemplate =
                                        """
                                        persona={persona_prompt}
                                        mood={mood_state}
                                        impression={impression_text}
                                        history={history_text}
                                        content={content}
                                        guidance={reply_guidance}
                                        section={guidance_section}
                                        trigger={current_trigger}
                                        time={current_time}
                                        """
                                                .trimIndent(),
                                personaPrompt = "你是小倩。",
                                moodState = "平静",
                                impressionText = "Alice：初次聊天。",
                                historyMessages =
                                        listOf(
                                                ReplierPromptHistoryMessage(
                                                        text = "早上好",
                                                        senderName = "Alice"
                                                )
                                        ),
                                currentTimeText = "2026-06-10 12:00:00"
                        )
                )

        val text = messages[1].textContent()

        assertTrue(text.contains("persona=你是小倩。"))
        assertTrue(text.contains("mood=平静"))
        assertTrue(text.contains("impression=Alice：初次聊天。"))
        assertTrue(text.contains("history=Alice: 早上好"))
        assertTrue(text.contains("content=planner thoughts"))
        assertTrue(text.contains("guidance=keep it short"))
        assertTrue(text.contains("补充说明：\nkeep it short"))
        assertTrue(text.contains("trigger=[msg_trigger]"))
        assertTrue(text.contains("hello"))
        assertTrue(text.contains("time=2026-06-10 12:00:00"))
    }

    private fun request(
            content: String,
            replyGuidance: String? = null,
            styleOverride: String? = null,
            emotionHint: String? = null,
            isProgressUpdate: Boolean = false,
            includeAction: Boolean = false,
            liveImage: String? = null,
            contentBlocks: List<Map<String, Any?>>? = null
    ): ReplierTaskRequest {
        val payload = linkedMapOf<String, Any?>("text" to "hello")
        contentBlocks?.let { payload["content_blocks"] = it }
        return ReplierTaskRequest(
                taskId = "task-1",
                routingKey = routingKey,
                trigger =
                        Trigger(
                                contextId = routingKey.contextId,
                                agentId = routingKey.agentId,
                                messageId = "msg-1",
                                triggerType = TriggerType.MSG,
                                priority = TriggerPriority.NORMAL,
                                timestampSeconds = 1.0,
                                payload = payload
                        ),
                content = content,
                replyGuidance = replyGuidance,
                styleOverride = styleOverride,
                emotionHint = emotionHint,
                isProgressUpdate = isProgressUpdate,
                includeAction = includeAction,
                liveImage = liveImage
        )
    }
}
