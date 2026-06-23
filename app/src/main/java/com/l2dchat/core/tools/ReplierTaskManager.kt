package com.l2dchat.core.tools

import com.l2dchat.core.context.RoutingKey
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.takeWhile
import kotlinx.coroutines.launch

class ReplierTaskManager(
        private val scope: CoroutineScope,
        private val generator: ReplierTaskGenerator,
        private val taskStore: ReplierTaskStore = NoopReplierTaskStore,
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) {
    private val lock = Any()
    private val tasks = linkedMapOf<String, ReplierTask>()
    private val taskCreatedAt = linkedMapOf<String, Long>()

    fun startTask(request: ReplierTaskRequest): ReplierTask {
        val now = clockMillis()
        // Each new turn reaps stale tasks the decision planner never adopted/killed, so
        // background tasks (and their generation coroutines) can't leak unbounded.
        reapStaleTasks(now)
        val task =
                synchronized(lock) {
                    require(tasks[request.taskId] == null) {
                        "Replier task already exists: ${request.taskId}"
                    }
                    taskCreatedAt[request.taskId] = now
                    ReplierTask(
                                    request = request,
                                    scope = scope,
                                    generator = generator
                            )
                        .also { tasks[request.taskId] = it }
                }
        observeTask(task, createdAtMillis = now)
        return task.start()
    }

    /** Remove tasks older than the TTL, cancelling any that are still running. */
    private fun reapStaleTasks(now: Long) {
        val stale =
                synchronized(lock) {
                    val cutoff = now - TASK_TTL_MILLIS
                    val expired =
                            taskCreatedAt.filterValues { it < cutoff }.keys.toList()
                    expired.mapNotNull { id ->
                        val task = tasks.remove(id)
                        taskCreatedAt.remove(id)
                        task
                    }
                }
        stale.forEach { task ->
            if (!task.snapshot.isTerminal) {
                scope.launch { task.cancel() }
            }
        }
    }

    fun getTask(taskId: String): ReplierTask? = synchronized(lock) { tasks[taskId] }

    fun activeTaskIds(): List<String> =
            synchronized(lock) {
                tasks.values
                        .filter { !it.snapshot.isTerminal }
                        .map { it.request.taskId }
            }

    fun backgroundTaskSummaries(routingKey: RoutingKey? = null): List<ReplierTaskSummary> =
            synchronized(lock) {
                tasks.values.mapNotNull { task ->
                    val snapshot = task.snapshot
                    if (!snapshot.backgrounded) {
                        return@mapNotNull null
                    }
                    if (routingKey != null && task.request.routingKey != routingKey) {
                        return@mapNotNull null
                    }
                    task.toSummary(snapshot)
                }
            }

    suspend fun waitForCompletion(taskId: String): ReplierTaskSnapshot? =
            getTask(taskId)?.waitForCompletion()

    suspend fun cancel(taskId: String): ReplierTaskSnapshot? =
            getTask(taskId)?.cancel()

    fun clearTask(taskId: String): Boolean =
            synchronized(lock) {
                taskCreatedAt.remove(taskId)
                tasks.remove(taskId) != null
            }

    private fun observeTask(task: ReplierTask, createdAtMillis: Long) {
        scope.launch {
            task.snapshots
                    .takeWhile { snapshot ->
                        taskStore.upsertTaskSnapshot(
                                request = task.request,
                                snapshot = snapshot,
                                createdAtMillis = createdAtMillis,
                                updatedAtMillis = clockMillis()
                        )
                        !snapshot.isTerminal
                    }
                    .collect()
        }
    }

    private companion object {
        // Background tasks not adopted/killed within this window are reclaimed.
        private const val TASK_TTL_MILLIS: Long = 120_000L
    }

    private fun ReplierTask.toSummary(snapshot: ReplierTaskSnapshot): ReplierTaskSummary =
            ReplierTaskSummary(
                    taskId = request.taskId,
                    routingKey = request.routingKey,
                    triggerMessageId = request.trigger.messageId,
                    triggerText = request.trigger.payload["text"]?.toString().orEmpty(),
                    state = snapshot.state,
                    previewText = snapshot.previewText,
                    replyText = snapshot.replyText,
                    errorMessage = snapshot.errorMessage,
                    backgrounded = snapshot.backgrounded
            )
}
