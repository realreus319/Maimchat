package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger
import java.util.PriorityQueue
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class PlannerLoop(
        val routingKey: RoutingKey,
        private val scope: CoroutineScope,
        private val processor: PlannerTriggerProcessor = NoopPlannerTriggerProcessor,
        private val replySink: PlannerReplySink = PlannerReplySink {},
        private val onError: (RoutingKey, Trigger, Throwable) -> Unit = { _, _, _ -> }
) {
    val loopId: String = routingKey.toString()

    private val lock = Any()
    private val triggerQueue = PriorityQueue<Trigger>()
    private val signal = Channel<Unit>(Channel.UNLIMITED)

    private var loopJob: Job? = null
    private var currentJob: Job? = null
    private var loopState: PlannerLoopState = PlannerLoopState.IDLE
    private var foregroundEpoch: Int = 0
    private var foregroundReplySent: Boolean = false
    private var shutdownRequested: Boolean = false

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
                    triggerQueue.add(trigger)
                    currentJob.takeIf { shouldInterrupt }
                }

        jobToCancel?.cancel()
        signal.send(Unit)
    }

    suspend fun run() {
        try {
            while (scope.isActive) {
                val trigger = awaitNextTrigger() ?: break
                processQueuedTrigger(trigger)
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

    private suspend fun awaitNextTrigger(): Trigger? {
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

    private suspend fun processQueuedTrigger(trigger: Trigger) {
        val ownerEpoch =
                synchronized(lock) {
                    foregroundEpoch += 1
                    foregroundReplySent = false
                    loopState = PlannerLoopState.GENERATING
                    foregroundEpoch
                }
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
                    try {
                        processor.process(context)
                    } catch (throwable: CancellationException) {
                        throw throwable
                    } catch (throwable: Throwable) {
                        onError(routingKey, trigger, throwable)
                    }
                }

        synchronized(lock) { currentJob = job }
        job.start()

        try {
            job.join()
        } finally {
            synchronized(lock) {
                if (currentJob == job) {
                    currentJob = null
                    if (loopState == PlannerLoopState.GENERATING) {
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

        val status =
                synchronized(lock) {
                    when {
                        ownerEpoch != foregroundEpoch ->
                                ReplySendStatus.STALE_FOREGROUND
                        foregroundReplySent ->
                                ReplySendStatus.DUPLICATE_REPLIER_REJECTED
                        else -> {
                            foregroundReplySent = true
                            ReplySendStatus.SENT
                        }
                    }
                }
        if (status != ReplySendStatus.SENT) {
            return ReplySendResult(status = status, sent = false)
        }

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
}
