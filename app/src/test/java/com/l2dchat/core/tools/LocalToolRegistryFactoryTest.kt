package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentModelState
import com.l2dchat.core.environment.EnvironmentState
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.environment.MotionTriggerResult
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalToolRegistryFactoryTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `normal registry exposes planner tools without background task tools by default`() {
        val registry = LocalToolRegistryFactory.normalRegistry()

        assertEquals(
                listOf(
                        ReplierTool.NAME,
                        GetWorldStateTool.NAME,
                        LookAtTool.NAME,
                        TriggerMotionTool.NAME
                ),
                registry.definitions.map { it.name }
        )
        assertTrue(registry.definitionsFor(ToolExecutionMode.DECISION).isEmpty())
    }

    @Test
    fun `normal registry adds wait for when task manager is provided`() {
        val registry =
                LocalToolRegistryFactory.normalRegistry(
                        replierTaskManager = taskManager()
                )

        assertEquals(
                listOf(
                        ReplierTool.NAME,
                        WaitForTool.NAME,
                        GetWorldStateTool.NAME,
                        LookAtTool.NAME,
                        TriggerMotionTool.NAME
                ),
                registry.definitions.map { it.name }
        )
        assertTrue(registry.definitionsFor(ToolExecutionMode.DECISION).isEmpty())
    }

    @Test
    fun `decision registry exposes only decision mode tools`() {
        val registry = LocalToolRegistryFactory.decisionRegistry(taskManager())

        assertTrue(registry.definitions.isEmpty())
        assertEquals(
                listOf(AdoptBackgroundReplyTool.NAME, KillBackgroundReplyTool.NAME),
                registry.definitionsFor(ToolExecutionMode.DECISION).map { it.name }
        )
    }

    @Test
    fun `normal registry uses injected environment and motion adapters`() {
        var capturedMotionRequest: MotionTriggerRequest? = null
        val registry =
                LocalToolRegistryFactory.normalRegistry(
                        environmentStateProvider =
                                EnvironmentStateProvider { context ->
                                    EnvironmentState(
                                            contextId = context.routingKey.contextId,
                                            agentId = context.routingKey.agentId,
                                            model = EnvironmentModelState(key = "mao")
                                    )
                                },
                        motionController =
                                MotionController { _, request ->
                                    capturedMotionRequest = request
                                    MotionTriggerResult(accepted = true, message = "queued")
                                }
                )

        val worldState =
                runBlocking {
                    registry.execute(
                            context(),
                            LlmToolCall(
                                    id = "call-1",
                                    name = GetWorldStateTool.NAME,
                                    argumentsJson = "{}"
                            )
                    )
                }
        val motion =
                runBlocking {
                    registry.execute(
                            context(),
                            LlmToolCall(
                                    id = "call-2",
                                    name = TriggerMotionTool.NAME,
                                    argumentsJson = """{"group":"TapBody","index":1}"""
                            )
                    )
                }

        assertFalse(worldState.result.isError)
        assertEquals(
                "mao",
                JsonParser.parseString(worldState.result.llmContent)
                        .asJsonObject["model"]
                        .asJsonObject["key"]
                        .asString
        )
        assertFalse(motion.result.isError)
        assertEquals("TapBody", capturedMotionRequest?.group)
        assertEquals(1, capturedMotionRequest?.index)
    }

    private fun taskManager(): ReplierTaskManager =
            ReplierTaskManager(
                    scope = CoroutineScope(SupervisorJob()),
                    generator =
                            ReplierTaskGenerator { request ->
                                flow { emit(ReplierTaskUpdate.Completed(request.content)) }
                            }
            )

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
