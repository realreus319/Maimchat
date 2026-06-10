package com.l2dchat.core.tools

import kotlinx.coroutines.CoroutineScope

class ReplierTaskManager(
        private val scope: CoroutineScope,
        private val generator: ReplierTaskGenerator
) {
    private val lock = Any()
    private val tasks = linkedMapOf<String, ReplierTask>()

    fun startTask(request: ReplierTaskRequest): ReplierTask {
        val task =
                synchronized(lock) {
                    require(tasks[request.taskId] == null) {
                        "Replier task already exists: ${request.taskId}"
                    }
                    ReplierTask(
                                    request = request,
                                    scope = scope,
                                    generator = generator
                            )
                            .also { tasks[request.taskId] = it }
                }
        return task.start()
    }

    fun getTask(taskId: String): ReplierTask? = synchronized(lock) { tasks[taskId] }

    fun activeTaskIds(): List<String> =
            synchronized(lock) {
                tasks.values
                        .filter { !it.snapshot.isTerminal }
                        .map { it.request.taskId }
            }

    suspend fun waitForCompletion(taskId: String): ReplierTaskSnapshot? =
            getTask(taskId)?.waitForCompletion()

    suspend fun cancel(taskId: String): ReplierTaskSnapshot? =
            getTask(taskId)?.cancel()
}
