package com.l2dchat.chat

import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.environment.MotionTriggerResult
import com.l2dchat.core.tools.ToolExecutionContext

class ChatMotionController(
        private val emitMotion: (MotionCommand) -> Boolean
) : MotionController {
    override suspend fun triggerMotion(
            context: ToolExecutionContext,
            request: MotionTriggerRequest
    ): MotionTriggerResult {
        val command =
                MotionCommand(
                        group = request.group,
                        index = request.index,
                        filePath = request.filePath,
                        loop = request.loop
                )
        val accepted = emitMotion(command)
        return MotionTriggerResult(
                accepted = accepted,
                message =
                        if (accepted) {
                            "Live2D motion event was emitted."
                        } else {
                            "Live2D motion event was rejected."
                        },
                metadata =
                        mapOf(
                                "controller" to "chat_motion_event",
                                "context_id" to context.routingKey.contextId,
                                "agent_id" to context.routingKey.agentId
                        )
        )
    }
}
