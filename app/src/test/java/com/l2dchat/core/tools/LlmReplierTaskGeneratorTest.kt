package com.l2dchat.core.tools

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.asFlow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LlmReplierTaskGeneratorTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `generator maps llm stream into replier task updates`() {
        val response = LlmResponse(message = LlmMessage.assistant("hello"), model = "fake")
        val client =
                FakeStreamClient(
                        events =
                                listOf(
                                        LlmStreamEvent.TextDelta("he"),
                                        LlmStreamEvent.TextDelta("llo"),
                                        LlmStreamEvent.Completed(response)
                                )
                )
        val generator =
                LlmReplierTaskGenerator(
                        llmClient = client,
                        config = LlmGenerationConfig(model = "fake"),
                        promptBuilder = ReplierPromptBuilder(systemPrompt = "system")
                )

        val updates = runBlocking { generator.generate(request("answer")).toList() }

        assertEquals(
                listOf(
                        ReplierTaskUpdate.TextDelta("he"),
                        ReplierTaskUpdate.TextDelta("llo"),
                        ReplierTaskUpdate.Completed("hello")
                ),
                updates
        )
        assertEquals("fake", client.lastConfig?.model)
        assertTrue(client.lastMessages.first().textContent().contains("system"))
        assertTrue(client.lastMessages.last().textContent().contains("answer"))
    }

    @Test
    fun `generator lets task complete from deltas when completion text is blank`() {
        val response = LlmResponse(message = LlmMessage.assistant(""), model = "fake")
        val client =
                FakeStreamClient(
                        events =
                                listOf(
                                        LlmStreamEvent.TextDelta("hello"),
                                        LlmStreamEvent.Completed(response)
                                )
                )
        val generator =
                LlmReplierTaskGenerator(
                        llmClient = client,
                        config = LlmGenerationConfig(model = "fake")
                )

        val updates = runBlocking { generator.generate(request("answer")).toList() }

        assertEquals(listOf(ReplierTaskUpdate.TextDelta("hello")), updates)
    }

    @Test
    fun `generator rejects tool calls from replier model`() {
        val client =
                FakeStreamClient(
                        events =
                                listOf(
                                        LlmStreamEvent.ToolCallStarted(
                                                LlmToolCall(
                                                        id = "call-1",
                                                        name = "replier",
                                                        argumentsJson = "{}"
                                                )
                                        )
                                )
                )
        val generator =
                LlmReplierTaskGenerator(
                        llmClient = client,
                        config = LlmGenerationConfig(model = "fake")
                )

        val error =
                runBlocking {
                    runCatching { generator.generate(request("answer")).toList() }.exceptionOrNull()
                }

        assertEquals("Replier generation does not support tool calls", error?.message)
    }

    private fun request(content: String): ReplierTaskRequest =
            ReplierTaskRequest(
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
                                    payload = mapOf("text" to "hello")
                            ),
                    content = content
            )

    private class FakeStreamClient(
            private val events: List<LlmStreamEvent>
    ) : LlmClient {
        var lastMessages: List<LlmMessage> = emptyList()
        var lastConfig: LlmGenerationConfig? = null

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse = throw UnsupportedOperationException("chatCompletion is not used")

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> {
            lastMessages = messages
            lastConfig = config
            return events.asFlow()
        }

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse = throw UnsupportedOperationException("chatCompletionWithTools is not used")
    }
}
