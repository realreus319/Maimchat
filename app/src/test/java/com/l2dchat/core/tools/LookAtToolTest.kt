package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentChatBubble
import com.l2dchat.core.environment.EnvironmentInteraction
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentState
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentSurfaceState
import com.l2dchat.core.environment.EnvironmentVisualSnapshot
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LookAtToolTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `returns visual metadata without image content blocks by default`() {
        val tool = LookAtTool(provider())

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString(
                                            """
                                            {
                                              "focus": "screen",
                                              "include_recent_bubbles": true,
                                              "max_recent_bubbles": 1
                                            }
                                            """
                                    )
                                    .asJsonObject
                    )
                }

        assertFalse(result.isError)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals("room-a", json["contextId"].asString)
        assertEquals("agent-a", json["agentId"].asString)
        assertEquals("screen", json["focus"].asString)
        assertEquals("model-a", json["model"].asJsonObject["key"].asString)
        assertEquals("/backgrounds/room.png", json["surface"].asJsonObject["backgroundPath"].asString)
        assertEquals("drag", json["lastInteraction"].asJsonObject["type"].asString)
        assertEquals("snapshot-1", json["visualSnapshot"].asJsonObject["reference"].asString)
        assertEquals(1, json["recentBubbles"].asJsonArray.size())
        assertEquals("latest", json["recentBubbles"].asJsonArray[0].asJsonObject["text"].asString)
        assertFalse(json.has("contentBlocks"))
        assertFalse(result.llmContent.contains("image_url"))
        assertTrue(json["message"].asString.contains("metadata"))
    }

    @Test
    fun `include image returns content blocks for direct image snapshot`() {
        val imageReference = "data:image/png;base64,abc123"
        val tool =
                LookAtTool(
                        provider(
                                visualSnapshot =
                                        EnvironmentVisualSnapshot(
                                                reference = imageReference,
                                                mimeType = "image/png",
                                                width = 320,
                                                height = 180,
                                                capturedAtMillis = 990L
                                        )
                        )
                )

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString(
                                            """
                                            {
                                              "include_image": true,
                                              "image_detail": "low"
                                            }
                                            """
                                    )
                                    .asJsonObject
                    )
                }

        assertFalse(result.isError)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals(imageReference, json["liveImage"].asString)
        val blocks = json["contentBlocks"].asJsonArray
        assertEquals(2, blocks.size())
        assertEquals("text", blocks[0].asJsonObject["type"].asString)
        val imageBlock = blocks[1].asJsonObject
        assertEquals("image_url", imageBlock["type"].asString)
        val imageUrl = imageBlock["image_url"].asJsonObject
        assertEquals(imageReference, imageUrl["url"].asString)
        assertEquals("low", imageUrl["detail"].asString)
        assertEquals(true, result.metadata["has_image_content"])
        assertEquals(imageReference, result.metadata["live_image"])
    }

    @Test
    fun `include image does not attach synthetic snapshot reference`() {
        val tool = LookAtTool(provider())

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString("""{"include_image": true}""").asJsonObject
                    )
                }

        assertFalse(result.isError)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertFalse(json.has("contentBlocks"))
        assertFalse(json.has("liveImage"))
        assertTrue(json["message"].asString.contains("no permission-safe direct image content"))
        assertEquals(false, result.metadata["has_image_content"])
    }

    @Test
    fun `honors metadata include flags`() {
        val tool = LookAtTool(provider())

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString(
                                            """
                                            {
                                              "include_model": false,
                                              "include_surface": false,
                                              "include_last_interaction": false,
                                              "include_snapshot_metadata": false,
                                              "include_recent_bubbles": false
                                            }
                                            """
                                    )
                                    .asJsonObject
                    )
                }

        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertFalse(json.has("model"))
        assertFalse(json.has("surface"))
        assertFalse(json.has("lastInteraction"))
        assertFalse(json.has("visualSnapshot"))
        assertFalse(json.has("recentBubbles"))
    }

    @Test
    fun `registry exposes look at only in normal mode`() {
        val registry = ToolRegistry(listOf(LookAtTool()))

        assertEquals(listOf(LookAtTool.NAME), registry.definitions.map { it.name })
        assertTrue(registry.definitionsFor(ToolExecutionMode.DECISION).isEmpty())

        val rejected =
                runBlocking {
                    registry.execute(
                            context(mode = ToolExecutionMode.DECISION),
                            LlmToolCall(id = "call-1", name = LookAtTool.NAME, argumentsJson = "{}")
                    )
                }

        assertTrue(rejected.result.isError)
        assertTrue(rejected.result.llmContent.contains("decision mode"))
    }

    private fun provider(
            visualSnapshot: EnvironmentVisualSnapshot? =
                    EnvironmentVisualSnapshot(
                            reference = "snapshot-1",
                            mimeType = "image/png",
                            width = 320,
                            height = 180,
                            capturedAtMillis = 990L
                    )
    ): EnvironmentStateProvider =
            EnvironmentStateProvider { context ->
                EnvironmentState(
                        contextId = context.routingKey.contextId,
                        agentId = context.routingKey.agentId,
                        model = EnvironmentModelState(key = "model-a", name = "Mao"),
                        surface =
                                EnvironmentSurfaceState(
                                        appVisible = true,
                                        wallpaperVisible = true,
                                        backgroundPath = "/backgrounds/room.png"
                                ),
                        lastInteraction =
                                EnvironmentInteraction(
                                        type = "drag",
                                        x = 4.0f,
                                        y = 8.0f,
                                        timestampMillis = 1_000L
                                ),
                        recentBubbles =
                                listOf(
                                        EnvironmentChatBubble(text = "old", fromUser = true),
                                        EnvironmentChatBubble(text = "latest", fromUser = false)
                                ),
                        visualSnapshot = visualSnapshot
                )
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
