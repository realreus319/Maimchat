package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.FormatInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.inbound.InboundBuilder
import com.l2dchat.core.inbound.InboundMessage
import com.l2dchat.core.perception.PerceptionDispatcher
import com.l2dchat.core.perception.PerceptionProcessor
import com.l2dchat.core.perception.TriggerSink
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.trigger.Trigger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/**
 * First local runtime slice.
 *
 * This is intentionally small: it proves the app can receive a local standard
 * message and emit a local assistant standard message without a backend.
 */
class LocalChatRuntime(
        scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
) {
    private val inboundBuilder = InboundBuilder()
    private val perceptionProcessor = PerceptionProcessor()
    private val pendingLock = Any()
    private val pendingTriggers = linkedMapOf<PendingTriggerKey, CompletableDeferred<Trigger>>()
    private val perceptionDispatcher =
            PerceptionDispatcher(
                    scope = scope,
                    triggerSink =
                            object : TriggerSink {
                                override suspend fun submit(trigger: Trigger) {
                                    removePending(trigger.toPendingKey())?.complete(trigger)
                                }
                            },
                    onError = { _, message, throwable ->
                        removePending(message.toPendingKey())?.completeExceptionally(throwable)
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
        val pending = registerPending(inboundMessage)

        try {
            perceptionDispatcher.submit(inboundMessage)
            val trigger = pending.await()
            replySink.send(
                    createReply(
                            inbound = inbound,
                            fallbackPlatform = fallbackPlatform,
                            fallbackAgentName = fallbackAgentName,
                            inboundText = trigger.payload["text"]?.toString().orEmpty()
                    )
            )
            return true
        } catch (throwable: Throwable) {
            removePending(inboundMessage.toPendingKey())
            throw throwable
        }
    }

    suspend fun stopAndDrain() {
        perceptionDispatcher.stopAndDrain()
    }

    fun cancel() {
        perceptionDispatcher.cancel()
        failPending(CancellationException("Local chat runtime cancelled"))
    }

    fun activePerceptionWorkerCount(): Int = perceptionDispatcher.workerCount

    private fun registerPending(message: InboundMessage): CompletableDeferred<Trigger> {
        val key = message.toPendingKey()
        val pending = CompletableDeferred<Trigger>()
        synchronized(pendingLock) {
            require(!pendingTriggers.containsKey(key)) {
                "Duplicate pending inbound message ${message.messageId} for ${message.routingKey}"
            }
            pendingTriggers[key] = pending
        }
        return pending
    }

    private fun removePending(key: PendingTriggerKey): CompletableDeferred<Trigger>? =
            synchronized(pendingLock) { pendingTriggers.remove(key) }

    private fun failPending(cause: Throwable) {
        val pending =
                synchronized(pendingLock) {
                    val pending = pendingTriggers.values.toList()
                    pendingTriggers.clear()
                    pending
                }
        pending.forEach { it.completeExceptionally(cause) }
    }

    private fun InboundMessage.toPendingKey(): PendingTriggerKey =
            PendingTriggerKey(routingKey = routingKey, messageId = messageId)

    private fun Trigger.toPendingKey(): PendingTriggerKey =
            PendingTriggerKey(
                    routingKey = RoutingKey(contextId = contextId, agentId = agentId),
                    messageId = messageId
            )

    private data class PendingTriggerKey(val routingKey: RoutingKey, val messageId: String)

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

    private fun createReply(
            inbound: MessageBase,
            fallbackPlatform: String,
            fallbackAgentName: String?,
            inboundText: String
    ): MessageBase {
        val platform = inbound.messageInfo.platform ?: fallbackPlatform
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val normalizedText = inboundText.ifBlank { inbound.rawMessage.orEmpty() }
        val replyText =
                if (normalizedText.isBlank()) {
                    "本地回复运行时已接管聊天链路。"
                } else {
                    "本地回复运行时已接收：$normalizedText"
                }

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
                                        "migration_phase" to "fixed_reply"
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
}
