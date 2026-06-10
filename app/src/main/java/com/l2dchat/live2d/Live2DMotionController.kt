package com.l2dchat.live2d

import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.environment.MotionTriggerResult
import com.l2dchat.core.tools.ToolExecutionContext

class Live2DMotionController(
        private val lifecycleManagerProvider: () -> Live2DModelLifecycleManager?
) : MotionController {
    override suspend fun triggerMotion(
            context: ToolExecutionContext,
            request: MotionTriggerRequest
    ): MotionTriggerResult {
        val manager =
                lifecycleManagerProvider()
                        ?: return MotionTriggerResult(
                                accepted = false,
                                message = "Live2D model lifecycle manager is not available."
                        )
        val accepted =
                if (request.group != null && request.index != null) {
                    manager.playMotionByGroup(request.group, request.index, request.loop)
                } else {
                    manager.playMotionByFile(requireNotNull(request.filePath), request.loop)
                }
        return MotionTriggerResult(
                accepted = accepted,
                message =
                        if (accepted) {
                            "Live2D motion request was queued."
                        } else {
                            "Live2D motion request was rejected."
                        },
                metadata =
                        mapOf(
                                "controller" to "live2d",
                                "context_id" to context.routingKey.contextId,
                                "agent_id" to context.routingKey.agentId
                        )
        )
    }
}
