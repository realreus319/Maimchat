package com.l2dchat.wallpaper

import com.l2dchat.chat.service.ChatRuntimeMode
import com.l2dchat.chat.service.ChatServiceClient

internal object WallpaperChatConnectionPolicy {
    enum class Action {
        READY,
        WAIT_FOR_READY,
        START_LOCAL_RUNTIME,
        CONNECT_REMOTE,
        FAIL_MISSING_REMOTE_URL
    }

    fun nextAction(
            runtimeMode: ChatRuntimeMode,
            connectionState: ChatServiceClient.ChatConnectionState,
            remoteUrl: String?
    ): Action =
            when (connectionState) {
                ChatServiceClient.ChatConnectionState.CONNECTED -> Action.READY
                ChatServiceClient.ChatConnectionState.CONNECTING -> Action.WAIT_FOR_READY
                ChatServiceClient.ChatConnectionState.DISCONNECTED,
                ChatServiceClient.ChatConnectionState.ERROR ->
                        when (runtimeMode) {
                            ChatRuntimeMode.LOCAL -> Action.START_LOCAL_RUNTIME
                            ChatRuntimeMode.REMOTE ->
                                    if (remoteUrl.isNullOrBlank()) {
                                        Action.FAIL_MISSING_REMOTE_URL
                                    } else {
                                        Action.CONNECT_REMOTE
                                    }
                        }
            }
}
