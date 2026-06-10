package com.l2dchat.chat

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.tools.GetWorldStateTool
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatEnvironmentStateProviderTest {
    @Test
    fun `world state exposes current model motions surface and bubbles`() {
        val provider = ChatEnvironmentStateProvider()
        provider.update(
                ChatEnvironmentUpdate(
                        modelKey = "mao",
                        modelName = "Mao",
                        modelFolderPath = "models/mao",
                        modelFile = "models/mao/mao.model3.json",
                        lifecycleState = "RENDERING",
                        motionFiles =
                                listOf(
                                        "models/mao/motions/TapBody_00.motion3.json",
                                        "models/mao/motions/wave.motion3.json"
                                ),
                        appVisible = true,
                        wallpaperVisible = false,
                        backgroundPath = "/tmp/bg.png"
                )
        )
        provider.updateRecentMessages(
                listOf(
                        ChatEnvironmentMessage("hello", fromUser = true, timestampMillis = 100L),
                        ChatEnvironmentMessage("hi", fromUser = false, timestampMillis = 200L)
                )
        )

        val result =
                runBlocking {
                    GetWorldStateTool(provider)
                            .execute(context(), JsonParser.parseString("{}").asJsonObject)
                }
        val json = JsonParser.parseString(result.llmContent).asJsonObject

        assertEquals("room-a", json["contextId"].asString)
        assertEquals("agent-a", json["agentId"].asString)
        assertEquals("Mao", json["model"].asJsonObject["name"].asString)
        assertEquals("models/mao", json["model"].asJsonObject["folderPath"].asString)
        assertEquals("RENDERING", json["model"].asJsonObject["lifecycleState"].asString)
        assertEquals("TapBody", json["motions"].asJsonArray[0].asJsonObject["group"].asString)
        assertEquals(0, json["motions"].asJsonArray[0].asJsonObject["index"].asInt)
        assertEquals(
                "models/mao/motions/TapBody_00.motion3.json",
                json["motions"].asJsonArray[0].asJsonObject["filePath"].asString
        )
        assertTrue(json["surface"].asJsonObject["appVisible"].asBoolean)
        assertFalse(json["surface"].asJsonObject["wallpaperVisible"].asBoolean)
        assertEquals("/tmp/bg.png", json["surface"].asJsonObject["backgroundPath"].asString)
        assertEquals("hi", json["recentBubbles"].asJsonArray[1].asJsonObject["text"].asString)
        assertEquals("chat_environment", json["metadata"].asJsonObject["provider"].asString)
    }

    @Test
    fun `surface background can be explicitly cleared`() {
        val provider = ChatEnvironmentStateProvider()
        provider.update(ChatEnvironmentUpdate(backgroundPath = "/tmp/bg.png"))

        provider.update(ChatEnvironmentUpdate(backgroundPath = null, hasBackgroundPath = true))

        val state = provider.currentState(context())
        assertNull(state.surface.backgroundPath)
    }

    @Test
    fun `interaction update preserves existing environment state`() {
        val provider = ChatEnvironmentStateProvider()
        provider.update(
                ChatEnvironmentUpdate(
                        modelName = "Mao",
                        motionFiles = listOf("models/mao/motions/wave.motion3.json"),
                        appVisible = true,
                        backgroundPath = "/tmp/bg.png"
                )
        )

        provider.update(
                ChatEnvironmentUpdate(
                        interaction =
                                ChatEnvironmentInteraction(
                                        type = "drag",
                                        x = 120.5f,
                                        y = 64f,
                                        timestampMillis = 300L
                                )
                )
        )

        val state = provider.currentState(context())
        assertEquals("Mao", state.model?.name)
        assertEquals(1, state.motions.size)
        assertEquals("/tmp/bg.png", state.surface.backgroundPath)
        assertEquals("drag", state.lastInteraction?.type)
        assertEquals(120.5f, state.lastInteraction?.x)
        assertEquals(64f, state.lastInteraction?.y)
        assertEquals(300L, state.lastInteraction?.timestampMillis)
    }

    @Test
    fun `visual snapshot update is exposed and can be cleared`() {
        val provider = ChatEnvironmentStateProvider()
        provider.update(
                ChatEnvironmentUpdate(
                        visualSnapshot =
                                ChatEnvironmentVisualSnapshot(
                                        reference = "android_app:abc123",
                                        mimeType =
                                                "application/vnd.l2dchat.environment-snapshot+json",
                                        width = 1080,
                                        height = 2400,
                                        capturedAtMillis = 500L
                                )
                )
        )

        val result =
                runBlocking {
                    GetWorldStateTool(provider)
                            .execute(context(), JsonParser.parseString("{}").asJsonObject)
                }
        val json = JsonParser.parseString(result.llmContent).asJsonObject

        assertEquals(
                "android_app:abc123",
                json["visualSnapshot"].asJsonObject["reference"].asString
        )
        assertEquals(1080, json["visualSnapshot"].asJsonObject["width"].asInt)
        assertEquals(2400, json["visualSnapshot"].asJsonObject["height"].asInt)

        provider.update(ChatEnvironmentUpdate(visualSnapshot = null, hasVisualSnapshot = true))

        assertNull(provider.currentState(context()).visualSnapshot)
    }

    private fun context(): ToolExecutionContext {
        val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")
        return ToolExecutionContext(
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
                mode = ToolExecutionMode.NORMAL
        )
    }
}
