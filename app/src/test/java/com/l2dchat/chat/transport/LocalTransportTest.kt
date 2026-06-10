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
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalTransportTest {
    @Test
    fun `send starts runtime without explicit start`() {
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
                            runtimeFactory = { runtimeScope ->
                                runtimeFor("local reply", runtimeScope)
                            }
                    )

            assertTrue(transport.send(buildMessage("hello", "message-1")))
            withTimeout(1_000L) { incoming.awaitSize(1) }

            assertEquals(listOf(ConnectionState.CONNECTING, ConnectionState.CONNECTED), states)
            assertEquals("local reply", incoming.single().rawMessage)
            transport.stop("done")
        }
    }

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
                            runtimeFactory = { runtimeScope ->
                                factoryCalls += 1
                                runtimeFor(replyText, runtimeScope)
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
            assertEquals(
                    listOf(
                            ConnectionState.CONNECTING,
                            ConnectionState.CONNECTED,
                            ConnectionState.CONNECTING,
                            ConnectionState.CONNECTED
                    ),
                    states
            )
            transport.stop("done")
        }
    }

    @Test
    fun `start reports error when runtime factory fails`() {
        val states = mutableListOf<ConnectionState>()
        val errors = mutableListOf<String>()

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        throw AssertionError("unexpected incoming text: $text")
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        errors += message
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = { throw IllegalStateException("factory boom") }
                    )

            transport.start()

            assertEquals(listOf(ConnectionState.CONNECTING, ConnectionState.ERROR), states)
            assertEquals(listOf("本地运行时启动失败：factory boom"), errors)
        }
    }

    @Test
    fun `rebuildRuntime reports error when replacement runtime fails`() {
        val states = mutableListOf<ConnectionState>()
        val errors = mutableListOf<String>()
        var factoryCalls = 0

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        throw AssertionError("unexpected incoming text: $text")
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        errors += message
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = { runtimeScope ->
                                factoryCalls += 1
                                if (factoryCalls == 2) {
                                    throw IllegalStateException("rebuild boom")
                                }
                                runtimeFor("first runtime", runtimeScope)
                            }
                    )

            transport.start()
            transport.rebuildRuntime("test failure")

            assertEquals(2, factoryCalls)
            assertEquals(
                    listOf(
                            ConnectionState.CONNECTING,
                            ConnectionState.CONNECTED,
                            ConnectionState.CONNECTING,
                            ConnectionState.ERROR
                    ),
                    states
            )
            assertEquals(listOf("本地运行时重建失败：rebuild boom"), errors)
        }
    }

    @Test
    fun `send reports error when runtime processing fails`() {
        val states = mutableListOf<ConnectionState>()
        val incoming = mutableListOf<MessageBase>()
        val errors = mutableListOf<String>()

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        incoming += MessageBase.fromJsonString(text)
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        errors += message
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = { runtimeScope ->
                                LocalChatRuntime(
                                        scope = runtimeScope,
                                        plannerProcessorFactory = {
                                            PlannerTriggerProcessor {
                                                throw IllegalStateException("planner boom")
                                            }
                                        }
                                )
                            }
                    )

            transport.start()
            assertTrue(transport.send(buildMessage("hello", "message-1")))
            withTimeout(1_000L) { errors.awaitSize(1) }

            assertTrue(incoming.isEmpty())
            assertEquals(ConnectionState.ERROR, states.last())
            assertEquals(listOf("本地运行时处理消息失败：planner boom"), errors)
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
                            runtimeFactory = { runtimeScope ->
                                runtimeFor("environment reply", runtimeScope)
                            }
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

    @Test
    fun `stop cancels in flight runtime work without reporting error`() {
        val states = mutableListOf<ConnectionState>()
        val incoming = mutableListOf<MessageBase>()
        val errors = mutableListOf<String>()
        val generationStarted = CompletableDeferred<Unit>()

        val callbacks =
                object : ChatTransportCallbacks {
                    override fun onStateChanged(state: ConnectionState) {
                        states += state
                    }

                    override fun onIncomingText(text: String) {
                        incoming += MessageBase.fromJsonString(text)
                    }

                    override fun onError(message: String, throwable: Throwable?) {
                        errors += message
                    }
                }

        runBlocking {
            val transport =
                    LocalTransport(
                            scope = this,
                            callbacks = callbacks,
                            platformProvider = { "test_platform" },
                            agentNameProvider = { "Bot" },
                            runtimeFactory = { runtimeScope ->
                                LocalChatRuntime(
                                        scope = runtimeScope,
                                        plannerProcessorFactory = {
                                            PlannerTriggerProcessor {
                                                generationStarted.complete(Unit)
                                                awaitCancellation()
                                            }
                                        }
                                )
                            }
                    )

            transport.start()
            assertTrue(transport.send(buildMessage("slow", "message-1")))
            withTimeout(1_000L) { generationStarted.await() }

            transport.stop("service destroyed", userInitiated = false)
            delay(50L)

            assertTrue(errors.isEmpty())
            assertTrue(incoming.isEmpty())
            assertEquals(ConnectionState.DISCONNECTED, states.last())
        }
    }

    private fun runtimeFor(replyText: String, scope: CoroutineScope): LocalChatRuntime =
            LocalChatRuntime(
                    scope = scope,
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
