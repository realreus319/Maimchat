package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReplierToolTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")
    private val context =
            ToolExecutionContext(
                    loopId = routingKey.toString(),
                    routingKey = routingKey,
                    trigger =
                            Trigger(
                                    contextId = routingKey.contextId,
                                    agentId = routingKey.agentId,
                                    messageId = "msg-1",
                                    triggerType = TriggerType.MSG,
                                    priority = TriggerPriority.NORMAL,
                                    timestampSeconds = 1.0,
                                    payload = mapOf("text" to "hello")
                            ),
                    foregroundEpoch = 1
            )

    @Test
    fun `replier returns planner managed reply text`() {
        val registry = ToolRegistry(listOf(ReplierTool()))

        val execution =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(
                                    id = "call-1",
                                    name = ReplierTool.NAME,
                                    argumentsJson =
                                            """
                                            {
                                              "content": "hello back",
                                              "reply_guidance": "short",
                                              "style_override": "casual",
                                              "emotion_hint": "warm",
                                              "is_progress_update": false,
                                              "include_action": true,
                                              "live_image": "frame-1"
                                            }
                                            """.trimIndent()
                            )
                    )
                }

        assertFalse(execution.result.isError)
        assertEquals("hello back", execution.result.replyText)
        assertFalse(execution.result.sent)
        val toolResult = execution.toLlmToolResult()
        val content = JsonParser.parseString(toolResult.content).asJsonObject
        assertEquals("hello back", content["replyText"].asString)
        assertEquals(false, content["sent"].asBoolean)
        assertEquals("warm", content["emotionHint"].asString)
    }

    @Test
    fun `replier rejects blank content as tool error`() {
        val registry = ToolRegistry(listOf(ReplierTool()))

        val execution =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(
                                    id = "call-1",
                                    name = ReplierTool.NAME,
                                    argumentsJson = """{"content":"   "}"""
                            )
                    )
                }

        assertTrue(execution.result.isError)
        assertEquals(null, execution.result.replyText)
        assertEquals("replier.content must not be blank", execution.toLlmToolResult().content)
    }

    @Test
    fun `replier can return task generated reply text`() {
        val taskManager =
                ReplierTaskManager(
                        scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob()),
                        generator =
                                ReplierTaskGenerator {
                                    flow { emit(ReplierTaskUpdate.Completed("generated reply")) }
                                }
                )
        val registry =
                ToolRegistry(
                        listOf(
                                ReplierTool(
                                        taskManager = taskManager,
                                        taskIdFactory = { "task-1" }
                                )
                        )
                )

        val execution =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(
                                    id = "call-1",
                                    name = ReplierTool.NAME,
                                    argumentsJson = """{"content":"seed reply"}"""
                            )
                    )
                }

        assertFalse(execution.result.isError)
        assertEquals("generated reply", execution.result.replyText)
        val content = JsonParser.parseString(execution.toLlmToolResult().content).asJsonObject
        assertEquals("task-1", content["taskId"].asString)
        assertEquals("COMPLETED", content["state"].asString)
        assertEquals("generated reply", content["replyText"].asString)
    }

    @Test
    fun `replier returns tool error when task generation fails`() {
        val taskManager =
                ReplierTaskManager(
                        scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob()),
                        generator =
                                ReplierTaskGenerator {
                                    flow { throw IllegalStateException("provider failed") }
                                }
                )
        val registry =
                ToolRegistry(
                        listOf(
                                ReplierTool(
                                        taskManager = taskManager,
                                        taskIdFactory = { "task-1" }
                                )
                        )
                )

        val execution =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(
                                    id = "call-1",
                                    name = ReplierTool.NAME,
                                    argumentsJson = """{"content":"seed reply"}"""
                            )
                    )
                }

        assertTrue(execution.result.isError)
        val content = JsonParser.parseString(execution.toLlmToolResult().content).asJsonObject
        assertEquals("task-1", content["taskId"].asString)
        assertEquals("FAILED", content["state"].asString)
        assertEquals("provider failed", content["error"].asString)
    }

    @Test
    fun `registry returns tool error for unknown or malformed tool calls`() {
        val registry = ToolRegistry(listOf(ReplierTool()))

        val unknown =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(id = "call-1", name = "missing_tool", argumentsJson = "{}")
                    )
                }
        val malformed =
                runBlocking {
                    registry.execute(
                            context,
                            LlmToolCall(
                                    id = "call-2",
                                    name = ReplierTool.NAME,
                                    argumentsJson = "[]"
                            )
                    )
                }

        assertTrue(unknown.result.isError)
        assertEquals("Unknown tool: missing_tool", unknown.toLlmToolResult().content)
        assertTrue(malformed.result.isError)
        assertEquals("Invalid JSON arguments for replier", malformed.toLlmToolResult().content)
    }
}
