package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.FormatInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.core.inbound.InboundBuilder
import com.l2dchat.core.inbound.InboundMessage
import com.l2dchat.core.perception.PerceptionDispatcher
import com.l2dchat.core.perception.PerceptionProcessor
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.NoopPlannerSessionStore
import com.l2dchat.core.reply.PlannerReply
import com.l2dchat.core.reply.PlannerReplySink
import com.l2dchat.core.reply.PlannerSessionStore
import com.l2dchat.core.reply.PlannerTriggerProcessor
import com.l2dchat.core.reply.ReplyLayerFactory
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.withTimeoutOrNull

/**
 * First local runtime slice.
 *
 * This is intentionally small: it proves the app can receive a local standard
 * message and emit a local assistant standard message without a backend.
 */
class LocalChatRuntime(
        scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
        private val perceptionStoreFactory: (RoutingKey) -> PerceptionStore? = { null },
        private val plannerSessionStoreFactory: (RoutingKey) -> PlannerSessionStore = {
            NoopPlannerSessionStore
        },
        private val plannerProcessorFactory: (RoutingKey) -> PlannerTriggerProcessor = {
            fixedReplyPlannerProcessor()
        },
        private val decisionPlannerProcessorFactory: (RoutingKey) -> PlannerTriggerProcessor? = {
            null
        },
        // When false, environment events (model switch, idle timer, snapshot, ...) drive runtime
        // state only and never start an LLM planner turn — avoiding background token cost.
        private val environmentRepliesEnabled: Boolean = true,
        // Hard upper bound for a single user turn so a stuck/hung provider can't block forever.
        private val turnTimeoutMillis: Long = DEFAULT_TURN_TIMEOUT_MILLIS
) {
    private val inboundBuilder = InboundBuilder()
    private val perceptionProcessor = PerceptionProcessor()
    private val pendingLock = Any()
    private val pendingReplies = linkedMapOf<PendingTriggerKey, PendingRuntimeReply>()
    private val environmentReplyTargetLock = Any()
    private val environmentReplyTargets = linkedMapOf<RoutingKey, RuntimeReplyTarget>()
    private val replyLayerFactory =
            ReplyLayerFactory(
                    scope = scope,
                    processorFactory = plannerProcessorFactory,
                    decisionProcessorFactory = decisionPlannerProcessorFactory,
                    replySinkFactory = { PlannerReplySink { reply -> handlePlannerReply(reply) } },
                    sessionStoreFactory = plannerSessionStoreFactory,
                    onError = { _, trigger, throwable ->
                        removePending(trigger.toPendingKey())?.completion?.completeExceptionally(throwable)
                    },
                    onCancelled = { _, trigger ->
                        removePending(trigger.toPendingKey())?.completion?.complete(false)
                    }
            )
    private val perceptionDispatcher =
            PerceptionDispatcher(
                    scope = scope,
                    triggerSink = replyLayerFactory,
                    storeFactory = perceptionStoreFactory,
                    onError = { _, message, throwable ->
                        removePending(message.toPendingKey())?.completion?.completeExceptionally(throwable)
                    }
            )

    fun shouldReply(message: MessageBase): Boolean {
        val messageType =
                message.messageInfo.additionalConfig
                        ?.get("message_type")
                        ?.toString()
                        ?.lowercase()
        return messageType == null || messageType == "chat"
    }

    suspend fun handleMessage(
            inbound: MessageBase,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): Boolean {
        if (!shouldReply(inbound)) {
            return false
        }
        val inboundMessage = inboundBuilder.fromMessageBase(message = inbound)
        val pending =
                registerPending(
                        message = inboundMessage,
                        inbound = inbound,
                        fallbackAgentName = fallbackAgentName,
                        replySink = replySink
                )

        try {
            perceptionDispatcher.submit(inboundMessage)
            return withTimeoutOrNull(turnTimeoutMillis) { pending.completion.await() }
                    ?: run {
                        removePending(inboundMessage.toPendingKey())
                        false
                    }
        } catch (throwable: Throwable) {
            removePending(inboundMessage.toPendingKey())
            throw throwable
        }
    }

    suspend fun submitEnvironmentTrigger(
            submission: EnvironmentTriggerSubmission,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): Boolean {
        if (!environmentRepliesEnabled) {
            return false
        }
        registerEnvironmentReplyTarget(
                routingKey = submission.routingKey,
                target =
                        RuntimeReplyTarget(
                                fallbackAgentName = fallbackAgentName,
                                replySink = replySink
                        )
        )
        replyLayerFactory.submitTrigger(submission.toTrigger())
        return true
    }

    /**
     * Submit a raw background trigger (e.g. a SYS worker-completion) into the planner and route its
     * reply to [replySink] via the routing-key reply target. Unlike [submitEnvironmentTrigger] this is
     * NOT gated by [environmentRepliesEnabled] — a worker result is a user-awaited answer, not an
     * ambient nudge, so it must always be delivered.
     */
    suspend fun submitBackgroundTrigger(
            trigger: Trigger,
            fallbackAgentName: String?,
            replySink: ReplySink
    ) {
        registerEnvironmentReplyTarget(
                routingKey = RoutingKey(contextId = trigger.contextId, agentId = trigger.agentId),
                target = RuntimeReplyTarget(fallbackAgentName = fallbackAgentName, replySink = replySink)
        )
        replyLayerFactory.submitTrigger(trigger)
    }

    suspend fun stopAndDrain() {
        perceptionDispatcher.stopAndDrain()
        replyLayerFactory.shutdown()
    }

    fun cancel() {
        perceptionDispatcher.cancel()
        replyLayerFactory.cancel()
        failPending(CancellationException("Local chat runtime cancelled"))
    }

    fun activePerceptionWorkerCount(): Int = perceptionDispatcher.workerCount

    fun activePlannerLoopCount(): Int = replyLayerFactory.loopCount

    private fun registerPending(
            message: InboundMessage,
            inbound: MessageBase,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): PendingRuntimeReply {
        val key = message.toPendingKey()
        val pending =
                PendingRuntimeReply(
                        inbound = inbound,
                        fallbackAgentName = fallbackAgentName,
                        replySink = replySink,
                        completion = CompletableDeferred()
                )
        synchronized(pendingLock) {
            // A duplicate resend of an already in-flight message reuses the existing pending
            // turn (both callers await the same result) instead of crashing the turn.
            pendingReplies[key]?.let {
                return it
            }
            pendingReplies[key] = pending
        }
        return pending
    }

    private suspend fun handlePlannerReply(reply: PlannerReply) {
        val pending = removePending(reply.trigger.toPendingKey())
        if (pending == null) {
            handleUnboundPlannerReply(reply)
            return
        }
        try {
            pending.replySink.send(
                    createReply(
                            inbound = pending.inbound,
                            fallbackAgentName = pending.fallbackAgentName,
                            inboundText = reply.text
                    )
            )
            pending.completion.complete(true)
        } catch (throwable: Throwable) {
            pending.completion.completeExceptionally(throwable)
            throw throwable
        }
    }

    private suspend fun handleUnboundPlannerReply(reply: PlannerReply) {
        // ENV = ambient nudges; SYS = background-worker completion (fired via submitBackgroundTrigger).
        // Both have no per-message pending turn, so they deliver through the routing-key reply target.
        if (reply.trigger.triggerType != TriggerType.ENV &&
                        reply.trigger.triggerType != TriggerType.SYS) {
            return
        }
        val target =
                synchronized(environmentReplyTargetLock) {
                    environmentReplyTargets[reply.routingKey]
                } ?: return
        target.replySink.send(
                createEnvironmentReply(
                        trigger = reply.trigger,
                        fallbackAgentName = target.fallbackAgentName,
                        replyText = reply.text
                )
        )
    }

    private fun removePending(key: PendingTriggerKey): PendingRuntimeReply? =
            synchronized(pendingLock) { pendingReplies.remove(key) }

    private fun registerEnvironmentReplyTarget(
            routingKey: RoutingKey,
            target: RuntimeReplyTarget
    ) {
        synchronized(environmentReplyTargetLock) { environmentReplyTargets[routingKey] = target }
    }

    private fun failPending(cause: Throwable) {
        val pending =
                synchronized(pendingLock) {
                    val pending = pendingReplies.values.toList()
                    pendingReplies.clear()
                    pending
                }
        pending.forEach { it.completion.completeExceptionally(cause) }
    }

    private fun InboundMessage.toPendingKey(): PendingTriggerKey =
            PendingTriggerKey(routingKey = routingKey, messageId = messageId)

    private fun Trigger.toPendingKey(): PendingTriggerKey =
            PendingTriggerKey(
                    routingKey = RoutingKey(contextId = contextId, agentId = agentId),
                    messageId = messageId
            )

    private data class PendingTriggerKey(val routingKey: RoutingKey, val messageId: String)

    private data class RuntimeReplyTarget(
            val fallbackAgentName: String?,
            val replySink: ReplySink
    )

    private data class PendingRuntimeReply(
            val inbound: MessageBase,
            val fallbackAgentName: String?,
            val replySink: ReplySink,
            val completion: CompletableDeferred<Boolean>
    )

    fun createReply(
            inbound: MessageBase,
            fallbackAgentName: String?
    ): MessageBase {
        val perception =
                perceptionProcessor.process(
                        inboundBuilder.fromMessageBase(message = inbound)
                )
        return createReply(
                inbound = inbound,
                fallbackAgentName = fallbackAgentName,
                inboundText = perception.parsedMessage.text
        )
    }

    private fun createEnvironmentReply(
            trigger: Trigger,
            fallbackAgentName: String?,
            replyText: String
    ): MessageBase {
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val assistantUser =
                UserInfo(
                        userId =
                                trigger.metadataString("agent_user_id")
                                        ?: trigger.agentId.takeIf { it.isNotBlank() }
                                        ?: "local_agent",
                        userNickname =
                                trigger.metadataString("agent_user_name")
                                        ?: agentName
                )
        val receiverUser =
                UserInfo(
                        userId = trigger.metadataString("receiver_user_id") ?: "local_user",
                        userNickname =
                                trigger.metadataString("receiver_user_name")
                                        ?: trigger.metadataString("receiver_user_nickname")
                                        ?: "用户"
                )
        val messageInfo =
                BaseMessageInfo(
                        messageId = generateMessageId(),
                        time = System.currentTimeMillis() / 1000.0,
                        senderInfo = SenderInfo(userInfo = assistantUser),
                        receiverInfo = ReceiverInfo(userInfo = receiverUser),
                        userInfo = assistantUser,
                        formatInfo =
                                FormatInfo(
                                        contentFormat = listOf("text"),
                                        acceptFormat = listOf("text", "image", "emoji", "voice")
                                ),
                        additionalConfig =
                                mapOf(
                                        "message_type" to "chat",
                                        "runtime" to "local",
                                        // SYS = background-worker completion → surface like a normal
                                        // reply (local_reply). ENV = ambient nudge → stays filtered.
                                        "migration_phase" to
                                                (if (trigger.triggerType == TriggerType.SYS) "local_reply"
                                                else "env_trigger"),
                                        "trigger_type" to trigger.triggerType.wireValue,
                                        "trigger_message_id" to trigger.messageId,
                                        "environment_source" to
                                                trigger.payload["source"].toString()
                                )
                )
        return MessageBase(messageInfo, Seg("text", replyText), replyText)
    }

    private fun createReply(
            inbound: MessageBase,
            fallbackAgentName: String?,
            inboundText: String
    ): MessageBase {
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val replyText = inboundText.ifBlank { "本地回复运行时已接管聊天链路。" }

        val assistantUser =
                normalizeUser(
                        inbound.messageInfo.receiverInfo?.userInfo,
                        fallbackId = "local_agent",
                        fallbackName = agentName
                )
        val requesterUser =
                normalizeUser(
                        inbound.messageInfo.senderInfo?.userInfo,
                        fallbackId = "local_user",
                        fallbackName = "用户"
                )
        val senderInfo = SenderInfo(groupInfo = inbound.messageInfo.groupInfo, userInfo = assistantUser)
        val receiverInfo = ReceiverInfo(groupInfo = inbound.messageInfo.groupInfo, userInfo = requesterUser)
        // Stable id derived from the inbound (user) message id so the reconciler can reproduce the
        // exact same id and dedup (addStandardMessage dedups by messageId). The inbound message id
        // equals the round's trigger_message_id.
        val replyMessageId =
                inbound.messageInfo.messageId?.takeIf { it.isNotBlank() }?.let { "reply_$it" }
                        ?: generateMessageId()
        val messageInfo =
                BaseMessageInfo(
                        messageId = replyMessageId,
                        time = System.currentTimeMillis() / 1000.0,
                        senderInfo = senderInfo,
                        receiverInfo = receiverInfo,
                        groupInfo = inbound.messageInfo.groupInfo,
                        userInfo = assistantUser,
                        formatInfo =
                                FormatInfo(
                                        contentFormat = listOf("text"),
                                        acceptFormat = listOf("text", "image", "emoji", "voice")
                                ),
                        additionalConfig =
                                mapOf(
                                        "message_type" to "chat",
                                        "runtime" to "local",
                                        "migration_phase" to "local_reply"
                                )
                )
        return MessageBase(messageInfo, Seg("text", replyText), replyText)
    }

    private fun normalizeUser(
            user: UserInfo?,
            fallbackId: String,
            fallbackName: String
    ): UserInfo =
            UserInfo(
                    userId = user?.userId?.takeIf { it.isNotBlank() } ?: fallbackId,
                    userNickname = user?.userNickname?.takeIf { it.isNotBlank() } ?: fallbackName,
                    userCardname = user?.userCardname
            )

    private fun generateMessageId(): String =
            "local_${System.currentTimeMillis()}_${(Math.random() * 1000).toInt()}"

    private fun Trigger.metadataString(key: String): String? {
        val metadata = payload["metadata"] as? Map<*, *> ?: return null
        return metadata[key]?.toString()?.takeIf { it.isNotBlank() }
    }

    companion object {
        // MUST stay >= PlannerLoop.TURN_BUDGET_MILLIS (420s): this bounds how long the message's
        // pending entry lives waiting for its reply. If it expires BEFORE the planner turn finishes
        // (a long worker task can run up to the 420s turn budget), the pending entry is removed and
        // the reply — when it finally arrives — is "unbound" and dropped (handleUnboundPlannerReply
        // only delivers ENV). That was the "worker finished but no reply" bug. Margin for the reply
        // to propagate after the turn caps.
        private const val DEFAULT_TURN_TIMEOUT_MILLIS: Long = 630_000L

        private fun fixedReplyPlannerProcessor(): PlannerTriggerProcessor =
                PlannerTriggerProcessor { context ->
                    // Without an LLM, only answer real user messages. Environment triggers are
                    // state signals, not chat turns — echoing them spams the UI.
                    if (context.trigger.triggerType == TriggerType.ENV) {
                        return@PlannerTriggerProcessor
                    }
                    val replyText =
                            if (context.triggerText.isBlank()) {
                                "本地回复运行时已接管聊天链路。"
                            } else {
                                "本地回复运行时已接收：${context.triggerText}"
                            }
                    context.sendReply(replyText)
                }
    }
}
