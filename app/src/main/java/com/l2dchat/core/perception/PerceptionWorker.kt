package com.l2dchat.core.perception

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.inbound.InboundMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch

class PerceptionWorker(
        private val routingKey: RoutingKey,
        private val scope: CoroutineScope,
        private val triggerSink: TriggerSink,
        private val processor: PerceptionProcessor = PerceptionProcessor(),
        private val store: PerceptionStore? = null,
        private val onError: (RoutingKey, InboundMessage, Throwable) -> Unit = { _, _, _ -> }
) {
    // Bounded with default SUSPEND overflow: caps memory under a flood while applying
    // backpressure to the producer rather than dropping user messages.
    private val queue = Channel<InboundMessage>(capacity = QUEUE_CAPACITY)
    private val job: Job =
            scope.launch {
                for (message in queue) {
                    processQueuedMessage(message)
                }
            }

    suspend fun submit(message: InboundMessage) {
        require(message.routingKey == routingKey) {
            "Message routing key ${message.routingKey} does not match worker $routingKey"
        }
        queue.send(message)
    }

    suspend fun stopAndDrain() {
        queue.close()
        job.join()
    }

    fun cancel() {
        queue.close()
        job.cancel()
    }

    private companion object {
        private const val QUEUE_CAPACITY = 128
    }

    private suspend fun processQueuedMessage(message: InboundMessage) {
        try {
            val result =
                    if (store != null) {
                        processor.processAndPersist(message, store)
                    } else {
                        processor.process(message)
                    }
            triggerSink.submit(result.trigger)
        } catch (throwable: Throwable) {
            onError(routingKey, message, throwable)
        }
    }
}
