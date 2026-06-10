package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import java.util.PriorityQueue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class PlannerLoop(
        val routingKey: RoutingKey,
        private val scope: CoroutineScope,
        private val processor: PlannerTriggerProcessor = NoopPlannerTriggerProcessor,
        private val decisionProcessor: PlannerTriggerProcessor? = null,
        private val replySink: PlannerReplySink = PlannerReplySink {},
        private val sessionStore: PlannerSessionStore = NoopPlannerSessionStore,
        private val clockMillis: () -> Long = { System.currentTimeMillis() },
        private val onError: (RoutingKey, Trigger, Throwable) -> Unit = { _, _, _ -> },
        private val onCancelled: (RoutingKey, Trigger) -> Unit = { _, _ -> }
) {
    val loopId: String = routingKey.toString()

    private val lock = Any()
    private val triggerQueue = PriorityQueue<QueuedPlannerTrigger>()
    private val signal = Channel<Unit>(Channel.UNLIMITED)

    private var loopJob: Job? = null
    private var currentJob: Job? = null
    private var loopState: PlannerLoopState = PlannerLoopState.IDLE
    private var foregroundEpoch: Int = 0
    private var foregroundReplySent: Boolean = false
    private var shutdownRequested: Boolean = false
    private var currentRound: ActivePlannerRound? = null
    private var roundSequence: Int = 0

    val state: PlannerLoopState
        get() = synchronized(lock) { loopState }

    val queuedTriggerCount: Int
        get() = synchronized(lock) { triggerQueue.size }

    val foregroundEpochSnapshot: Int
        get() = synchronized(lock) { foregroundEpoch }

    fun start(): Job =
            synchronized(lock) {
                val activeJob = loopJob
                if (activeJob?.isActive == true) {
                    return@synchronized activeJob
                }
                val newJob = scope.launch { run() }
                loopJob = newJob
                newJob
            }

    suspend fun submitTrigger(trigger: Trigger) {
        require(trigger.contextId == routingKey.contextId && trigger.agentId == routingKey.agentId) {
            "Trigger ${trigger.loopId()} does not match planner loop $loopId"
        }

        val jobToCancel =
                synchronized(lock) {
                    check(!shutdownRequested) { "PlannerLoop $loopId is shut down" }
                    val shouldInterrupt =
                            trigger.canInterrupt() &&
                                    (loopState == PlannerLoopState.GENERATING ||
                                            loopState == PlannerLoopState.DECIDING)
                    if (shouldInterrupt) {
                        foregroundEpoch += 1
                    }
                    triggerQueue.add(
                            QueuedPlannerTrigger(
                                    trigger = trigger,
                                    requiresDecision = shouldInterrupt && decisionProcessor != null
                            )
                    )
                    currentJob.takeIf { shouldInterrupt }
                }

        jobToCancel?.cancel()
        signal.send(Unit)
    }

    suspend fun run() {
        try {
            while (scope.isActive) {
                val queuedTrigger = awaitNextTrigger() ?: break
                processQueuedTrigger(queuedTrigger)
            }
        } catch (_: CancellationException) {
            // Normal shutdown path.
        }
    }

    suspend fun shutdown() {
        val jobs =
                synchronized(lock) {
                    shutdownRequested = true
                    triggerQueue.clear()
                    listOfNotNull(currentJob, loopJob)
                }
        signal.close()
        jobs.forEach { it.cancelAndJoin() }
    }

    fun cancel() {
        val jobs =
                synchronized(lock) {
                    shutdownRequested = true
                    triggerQueue.clear()
                    listOfNotNull(currentJob, loopJob)
                }
        signal.close()
        jobs.forEach { it.cancel() }
    }

    private suspend fun awaitNextTrigger(): QueuedPlannerTrigger? {
        while (true) {
            val next =
                    synchronized(lock) {
                        if (shutdownRequested && triggerQueue.isEmpty()) {
                            return null
                        }
                        triggerQueue.poll()
                    }
            if (next != null) {
                return next
            }
            if (signal.receiveCatching().isClosed) {
                return null
            }
        }
    }

    private suspend fun processQueuedTrigger(queuedTrigger: QueuedPlannerTrigger) {
        val trigger = queuedTrigger.trigger
        val turnProcessor =
                if (queuedTrigger.requiresDecision) {
                    decisionProcessor ?: processor
                } else {
                    processor
                }
        val stateForTurn =
                if (queuedTrigger.requiresDecision) {
                    PlannerLoopState.DECIDING
                } else {
                    PlannerLoopState.GENERATING
                }
        val ownerEpoch =
                synchronized(lock) {
                    foregroundEpoch += 1
                    foregroundReplySent = false
                    loopState = stateForTurn
                    foregroundEpoch
                }
        val activeRound = createActiveRound(trigger, ownerEpoch)
        val context =
                PlannerTurnContext(
                        loopId = loopId,
                        routingKey = routingKey,
                        trigger = trigger,
                        foregroundEpoch = ownerEpoch,
                        sendReplyDelegate = ::sendReply
                )
        val job =
                scope.launch(start = CoroutineStart.LAZY) {
                    var finalState = PlannerSessionState.COMPLETED
                    try {
                        persistRoundStart(activeRound, trigger)
                        turnProcessor.process(context)
                    } catch (throwable: CancellationException) {
                        finalState = PlannerSessionState.CANCELLED
                        onCancelled(routingKey, trigger)
                        throw throwable
                    } catch (throwable: Throwable) {
                        finalState = PlannerSessionState.FAILED
                        onError(routingKey, trigger, throwable)
                    } finally {
                        withContext(NonCancellable) {
                            sessionStore.upsertRound(
                                    activeRound.toRoundRecord(
                                            state = finalState,
                                            updatedAtMillis = clockMillis()
                                    )
                            )
                        }
                    }
                }

        synchronized(lock) {
            currentJob = job
            currentRound = activeRound
        }
        job.start()

        try {
            job.join()
        } finally {
            synchronized(lock) {
                if (currentJob == job) {
                    currentJob = null
                    if (currentRound == activeRound) {
                        currentRound = null
                    }
                    if (loopState == PlannerLoopState.GENERATING ||
                                    loopState == PlannerLoopState.DECIDING) {
                        loopState = PlannerLoopState.IDLE
                    }
                }
            }
        }
    }

    private suspend fun sendReply(
            ownerEpoch: Int,
            trigger: Trigger,
            text: String
    ): ReplySendResult {
        val normalizedText = text.trim()
        if (normalizedText.isBlank()) {
            return ReplySendResult(status = ReplySendStatus.BLANK_REJECTED, sent = false)
        }

        var assistantMessage: PlannerSessionMessageRecord? = null
        val messageCreatedAtMillis = clockMillis()
        val status =
                synchronized(lock) {
                    when {
                        ownerEpoch != foregroundEpoch ->
                                ReplySendStatus.STALE_FOREGROUND
                        foregroundReplySent ->
                                ReplySendStatus.DUPLICATE_REPLIER_REJECTED
                        else -> {
                            foregroundReplySent = true
                            assistantMessage =
                                    currentRound
                                            ?.takeIf { it.ownerEpoch == ownerEpoch }
                                            ?.nextMessageRecord(
                                                    role = PlannerSessionRole.ASSISTANT,
                                                    content = normalizedText,
                                                    payload = mapOf("trigger_message_id" to trigger.messageId),
                                                    createdAtMillis = messageCreatedAtMillis
                                            )
                            ReplySendStatus.SENT
                        }
                    }
                }
        if (status != ReplySendStatus.SENT) {
            return ReplySendResult(status = status, sent = false)
        }

        assistantMessage?.let { sessionStore.appendMessage(it) }
        replySink.send(
                PlannerReply(
                        routingKey = routingKey,
                        trigger = trigger,
                        text = normalizedText,
                        foregroundEpoch = ownerEpoch
                )
        )
        return ReplySendResult(status = ReplySendStatus.SENT, sent = true)
    }

    private fun createActiveRound(trigger: Trigger, ownerEpoch: Int): ActivePlannerRound {
        val createdAtMillis = clockMillis()
        val sequence =
                synchronized(lock) {
                    roundSequence += 1
                    roundSequence
                }
        return ActivePlannerRound(
                roundId =
                        "round_${Integer.toHexString(loopId.hashCode())}_${trigger.messageId}_${createdAtMillis}_$sequence",
                ownerEpoch = ownerEpoch,
                contextId = routingKey.contextId,
                agentId = routingKey.agentId,
                triggerMessageId = trigger.messageId,
                createdAtMillis = createdAtMillis
        )
    }

    private suspend fun persistRoundStart(round: ActivePlannerRound, trigger: Trigger) {
        sessionStore.upsertRound(
                round.toRoundRecord(
                        state = PlannerSessionState.GENERATING,
                        updatedAtMillis = round.createdAtMillis
                )
        )
        sessionStore.appendMessage(
                round.nextMessageRecord(
                        role = PlannerSessionRole.TRIGGER,
                        content = trigger.triggerType.name,
                        payload = trigger.toSessionPayload(),
                        createdAtMillis = round.createdAtMillis
                )
        )
        sessionStore.appendMessage(
                round.nextMessageRecord(
                        role = PlannerSessionRole.USER,
                        content = trigger.payload["text"]?.toString().orEmpty(),
                        payload = mapOf("trigger_message_id" to trigger.messageId),
                        createdAtMillis = round.createdAtMillis
                )
        )
    }

    private fun Trigger.toSessionPayload(): Map<String, Any?> =
            linkedMapOf(
                    "trigger_type" to triggerType.name,
                    "priority" to priority.name,
                    "timestamp_seconds" to timestampSeconds,
                    "message_id" to messageId,
                    "payload" to payload
            )

    private data class QueuedPlannerTrigger(
            val trigger: Trigger,
            val requiresDecision: Boolean
    ) : Comparable<QueuedPlannerTrigger> {
        override fun compareTo(other: QueuedPlannerTrigger): Int =
                trigger.compareTo(other.trigger)
    }

    private data class ActivePlannerRound(
            val roundId: String,
            val ownerEpoch: Int,
            val contextId: String,
            val agentId: String,
            val triggerMessageId: String?,
            val createdAtMillis: Long,
            var nextSequence: Int = 0
    ) {
        fun toRoundRecord(state: String, updatedAtMillis: Long): PlannerRoundRecord =
                PlannerRoundRecord(
                        roundId = roundId,
                        contextId = contextId,
                        agentId = agentId,
                        triggerMessageId = triggerMessageId,
                        state = state,
                        createdAtMillis = createdAtMillis,
                        updatedAtMillis = updatedAtMillis
                )

        fun nextMessageRecord(
                role: String,
                content: String?,
                payload: Map<String, Any?>?,
                createdAtMillis: Long
        ): PlannerSessionMessageRecord {
            val sequence = nextSequence
            nextSequence += 1
            return PlannerSessionMessageRecord(
                    plannerMessageId = "planner_${roundId}_${sequence}_$role",
                    roundId = roundId,
                    sequence = sequence,
                    role = role,
                    content = content,
                    payload = payload,
                    createdAtMillis = createdAtMillis
            )
        }
    }
}
