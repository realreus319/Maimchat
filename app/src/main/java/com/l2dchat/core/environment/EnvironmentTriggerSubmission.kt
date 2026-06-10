package com.l2dchat.core.environment

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.inbound.ContentBlock
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import com.l2dchat.core.trigger.contentBlocksPayload
import com.l2dchat.core.trigger.validateTriggerMultimodal

data class EnvironmentTriggerSubmission(
        val routingKey: RoutingKey,
        val text: String,
        val priority: TriggerPriority = TriggerPriority.NORMAL,
        val timestampSeconds: Double = System.currentTimeMillis() / 1000.0,
        val messageId: String = generateEnvironmentTriggerMessageId(),
        val source: String = DEFAULT_SOURCE,
        val metadata: Map<String, Any?> = emptyMap(),
        val contentBlocks: List<ContentBlock> = emptyList(),
        val sessionId: String? = null,
        val liveImage: Boolean = false
) {
    init {
        require(text.isNotBlank()) { "Environment trigger text must not be blank" }
        require(messageId.isNotBlank()) { "Environment trigger messageId must not be blank" }
        require(source.isNotBlank()) { "Environment trigger source must not be blank" }
        require(contentBlocks.all { it.type == "text" || it.type == "image_url" }) {
            "Environment trigger content blocks only support text and image_url"
        }
    }

    fun toTrigger(): Trigger {
        val payload =
                linkedMapOf<String, Any?>(
                        "text" to text,
                        "source" to source,
                        "metadata" to metadata,
                        "sender_id" to metadata["sender_id"],
                        "session_id" to sessionId,
                        "live_image" to liveImage
                )
        if (contentBlocks.isNotEmpty()) {
            payload["content_blocks"] = contentBlocksPayload(contentBlocks)
        }
        validateTriggerMultimodal(payload)
        return Trigger(
                contextId = routingKey.contextId,
                agentId = routingKey.agentId,
                messageId = messageId,
                triggerType = TriggerType.ENV,
                priority = priority,
                timestampSeconds = timestampSeconds,
                payload = payload
        )
    }

    companion object {
        const val DEFAULT_SOURCE: String = "android_environment"

        fun generateEnvironmentTriggerMessageId(): String =
                "env_${System.currentTimeMillis()}_${(Math.random() * 1000).toInt()}"
    }
}
