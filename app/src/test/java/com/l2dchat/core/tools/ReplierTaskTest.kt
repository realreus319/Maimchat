package com.l2dchat.core.tools

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReplierTaskTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `task streams preview and completes with accumulated reply`() {
        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(ReplierTaskUpdate.TextDelta("hel"))
                                            emit(ReplierTaskUpdate.TextDelta("lo"))
                                        }
                                    }
                    )

            val task = manager.startTask(request("task-1"))
            val completed = withTimeout(1_000L) { task.waitForCompletion() }

            assertEquals(ReplierTaskState.COMPLETED, completed.state)
            assertEquals("hello", completed.previewText)
            assertEquals("hello", completed.replyText)
            assertFalse(manager.activeTaskIds().contains("task-1"))
        }
    }

    @Test
    fun `task can move to background while generation continues`() {
        val release = CompletableDeferred<Unit>()

        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(ReplierTaskUpdate.TextDelta("h"))
                                            release.await()
                                            emit(ReplierTaskUpdate.TextDelta("i"))
                                        }
                                    }
                    )

            val task = manager.startTask(request("task-1"))
            withTimeout(1_000L) { task.snapshots.first { it.previewText == "h" } }

            assertTrue(task.moveToBackground())
            assertEquals(ReplierTaskState.BACKGROUND, task.snapshot.state)
            assertTrue(task.snapshot.backgrounded)
            assertEquals(listOf("task-1"), manager.activeTaskIds())

            release.complete(Unit)

            val completed = withTimeout(1_000L) { task.waitForCompletion() }
            assertEquals(ReplierTaskState.COMPLETED, completed.state)
            assertTrue(completed.backgrounded)
            assertEquals("hi", completed.replyText)
            val backgroundSummary = manager.backgroundTaskSummaries(routingKey).single()
            assertEquals("task-1", backgroundSummary.taskId)
            assertEquals(ReplierTaskState.COMPLETED, backgroundSummary.state)
            assertEquals("hi", backgroundSummary.replyText)
        }
    }

    @Test
    fun `cancel marks running task cancelled`() {
        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator = ReplierTaskGenerator { flow { awaitCancellation() } }
                    )

            val task = manager.startTask(request("task-1"))
            val cancelled = withTimeout(1_000L) { task.cancel() }

            assertEquals(ReplierTaskState.CANCELLED, cancelled.state)
            assertFalse(manager.activeTaskIds().contains("task-1"))
        }
    }

    @Test
    fun `failure marks task failed with error message`() {
        runBlocking {
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow { throw IllegalStateException("provider failed") }
                                    }
                    )

            val task = manager.startTask(request("task-1"))
            val failed = withTimeout(1_000L) { task.waitForCompletion() }

            assertEquals(ReplierTaskState.FAILED, failed.state)
            assertEquals("provider failed", failed.errorMessage)
            assertFalse(manager.activeTaskIds().contains("task-1"))
        }
    }

    @Test
    fun `manager persists task snapshots to store`() {
        runBlocking {
            val store = RecordingReplierTaskStore()
            val manager =
                    ReplierTaskManager(
                            scope = this,
                            generator =
                                    ReplierTaskGenerator {
                                        flow {
                                            emit(ReplierTaskUpdate.TextDelta("he"))
                                            emit(ReplierTaskUpdate.Completed("hello"))
                                        }
                                    },
                            taskStore = store
                    )

            val task = manager.startTask(request("task-1", roundId = "round-1"))
            val completed = withTimeout(1_000L) { task.waitForCompletion() }
            withTimeout(1_000L) { store.awaitState(ReplierTaskState.COMPLETED) }

            assertEquals(ReplierTaskState.COMPLETED, completed.state)
            val persisted = store.snapshots().last()
            assertEquals("task-1", persisted.request.taskId)
            assertEquals("round-1", persisted.request.roundId)
            assertEquals(ReplierTaskState.COMPLETED, persisted.snapshot.state)
            assertEquals("hello", persisted.snapshot.replyText)
        }
    }

    private fun request(taskId: String): ReplierTaskRequest =
            request(taskId = taskId, roundId = "round_msg-1")

    private fun request(taskId: String, roundId: String): ReplierTaskRequest =
            ReplierTaskRequest(
                    taskId = taskId,
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
                    roundId = roundId,
                    content = "hello"
            )

    private class RecordingReplierTaskStore : ReplierTaskStore {
        private val lock = Any()
        private val records = mutableListOf<Record>()

        override suspend fun upsertTaskSnapshot(
                request: ReplierTaskRequest,
                snapshot: ReplierTaskSnapshot,
                createdAtMillis: Long,
                updatedAtMillis: Long
        ) {
            synchronized(lock) {
                records += Record(request, snapshot, createdAtMillis, updatedAtMillis)
            }
        }

        fun snapshots(): List<Record> = synchronized(lock) { records.toList() }

        suspend fun awaitState(state: ReplierTaskState) {
            while (snapshots().none { it.snapshot.state == state }) {
                delay(10L)
            }
        }

        data class Record(
                val request: ReplierTaskRequest,
                val snapshot: ReplierTaskSnapshot,
                val createdAtMillis: Long,
                val updatedAtMillis: Long
        )
    }
}
