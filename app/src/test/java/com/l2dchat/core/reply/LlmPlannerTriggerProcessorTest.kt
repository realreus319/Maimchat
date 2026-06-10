package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Test

class LlmPlannerTriggerProcessorTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `processor sends final assistant text when no tool call is returned`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client = FixedLlmClient(LlmResponse(message = LlmMessage.assistant("llm reply"), model = "fake"))

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    LlmPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            promptBuilder = PlannerPromptBuilder(systemPrompt = "system")
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("llm reply", withTimeout(1_000L) { replyDone.await() }.text)
            assertEquals("system", client.messages.single().first().textContent())
            loop.shutdown()
        }
    }

    @Test
    fun `processor uses system prompt provider override`() {
        val replyDone = CompletableDeferred<PlannerReply>()
        val client =
                FixedLlmClient(
                        LlmResponse(message = LlmMessage.assistant("llm reply"), model = "fake")
                )

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    LlmPlannerTriggerProcessor(
                                            llmClient = client,
                                            config = LlmGenerationConfig(model = "fake"),
                                            promptBuilder = PlannerPromptBuilder(systemPrompt = "system"),
                                            systemPromptProvider =
                                                    PlannerSystemPromptProvider { "room system" }
                                    ),
                            replySink = PlannerReplySink { replyDone.complete(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("hello"))

            assertEquals("llm reply", withTimeout(1_000L) { replyDone.await() }.text)
            assertEquals("room system", client.messages.single().first().textContent())
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

    private class FixedLlmClient(private val response: LlmResponse) : LlmClient {
        val messages = mutableListOf<List<LlmMessage>>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse {
            this.messages.add(messages)
            return response
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
        ): LlmResponse = response
    }
}
