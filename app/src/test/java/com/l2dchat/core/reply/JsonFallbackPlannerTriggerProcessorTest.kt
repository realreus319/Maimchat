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
import com.l2dchat.core.tools.ReplierTaskUpdate
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
                                                        """{"type":"tool_call","tool":"replier","arguments":{"thinking":"json reply"}}"""
                                                ),
                                        model = "fake"
                                )
                        )
                )

        runBlocking {
            val taskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator { request ->
                                        flow { emit(ReplierTaskUpdate.Completed(request.thinking)) }
                                    }
                    )
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    JsonFallbackPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry =
                                                    ToolRegistry(
                                                            listOf(ReplierTool(taskManager = taskManager))
                                                    ),
                                            promptBuilder = PlannerPromptBuilder(systemPrompt = "system"),
                                            systemPromptProvider =
                                                    PlannerSystemPromptProvider { "room system" }
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("json reply", withTimeout(1_000L) { replyDone.await() }.text)
            val firstCallMessages = client.messages.single()
            assertEquals("room system", firstCallMessages[0].textContent())
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
            val waitTaskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator = ReplierTaskGenerator { flow { awaitCancellation() } }
                    )
            val task =
                    waitTaskManager.startTask(
                            ReplierTaskRequest(
                                    taskId = "task-1",
                                    routingKey = routingKey,
                                    trigger = trigger("old"),
                                    thinking = "old"
                            )
                    )
            val replyTaskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator { request ->
                                        flow { emit(ReplierTaskUpdate.Completed(request.thinking)) }
                                    }
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
                                                toolRegistry =
                                                        ToolRegistry(
                                                                listOf(
                                                                        WaitForTool(waitTaskManager),
                                                                        ReplierTool(
                                                                                taskManager =
                                                                                        replyTaskManager
                                                                        )
                                                                )
                                                        )
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

    @Test
    fun `fallback parses tool call wrapped in prose without leaking raw json`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ScriptedJsonClient(
                        listOf(
                                LlmResponse(
                                        message =
                                                LlmMessage.assistant(
                                                        "Let me think about this. " +
                                                                """{"type":"tool_call","tool":"replier","arguments":{"thinking":"wrapped reply"}}""" +
                                                                " I will reply now."
                                                ),
                                        model = "fake"
                                )
                        )
                )

        runBlocking {
            val taskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator { request ->
                                        flow { emit(ReplierTaskUpdate.Completed(request.thinking)) }
                                    }
                    )
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    JsonFallbackPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry =
                                                    ToolRegistry(
                                                            listOf(ReplierTool(taskManager = taskManager))
                                                    )
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            // The embedded tool call is extracted and executed; the raw prose+JSON is never
            // sent to the user.
            assertEquals("wrapped reply", withTimeout(1_000L) { replyDone.await() }.text)
            loop.shutdown()
        }
    }

    @Test
    fun `fallback forces replier when tool budget is exhausted`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ScriptedJsonClient(
                        listOf(
                                LlmResponse(
                                        message =
                                                LlmMessage.assistant(
                                                        """{"type":"tool_call","tool":"replier","arguments":{"thinking":"loop"}}"""
                                                ),
                                        model = "fake"
                                ),
                                LlmResponse(
                                        message = LlmMessage.assistant("plain final answer"),
                                        model = "fake"
                                )
                        )
                )

        runBlocking {
            val taskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(
                                                    ReplierTaskUpdate.Completed(
                                                            "forced replier answer"
                                                    )
                                            )
                                        }
                                    }
                    )
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    JsonFallbackPlannerTriggerProcessor(
                                            llmClient = client,
                                            config =
                                                    LlmGenerationConfig(
                                                            model = "fake",
                                                            maxToolRounds = 0
                                                    ),
                                            toolRegistry =
                                                    ToolRegistry(
                                                            listOf(
                                                                    ReplierTool(
                                                                            taskManager = taskManager
                                                                    )
                                                            )
                                                    )
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            // Tool budget is 0, so the planner text is not surfaced; the replier composes the reply.
            assertEquals("forced replier answer", withTimeout(1_000L) { replyDone.await() }.text)
            assertEquals(1, client.messages.size)
            loop.shutdown()
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
