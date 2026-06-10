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
        }
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
            fallbackPlatform: String,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): Boolean {
        if (!shouldReply(inbound)) {
            return false
        }
        val inboundMessage =
                inboundBuilder.fromMessageBase(
                        message = inbound,
                        fallbackPlatform = fallbackPlatform
                )
        val pending =
                registerPending(
                        message = inboundMessage,
                        inbound = inbound,
                        fallbackPlatform = fallbackPlatform,
                        fallbackAgentName = fallbackAgentName,
                        replySink = replySink
                )

        try {
            perceptionDispatcher.submit(inboundMessage)
            return pending.completion.await()
        } catch (throwable: Throwable) {
            removePending(inboundMessage.toPendingKey())
            throw throwable
        }
    }

    suspend fun submitEnvironmentTrigger(
            submission: EnvironmentTriggerSubmission,
            fallbackPlatform: String,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): Boolean {
        registerEnvironmentReplyTarget(
                routingKey = submission.routingKey,
                target =
                        RuntimeReplyTarget(
                                fallbackPlatform = fallbackPlatform,
                                fallbackAgentName = fallbackAgentName,
                                replySink = replySink
                        )
        )
        replyLayerFactory.submitTrigger(submission.toTrigger())
        return true
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
            fallbackPlatform: String,
            fallbackAgentName: String?,
            replySink: ReplySink
    ): PendingRuntimeReply {
        val key = message.toPendingKey()
        val pending =
                PendingRuntimeReply(
                        inbound = inbound,
                        fallbackPlatform = fallbackPlatform,
                        fallbackAgentName = fallbackAgentName,
                        replySink = replySink,
                        completion = CompletableDeferred()
                )
        synchronized(pendingLock) {
            require(!pendingReplies.containsKey(key)) {
                "Duplicate pending inbound message ${message.messageId} for ${message.routingKey}"
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
                            fallbackPlatform = pending.fallbackPlatform,
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
        if (reply.trigger.triggerType != TriggerType.ENV) {
            return
        }
        val target =
                synchronized(environmentReplyTargetLock) {
                    environmentReplyTargets[reply.routingKey]
                } ?: return
        target.replySink.send(
                createEnvironmentReply(
                        trigger = reply.trigger,
                        fallbackPlatform = target.fallbackPlatform,
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
            val fallbackPlatform: String,
            val fallbackAgentName: String?,
            val replySink: ReplySink
    )

    private data class PendingRuntimeReply(
            val inbound: MessageBase,
            val fallbackPlatform: String,
            val fallbackAgentName: String?,
            val replySink: ReplySink,
            val completion: CompletableDeferred<Boolean>
    )

    fun createReply(
            inbound: MessageBase,
            fallbackPlatform: String,
            fallbackAgentName: String?
    ): MessageBase {
        val perception =
                perceptionProcessor.process(
                        inboundBuilder.fromMessageBase(
                                message = inbound,
                                fallbackPlatform = fallbackPlatform
                        )
                )
        return createReply(
                inbound = inbound,
                fallbackPlatform = fallbackPlatform,
                fallbackAgentName = fallbackAgentName,
                inboundText = perception.parsedMessage.text
        )
    }

    private fun createEnvironmentReply(
            trigger: Trigger,
            fallbackPlatform: String,
            fallbackAgentName: String?,
            replyText: String
    ): MessageBase {
        val platform = fallbackPlatform.ifBlank { "android" }
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val assistantUser =
                UserInfo(
                        platform = platform,
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
                        platform = platform,
                        userId = trigger.metadataString("receiver_user_id") ?: "local_user",
                        userNickname =
                                trigger.metadataString("receiver_user_name")
                                        ?: trigger.metadataString("receiver_user_nickname")
                                        ?: "用户"
                )
        val messageInfo =
                BaseMessageInfo(
                        platform = platform,
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
                                        "migration_phase" to "env_trigger",
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
            fallbackPlatform: String,
            fallbackAgentName: String?,
            inboundText: String
    ): MessageBase {
        val platform = inbound.messageInfo.platform ?: fallbackPlatform
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val replyText = inboundText.ifBlank { "本地回复运行时已接管聊天链路。" }

        val assistantUser =
                normalizeUser(
                        inbound.messageInfo.receiverInfo?.userInfo,
                        platform,
                        fallbackId = "local_agent",
                        fallbackName = agentName
                )
        val requesterUser =
                normalizeUser(
                        inbound.messageInfo.senderInfo?.userInfo,
                        platform,
                        fallbackId = "local_user",
                        fallbackName = "用户"
                )
        val senderInfo = SenderInfo(groupInfo = inbound.messageInfo.groupInfo, userInfo = assistantUser)
        val receiverInfo = ReceiverInfo(groupInfo = inbound.messageInfo.groupInfo, userInfo = requesterUser)
        val messageInfo =
                BaseMessageInfo(
                        platform = platform,
                        messageId = generateMessageId(),
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
            platform: String,
            fallbackId: String,
            fallbackName: String
    ): UserInfo =
            UserInfo(
                    platform = user?.platform ?: platform,
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
        private fun fixedReplyPlannerProcessor(): PlannerTriggerProcessor =
                PlannerTriggerProcessor { context ->
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
