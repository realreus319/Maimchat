package com.l2dchat.core.storage

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.reply.PlannerTurnContext
import com.l2dchat.core.reply.ReplySendResult
import com.l2dchat.core.reply.ReplySendStatus
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class RoomPlannerSystemPromptProviderTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `provider prefers agent template over global template`() {
        val dao =
                FakeRuntimeStateDao(
                        templates =
                                listOf(
                                        template(
                                                templateId = "global",
                                                agentId = null,
                                                body = "global system",
                                                updatedAtMillis = 10L
                                        ),
                                        template(
                                                templateId = "agent",
                                                agentId = routingKey.agentId,
                                                body = "agent system",
                                                updatedAtMillis = 1L
                                        )
                                )
                )

        val prompt =
                runBlocking {
                    RoomPlannerSystemPromptProvider(dao, "planner_system")
                            .systemPromptFor(turnContext())
                }

        assertEquals("agent system", prompt)
    }

    @Test
    fun `provider falls back to global template`() {
        val dao =
                FakeRuntimeStateDao(
                        templates =
                                listOf(
                                        template(
                                                templateId = "global",
                                                agentId = null,
                                                body = "global system"
                                        )
                                )
                )

        val prompt =
                runBlocking {
                    RoomPlannerSystemPromptProvider(dao, "planner_system")
                            .systemPromptFor(turnContext())
                }

        assertEquals("global system", prompt)
    }

    @Test
    fun `provider ignores blank selected template`() {
        val dao =
                FakeRuntimeStateDao(
                        templates =
                                listOf(
                                        template(
                                                templateId = "blank",
                                                agentId = routingKey.agentId,
                                                body = "   "
                                        )
                                )
                )

        val prompt =
                runBlocking {
                    RoomPlannerSystemPromptProvider(dao, "planner_system")
                            .systemPromptFor(turnContext())
                }

        assertNull(prompt)
    }

    private fun turnContext(): PlannerTurnContext =
            PlannerTurnContext(
                    loopId = routingKey.toString(),
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
                    sendReplyDelegate = { _, _, _ -> ReplySendResult(ReplySendStatus.SENT, true) }
            )

    private fun template(
            templateId: String,
            agentId: String?,
            body: String,
            updatedAtMillis: Long = 0L
    ): PromptTemplateEntity =
            PromptTemplateEntity(
                    templateId = templateId,
                    agentId = agentId,
                    name = "planner_system",
                    body = body,
                    updatedAtMillis = updatedAtMillis
            )

    private class FakeRuntimeStateDao(
            private val templates: List<PromptTemplateEntity> = emptyList()
    ) : RuntimeStateDao {
        override suspend fun upsertAgentConfig(config: AgentConfigEntity) = Unit

        override suspend fun upsertPromptTemplate(template: PromptTemplateEntity) = Unit

        override suspend fun upsertMemory(memory: MemoryEntity) = Unit

        override suspend fun upsertImpression(impression: ImpressionEntity) = Unit

        override suspend fun upsertMoodState(moodState: MoodStateEntity) = Unit

        override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) = Unit

        override suspend fun queryMediaBlocksForMessage(messageId: String): List<MediaBlockEntity> =
                emptyList()

        override suspend fun queryAgentConfig(agentId: String): AgentConfigEntity? = null

        override suspend fun queryPromptTemplate(
                name: String,
                agentId: String
        ): PromptTemplateEntity? =
                templates
                        .filter { it.name == name && (it.agentId == agentId || it.agentId == null) }
                        .sortedWith(
                                compareBy<PromptTemplateEntity> {
                                            if (it.agentId == agentId) 0 else 1
                                        }
                                        .thenByDescending { it.updatedAtMillis }
                        )
                        .firstOrNull()

        override suspend fun queryMemories(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<MemoryEntity> = emptyList()

        override suspend fun queryImpression(
                contextId: String,
                agentId: String,
                subjectId: String
        ): ImpressionEntity? = null

        override suspend fun queryMoodState(contextId: String, agentId: String): MoodStateEntity? =
                null
    }
}
