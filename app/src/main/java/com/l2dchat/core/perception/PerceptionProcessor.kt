package com.l2dchat.core.perception

import com.l2dchat.core.inbound.InboundMessage
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import com.l2dchat.core.trigger.contentBlocksPayload
import com.l2dchat.core.trigger.validateTriggerMultimodal

data class PerceptionResult(
        val parsedMessage: ParsedMessage,
        val trigger: Trigger
)

class PerceptionProcessor(private val parser: MessageParser = DefaultMessageParser()) {
    fun process(message: InboundMessage): PerceptionResult {
        val parsed = parser.parse(message)
        return PerceptionResult(parsedMessage = parsed, trigger = createTrigger(parsed))
    }

    fun createTrigger(parsed: ParsedMessage): Trigger {
        val payload =
                linkedMapOf<String, Any?>(
                        "text" to parsed.text,
                        "sender_id" to parsed.senderId,
                        "sender_name" to parsed.senderName,
                        "is_command" to parsed.isCommand,
                        "command_name" to parsed.commandName,
                        "command_args" to parsed.commandArgs,
                        "mentioned_names" to parsed.mentionedNames
                )
        if (parsed.contentBlocks.isNotEmpty()) {
            payload["content_blocks"] = contentBlocksPayload(parsed.contentBlocks)
        }
        validateTriggerMultimodal(payload)

        return Trigger(
                contextId = parsed.contextId,
                agentId = parsed.agentId,
                messageId = parsed.messageId,
                triggerType = TriggerType.MSG,
                priority =
                        if (parsed.isMentioned) {
                            TriggerPriority.HIGH
                        } else {
                            TriggerPriority.NORMAL
                        },
                timestampSeconds = parsed.timestampSeconds,
                payload = payload
        )
    }
}
