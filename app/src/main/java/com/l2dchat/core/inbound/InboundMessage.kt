package com.l2dchat.core.inbound

import com.l2dchat.chat.MessageBase
import com.l2dchat.core.context.RoutingKey

data class InboundMessage(
        val routingKey: RoutingKey,
        val messageId: String,
        val platform: String,
        val senderId: String,
        val senderName: String?,
        val text: String,
        val contentBlocks: List<ContentBlock>,
        val timestampSeconds: Double,
        val rawMessage: MessageBase,
        val isGroupChat: Boolean
)

data class InboundValidationResult(
        val isValid: Boolean,
        val errors: List<String> = emptyList()
)

data class RoomRoute(
        val contextId: String,
        val agentId: String?,
        val isGroupChat: Boolean
)
