package com.l2dchat.core.llm

import com.google.gson.JsonParser
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

class OpenAiCompatibleClientTest {
    @Test
    fun `chatCompletion sends openai compatible request and parses text`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = "https://example.test/v1",
                        apiKeyProvider = { "secret-key" },
                        httpClient =
                                fakeHttpClient(recorded) {
                                    jsonResponse(
                                            """
                                            {
                                              "id": "chatcmpl-1",
                                              "model": "fake-model",
                                              "choices": [
                                                {
                                                  "finish_reason": "stop",
                                                  "message": {
                                                    "role": "assistant",
                                                    "content": "pong"
                                                  }
                                                }
                                              ],
                                              "usage": {
                                                "prompt_tokens": 3,
                                                "completion_tokens": 2
                                              }
                                            }
                                            """.trimIndent()
                                    )
                                },
                        maxRetries = 0
                )

        val response =
                runBlocking {
                    client.chatCompletion(
                            messages = listOf(LlmMessage.system("sys"), LlmMessage.user("ping")),
                            config =
                                    LlmGenerationConfig(
                                            model = "fake-model",
                                            temperature = 0.2,
                                            maxTokens = 64
                                    )
                    )
                }

        assertEquals("pong", response.text)
        assertEquals(5, response.usage?.totalTokens)
        assertEquals("Bearer secret-key", recorded.single().authorization)
        val body = JsonParser.parseString(recorded.single().body).asJsonObject
        assertEquals("fake-model", body["model"].asString)
        assertEquals(false, body["stream"].asBoolean)
        assertEquals(2, body.getAsJsonArray("messages").size())
        assertEquals(0.2, body["temperature"].asDouble, 0.0)
        assertEquals(64, body["max_tokens"].asInt)
    }

    @Test
    fun `chatCompletionWithTools executes tool call and sends tool result message`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = "https://example.test/v1",
                        httpClient =
                                fakeHttpClient(recorded) {
                                    if (recorded.size == 1) {
                                        sseResponse(
                                                """
                                                data: {"id":"chatcmpl-tool","model":"fake-model","choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"replier","arguments":"{\"reply_text\":\"hi\"}"}}]},"finish_reason":"tool_calls"}]}

                                                data: [DONE]
                                                """.trimIndent()
                                        )
                                    } else {
                                        sseResponse(
                                                """
                                                data: {"id":"chatcmpl-final","model":"fake-model","choices":[{"delta":{"role":"assistant","content":"done"},"finish_reason":"stop"}]}

                                                data: [DONE]
                                                """.trimIndent()
                                        )
                                    }
                                },
                        maxRetries = 0
                )
        val executed = mutableListOf<LlmToolCall>()

        val response =
                runBlocking {
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
                                        executed.add(call)
                                        LlmToolResult(
                                                toolCallId = call.id,
                                                name = call.name,
                                                content = "sent"
                                        )
                                    }
                    )
                }

        assertEquals("done", response.text)
        assertEquals("replier", executed.single().name)
        assertEquals(2, recorded.size)
        assertEquals(true, JsonParser.parseString(recorded[0].body).asJsonObject["stream"].asBoolean)
        val secondBody = JsonParser.parseString(recorded[1].body).asJsonObject
        val messages = secondBody.getAsJsonArray("messages")
        assertEquals("assistant", messages[1].asJsonObject["role"].asString)
        assertNotNull(messages[1].asJsonObject.getAsJsonArray("tool_calls"))
        assertEquals("tool", messages[2].asJsonObject["role"].asString)
        assertEquals("call-1", messages[2].asJsonObject["tool_call_id"].asString)
    }

    @Test
    fun `chatCompletionStream emits text deltas and completed response`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = "https://example.test/v1",
                        httpClient =
                                fakeHttpClient(recorded) {
                                    sseResponse(
                                            """
                                            data: {"id":"chatcmpl-stream","model":"fake-model","choices":[{"delta":{"content":"he"},"finish_reason":null}]}
                                            
                                            data: {"id":"chatcmpl-stream","model":"fake-model","choices":[{"delta":{"content":"llo"},"finish_reason":"stop"}]}
                                            
                                            data: [DONE]
                                            """.trimIndent()
                                    )
                                },
                        maxRetries = 0
                )

        val events =
                runBlocking {
                    client.chatCompletionStream(
                                    messages = listOf(LlmMessage.user("hello")),
                                    config = LlmGenerationConfig(model = "fake-model")
                            )
                            .toList()
                }

        assertEquals(LlmStreamEvent.TextDelta("he"), events[0])
        assertEquals(LlmStreamEvent.TextDelta("llo"), events[1])
        val completed = events[2] as LlmStreamEvent.Completed
        assertEquals("hello", completed.response.text)
        val body = JsonParser.parseString(recorded.single().body).asJsonObject
        assertEquals(true, body["stream"].asBoolean)
    }

    @Test
    fun `chatCompletionWithTools degrades to forced final answer when tool budget is exhausted`() {
        val recorded = mutableListOf<RecordedRequest>()
        val toolCallResponse =
                """
                data: {"id":"chatcmpl-tool","model":"fake-model","choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"replier","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}

                data: [DONE]
                """.trimIndent()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = "https://example.test/v1",
                        httpClient =
                                fakeHttpClient(recorded) {
                                    // Keep returning tool calls until the forced-final request
                                    // (which omits tools) arrives, then answer with text.
                                    val last = recorded.last().body
                                    val wantsTools =
                                            JsonParser.parseString(last)
                                                    .asJsonObject
                                                    .has("tools")
                                    if (wantsTools) {
                                        sseResponse(toolCallResponse)
                                    } else {
                                        sseResponse(
                                                """
                                                data: {"id":"chatcmpl-final","model":"fake-model","choices":[{"delta":{"role":"assistant","content":"forced answer"},"finish_reason":"stop"}]}

                                                data: [DONE]
                                                """.trimIndent()
                                        )
                                    }
                                },
                        maxRetries = 0
                )

        val response =
                runBlocking {
                    client.chatCompletionWithTools(
                            messages = listOf(LlmMessage.user("hello")),
                            tools = listOf(LlmToolDefinition(name = "replier", description = "Reply")),
                            config = LlmGenerationConfig(model = "fake-model", maxToolRounds = 2),
                            toolExecutor = LlmToolExecutor { call ->
                                LlmToolResult(toolCallId = call.id, name = call.name, content = "sent")
                            }
                    )
                }

        // Instead of throwing after the round budget, it issues one tools-disabled request.
        assertEquals("forced answer", response.text)
        val finalBody = JsonParser.parseString(recorded.last().body).asJsonObject
        assertEquals(true, finalBody["stream"].asBoolean)
        assertEquals(false, finalBody.has("tools"))
    }

    @Test
    fun `chatCompletion parses reasoning_content without losing it`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = "https://example.test/v1",
                        httpClient =
                                fakeHttpClient(recorded) {
                                    jsonResponse(
                                            """
                                            {
                                              "id": "chatcmpl-r",
                                              "model": "fake-model",
                                              "choices": [
                                                {
                                                  "finish_reason": "stop",
                                                  "message": {
                                                    "role": "assistant",
                                                    "reasoning_content": "thinking hard",
                                                    "content": "final answer"
                                                  }
                                                }
                                              ]
                                            }
                                            """.trimIndent()
                                    )
                                },
                        maxRetries = 0
                )

        val response =
                runBlocking {
                    client.chatCompletion(
                            messages = listOf(LlmMessage.user("hi")),
                            config = LlmGenerationConfig(model = "fake-model")
                    )
                }

        assertEquals("final answer", response.text)
        assertEquals("thinking hard", response.message.reasoningContent)
    }

    private fun fakeHttpClient(
            recorded: MutableList<RecordedRequest>,
            responseFactory: () -> Response
    ): OkHttpClient =
            OkHttpClient.Builder()
                    .addInterceptor(
                            Interceptor { chain ->
                                val request = chain.request()
                                val buffer = Buffer()
                                request.body?.writeTo(buffer)
                                recorded.add(
                                        RecordedRequest(
                                                authorization = request.header("Authorization"),
                                                body = buffer.readUtf8()
                                        )
                                )
                                responseFactory()
                                        .newBuilder()
                                        .request(request)
                                        .protocol(Protocol.HTTP_1_1)
                                        .build()
                            }
                    )
                    .build()

    private fun jsonResponse(body: String): Response =
            Response.Builder()
                    .code(200)
                    .message("OK")
                    .body(body.toResponseBody("application/json".toMediaType()))
                    .request(okhttp3.Request.Builder().url("https://example.test").build())
                    .protocol(Protocol.HTTP_1_1)
                    .build()

    private fun sseResponse(body: String): Response =
            Response.Builder()
                    .code(200)
                    .message("OK")
                    .body(body.toResponseBody("text/event-stream".toMediaType()))
                    .request(okhttp3.Request.Builder().url("https://example.test").build())
                    .protocol(Protocol.HTTP_1_1)
                    .build()

    private data class RecordedRequest(
            val authorization: String?,
            val body: String
    )
}
