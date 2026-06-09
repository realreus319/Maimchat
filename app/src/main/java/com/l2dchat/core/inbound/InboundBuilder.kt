package com.l2dchat.core.inbound

import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.Seg
import com.l2dchat.core.context.RoutingKey

class InboundBuilder {
    fun fromMessageBase(
            message: MessageBase,
            fallbackContextId: String? = null,
            fallbackAgentId: String? = null,
            fallbackPlatform: String = "android"
    ): InboundMessage {
        val info = message.messageInfo
        val groupInfo = info.groupInfo ?: info.senderInfo?.groupInfo ?: info.receiverInfo?.groupInfo
        val senderUser = info.senderInfo?.userInfo ?: info.userInfo
        val receiverUser = info.receiverInfo?.userInfo
        val platform =
                info.platform
                        ?: senderUser?.platform
                        ?: receiverUser?.platform
                        ?: groupInfo?.platform
                        ?: fallbackPlatform
        val contextId =
                groupInfo?.groupId?.takeIf { it.isNotBlank() }
                        ?: senderUser?.userId?.takeIf { it.isNotBlank() }
                        ?: fallbackContextId?.takeIf { it.isNotBlank() }
                        ?: RoutingKey.DEFAULT_CONTEXT_ID
        val agentId =
                receiverUser?.userId?.takeIf { it.isNotBlank() }
                        ?: fallbackAgentId?.takeIf { it.isNotBlank() }
                        ?: RoutingKey.DEFAULT_AGENT_ID
        val text = extractText(message.messageSegment).ifBlank { message.rawMessage.orEmpty() }
        val blocks = normalizeContentBlocks(message.messageSegment, text)

        return InboundMessage(
                routingKey = RoutingKey(contextId = contextId, agentId = agentId),
                messageId =
                        info.messageId?.takeIf { it.isNotBlank() }
                                ?: stableMessageId(message.toJsonString()),
                platform = platform,
                senderId = senderUser?.userId?.takeIf { it.isNotBlank() } ?: "unknown",
                senderName =
                        senderUser?.userNickname?.takeIf { it.isNotBlank() }
                                ?: senderUser?.userCardname?.takeIf { it.isNotBlank() },
                text = text,
                contentBlocks = blocks,
                timestampSeconds = info.time ?: (System.currentTimeMillis() / 1000.0),
                rawMessage = message,
                isGroupChat = groupInfo?.groupId?.isNotBlank() == true
        )
    }

    fun parseRoom(room: String): RoomRoute {
        if (room.isBlank()) {
            throw IllegalArgumentException("Empty room string")
        }

        if (room.startsWith("private:")) {
            val parts = room.split(":")
            if (parts.size < 2 || parts[1].isBlank()) {
                throw IllegalArgumentException("Invalid private room format: $room")
            }
            val agentId = parts.getOrNull(2)?.takeIf { it.isNotBlank() }
            return RoomRoute(contextId = parts[1], agentId = agentId, isGroupChat = false)
        }

        if (room.startsWith("group:")) {
            val parts = room.split(":")
            if (parts.size < 2 || parts[1].isBlank()) {
                throw IllegalArgumentException("Invalid group room format: $room")
            }
            return RoomRoute(contextId = parts[1], agentId = null, isGroupChat = true)
        }

        throw IllegalArgumentException("Unknown room format: $room")
    }

    fun validateMultimodalMirror(text: String, blocks: List<ContentBlock>): InboundValidationResult {
        val imageCount = countImageBlocks(blocks)
        if (imageCount == 0) {
            return InboundValidationResult(isValid = true)
        }
        val markerCount = countInlineImageMarkers(text)
        return if (markerCount == imageCount) {
            InboundValidationResult(isValid = true)
        } else {
            InboundValidationResult(
                    isValid = false,
                    errors = listOf("marker ($markerCount) != image ($imageCount)")
            )
        }
    }

    fun normalizeContentBlocks(segment: Seg, fallbackText: String): List<ContentBlock> {
        val blocks =
                when (segment.type) {
                    "text" -> listOf(ContentBlock.text(segment.data.toString()))
                    "seglist" -> {
                        @Suppress("UNCHECKED_CAST")
                        val segments = segment.data as? List<Seg>
                        segments?.map { it.toContentBlock() }.orEmpty()
                    }
                    else -> listOf(segment.toContentBlock())
                }.filterNot { it.type == "text" && it.text.isNullOrBlank() }

        if (blocks.any { it.type == "text" } || fallbackText.isBlank()) {
            return blocks
        }
        return listOf(ContentBlock.text(fallbackText)) + blocks
    }

    fun extractText(segment: Seg): String =
            when (segment.type) {
                "text" -> segment.data.toString()
                "seglist" -> {
                    @Suppress("UNCHECKED_CAST")
                    val segments = segment.data as? List<Seg>
                    segments?.joinToString(" ") { extractText(it) }.orEmpty().trim()
                }
                else -> ""
            }

    fun countInlineImageMarkers(text: String): Int = INLINE_IMAGE_REGEX.findAll(text).count()

    fun countImageBlocks(blocks: List<ContentBlock>): Int =
            blocks.count { it.type == "image_url" }

    private fun Seg.toContentBlock(): ContentBlock =
            when (type) {
                "text" -> ContentBlock.text(data.toString())
                "image", "realtime_image", "image_url" -> ContentBlock.imageUrl(data.toString())
                else -> ContentBlock.opaque(type = type, data = data.toString())
            }

    private fun stableMessageId(json: String): String = "inbound_${Integer.toHexString(json.hashCode())}"

    companion object {
        private val INLINE_IMAGE_REGEX = Regex("""\[image(\d+)\]""")
    }
}
