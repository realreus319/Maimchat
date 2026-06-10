package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.environment.MotionTriggerResult
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TriggerMotionToolTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `triggers motion by group and index`() {
        var capturedRequest: MotionTriggerRequest? = null
        val tool =
                TriggerMotionTool(
                        MotionController { _, request ->
                            capturedRequest = request
                            MotionTriggerResult(
                                    accepted = true,
                                    message = "queued",
                                    metadata = mapOf("source" to "fake")
                            )
                        }
                )

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString(
                                            """{"group":"TapBody","index":1,"loop":true}"""
                                    )
                                    .asJsonObject
                    )
                }

        assertFalse(result.isError)
        assertEquals("TapBody", capturedRequest?.group)
        assertEquals(1, capturedRequest?.index)
        assertEquals(true, capturedRequest?.loop)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals(true, json["accepted"].asBoolean)
        assertEquals("TapBody", json["group"].asString)
        assertEquals(1, json["index"].asInt)
        assertEquals(true, json["loop"].asBoolean)
        assertEquals("queued", json["message"].asString)
        assertEquals("fake", json["metadata"].asJsonObject["source"].asString)
    }

    @Test
    fun `triggers motion by file path`() {
        var capturedRequest: MotionTriggerRequest? = null
        val tool =
                TriggerMotionTool(
                        MotionController { _, request ->
                            capturedRequest = request
                            MotionTriggerResult(accepted = true, message = "queued")
                        }
                )

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString("""{"file_path":"mao/wave.motion3.json"}""")
                                    .asJsonObject
                    )
                }

        assertFalse(result.isError)
        assertEquals("mao/wave.motion3.json", capturedRequest?.filePath)
        assertEquals(false, capturedRequest?.loop)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals("mao/wave.motion3.json", json["filePath"].asString)
    }

    @Test
    fun `failed controller response is returned as tool error`() {
        val tool =
                TriggerMotionTool(
                        MotionController { _, _ ->
                            MotionTriggerResult(
                                    accepted = false,
                                    message = "model is not ready"
                            )
                        }
                )

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString("""{"group":"Idle","index":0}""").asJsonObject
                    )
                }

        assertTrue(result.isError)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals(false, json["accepted"].asBoolean)
        assertEquals("model is not ready", json["message"].asString)
    }

    @Test
    fun `registry converts invalid motion arguments into tool error`() {
        val registry = ToolRegistry(listOf(TriggerMotionTool()))

        val result =
                runBlocking {
                    registry.execute(
                            context(),
                            LlmToolCall(
                                    id = "call-1",
                                    name = TriggerMotionTool.NAME,
                                    argumentsJson = """{"group":"Idle"}"""
                            )
                    )
                }

        assertTrue(result.result.isError)
        assertTrue(result.result.llmContent.contains("index"))
    }

    @Test
    fun `registry exposes trigger motion only in normal mode`() {
        val registry = ToolRegistry(listOf(TriggerMotionTool()))

        assertEquals(listOf(TriggerMotionTool.NAME), registry.definitions.map { it.name })
        assertTrue(registry.definitionsFor(ToolExecutionMode.DECISION).isEmpty())

        val rejected =
                runBlocking {
                    registry.execute(
                            context(mode = ToolExecutionMode.DECISION),
                            LlmToolCall(
                                    id = "call-1",
                                    name = TriggerMotionTool.NAME,
                                    argumentsJson = """{"group":"Idle","index":0}"""
                            )
                    )
                }

        assertTrue(rejected.result.isError)
        assertTrue(rejected.result.llmContent.contains("decision mode"))
    }

    private fun context(mode: ToolExecutionMode = ToolExecutionMode.NORMAL): ToolExecutionContext =
            ToolExecutionContext(
                    loopId = routingKey.loopId,
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
                    foregroundEpoch = 1,
                    mode = mode
            )
}
