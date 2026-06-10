package com.l2dchat.core.llm

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class LlmClientContractTest {
    @Test
    fun `message helpers preserve text and tool call shape`() {
        val toolCall = LlmToolCall(id = "call-1", name = "replier", argumentsJson = "{}")
        val assistant = LlmMessage.assistantToolCalls(listOf(toolCall), text = "thinking")
        val toolResult =
                LlmMessage.toolResult(
                        LlmToolResult(
                                toolCallId = toolCall.id,
                                name = toolCall.name,
                                content = "reply sent"
                        )
                )

        assertEquals(LlmMessageRole.ASSISTANT, assistant.role)
        assertEquals("thinking", assistant.textContent())
        assertEquals(listOf(toolCall), assistant.toolCalls)
        assertEquals(LlmMessageRole.TOOL, toolResult.role)
        assertEquals("call-1", toolResult.toolCallId)
        assertEquals("reply sent", toolResult.textContent())
        assertFalse(assistant.isBlankAssistantResponse())
    }

    @Test
    fun `fake client executes scripted tool calls through contract`() {
        val toolCall =
                LlmToolCall(
                        id = "call-1",
                        name = "replier",
                        argumentsJson = """{"reply_text":"hello"}"""
                )
        val client =
                ScriptedLlmClient(
                        firstResponse =
                                LlmResponse(
                                        message = LlmMessage.assistantToolCalls(listOf(toolCall)),
                                        model = "fake-model",
                                        finishReason = LlmFinishReason.TOOL_CALLS
                                ),
                        finalResponse =
                                LlmResponse(
                                        message = LlmMessage.assistant("done"),
                                        model = "fake-model"
                                )
                )
        val executedCalls = mutableListOf<LlmToolCall>()

        runBlocking {
            val response =
                    client.chatCompletionWithTools(
                            messages = listOf(LlmMessage.user("hello")),
                            tools =
                                    listOf(
                                            LlmToolDefinition(
                                                    name = "replier",
                                                    description = "Send a reply"
                                            )
                                    ),
                            config = LlmGenerationConfig(model = "fake-model"),
                            toolExecutor =
                                    LlmToolExecutor { call ->
                                        executedCalls.add(call)
                                        LlmToolResult(
                                                toolCallId = call.id,
                                                name = call.name,
                                                content = "reply sent"
                                        )
                                    }
                    )

            assertEquals(listOf(toolCall), executedCalls)
            assertEquals("done", response.text)
            assertEquals(1, client.completionMessages.last().count { it.role == LlmMessageRole.TOOL })
        }
    }

    @Test
    fun `fake client streams text and completion events`() {
        val response = LlmResponse(message = LlmMessage.assistant("hello"), model = "fake-model")
        val client = ScriptedLlmClient(firstResponse = response, finalResponse = response)

        runBlocking {
            val events =
                    client.chatCompletionStream(
                                    messages = listOf(LlmMessage.user("hello")),
                                    config = LlmGenerationConfig(model = "fake-model")
                            )
                            .toList()

            assertEquals(listOf(LlmStreamEvent.TextDelta("hello"), LlmStreamEvent.Completed(response)), events)
        }
    }

    private class ScriptedLlmClient(
            private val firstResponse: LlmResponse,
            private val finalResponse: LlmResponse
    ) : LlmClient {
        val completionMessages = mutableListOf<List<LlmMessage>>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse {
            completionMessages.add(messages)
            return if (completionMessages.size == 1) firstResponse else finalResponse
        }

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> =
                flow {
                    val response = chatCompletion(messages, config)
                    response.text.takeIf { it.isNotBlank() }?.let {
                        emit(LlmStreamEvent.TextDelta(it))
                    }
                    emit(LlmStreamEvent.Completed(response))
                }

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse {
            val history = messages.toMutableList()
            val first = chatCompletion(history, config)
            history.add(first.message)
            first.toolCalls.forEach { call ->
                val result = toolExecutor.execute(call)
                history.add(LlmMessage.toolResult(result))
            }
            return chatCompletion(history, config)
        }
    }
}
