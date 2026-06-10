package com.l2dchat.core.tools

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.llm.LlmToolDefinition
import kotlinx.coroutines.withTimeoutOrNull

object DecisionTools {
    fun defaultTools(
            taskManager: ReplierTaskManager,
            gson: Gson = Gson()
    ): List<Tool> =
            listOf(
                    WaitForTool(taskManager = taskManager, gson = gson),
                    AdoptBackgroundReplyTool(taskManager = taskManager, gson = gson),
                    KillBackgroundReplyTool(taskManager = taskManager, gson = gson)
            )
}

class WaitForTool(
        private val taskManager: ReplierTaskManager,
        private val gson: Gson = Gson()
) : Tool {
    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Wait for a replier task to finish and return the latest task state.",
                    parameters = taskIdParameters("Optional maximum wait time in milliseconds.")
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val taskId = arguments.requiredTaskId()
        val snapshot =
                awaitSnapshot(taskManager, taskId, arguments.timeoutMillisOrNull())
                        ?: return missingTaskResult(taskId)
        return ToolExecutionResult(llmContent = gson.toJson(snapshot.toJson()))
    }

    companion object {
        const val NAME: String = "wait_for"
    }
}

class AdoptBackgroundReplyTool(
        private val taskManager: ReplierTaskManager,
        private val gson: Gson = Gson()
) : Tool {
    override val allowedModes: Set<ToolExecutionMode> = setOf(ToolExecutionMode.DECISION)

    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Adopt a completed background replier task and return its reply text to the planner.",
                    parameters = taskIdParameters("Optional maximum wait time in milliseconds.")
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val taskId = arguments.requiredTaskId()
        val snapshot =
                awaitSnapshot(taskManager, taskId, arguments.timeoutMillisOrNull())
                        ?: return missingTaskResult(taskId)
        if (snapshot.state != ReplierTaskState.COMPLETED || snapshot.replyText.isNullOrBlank()) {
            return ToolExecutionResult(
                    llmContent = gson.toJson(snapshot.toJson(error = "Background reply is not completed")),
                    isError = true,
                    metadata = mapOf("task_id" to taskId, "state" to snapshot.state.name)
            )
        }
        return ToolExecutionResult(
                llmContent =
                        gson.toJson(
                                snapshot.toJson().apply {
                                    addProperty("adopted", true)
                                    addProperty("sent", false)
                                }
                        ),
                replyText = snapshot.replyText,
                sent = false,
                metadata = mapOf("task_id" to taskId, "state" to snapshot.state.name)
        )
    }

    companion object {
        const val NAME: String = "adopt_background_reply"
    }
}

class KillBackgroundReplyTool(
        private val taskManager: ReplierTaskManager,
        private val gson: Gson = Gson()
) : Tool {
    override val allowedModes: Set<ToolExecutionMode> = setOf(ToolExecutionMode.DECISION)

    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description = "Cancel a background replier task that should not be sent.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "required" to listOf("task_id"),
                                    "properties" to
                                            mapOf(
                                                    "task_id" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to "The replier task id."
                                                            )
                                            )
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val taskId = arguments.requiredTaskId()
        val snapshot = taskManager.cancel(taskId) ?: return missingTaskResult(taskId)
        return ToolExecutionResult(
                llmContent =
                        gson.toJson(
                                snapshot.toJson().apply {
                                    addProperty("killed", snapshot.state == ReplierTaskState.CANCELLED)
                                }
                        ),
                metadata = mapOf("task_id" to taskId, "state" to snapshot.state.name)
        )
    }

    companion object {
        const val NAME: String = "kill_background_reply"
    }
}

private fun taskIdParameters(timeoutDescription: String): Map<String, Any> =
        mapOf(
                "type" to "object",
                "additionalProperties" to false,
                "required" to listOf("task_id"),
                "properties" to
                        mapOf(
                                "task_id" to
                                        mapOf(
                                                "type" to "string",
                                                "description" to "The replier task id."
                                        ),
                                "timeout_millis" to
                                        mapOf(
                                                "type" to "integer",
                                                "minimum" to 1,
                                                "description" to timeoutDescription
                                        )
                        )
        )

private suspend fun awaitSnapshot(
        taskManager: ReplierTaskManager,
        taskId: String,
        timeoutMillis: Long?
): ReplierTaskSnapshot? {
    val task = taskManager.getTask(taskId) ?: return null
    if (task.snapshot.isTerminal) {
        return task.snapshot
    }
    return if (timeoutMillis == null) {
        task.waitForCompletion()
    } else {
        withTimeoutOrNull(timeoutMillis) { task.waitForCompletion() } ?: task.snapshot
    }
}

private fun missingTaskResult(taskId: String): ToolExecutionResult =
        ToolExecutionResult(
                llmContent = "Replier task not found: $taskId",
                isError = true,
                metadata = mapOf("task_id" to taskId)
        )

private fun ReplierTaskSnapshot.toJson(error: String? = errorMessage): JsonObject =
        JsonObject().apply {
            addProperty("taskId", taskId)
            addProperty("state", state.name)
            addProperty("previewText", previewText)
            replyText?.let { addProperty("replyText", it) }
            error?.let { addProperty("error", it) }
        }

private fun JsonObject.requiredTaskId(): String {
    val taskId = stringOrNull("task_id")?.trim().orEmpty()
    require(taskId.isNotBlank()) { "task_id must not be blank" }
    return taskId
}

private fun JsonObject.timeoutMillisOrNull(): Long? =
        longOrNull("timeout_millis")?.also {
            require(it > 0) { "timeout_millis must be positive" }
        }

private fun JsonObject.stringOrNull(name: String): String? =
        get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asString

private fun JsonObject.longOrNull(name: String): Long? {
    val primitive = get(name)?.takeIf { !it.isJsonNull && it.isJsonPrimitive }?.asJsonPrimitive
            ?: return null
    return runCatching {
            when {
                primitive.isNumber -> primitive.asLong
                primitive.isString -> primitive.asString.trim().toLong()
                else -> null
            }
        }
        .getOrNull()
}
