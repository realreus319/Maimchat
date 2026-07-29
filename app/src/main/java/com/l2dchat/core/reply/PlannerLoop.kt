package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import java.util.PriorityQueue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout

/**
 * Hard upper bound for ONE planner turn (planner LLM call(s) + any worker/replier tools). A turn must
 * always finalize so the round never hangs in 'generating'. Must comfortably fit a FULL worker turn:
 * the worker tool caps at ~300s, AND the planner needs post-worker LLM calls (decide → replier, each
 * with first-token timeout × retries) to turn the worker result into a reply. 420s (only ~120s left
 * after a 300s worker) was too tight — a 398s worker turn's post-step hit the budget and the reply was
 * lost. 600s leaves ~300s for the post-worker replier path.
 */
private const val TURN_BUDGET_MILLIS = 600_000L

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
    private val tlog = com.l2dchat.logging.L2DLogger.module(com.l2dchat.logging.LogModule.CHAT)

    private var loopJob: Job? = null
    private var currentJob: Job? = null
    private var loopState: PlannerLoopState = PlannerLoopState.IDLE

    // Trigger type of the turn currently being processed. A SYS turn is a background-worker completion
    // delivery — it must NOT be interrupted (forking a result delivery into a decision is pointless in
    // the async model), so shouldInterrupt excludes it.
    private var activeTriggerType: TriggerType? = null
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
                                            loopState == PlannerLoopState.DECIDING) &&
                                    // Never interrupt a background-worker completion (SYS) delivery...
                                    activeTriggerType != TriggerType.SYS &&
                                    // ...and a SYS completion itself never interrupts an in-progress
                                    // turn — it QUEUES and delivers after (forking a background result
                                    // into a decision/adopt path drops it ~1/3 of the time).
                                    trigger.triggerType != TriggerType.SYS
                    if (shouldInterrupt) {
                        foregroundEpoch += 1
                    }
                    triggerQueue.add(
                            QueuedPlannerTrigger(
                                    trigger = trigger,
                                    requiresDecision = shouldInterrupt && decisionProcessor != null
                            )
                    )
                    tlog.info(
                        "[trig] submit ${trigger.triggerType} ${trigger.messageId} " +
                            "interrupt=$shouldInterrupt state=$loopState active=$activeTriggerType " +
                            "qsize=${triggerQueue.size}"
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
                    foregroundReplySent = queuedTrigger.preReplySent
                    loopState = stateForTurn
                    activeTriggerType = trigger.triggerType
                    foregroundEpoch
                }
        tlog.info("[trig] process ${trigger.triggerType} ${trigger.messageId} decision=${queuedTrigger.requiresDecision}")
        val activeRound = createActiveRound(trigger, ownerEpoch)
        val context =
                PlannerTurnContext(
                        loopId = loopId,
                        routingKey = routingKey,
                        trigger = trigger,
                        foregroundEpoch = ownerEpoch,
                        roundId = activeRound.roundId,
                        sendReplyDelegate = ::sendReply
                )
        val job =
                scope.launch(start = CoroutineStart.LAZY) {
                    var finalState = PlannerSessionState.COMPLETED
                    try {
                        persistRoundStart(activeRound, trigger)
                        // Hard turn budget: a turn must never hang forever. The planner's per-LLM-call
                        // timeout is the user's model budget (can be large/unset), and a stalled network
                        // call — or any other never-returning suspend — would otherwise leave the round
                        // stuck in 'generating' with no reply. Bound the WHOLE turn so it always reaches
                        // the finalizer below; the budget is generous enough for a full worker turn.
                        withTimeout(TURN_BUDGET_MILLIS) { turnProcessor.process(context) }
                    } catch (throwable: TimeoutCancellationException) {
                        finalState = PlannerSessionState.FAILED
                        onError(routingKey, trigger, throwable)
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

        // Closing edge of the fork/decision loop (docs/plans/interrupt_fork_design.md). A DECISION
        // turn ONLY disposes the in-flight reply (`adopt` sends the old one / `kill` discards it). When
        // it settles (still the latest turn, not superseded/cancelled), ALWAYS return to NORMAL via a
        // real SYS trigger — its own slot in the prompt ("[sys_trigger] …"). The SYS text tells the
        // resumed NORMAL planner what happened:
        //   - kill  (no reply sent) -> "请重新回复最新消息" -> planner calls replier -> fresh reply
        //   - adopt (reply sent)    -> "已回复,只做事后操作" -> post-ops only; replier is duplicate-blocked
        //                              because the continuation carries preReplySent=true.
        val (settledDecision, decisionReplySent) =
                synchronized(lock) {
                    val settled =
                            queuedTrigger.requiresDecision &&
                                    ownerEpoch == foregroundEpoch &&
                                    !shutdownRequested
                    settled to foregroundReplySent
                }
        if (settledDecision) {
            val userText = trigger.payload["text"]?.toString()?.trim().orEmpty()
            val sysText =
                    if (decisionReplySent) {
                        "你刚才采用的后台回复已经发送给用户了，本轮回复已完成。不要再回复用户，只需做必要的" +
                                "事后处理（如更新心情、动作）后结束本回合。"
                    } else {
                        // Embed the interrupting message's own text so the resumed NORMAL planner/replier
                        // actually answers IT (the SYS trigger is the "current message"; without the text
                        // the replier only sees memory context and falls back to a generic greeting).
                        "用户刚刚发来一条更新的消息：「$userText」，它打断并取消了你上一条尚未完成的回复。" +
                                "请现在就直接回复这条最新消息（像平时回复用户一样，正常回复一次即可）。"
                    }
            val sysTrigger =
                    Trigger(
                            contextId = trigger.contextId,
                            agentId = trigger.agentId,
                            // Reuse the ORIGINAL interrupting message id so the continuation's reply
                            // threads back to that user message (completes its pending + displays). The
                            // trigger TYPE is still SYS, so the prompt renders "[sys_trigger] <text>".
                            messageId = trigger.messageId,
                            triggerType = TriggerType.SYS,
                            priority = TriggerPriority.HIGH,
                            timestampSeconds = clockMillis() / 1000.0,
                            payload = mapOf("text" to sysText)
                    )
            synchronized(lock) {
                triggerQueue.add(
                        QueuedPlannerTrigger(
                                trigger = sysTrigger,
                                requiresDecision = false,
                                preReplySent = decisionReplySent
                        )
                )
            }
            signal.send(Unit)
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
            val requiresDecision: Boolean,
            // Seeds foregroundReplySent for this turn. A SYS continuation after an `adopt` decision
            // carries `true` so the post-ops-only continuation can't re-send a duplicate reply; a
            // `kill` continuation carries `false` so it CAN generate the fresh reply.
            val preReplySent: Boolean = false
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
