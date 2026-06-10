package com.l2dchat.core.perception

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.inbound.InboundMessage
import kotlinx.coroutines.CoroutineScope

class PerceptionDispatcher(
        private val scope: CoroutineScope,
        private val triggerSink: TriggerSink,
        private val storeFactory: (RoutingKey) -> PerceptionStore? = { null },
        private val processorFactory: (RoutingKey) -> PerceptionProcessor = { PerceptionProcessor() },
        private val onError: (RoutingKey, InboundMessage, Throwable) -> Unit = { _, _, _ -> }
) {
    private val workerLock = Any()
    private val workers = linkedMapOf<RoutingKey, PerceptionWorker>()

    val workerCount: Int
        get() = synchronized(workerLock) { workers.size }

    suspend fun submit(message: InboundMessage) {
        val worker = synchronized(workerLock) { workerForLocked(message.routingKey) }
        worker.submit(message)
    }

    suspend fun stopAndDrain() {
        val activeWorkers =
                synchronized(workerLock) {
                    val activeWorkers = workers.values.toList()
                    workers.clear()
                    activeWorkers
                }
        activeWorkers.forEach { it.stopAndDrain() }
    }

    fun cancel() {
        val activeWorkers =
                synchronized(workerLock) {
                    val activeWorkers = workers.values.toList()
                    workers.clear()
                    activeWorkers
                }
        activeWorkers.forEach { it.cancel() }
    }

    private fun workerForLocked(routingKey: RoutingKey): PerceptionWorker =
            workers.getOrPut(routingKey) {
                PerceptionWorker(
                        routingKey = routingKey,
                        scope = scope,
                        triggerSink = triggerSink,
                        processor = processorFactory(routingKey),
                        store = storeFactory(routingKey),
                        onError = onError
                )
            }
}
