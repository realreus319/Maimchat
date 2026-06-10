package com.l2dchat.core.storage

import com.google.gson.Gson
import com.l2dchat.core.inbound.ContentBlock
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.RuntimeMessageMapper
import com.l2dchat.core.perception.ParsedMessage
import com.l2dchat.core.perception.PerceptionStore

interface MediaBlockStore {
    suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity)
}

class RoomMediaBlockStore(private val stateDao: RuntimeStateDao) : MediaBlockStore {
    override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) {
        stateDao.upsertMediaBlock(mediaBlock)
    }
}

class RoomPerceptionStore(
        private val messageDao: RuntimeMessageDao,
        private val mediaBlockStore: MediaBlockStore,
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) : PerceptionStore {
    constructor(
            messageDao: RuntimeMessageDao,
            stateDao: RuntimeStateDao,
            clockMillis: () -> Long = { System.currentTimeMillis() }
    ) : this(messageDao, RoomMediaBlockStore(stateDao), clockMillis)

    private val gson = Gson()

    override suspend fun persist(parsedMessage: ParsedMessage) {
        val timestampMillis = parsedMessage.timestampMillis()
        messageDao.appendStandardMessage(
                RuntimeMessageMapper.toStandardEntity(
                        message = parsedMessage.rawMessage.rawMessage,
                        contextId = parsedMessage.contextId,
                        agentId = parsedMessage.agentId,
                        fallbackTimestampMillis = timestampMillis
                )
        )

        parsedMessage.contentBlocks.forEachIndexed { index, block ->
            block.toMediaBlockEntity(parsedMessage, index, timestampMillis)?.let {
                mediaBlockStore.upsertMediaBlock(it)
            }
        }
    }

    private fun ParsedMessage.timestampMillis(): Long {
        val millis = timestampSeconds.takeIf { it > 0.0 }?.let { (it * 1000).toLong() }
        return millis ?: clockMillis()
    }

    private fun ContentBlock.toMediaBlockEntity(
            parsedMessage: ParsedMessage,
            sequence: Int,
            timestampMillis: Long
    ): MediaBlockEntity? {
        if (type == "text") {
            return null
        }
        val uri = mediaReference() ?: return null
        return MediaBlockEntity(
                mediaId = mediaId(parsedMessage.messageId, sequence, this),
                messageId = parsedMessage.messageId,
                contextId = parsedMessage.contextId,
                sequence = sequence,
                type = type,
                uri = uri,
                mimeType = mimeType(),
                metadataJson = gson.toJson(toPayloadMap()),
                createdAtMillis = timestampMillis
        )
    }

    private fun ContentBlock.mediaReference(): String? =
            imageUrl?.takeIf { it.isNotBlank() }
                    ?: data?.takeIf { it.isNotBlank() }
                    ?: text?.takeIf { it.isNotBlank() }

    private fun ContentBlock.mimeType(): String? =
            when (type) {
                "image_url", "emoji" -> "image/*"
                "voice" -> "audio/*"
                else -> null
            }

    private fun mediaId(messageId: String, sequence: Int, block: ContentBlock): String =
            "media_${messageId}_${sequence}_${Integer.toHexString(block.toPayloadMap().toString().hashCode())}"
}
