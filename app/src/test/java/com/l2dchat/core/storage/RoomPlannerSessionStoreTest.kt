package com.l2dchat.core.storage

import com.l2dchat.core.message.PlannerMessageEntity
import com.l2dchat.core.message.PlannerRoundEntity
import com.l2dchat.core.message.ToolTaskEntity
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.reply.PlannerRoundRecord
import com.l2dchat.core.reply.PlannerSessionMessageRecord
import com.l2dchat.core.reply.PlannerSessionRole
import com.l2dchat.core.reply.PlannerSessionState
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskSnapshot
import com.l2dchat.core.tools.ReplierTaskState
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
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

    @Test
    fun `replier task store upserts tool task snapshots`() {
        val dao = FakePlannerStateDao()
        val store = RoomReplierTaskStore(dao)

        runBlocking {
            store.upsertTaskSnapshot(
                    request = replierRequest(),
                    snapshot =
                            ReplierTaskSnapshot(
                                    taskId = "task-1",
                                    state = ReplierTaskState.COMPLETED,
                                    previewText = "draft reply",
                                    replyText = "final reply",
                                    backgrounded = true
                            ),
                    createdAtMillis = 100L,
                    updatedAtMillis = 150L
            )
        }

        val task = dao.toolTasks.single()
        assertEquals("task-1", task.taskId)
        assertEquals("round-1", task.roundId)
        assertEquals(ReplierTool.NAME, task.toolName)
        assertEquals(ReplierTaskState.COMPLETED.name, task.state)
        assertTrue(task.inputJson.orEmpty().contains("\"thinking\":\"seed reply\""))
        assertTrue(task.outputJson.orEmpty().contains("\"reply_text\":\"final reply\""))
        assertEquals(null, task.error)
        assertEquals(100L, task.createdAtMillis)
        assertEquals(150L, task.updatedAtMillis)
    }

    private fun replierRequest(): ReplierTaskRequest {
        val routingKey = RoutingKey(contextId = "ctx", agentId = "agent")
        return ReplierTaskRequest(
                taskId = "task-1",
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
                roundId = "round-1",
                thinking = "seed reply"
        )
    }

    private class FakePlannerStateDao : PlannerStateDao {
        val rounds = mutableListOf<PlannerRoundEntity>()
        val messages = mutableListOf<PlannerMessageEntity>()
        val toolTasks = mutableListOf<ToolTaskEntity>()

        override suspend fun appendPlannerRound(round: PlannerRoundEntity) {
            rounds.removeAll { it.roundId == round.roundId }
            rounds.add(round)
        }

        override suspend fun queryGeneratingRoundTriggerIds(beforeMillis: Long): List<String?> =
                rounds
                        .filter {
                            it.state == PlannerSessionState.GENERATING &&
                                    it.createdAtMillis < beforeMillis
                        }
                        .map { it.triggerMessageId }

        override suspend fun failGeneratingRounds(nowMillis: Long, beforeMillis: Long): Int {
            var updatedCount = 0
            rounds.indices.forEach { index ->
                val round = rounds[index]
                if (
                        round.state == PlannerSessionState.GENERATING &&
                                round.createdAtMillis < beforeMillis
                ) {
                    rounds[index] =
                            round.copy(
                                    state = PlannerSessionState.FAILED,
                                    updatedAtMillis = nowMillis
                            )
                    updatedCount += 1
                }
            }
            return updatedCount
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

        override suspend fun deleteAllPlannerRounds() {
            rounds.clear()
        }

        override suspend fun deleteAllPlannerMessages() {
            messages.clear()
        }

        override suspend fun deleteAllToolTasks() {
            toolTasks.clear()
        }

        override suspend fun queryPlannerMessages(roundId: String): List<PlannerMessageEntity> =
                messages.filter { it.roundId == roundId }.sortedBy { it.sequence }

        override suspend fun queryRecentCompletedReplies(sinceMs: Long): List<CompletedReplyRow> =
                rounds
                        .asSequence()
                        .filter {
                            it.state == PlannerSessionState.COMPLETED &&
                                    it.triggerMessageId?.startsWith("msg") == true &&
                                    it.createdAtMillis >= sinceMs
                        }
                        .mapNotNull { round ->
                            val reply =
                                    messages
                                            .asSequence()
                                            .filter {
                                                it.roundId == round.roundId &&
                                                        it.role == PlannerSessionRole.ASSISTANT &&
                                                        !it.content.isNullOrEmpty()
                                            }
                                            .maxByOrNull { it.sequence }
                                            ?: return@mapNotNull null
                            CompletedReplyRow(
                                    roundId = round.roundId,
                                    triggerMessageId = round.triggerMessageId,
                                    replyText = reply.content.orEmpty(),
                                    createdAtMs = round.createdAtMillis
                            )
                        }
                        .sortedBy { it.createdAtMs }
                        .toList()
    }
}
