package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmTextPart
import com.l2dchat.core.tools.ReplierTaskGenerator
import com.l2dchat.core.tools.ReplierTaskManager
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskUpdate
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun `builder can override system prompt per turn`() {
        val messages =
                PlannerPromptBuilder(systemPrompt = "system").buildMessages(
                        context = turnContext(trigger("msg-1", text = "hello")),
                        systemPromptOverride = "room system"
                )

        assertEquals("room system", messages[0].textContent())
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

    @Test
    fun `builder prepends prompt context blocks to user message`() {
        val messages =
                PlannerPromptBuilder(
                                systemPrompt = "system",
                                contextProvider =
                                        PlannerPromptContextProvider {
                                            listOf(
                                                    PlannerPromptContextBlock(
                                                            name = "memory",
                                                            content = "likes concise replies"
                                                    )
                                            )
                                        }
                        )
                        .buildMessages(context = turnContext(trigger("msg-1", text = "hello")))

        val userText = messages[1].textContent()
        assertTrue(userText.contains("[planner_context]"))
        assertTrue(userText.contains("[memory]"))
        assertTrue(userText.contains("likes concise replies"))
        assertTrue(userText.contains("hello"))
    }

    @Test
    fun `background replier prompt provider lists current routing key tasks`() {
        runBlocking {
            val otherRoutingKey = RoutingKey(contextId = "room-b", agentId = "agent-a")
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(ReplierTaskUpdate.TextDelta("old draft"))
                                            awaitCancellation()
                                        }
                                    }
                    )
            val task = manager.startTask(replierRequest("task-1", routingKey, "old message"))
            val otherTask = manager.startTask(replierRequest("task-2", otherRoutingKey, "other"))
            try {
                withTimeout(1_000L) { task.snapshots.first { it.previewText == "old draft" } }
                withTimeout(1_000L) { otherTask.snapshots.first { it.previewText == "old draft" } }
                assertTrue(task.moveToBackground())
                assertTrue(otherTask.moveToBackground())

                val messages =
                        PlannerPromptBuilder(
                                        systemPrompt = "system",
                                        contextProvider =
                                                BackgroundReplierPromptContextProvider(manager)
                                )
                                .buildMessages(
                                        context = turnContext(trigger("msg-new", text = "new"))
                                )

                val userText = messages[1].textContent()
                assertTrue(userText.contains("[background_replier_tasks]"))
                assertTrue(userText.contains("task_id: task-1"))
                assertTrue(userText.contains("state: BACKGROUND"))
                assertTrue(userText.contains("trigger_text: old message"))
                assertTrue(userText.contains("preview: old draft"))
                assertFalse(userText.contains("task_id: task-2"))
            } finally {
                task.cancel()
                otherTask.cancel()
            }
        }
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

    private fun replierRequest(
            taskId: String,
            requestRoutingKey: RoutingKey,
            text: String
    ): ReplierTaskRequest =
            ReplierTaskRequest(
                    taskId = taskId,
                    routingKey = requestRoutingKey,
                    trigger =
                            Trigger(
                                    contextId = requestRoutingKey.contextId,
                                    agentId = requestRoutingKey.agentId,
                                    messageId = "$taskId-trigger",
                                    triggerType = TriggerType.MSG,
                                    priority = TriggerPriority.NORMAL,
                                    timestampSeconds = 1.0,
                                    payload = mapOf("text" to text)
                            ),
                    content = text
            )
}
