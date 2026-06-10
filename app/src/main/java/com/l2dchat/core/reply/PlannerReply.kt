package com.l2dchat.core.reply

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.Trigger

data class PlannerReply(
        val routingKey: RoutingKey,
        val trigger: Trigger,
        val text: String,
        val foregroundEpoch: Int
)

enum class ReplySendStatus {
    SENT,
    DUPLICATE_REPLIER_REJECTED,
    STALE_FOREGROUND,
    BLANK_REJECTED
}

data class ReplySendResult(
        val status: ReplySendStatus,
        val sent: Boolean
)

fun interface PlannerReplySink {
    suspend fun send(reply: PlannerReply)
}

fun interface PlannerTriggerProcessor {
    suspend fun process(context: PlannerTurnContext)
}

object NoopPlannerTriggerProcessor : PlannerTriggerProcessor {
    override suspend fun process(context: PlannerTurnContext) = Unit
}

class PlannerTurnContext internal constructor(
        val loopId: String,
        val routingKey: RoutingKey,
        val trigger: Trigger,
        val foregroundEpoch: Int,
        val roundId: String = "round_${trigger.messageId}",
        private val sendReplyDelegate: suspend (Int, Trigger, String) -> ReplySendResult
) {
    val triggerText: String
        get() = trigger.payload["text"]?.toString().orEmpty()

    suspend fun sendReply(text: String): ReplySendResult =
            sendReplyDelegate(foregroundEpoch, trigger, text)
}
