package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentChatBubble
import com.l2dchat.core.environment.EnvironmentInteraction
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentMotion
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

class GetWorldStateToolTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `returns environment state as planner json`() {
        val tool =
                GetWorldStateTool(
                        EnvironmentStateProvider { context ->
                            EnvironmentState(
                                    contextId = context.routingKey.contextId,
                                    agentId = context.routingKey.agentId,
                                    model =
                                            EnvironmentModelState(
                                                    key = "model-a",
                                                    name = "Mao",
                                                    folderPath = "/models/mao",
                                                    lifecycleState = "READY"
                                            ),
                                    motions =
                                            listOf(
                                                    EnvironmentMotion(
                                                            group = "Idle",
                                                            index = 0,
                                                            filePath = "mao/idle.motion3.json",
                                                            displayName = "Idle 0"
                                                    ),
                                                    EnvironmentMotion(
                                                            group = "TapBody",
                                                            index = 1,
                                                            filePath = "mao/wave.motion3.json",
                                                            displayName = "Wave"
                                                    )
                                            ),
                                    expression = "smile",
                                    surface =
                                            EnvironmentSurfaceState(
                                                    appVisible = true,
                                                    wallpaperVisible = false,
                                                    backgroundPath = "/backgrounds/room.png"
                                            ),
                                    lastInteraction =
                                            EnvironmentInteraction(
                                                    type = "touch",
                                                    x = 12.5f,
                                                    y = 32.0f,
                                                    timestampMillis = 1_000L
                                            ),
                                    recentBubbles =
                                            listOf(
                                                    EnvironmentChatBubble(
                                                            text = "old",
                                                            fromUser = true,
                                                            timestampMillis = 900L
                                                    ),
                                                    EnvironmentChatBubble(
                                                            text = "latest",
                                                            fromUser = false,
                                                            timestampMillis = 950L
                                                    )
                                            ),
                                    visualSnapshot =
                                            EnvironmentVisualSnapshot(
                                                    reference = "snapshot-1",
                                                    mimeType = "image/png",
                                                    width = 640,
                                                    height = 360,
                                                    capturedAtMillis = 990L
                                            ),
                                    metadata = mapOf("source" to "test")
                            )
                        }
                )

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString("""{"max_recent_bubbles":1}""").asJsonObject
                    )
                }

        assertFalse(result.isError)
        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals("room-a", json["contextId"].asString)
        assertEquals("agent-a", json["agentId"].asString)
        assertEquals("model-a", json["model"].asJsonObject["key"].asString)
        assertEquals("Mao", json["model"].asJsonObject["name"].asString)
        assertEquals(2, json["motions"].asJsonArray.size())
        assertEquals("TapBody", json["motions"].asJsonArray[1].asJsonObject["group"].asString)
        assertEquals("smile", json["expression"].asString)
        assertEquals(true, json["surface"].asJsonObject["appVisible"].asBoolean)
        assertEquals("touch", json["lastInteraction"].asJsonObject["type"].asString)
        assertEquals(1, json["recentBubbles"].asJsonArray.size())
        assertEquals("latest", json["recentBubbles"].asJsonArray[0].asJsonObject["text"].asString)
        assertEquals("snapshot-1", json["visualSnapshot"].asJsonObject["reference"].asString)
        assertEquals("test", json["metadata"].asJsonObject["source"].asString)
    }

    @Test
    fun `honors include flags and count limits`() {
        val provider =
                EnvironmentStateProvider { context ->
                    EnvironmentState(
                            contextId = context.routingKey.contextId,
                            agentId = context.routingKey.agentId,
                            model = EnvironmentModelState(key = "model-a"),
                            motions =
                                    listOf(
                                            EnvironmentMotion(group = "Idle", index = 0),
                                            EnvironmentMotion(group = "Idle", index = 1)
                                    ),
                            recentBubbles =
                                    listOf(
                                            EnvironmentChatBubble(text = "first", fromUser = true),
                                            EnvironmentChatBubble(text = "second", fromUser = false)
                                    ),
                            visualSnapshot = EnvironmentVisualSnapshot(reference = "snapshot-1")
                    )
                }
        val tool = GetWorldStateTool(provider)

        val result =
                runBlocking {
                    tool.execute(
                            context(),
                            JsonParser.parseString(
                                            """
                                            {
                                              "include_model": false,
                                              "max_motions": 1,
                                              "include_recent_bubbles": false,
                                              "include_visual_snapshot": false
                                            }
                                            """
                                    )
                                    .asJsonObject
                    )
                }

        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertFalse(json.has("model"))
        assertEquals(1, json["motions"].asJsonArray.size())
        assertFalse(json.has("recentBubbles"))
        assertFalse(json.has("visualSnapshot"))
    }

    @Test
    fun `empty provider mirrors routing key`() {
        val result =
                runBlocking {
                    GetWorldStateTool(EmptyEnvironmentStateProvider)
                            .execute(context(), JsonParser.parseString("{}").asJsonObject)
                }

        val json = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals("room-a", json["contextId"].asString)
        assertEquals("agent-a", json["agentId"].asString)
        assertTrue(json["motions"].asJsonArray.size() == 0)
        assertTrue(json["recentBubbles"].asJsonArray.size() == 0)
    }

    @Test
    fun `registry exposes world state only in normal mode`() {
        val registry = ToolRegistry(listOf(GetWorldStateTool()))

        assertEquals(listOf(GetWorldStateTool.NAME), registry.definitions.map { it.name })
        assertTrue(registry.definitionsFor(ToolExecutionMode.DECISION).isEmpty())

        val rejected =
                runBlocking {
                    registry.execute(
                            context(mode = ToolExecutionMode.DECISION),
                            LlmToolCall(
                                    id = "call-1",
                                    name = GetWorldStateTool.NAME,
                                    argumentsJson = "{}"
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
