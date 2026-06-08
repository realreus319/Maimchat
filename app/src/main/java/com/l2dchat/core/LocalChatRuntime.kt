package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.FormatInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.reply.ReplySink

/**
 * First local runtime slice.
 *
 * This is intentionally small: it proves the app can receive a local standard
 * message and emit a local assistant standard message without a backend.
 */
class LocalChatRuntime {

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
        replySink.send(
                createReply(
                        inbound = inbound,
                        fallbackPlatform = fallbackPlatform,
                        fallbackAgentName = fallbackAgentName
                )
        )
        return true
    }

    fun createReply(
            inbound: MessageBase,
            fallbackPlatform: String,
            fallbackAgentName: String?
    ): MessageBase {
        val platform = inbound.messageInfo.platform ?: fallbackPlatform
        val agentName = fallbackAgentName?.takeIf { it.isNotBlank() } ?: "Maimchat"
        val inboundText = extractText(inbound.messageSegment).ifBlank { inbound.rawMessage.orEmpty() }
        val replyText =
                if (inboundText.isBlank()) {
                    "本地回复运行时已接管聊天链路。"
                } else {
                    "本地回复运行时已接收：$inboundText"
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

    private fun extractText(segment: Seg): String =
            when (segment.type) {
                "text" -> segment.data.toString()
                "seglist" -> {
                    @Suppress("UNCHECKED_CAST")
                    val list = segment.data as? List<Seg>
                    list?.joinToString(" ") { extractText(it) }.orEmpty().trim()
                }
                else -> ""
            }

    private fun generateMessageId(): String =
            "local_${System.currentTimeMillis()}_${(Math.random() * 1000).toInt()}"
}
