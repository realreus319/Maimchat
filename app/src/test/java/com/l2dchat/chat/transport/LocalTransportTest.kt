package com.l2dchat.chat.transport

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.chat.ChatWebSocketManager.ConnectionState
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.core.LocalChatRuntime
import com.l2dchat.core.reply.PlannerTriggerProcessor
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalTransportTest {
    @Test
    fun `rebuildRuntime swaps the runtime used for later sends`() {
        val states = mutableListOf<ConnectionState>()
        val incoming = mutableListOf<MessageBase>()
        var replyText = "first runtime"
        var factoryCalls = 0

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        incoming += MessageBase.fromJsonString(text)
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        throw AssertionError(message, throwable)
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = {
                                factoryCalls += 1
                                runtimeFor(replyText)
                            }
                    )

            transport.start()
            assertTrue(transport.send(buildMessage("hello", "message-1")))
            withTimeout(1_000L) { incoming.awaitSize(1) }
            assertEquals("first runtime", incoming.single().rawMessage)

            replyText = "second runtime"
            transport.rebuildRuntime("test update")
            assertTrue(transport.send(buildMessage("again", "message-2")))
            withTimeout(1_000L) { incoming.awaitSize(2) }

            assertEquals(listOf("first runtime", "second runtime"), incoming.map { it.rawMessage })
            assertEquals(2, factoryCalls)
            assertEquals(ConnectionState.CONNECTED, states.last())
            transport.stop("done")
        }
    }

    @Test
    fun `submitEnvironmentTrigger sends unbound environment replies through callbacks`() {
        val states = mutableListOf<ConnectionState>()
        val incoming = mutableListOf<MessageBase>()

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        incoming += MessageBase.fromJsonString(text)
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        throw AssertionError(message, throwable)
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = { runtimeFor("environment reply") }
                    )

            assertTrue(
                    transport.submitEnvironmentTrigger(
                            EnvironmentTriggerSubmission(
                                    routingKey =
                                            RoutingKey(
                                                    contextId = "room-bot",
                                                    agentId = "bot-id"
                                            ),
                                    text = "app visible",
                                    source = "android_app",
                                    metadata =
                                            mapOf(
                                                    "receiver_user_id" to "user-id",
                                                    "receiver_user_name" to "Alice",
                                                    "agent_user_id" to "bot-id",
                                                    "agent_user_name" to "Bot"
                                            )
                            )
                    )
            )

            withTimeout(1_000L) { incoming.awaitSize(1) }
            assertEquals("environment reply", incoming.single().rawMessage)
            assertEquals(ConnectionState.CONNECTED, states.last())
            transport.stop("done")
        }
    }

    private fun runtimeFor(replyText: String): LocalChatRuntime =
            LocalChatRuntime(
                    plannerProcessorFactory = {
                        PlannerTriggerProcessor { context -> context.sendReply(replyText) }
                    }
            )

    private suspend fun <T> List<T>.awaitSize(size: Int) {
        while (this.size < size) {
            delay(10L)
        }
    }

    private fun buildMessage(content: String, messageId: String): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "test_platform",
                                    messageId = messageId,
                                    senderInfo =
                                            SenderInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform = "test_platform",
                                                                    userId = "user-id",
                                                                    userNickname = "Alice"
                                                            )
                                            ),
                                    receiverInfo =
                                            ReceiverInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform = "test_platform",
                                                                    userId = "bot-id",
                                                                    userNickname = "Bot"
                                                            )
                                            ),
                                    additionalConfig = mapOf("message_type" to "chat")
                            ),
                    messageSegment = Seg("text", content),
                    rawMessage = content
            )
}
