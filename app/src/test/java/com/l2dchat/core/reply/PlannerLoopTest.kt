package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Test

class PlannerLoopTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `factory creates one loop per routing key`() {
        runBlocking {
            val factory = ReplyLayerFactory(scope = this)

            factory.submitTrigger(trigger("msg-1", contextId = "room-a", agentId = "agent-a"))
            factory.submitTrigger(trigger("msg-2", contextId = "room-b", agentId = "agent-b"))

            assertEquals(2, factory.loopCount)
            factory.shutdown()
        }
    }

    @Test
    fun `loop processes queued triggers by priority and timestamp`() {
        val order = mutableListOf<String>()
        val done = CompletableDeferred<List<String>>()

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    PlannerTriggerProcessor { context ->
                                        order.add(context.trigger.messageId)
                                        if (order.size == 3) {
                                            done.complete(order.toList())
                                        }
                                    }
                    )

            loop.submitTrigger(trigger("normal-late", priority = TriggerPriority.NORMAL, timestamp = 30.0))
            loop.submitTrigger(trigger("high", priority = TriggerPriority.HIGH, timestamp = 20.0))
            loop.submitTrigger(trigger("normal-early", priority = TriggerPriority.NORMAL, timestamp = 10.0))
            loop.start()

            assertEquals(
                    listOf("high", "normal-early", "normal-late"),
                    withTimeout(1_000L) { done.await() }
            )
            loop.shutdown()
        }
    }

    @Test
    fun `duplicate foreground replies are rejected`() {
        val statuses = mutableListOf<ReplySendStatus>()
        val replies = mutableListOf<PlannerReply>()
        val done = CompletableDeferred<Unit>()

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    PlannerTriggerProcessor { context ->
                                        statuses.add(context.sendReply("first").status)
                                        statuses.add(context.sendReply("second").status)
                                        done.complete(Unit)
                                    },
                            replySink = PlannerReplySink { replies.add(it) }
                    )
            loop.start()
            loop.submitTrigger(trigger("msg-1"))

            withTimeout(1_000L) { done.await() }

            assertEquals(
                    listOf(ReplySendStatus.SENT, ReplySendStatus.DUPLICATE_REPLIER_REJECTED),
                    statuses
            )
            assertEquals(listOf("first"), replies.map { it.text })
            loop.shutdown()
        }
    }

    @Test
    fun `stale foreground replies are rejected after msg interrupt`() {
        val firstStarted = CompletableDeferred<Unit>()
        val staleStatus = CompletableDeferred<ReplySendStatus>()
        val secondDone = CompletableDeferred<Unit>()

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    PlannerTriggerProcessor { context ->
                                        if (context.trigger.messageId == "first") {
                                            firstStarted.complete(Unit)
                                            try {
                                                awaitCancellation()
                                            } catch (_: CancellationException) {
                                                withContext(NonCancellable) {
                                                    staleStatus.complete(context.sendReply("late").status)
                                                }
                                            }
                                        } else {
                                            context.sendReply("second")
                                            secondDone.complete(Unit)
                                        }
                                    }
                    )
            loop.start()
            loop.submitTrigger(trigger("first"))
            withTimeout(1_000L) { firstStarted.await() }

            loop.submitTrigger(trigger("second"))

            assertEquals(
                    ReplySendStatus.STALE_FOREGROUND,
                    withTimeout(1_000L) { staleStatus.await() }
            )
            withTimeout(1_000L) { secondDone.await() }
            loop.shutdown()
        }
    }

    private fun trigger(
            messageId: String,
            contextId: String = routingKey.contextId,
            agentId: String = routingKey.agentId,
            priority: TriggerPriority = TriggerPriority.NORMAL,
            timestamp: Double = 1.0,
            triggerType: TriggerType = TriggerType.MSG
    ): Trigger =
            Trigger(
                    contextId = contextId,
                    agentId = agentId,
                    messageId = messageId,
                    triggerType = triggerType,
                    priority = priority,
                    timestampSeconds = timestamp,
                    payload = mapOf("text" to messageId)
            )
}
