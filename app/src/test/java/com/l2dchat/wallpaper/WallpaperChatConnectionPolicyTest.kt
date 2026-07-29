package com.l2dchat.wallpaper

import com.l2dchat.chat.service.ChatServiceClient
import org.junit.Assert.assertEquals
import org.junit.Test

class WallpaperChatConnectionPolicyTest {
    @Test
    fun `connected state is already ready`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.READY,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeState = ChatServiceClient.RuntimeState.RUNNING
                )
        )
    }

    @Test
    fun `connecting state waits for the current runtime`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.WAIT_FOR_READY,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeState = ChatServiceClient.RuntimeState.STARTING
                )
        )
    }

    @Test
    fun `disconnected or error state starts the local runtime`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.START_LOCAL_RUNTIME,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeState = ChatServiceClient.RuntimeState.STOPPED
                )
        )
        assertEquals(
                WallpaperChatConnectionPolicy.Action.START_LOCAL_RUNTIME,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeState = ChatServiceClient.RuntimeState.ERROR
                )
        )
    }
}
