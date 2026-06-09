package com.l2dchat.core.perception

import com.l2dchat.core.inbound.InboundMessage

interface MessageParser {
    fun parse(message: InboundMessage): ParsedMessage
}
