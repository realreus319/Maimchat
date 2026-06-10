package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.perception.TriggerSink
import com.l2dchat.core.trigger.Trigger
import kotlinx.coroutines.CoroutineScope

class ReplyLayerFactory(
        private val scope: CoroutineScope,
        private val processorFactory: (RoutingKey) -> PlannerTriggerProcessor = {
            NoopPlannerTriggerProcessor
        },
        private val replySinkFactory: (RoutingKey) -> PlannerReplySink = { PlannerReplySink {} },
        private val onError: (RoutingKey, Trigger, Throwable) -> Unit = { _, _, _ -> }
) : TriggerSink {
    private val lock = Any()
    private val loops = linkedMapOf<RoutingKey, PlannerLoop>()

    val loopCount: Int
        get() = synchronized(lock) { loops.size }

    override suspend fun submit(trigger: Trigger) {
        submitTrigger(trigger)
    }

    suspend fun submitTrigger(trigger: Trigger) {
        val loop =
                getOrCreateLoop(
                        RoutingKey(contextId = trigger.contextId, agentId = trigger.agentId)
                )
        loop.submitTrigger(trigger)
    }

    fun getLoop(routingKey: RoutingKey): PlannerLoop? = synchronized(lock) { loops[routingKey] }

    fun getOrCreateLoop(routingKey: RoutingKey): PlannerLoop =
            synchronized(lock) {
                loops.getOrPut(routingKey) {
                    PlannerLoop(
                                    routingKey = routingKey,
                                    scope = scope,
                                    processor = processorFactory(routingKey),
                                    replySink = replySinkFactory(routingKey),
                                    onError = onError
                            )
                            .also { it.start() }
                }
            }

    suspend fun shutdown() {
        val activeLoops =
                synchronized(lock) {
                    val activeLoops = loops.values.toList()
                    loops.clear()
                    activeLoops
                }
        activeLoops.forEach { it.shutdown() }
    }
}
