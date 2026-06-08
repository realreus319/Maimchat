package com.l2dchat.core.message

import com.l2dchat.chat.MessageBase

data class VisibleMessageRecord(
        val messageId: String,
        val content: String,
        val isFromUser: Boolean,
        val timestampMillis: Long,
        val motionGroup: String? = null,
        val motionIndex: Int? = null,
        val motionLoop: Boolean = false
)

object RuntimeMessageMapper {
    const val DEFAULT_CONTEXT_ID = "default"

    fun contextIdForModel(modelKey: String?): String =
            modelKey?.takeIf { it.isNotBlank() } ?: DEFAULT_CONTEXT_ID

    fun toVisibleEntity(
            record: VisibleMessageRecord,
            contextId: String,
            agentId: String?
    ): VisibleMessageEntity =
            VisibleMessageEntity(
                    messageId = record.messageId,
                    contextId = contextId,
                    agentId = agentId,
                    content = record.content,
                    isFromUser = record.isFromUser,
                    timestampMillis = record.timestampMillis,
                    motionGroup = record.motionGroup,
                    motionIndex = record.motionIndex,
                    motionLoop = record.motionLoop
            )

    fun toVisibleRecord(entity: VisibleMessageEntity): VisibleMessageRecord =
            VisibleMessageRecord(
                    messageId = entity.messageId,
                    content = entity.content,
                    isFromUser = entity.isFromUser,
                    timestampMillis = entity.timestampMillis,
                    motionGroup = entity.motionGroup,
                    motionIndex = entity.motionIndex,
                    motionLoop = entity.motionLoop
            )

    fun toStandardEntity(
            message: MessageBase,
            contextId: String,
            agentId: String?,
            fallbackTimestampMillis: Long = System.currentTimeMillis()
    ): StandardMessageEntity {
        val json = message.toJsonString()
        val info = message.messageInfo
        return StandardMessageEntity(
                messageId = info.messageId?.takeIf { it.isNotBlank() } ?: stableMessageId(json),
                contextId = contextId,
                agentId = agentId,
                platform = info.platform,
                senderUserId = info.senderInfo?.userInfo?.userId,
                receiverUserId = info.receiverInfo?.userInfo?.userId,
                timestampMillis = timestampMillis(info.time, fallbackTimestampMillis),
                rawText = message.rawMessage,
                messageJson = json
        )
    }

    fun toStandardMessage(entity: StandardMessageEntity): MessageBase =
            MessageBase.fromJsonString(entity.messageJson)

    private fun stableMessageId(json: String): String =
            "standard_${Integer.toHexString(json.hashCode())}"

    private fun timestampMillis(seconds: Double?, fallbackTimestampMillis: Long): Long {
        val millis = seconds?.takeIf { it > 0.0 }?.let { (it * 1000).toLong() }
        return millis ?: fallbackTimestampMillis
    }
}
