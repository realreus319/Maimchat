package com.l2dchat.core.trigger

import com.l2dchat.core.inbound.ContentBlock
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

enum class TriggerType(val wireValue: String) {
    MSG("msg"),
    ENV("env"),
    SYS("sys")
}

enum class TriggerPriority(val rank: Int) {
    HIGH(1),
    NORMAL(2),
    LOW(3)
}

data class Trigger(
        val contextId: String,
        val agentId: String,
        val messageId: String,
        val triggerType: TriggerType,
        val priority: TriggerPriority,
        val timestampSeconds: Double,
        val payload: Map<String, Any?> = emptyMap()
) : Comparable<Trigger> {
    fun canInterrupt(): Boolean = triggerType == TriggerType.MSG

    fun loopId(): String = "$contextId:$agentId"

    override fun compareTo(other: Trigger): Int {
        val priorityCompare = priority.rank.compareTo(other.priority.rank)
        if (priorityCompare != 0) {
            return priorityCompare
        }
        return timestampSeconds.compareTo(other.timestampSeconds)
    }

    fun toMap(): Map<String, Any?> =
            linkedMapOf(
                    "context_id" to contextId,
                    "agent_id" to agentId,
                    "message_id" to messageId,
                    "type" to triggerType.wireValue,
                    "priority" to priority.rank,
                    "timestamp" to timestampSeconds,
                    "payload" to payload
            )

    fun toSessionMessage(resolvedText: String): Map<String, Any?> {
        val normalizedText = resolvedText.trim()
        val prefix =
                when (triggerType) {
                    TriggerType.MSG ->
                            "[msg_trigger][${formatPromptTime(timestampSeconds)}]" +
                                    "[${formatSenderLabel(payload)}]"
                    TriggerType.ENV -> "[env_trigger]"
                    TriggerType.SYS -> "[sys_trigger]"
                }
        val content = if (normalizedText.isBlank()) prefix else "$prefix $normalizedText"
        val result =
                linkedMapOf<String, Any?>(
                        "role" to "user",
                        "content" to content,
                        "trigger_type" to triggerType.wireValue,
                        "message_id" to messageId,
                        "priority" to priority.rank,
                        "timestamp" to timestampSeconds
                )

        @Suppress("UNCHECKED_CAST")
        val contentBlocks = payload["content_blocks"] as? List<Map<String, Any>>
        if (!contentBlocks.isNullOrEmpty()) {
            result["content_blocks"] = formatSessionContentBlocks(contentBlocks, content)
        }
        return result
    }
}

fun validateTriggerMultimodal(payload: Map<String, Any?>) {
    @Suppress("UNCHECKED_CAST")
    val blocks = payload["content_blocks"] as? List<Map<String, Any?>> ?: return
    val imageCount = blocks.count { it["type"] == "image_url" }
    if (imageCount == 0) {
        return
    }
    val text = payload["text"]?.toString().orEmpty()
    val markerCount = INLINE_IMAGE_REGEX.findAll(text).count()
    // Inline [imageN] markers are optional: the common case is plain text plus appended image
    // blocks with no markers, which is valid. Only reject when markers ARE present but don't line
    // up with the available image blocks (a dangling inline reference to a missing image).
    if (markerCount > 0 && markerCount != imageCount) {
        throw IllegalArgumentException(
                "Inline image marker count ($markerCount) does not match image block count ($imageCount)."
        )
    }
}

fun contentBlocksPayload(blocks: List<ContentBlock>): List<Map<String, Any>> =
        blocks.map { it.toPayloadMap() }

private fun formatSessionContentBlocks(
        contentBlocks: List<Map<String, Any>>,
        contentText: String
): List<Map<String, Any>> {
    val formatted = mutableListOf<Map<String, Any>>()
    var textInserted = false

    contentBlocks.forEach { block ->
        val copy = block.toMutableMap()
        if (!textInserted && copy["type"] == "text") {
            copy["text"] = contentText
            textInserted = true
        }
        formatted.add(copy)
    }

    if (contentText.isNotBlank() && !textInserted) {
        formatted.add(0, mapOf("type" to "text", "text" to contentText))
    }
    return formatted
}

private fun formatPromptTime(timestampSeconds: Double): String =
        try {
            val millis = (timestampSeconds * 1000).toLong()
            SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(Date(millis))
        } catch (_: Exception) {
            "??:??:??"
        }

private fun formatSenderLabel(payload: Map<String, Any?>): String {
    val senderName = payload["sender_name"]?.toString()?.trim().orEmpty()
    val senderId = shortPromptId(payload["sender_id"])
    return when {
        senderName.isNotBlank() && senderId.isNotBlank() -> "$senderName($senderId)"
        senderName.isNotBlank() -> senderName
        senderId.isNotBlank() -> senderId
        else -> "user"
    }
}

fun shortPromptId(value: Any?, length: Int = 8): String {
    var text = value?.toString()?.trim().orEmpty()
    if (text.isBlank()) {
        return ""
    }
    UUID_ID_REGEX.find(text)?.let { return it.groupValues[1].take(length) }
    HEX_ID_REGEX.find(text)?.let { return it.groupValues[1].take(length) }
    listOf(":", "/").forEach { delimiter ->
        val parts = text.split(delimiter).filter { it.isNotBlank() }
        if (parts.isNotEmpty()) {
            text = parts.last()
        }
    }
    return text.take(length)
}

private val INLINE_IMAGE_REGEX = Regex("""\[image(\d+)\]""")
private val UUID_ID_REGEX =
        Regex("""\b([0-9a-fA-F]{8})-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b""")
private val HEX_ID_REGEX = Regex("""([0-9a-fA-F]{8,})""")
