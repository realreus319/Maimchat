package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
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
    fun `factory wires decision processor into created loops`() {
        val firstStarted = CompletableDeferred<Unit>()
        val decisionDone = CompletableDeferred<String>()

        runBlocking {
            val factory =
                    ReplyLayerFactory(
                            scope = this,
                            processorFactory = {
                                PlannerTriggerProcessor { context ->
                                    if (context.trigger.messageId == "first") {
                                        firstStarted.complete(Unit)
                                        awaitCancellation()
                                    } else {
                                        context.sendReply("normal ${context.trigger.messageId}")
                                    }
                                }
                            },
                            decisionProcessorFactory = {
                                PlannerTriggerProcessor { context ->
                                    context.sendReply("decision ${context.trigger.messageId}")
                                    decisionDone.complete(context.trigger.messageId)
                                }
                            }
                    )
            factory.submitTrigger(trigger("first"))
            withTimeout(1_000L) { firstStarted.await() }

            factory.submitTrigger(trigger("second"))

            assertEquals("second", withTimeout(1_000L) { decisionDone.await() })
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

    @Test
    fun `interrupted msg can be processed by decision processor`() {
        val firstStarted = CompletableDeferred<Unit>()
        val decisionDone = CompletableDeferred<String>()
        val decisionState = CompletableDeferred<PlannerLoopState>()
        val thirdDone = CompletableDeferred<Unit>()
        val normalProcessed = mutableListOf<String>()

        runBlocking {
            lateinit var loop: PlannerLoop
            loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    PlannerTriggerProcessor { context ->
                                        if (context.trigger.messageId == "first") {
                                            firstStarted.complete(Unit)
                                            awaitCancellation()
                                        } else {
                                            normalProcessed.add(context.trigger.messageId)
                                            context.sendReply("normal ${context.trigger.messageId}")
                                            if (context.trigger.messageId == "third") {
                                                thirdDone.complete(Unit)
                                            }
                                        }
                                    },
                            decisionProcessor =
                                    PlannerTriggerProcessor { context ->
                                        decisionState.complete(loop.state)
                                        context.sendReply("decision ${context.trigger.messageId}")
                                        decisionDone.complete(context.trigger.messageId)
                                    }
                    )
            loop.start()
            loop.submitTrigger(trigger("first"))
            withTimeout(1_000L) { firstStarted.await() }

            loop.submitTrigger(trigger("second"))

            assertEquals("second", withTimeout(1_000L) { decisionDone.await() })
            assertEquals(PlannerLoopState.DECIDING, withTimeout(1_000L) { decisionState.await() })
            waitUntilIdle(loop)

            loop.submitTrigger(trigger("third"))

            withTimeout(1_000L) { thirdDone.await() }
            assertEquals(listOf("third"), normalProcessed)
            loop.shutdown()
        }
    }

    @Test
    fun `loop persists planner round trigger user and assistant messages`() {
        var now = 1_000L
        val store = RecordingPlannerSessionStore()
        val replyDone = CompletableDeferred<PlannerReply>()

        runBlocking {
            val loop =
                    PlannerLoop(
                            routingKey = routingKey,
                            scope = this,
                            processor =
                                    PlannerTriggerProcessor { context ->
                                        context.sendReply("stored reply")
                                    },
                            replySink = PlannerReplySink { replyDone.complete(it) },
                            sessionStore = store,
                            clockMillis = { now++ }
                    )
            loop.start()
            loop.submitTrigger(trigger("msg-1"))

            withTimeout(1_000L) { replyDone.await() }
            withTimeout(1_000L) { store.completed.await() }

            assertEquals(
                    listOf(PlannerSessionState.GENERATING, PlannerSessionState.COMPLETED),
                    store.rounds.map { it.state }
            )
            assertEquals("msg-1", store.rounds.first().triggerMessageId)
            assertEquals(
                    listOf(
                            PlannerSessionRole.TRIGGER,
                            PlannerSessionRole.USER,
                            PlannerSessionRole.ASSISTANT
                    ),
                    store.messages.map { it.role }
            )
            assertEquals("MSG", store.messages[0].content)
            assertEquals("msg-1", store.messages[1].content)
            assertEquals("stored reply", store.messages[2].content)
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

    private suspend fun waitUntilIdle(loop: PlannerLoop) {
        withTimeout(1_000L) {
            while (loop.state != PlannerLoopState.IDLE) {
                delay(10L)
            }
        }
    }

    private class RecordingPlannerSessionStore : PlannerSessionStore {
        val rounds = mutableListOf<PlannerRoundRecord>()
        val messages = mutableListOf<PlannerSessionMessageRecord>()
        val completed = CompletableDeferred<Unit>()

        override suspend fun upsertRound(round: PlannerRoundRecord) {
            rounds.add(round)
            if (round.state == PlannerSessionState.COMPLETED) {
                completed.complete(Unit)
            }
        }

        override suspend fun appendMessage(message: PlannerSessionMessageRecord) {
            messages.add(message)
        }
    }
}
