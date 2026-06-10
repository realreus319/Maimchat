package com.l2dchat.chat

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.MotionTriggerRequest
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatMotionControllerTest {
    @Test
    fun `trigger emits motion command and reports accepted`() {
        var emitted: MotionCommand? = null
        val controller = ChatMotionController { command ->
            emitted = command
            true
        }

        val result =
                runBlocking {
                    controller.triggerMotion(
                            context(),
                            MotionTriggerRequest(group = "Idle", index = 0, loop = true)
                    )
                }

        assertTrue(result.accepted)
        assertEquals(MotionCommand(group = "Idle", index = 0, loop = true), emitted)
        assertEquals("chat_motion_event", result.metadata["controller"])
    }

    @Test
    fun `trigger propagates rejected emitter result`() {
        val controller = ChatMotionController { false }

        val result =
                runBlocking {
                    controller.triggerMotion(
                            context(),
                            MotionTriggerRequest(filePath = "mao/wave.motion3.json")
                    )
                }

        assertFalse(result.accepted)
    }

    private fun context(): ToolExecutionContext {
        val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")
        return ToolExecutionContext(
                loopId = routingKey.loopId,
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
                mode = ToolExecutionMode.NORMAL
        )
    }
}
