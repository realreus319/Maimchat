package com.l2dchat.chat.transport

import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.chat.ChatWebSocketManager.RuntimeMode
import com.l2dchat.chat.MessageBase

interface ChatTransport {
    val mode: RuntimeMode

    fun stop(reason: String = "stop", userInitiated: Boolean = true)

    fun send(message: MessageBase): Boolean
}

interface ChatTransportCallbacks {
    fun onStateChanged(state: ConnectionState)

    fun onIncomingText(text: String)

    fun onError(message: String, throwable: Throwable?)

    /** Emitted while the runtime is actively processing a turn (so the UI can show a thinking hint). */
    fun onProcessingChanged(processing: Boolean) {}

    /**
     * A specific outgoing message could not be processed (e.g. network/LLM error). [messageId]
     * correlates the failure to the optimistic bubble so the UI can flag exactly that message.
     */
    fun onMessageFailed(messageId: String?, message: String, throwable: Throwable?) {}
}

data class RemoteWebSocketConfig(
        val url: String,
        val platform: String,
        val authToken: String?
)
