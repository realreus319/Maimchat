package com.l2dchat.core.reply

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.llm.LlmToolResult
import com.l2dchat.core.tools.AdoptBackgroundReplyTool
import com.l2dchat.core.tools.DecisionTools
import com.l2dchat.core.tools.KillBackgroundReplyTool
import com.l2dchat.core.tools.ReplierTaskGenerator
import com.l2dchat.core.tools.ReplierTaskManager
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskUpdate
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.tools.ToolRegistry
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ToolCallingPlannerTriggerProcessorTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `processor sends replier tool result through reply sink`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ExecutingToolClient(
                        toolCalls =
                                listOf(
                                        LlmToolCall(
                                                id = "call-1",
                                                name = ReplierTool.NAME,
                                                argumentsJson = """{"content":"tool reply"}"""
                                        )
                                ),
                        finalText = "final text that should not be sent"
                )

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    ToolCallingPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry = ToolRegistry(listOf(ReplierTool())),
                                            promptBuilder = PlannerPromptBuilder(systemPrompt = "system")
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("tool reply", withTimeout(1_000L) { replyDone.await() }.text)
            assertEquals(listOf(ReplierTool.NAME), client.toolDefinitions.single().map { it.name })
            val resultContent =
                    JsonParser.parseString(client.toolResults.single().content).asJsonObject
            assertEquals("tool reply", resultContent["replyText"].asString)
            assertEquals(false, resultContent["sent"].asBoolean)
            loop.shutdown()
        }
    }

    @Test
    fun `processor falls back to final assistant text when tool does not reply`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ExecutingToolClient(
                        toolCalls =
                                listOf(
                                        LlmToolCall(
                                                id = "call-1",
                                                name = ReplierTool.NAME,
                                                argumentsJson = """{"content":"   "}"""
                                        )
                                ),
                        finalText = "fallback reply"
                )

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    ToolCallingPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry = ToolRegistry(listOf(ReplierTool()))
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("fallback reply", withTimeout(1_000L) { replyDone.await() }.text)
            assertTrue(client.toolResults.single().isError)
            loop.shutdown()
        }
    }

    @Test
    fun `processor can execute decision mode background adoption tools`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                ExecutingToolClient(
                        toolCalls =
                                listOf(
                                        LlmToolCall(
                                                id = "call-1",
                                                name = AdoptBackgroundReplyTool.NAME,
                                                argumentsJson = """{"task_id":"task-1"}"""
                                        )
                                ),
                        finalText = "final text that should not be sent"
                )

        runBlocking {
            val taskManager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow { emit(ReplierTaskUpdate.Completed("background reply")) }
                                    }
                    )
            taskManager.startTask(replierTaskRequest("task-1"))
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    ToolCallingPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            toolRegistry =
                                                    ToolRegistry(
                                                            listOf(ReplierTool()) +
                                                                    DecisionTools.defaultTools(taskManager)
                                                    ),
                                            toolMode = ToolExecutionMode.DECISION
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("new message"))

            assertEquals("background reply", withTimeout(1_000L) { replyDone.await() }.text)
            assertEquals(
                    listOf(AdoptBackgroundReplyTool.NAME, KillBackgroundReplyTool.NAME),
                    client.toolDefinitions.single().map { it.name }
            )
            val resultContent =
                    JsonParser.parseString(client.toolResults.single().content).asJsonObject
            assertEquals("COMPLETED", resultContent["state"].asString)
            assertEquals(true, resultContent["adopted"].asBoolean)
            loop.shutdown()
        }
    }

    private fun trigger(text: String): Trigger =
            Trigger(
                    contextId = routingKey.contextId,
                    agentId = routingKey.agentId,
                    messageId = "msg-1",
                    triggerType = TriggerType.MSG,
                    priority = TriggerPriority.NORMAL,
                    timestampSeconds = 1.0,
                    payload = mapOf("text" to text)
            )

    private fun replierTaskRequest(taskId: String): ReplierTaskRequest =
            ReplierTaskRequest(
                    taskId = taskId,
                    routingKey = routingKey,
                    trigger = trigger("old message"),
                    content = "old message"
            )

    private class ExecutingToolClient(
            private val toolCalls: List<LlmToolCall>,
            private val finalText: String
    ) : LlmClient {
        val toolDefinitions = mutableListOf<List<LlmToolDefinition>>()
        val toolResults = mutableListOf<LlmToolResult>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse = LlmResponse(message = LlmMessage.assistant(finalText), model = config.model)

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> = emptyFlow()

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse {
            toolDefinitions.add(tools)
            toolCalls.forEach { toolResults.add(toolExecutor.execute(it)) }
            return LlmResponse(message = LlmMessage.assistant(finalText), model = config.model)
        }
    }
}
