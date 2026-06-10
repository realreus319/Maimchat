package com.l2dchat.core.environment

import com.l2dchat.core.tools.ToolExecutionContext

data class MotionTriggerRequest(
        val group: String? = null,
        val index: Int? = null,
        val filePath: String? = null,
        val loop: Boolean = false
) {
    init {
        require(group == null || group.isNotBlank()) { "Motion group must not be blank" }
        require(index == null || index >= 0) { "Motion index must be non-negative" }
        require(filePath == null || filePath.isNotBlank()) {
            "Motion filePath must not be blank"
        }
        require(filePath != null || group != null) {
            "Motion trigger requires filePath or group"
        }
        require(group == null || index != null) {
            "Motion trigger requires index when group is provided"
        }
    }
}

data class MotionTriggerResult(
        val accepted: Boolean,
        val message: String,
        val metadata: Map<String, Any?> = emptyMap()
) {
    init {
        require(message.isNotBlank()) { "Motion trigger message must not be blank" }
    }
}

fun interface MotionController {
    suspend fun triggerMotion(
            context: ToolExecutionContext,
            request: MotionTriggerRequest
    ): MotionTriggerResult
}

object NoopMotionController : MotionController {
    override suspend fun triggerMotion(
            context: ToolExecutionContext,
            request: MotionTriggerRequest
    ): MotionTriggerResult =
            MotionTriggerResult(
                    accepted = false,
                    message = "No Live2D motion controller is available."
            )
}
