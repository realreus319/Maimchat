package com.l2dchat.core.reply

import com.l2dchat.chat.MessageBase

interface ReplySink {
    suspend fun send(message: MessageBase)
}
