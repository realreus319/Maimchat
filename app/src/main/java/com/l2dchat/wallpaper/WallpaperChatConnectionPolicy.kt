package com.l2dchat.wallpaper

import com.l2dchat.chat.service.ChatServiceClient

internal object WallpaperChatConnectionPolicy {
    enum class Action {
        READY,
        WAIT_FOR_READY,
        START_LOCAL_RUNTIME
    }

    fun nextAction(runtimeState: ChatServiceClient.RuntimeState): Action =
            when (runtimeState) {
                ChatServiceClient.RuntimeState.RUNNING -> Action.READY
                ChatServiceClient.RuntimeState.STARTING -> Action.WAIT_FOR_READY
                ChatServiceClient.RuntimeState.STOPPED,
                ChatServiceClient.RuntimeState.ERROR -> Action.START_LOCAL_RUNTIME
            }
}
