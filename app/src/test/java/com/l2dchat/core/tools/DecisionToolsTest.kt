package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DecisionToolsTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `registry exposes tools by execution mode and rejects disallowed calls`() {
        val manager =
                ReplierTaskManager(
                        scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.SupervisorJob()),
                        generator =
                                ReplierTaskGenerator {
                                    flow { emit(ReplierTaskUpdate.Completed("done")) }
                                }
                )
        val registry =
                ToolRegistry(
                        listOf(ReplierTool(), WaitForTool(manager)) +
                                DecisionTools.defaultTools(manager).filter {
                                    it.definition.name != WaitForTool.NAME
                                }
                )

        assertEquals(
                listOf(ReplierTool.NAME, WaitForTool.NAME),
                registry.definitions.map { it.name }
        )
        assertEquals(
                listOf(AdoptBackgroundReplyTool.NAME, KillBackgroundReplyTool.NAME),
                registry.definitionsFor(ToolExecutionMode.DECISION).map { it.name }
        )

        val normalRejected =
                runBlocking {
                    registry.execute(
                            context(),
                            LlmToolCall(
                                    id = "call-1",
                                    name = AdoptBackgroundReplyTool.NAME,
                                    argumentsJson = """{"task_id":"task-1"}"""
                            )
                    )
                }
        val decisionRejected =
                runBlocking {
                    registry.execute(
                            context(mode = ToolExecutionMode.DECISION),
                            LlmToolCall(
                                    id = "call-2",
                                    name = WaitForTool.NAME,
                                    argumentsJson = """{"task_id":"task-1"}"""
                            )
                    )
                }

        assertTrue(normalRejected.result.isError)
        assertTrue(normalRejected.result.llmContent.contains("normal mode"))
        assertTrue(decisionRejected.result.isError)
        assertTrue(decisionRejected.result.llmContent.contains("decision mode"))
    }

    @Test
    fun `wait_for returns latest task state when timeout expires`() {
        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator = ReplierTaskGenerator { flow { awaitCancellation() } }
                    )
            val task = manager.startTask(request("task-1"))

            try {
                val result =
                        WaitForTool(manager)
                                .execute(
                                        context(),
                                        JsonParser.parseString(
                                                        """{"task_id":"task-1","timeout_millis":1}"""
                                                )
                                                .asJsonObject
                                )

                assertFalse(result.isError)
                val content = JsonParser.parseString(result.llmContent).asJsonObject
                assertEquals("task-1", content["taskId"].asString)
                assertEquals("GENERATING", content["state"].asString)
                // A timeout must be flagged so the planner doesn't mistake it for completion.
                assertTrue(content["timed_out"].asBoolean)
            } finally {
                task.cancel()
            }
        }
    }

    @Test
    fun `adopt background reply waits for completion and returns reply text`() {
        val release = CompletableDeferred<Unit>()

        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(ReplierTaskUpdate.TextDelta("old"))
                                            release.await()
                                            emit(ReplierTaskUpdate.TextDelta(" reply"))
                                        }
                                    }
                    )
            val task = manager.startTask(request("task-1"))
            withTimeout(1_000L) { task.snapshots.first { it.previewText == "old" } }
            assertTrue(task.moveToBackground())
            release.complete(Unit)

            val result =
                    AdoptBackgroundReplyTool(manager)
                            .execute(
                                    context(mode = ToolExecutionMode.DECISION),
                                    JsonParser.parseString(
                                                    """{"task_id":"task-1","timeout_millis":1000}"""
                                            )
                                            .asJsonObject
                            )

            assertFalse(result.isError)
            assertEquals("old reply", result.replyText)
            val content = JsonParser.parseString(result.llmContent).asJsonObject
            assertEquals("COMPLETED", content["state"].asString)
            assertEquals(true, content["adopted"].asBoolean)
            assertEquals(false, content["sent"].asBoolean)
            assertEquals(true, content["cleared"].asBoolean)
            assertNull(manager.getTask("task-1"))
            assertTrue(manager.backgroundTaskSummaries(routingKey).isEmpty())
        }
    }

    @Test
    fun `kill background reply cancels running task`() {
        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator = ReplierTaskGenerator { flow { awaitCancellation() } }
                    )
            val task = manager.startTask(request("task-1"))
            assertTrue(task.moveToBackground())

            val result =
                    KillBackgroundReplyTool(manager)
                            .execute(
                                    context(mode = ToolExecutionMode.DECISION),
                                    JsonParser.parseString("""{"task_id":"task-1"}""").asJsonObject
                            )

            assertFalse(result.isError)
            val content = JsonParser.parseString(result.llmContent).asJsonObject
            assertEquals("CANCELLED", content["state"].asString)
            assertEquals(true, content["killed"].asBoolean)
            assertEquals(true, content["cleared"].asBoolean)
            assertNull(manager.getTask("task-1"))
            assertTrue(manager.backgroundTaskSummaries(routingKey).isEmpty())
        }
    }

    private fun context(mode: ToolExecutionMode = ToolExecutionMode.NORMAL): ToolExecutionContext =
            ToolExecutionContext(
                    loopId = routingKey.toString(),
                    routingKey = routingKey,
                    trigger = trigger(),
                    foregroundEpoch = 1,
                    mode = mode
            )

    private fun request(taskId: String): ReplierTaskRequest =
            ReplierTaskRequest(
                    taskId = taskId,
                    routingKey = routingKey,
                    trigger = trigger(),
                    thinking = "hello"
            )

    private fun trigger(): Trigger =
            Trigger(
                    contextId = routingKey.contextId,
                    agentId = routingKey.agentId,
                    messageId = "msg-1",
                    triggerType = TriggerType.MSG,
                    priority = TriggerPriority.NORMAL,
                    timestampSeconds = 1.0,
                    payload = mapOf("text" to "hello")
            )
}
