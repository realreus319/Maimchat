package com.l2dchat.wallpaper

import com.l2dchat.chat.service.ChatRuntimeMode
import com.l2dchat.chat.service.ChatServiceClient
import org.junit.Assert.assertEquals
import org.junit.Test

class WallpaperChatConnectionPolicyTest {
    @Test
    fun `connected state is already ready for both runtime modes`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.READY,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.LOCAL,
                        connectionState = ChatServiceClient.ChatConnectionState.CONNECTED,
                        remoteUrl = null
                )
        )
        assertEquals(
                WallpaperChatConnectionPolicy.Action.READY,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.REMOTE,
                        connectionState = ChatServiceClient.ChatConnectionState.CONNECTED,
                        remoteUrl = null
                )
        )
    }

    @Test
    fun `connecting state waits for the current runtime`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.WAIT_FOR_READY,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.LOCAL,
                        connectionState = ChatServiceClient.ChatConnectionState.CONNECTING,
                        remoteUrl = null
                )
        )
    }

    @Test
    fun `local mode starts runtime without remote url`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.START_LOCAL_RUNTIME,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.LOCAL,
                        connectionState = ChatServiceClient.ChatConnectionState.DISCONNECTED,
                        remoteUrl = null
                )
        )
        assertEquals(
                WallpaperChatConnectionPolicy.Action.START_LOCAL_RUNTIME,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.LOCAL,
                        connectionState = ChatServiceClient.ChatConnectionState.ERROR,
                        remoteUrl = "ws://localhost:8080/ws"
                )
        )
    }

    @Test
    fun `remote mode requires a websocket url before connecting`() {
        assertEquals(
                WallpaperChatConnectionPolicy.Action.FAIL_MISSING_REMOTE_URL,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.REMOTE,
                        connectionState = ChatServiceClient.ChatConnectionState.DISCONNECTED,
                        remoteUrl = null
                )
        )
        assertEquals(
                WallpaperChatConnectionPolicy.Action.CONNECT_REMOTE,
                WallpaperChatConnectionPolicy.nextAction(
                        runtimeMode = ChatRuntimeMode.REMOTE,
                        connectionState = ChatServiceClient.ChatConnectionState.ERROR,
                        remoteUrl = "ws://localhost:8080/ws"
                )
        )
    }
}
