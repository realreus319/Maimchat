package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmMessageRole
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.tools.ReplierTaskGenerator
import com.l2dchat.core.tools.ReplierTaskManager
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.tools.ToolRegistry
import com.l2dchat.core.tools.WaitForTool
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class JsonFallbackPlannerTriggerProcessorTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `fallback executes json replier call and sends reply`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ScriptedJsonClient(
                        listOf(
                                LlmResponse(
                                        message =
                                                LlmMessage.assistant(
                                                        """{"type":"tool_call","tool":"replier","arguments":{"content":"json reply"}}"""
                                                ),
                                        model = "fake"
                                )
                        )
                )

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    JsonFallbackPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry = ToolRegistry(listOf(ReplierTool())),
                                            promptBuilder = PlannerPromptBuilder(systemPrompt = "system")
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("json reply", withTimeout(1_000L) { replyDone.await() }.text)
            val firstCallMessages = client.messages.single()
            assertEquals(LlmMessageRole.SYSTEM, firstCallMessages[1].role)
            assertTrue(firstCallMessages[1].textContent().contains("Native tool calling is unavailable"))
            assertTrue(firstCallMessages[1].textContent().contains("replier"))
            loop.shutdown()
        }
    }

    @Test
    fun `fallback feeds tool result back before final json response`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ScriptedJsonClient(
                        listOf(
                                LlmResponse(
                                        message =
                                                LlmMessage.assistant(
                                                        """{"type":"tool_call","tool":"wait_for","arguments":{"task_id":"task-1","timeout_millis":1}}"""
                                                ),
                                        model = "fake"
                                ),
                                LlmResponse(
                                        message =
                                                LlmMessage.assistant(
                                                        """{"type":"final","text":"after wait"}"""
                                                ),
                                        model = "fake"
                                )
                        )
                )

        runBlocking {
            val taskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator = ReplierTaskGenerator { flow { awaitCancellation() } }
                    )
            val task =
                    taskManager.startTask(
                            ReplierTaskRequest(
                                    taskId = "task-1",
                                    routingKey = routingKey,
                                    trigger = trigger("old"),
                                    content = "old"
                            )
                    )
            try {
                val loop =
                        PlannerLoop(
                                routingKey = routingKey,
                                scope = this,
                                processor =
                                        JsonFallbackPlannerTriggerProcessor(
                                                llmClient = client,
                                                config = LlmGenerationConfig(model = "fake"),
                                                toolRegistry = ToolRegistry(listOf(WaitForTool(taskManager)))
                                        ),
                                replySink = PlannerReplySink { replyDone.complete(it) }
                        )
                loop.start()
                loop.submitTrigger(trigger("hello"))

                assertEquals("after wait", withTimeout(1_000L) { replyDone.await() }.text)
                assertEquals(2, client.messages.size)
                assertTrue(client.messages[1].any { it.textContent().contains("[tool_result]") })
                assertTrue(client.messages[1].any { it.textContent().contains("GENERATING") })
                loop.shutdown()
            } finally {
                task.cancel()
            }
        }
    }

    private fun trigger(text: String): Trigger =
            Trigger(
                    contextId = routingKey.contextId,
                    agentId = routingKey.agentId,
                    messageId = text,
                    triggerType = TriggerType.MSG,
                    priority = TriggerPriority.NORMAL,
                    timestampSeconds = 1.0,
                    payload = mapOf("text" to text)
            )

    private class ScriptedJsonClient(
            responses: List<LlmResponse>
    ) : LlmClient {
        private val remainingResponses = responses.toMutableList()
        val messages = mutableListOf<List<LlmMessage>>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse {
            this.messages.add(messages)
            check(remainingResponses.isNotEmpty()) { "No scripted LLM response left" }
            return remainingResponses.removeAt(0)
        }

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> = emptyFlow()

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse = throw UnsupportedOperationException("native tools are not used")
    }
}
