package com.l2dchat.core.storage

import com.l2dchat.core.message.PlannerMessageEntity
import com.l2dchat.core.message.PlannerRoundEntity
import com.l2dchat.core.message.ToolTaskEntity
import com.l2dchat.core.reply.PlannerRoundRecord
import com.l2dchat.core.reply.PlannerSessionMessageRecord
import com.l2dchat.core.reply.PlannerSessionRole
import com.l2dchat.core.reply.PlannerSessionState
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RoomPlannerSessionStoreTest {
    @Test
    fun `upserts planner round and serializes message payload`() {
        val dao = FakePlannerStateDao()
        val store = RoomPlannerSessionStore(dao)

        runBlocking {
            store.upsertRound(
                    PlannerRoundRecord(
                            roundId = "round-1",
                            contextId = "ctx",
                            agentId = "agent",
                            triggerMessageId = "msg-1",
                            state = PlannerSessionState.GENERATING,
                            createdAtMillis = 100L,
                            updatedAtMillis = 101L
                    )
            )
            store.appendMessage(
                    PlannerSessionMessageRecord(
                            plannerMessageId = "planner-1",
                            roundId = "round-1",
                            sequence = 0,
                            role = PlannerSessionRole.TRIGGER,
                            content = "MSG",
                            payload = mapOf("message_id" to "msg-1"),
                            createdAtMillis = 102L
                    )
            )
        }

        val round = dao.rounds.single()
        assertEquals("round-1", round.roundId)
        assertEquals("ctx", round.contextId)
        assertEquals(PlannerSessionState.GENERATING, round.state)

        val message = dao.messages.single()
        assertEquals("planner-1", message.plannerMessageId)
        assertEquals(PlannerSessionRole.TRIGGER, message.role)
        assertTrue(message.payloadJson.orEmpty().contains("\"message_id\":\"msg-1\""))
    }

    private class FakePlannerStateDao : PlannerStateDao {
        val rounds = mutableListOf<PlannerRoundEntity>()
        val messages = mutableListOf<PlannerMessageEntity>()
        val toolTasks = mutableListOf<ToolTaskEntity>()

        override suspend fun appendPlannerRound(round: PlannerRoundEntity) {
            rounds.removeAll { it.roundId == round.roundId }
            rounds.add(round)
        }

        override suspend fun appendPlannerMessage(message: PlannerMessageEntity) {
            messages.removeAll { it.plannerMessageId == message.plannerMessageId }
            messages.add(message)
        }

        override suspend fun appendToolTask(task: ToolTaskEntity) {
            toolTasks.removeAll { it.taskId == task.taskId }
            toolTasks.add(task)
        }

        override suspend fun updateToolTaskState(
                taskId: String,
                state: String,
                outputJson: String?,
                error: String?,
                updatedAtMillis: Long
        ) {
            val index = toolTasks.indexOfFirst { it.taskId == taskId }
            if (index >= 0) {
                val task = toolTasks[index]
                toolTasks[index] =
                        task.copy(
                                state = state,
                                outputJson = outputJson,
                                error = error,
                                updatedAtMillis = updatedAtMillis
                        )
            }
        }

        override suspend fun queryPlannerMessages(roundId: String): List<PlannerMessageEntity> =
                messages.filter { it.roundId == roundId }.sortedBy { it.sequence }
    }
}
