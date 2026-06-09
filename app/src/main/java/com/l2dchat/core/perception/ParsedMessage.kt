package com.l2dchat.core.perception

import com.l2dchat.core.inbound.ContentBlock
import com.l2dchat.core.inbound.InboundMessage

data class ParsedMessage(
        val messageId: String,
        val contextId: String,
        val agentId: String,
        val text: String,
        val senderId: String,
        val senderName: String?,
        val isMentioned: Boolean = false,
        val mentionedNames: List<String> = emptyList(),
        val isCommand: Boolean = false,
        val commandName: String? = null,
        val commandArgs: Map<String, Any> = emptyMap(),
        val contentBlocks: List<ContentBlock> = emptyList(),
        val timestampSeconds: Double,
        val rawMessage: InboundMessage
)
