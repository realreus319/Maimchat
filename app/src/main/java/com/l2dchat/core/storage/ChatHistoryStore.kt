package com.l2dchat.core.storage

import com.l2dchat.chat.MessageBase
import com.l2dchat.core.message.RuntimeMessageMapper
import com.l2dchat.core.message.VisibleMessageRecord

interface ChatHistoryStore {
    suspend fun appendVisibleMessage(
            contextId: String,
            agentId: String?,
            message: VisibleMessageRecord
    )

    suspend fun appendStandardMessage(
            contextId: String,
            agentId: String?,
            message: MessageBase,
            fallbackTimestampMillis: Long = System.currentTimeMillis()
    )

    suspend fun queryRecentVisibleMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<VisibleMessageRecord>

    suspend fun queryRecentStandardMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<MessageBase>

    suspend fun queryStandardHistoryUntilMessage(
            contextId: String,
            agentId: String?,
            messageId: String,
            limit: Int
    ): List<MessageBase>
}

class RoomChatHistoryStore(private val messageDao: RuntimeMessageDao) : ChatHistoryStore {
    override suspend fun appendVisibleMessage(
            contextId: String,
            agentId: String?,
            message: VisibleMessageRecord
    ) {
        messageDao.appendMessage(RuntimeMessageMapper.toVisibleEntity(message, contextId, agentId))
    }

    override suspend fun appendStandardMessage(
            contextId: String,
            agentId: String?,
            message: MessageBase,
            fallbackTimestampMillis: Long
    ) {
        messageDao.appendStandardMessage(
                RuntimeMessageMapper.toStandardEntity(
                        message = message,
                        contextId = contextId,
                        agentId = agentId,
                        fallbackTimestampMillis = fallbackTimestampMillis
                )
        )
    }

    override suspend fun queryRecentVisibleMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<VisibleMessageRecord> =
            messageDao.queryRecentMessages(contextId, agentId, limit).map {
                RuntimeMessageMapper.toVisibleRecord(it)
            }

    override suspend fun queryRecentStandardMessages(
            contextId: String,
            agentId: String?,
            limit: Int
    ): List<MessageBase> =
            messageDao.queryRecentStandardMessages(contextId, agentId, limit).map {
                RuntimeMessageMapper.toStandardMessage(it)
            }

    override suspend fun queryStandardHistoryUntilMessage(
            contextId: String,
            agentId: String?,
            messageId: String,
            limit: Int
    ): List<MessageBase> =
            messageDao.queryStandardHistoryUntilMessage(contextId, agentId, messageId, limit).map {
                RuntimeMessageMapper.toStandardMessage(it)
            }
}
