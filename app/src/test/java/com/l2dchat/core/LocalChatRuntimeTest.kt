package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.perception.ParsedMessage
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.ReplySink
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalChatRuntimeTest {
    @Test
    fun `handleMessage emits fixed chat reply through sink`() {
        val emitted = mutableListOf<MessageBase>()

        runBlocking {
            val runtime = LocalChatRuntime(scope = this)
            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "你好"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            assertEquals(1, runtime.activePerceptionWorkerCount())
            assertEquals(1, runtime.activePlannerLoopCount())
            val reply = emitted.single()
            assertEquals("test_platform", reply.messageInfo.platform)
            assertEquals("bot-id", reply.messageInfo.senderInfo?.userInfo?.userId)
            assertEquals("user-id", reply.messageInfo.receiverInfo?.userInfo?.userId)
            assertEquals("chat", reply.messageInfo.additionalConfig?.get("message_type"))
            assertEquals("local", reply.messageInfo.additionalConfig?.get("runtime"))
            assertTrue(reply.rawMessage.orEmpty().contains("你好"))
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `handleMessage ignores non chat messages`() {
        val emitted = mutableListOf<MessageBase>()

        runBlocking {
            val runtime = LocalChatRuntime(scope = this)
            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "播放动作", messageType = "motion"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertFalse(handled)
            assertEquals(0, runtime.activePerceptionWorkerCount())
            assertEquals(0, runtime.activePlannerLoopCount())
            assertTrue(emitted.isEmpty())
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `handleMessage persists parsed message before emitting reply`() {
        val order = mutableListOf<String>()
        val store =
                object : PerceptionStore {
                    override suspend fun persist(parsedMessage: ParsedMessage) {
                        order.add("persist:${parsedMessage.messageId}")
                    }
                }
        val sink =
                object : ReplySink {
                    override suspend fun send(message: MessageBase) {
                        order.add("reply:${message.rawMessage}")
                    }
                }

        runBlocking {
            val runtime =
                    LocalChatRuntime(
                            scope = this,
                            perceptionStoreFactory = { store }
                    )
            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "记录我"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = sink
                    )

            assertTrue(handled)
            assertEquals(
                    listOf("persist:inbound-id", "reply:本地回复运行时已接收：记录我"),
                    order
            )
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `createReply uses fallback agent when receiver is missing`() {
        val reply =
                LocalChatRuntime().createReply(
                        inbound = buildMessage(content = "", receiver = null),
                        fallbackPlatform = "fallback_platform",
                        fallbackAgentName = "Fallback Agent"
                )

        assertEquals("Fallback Agent", reply.messageInfo.senderInfo?.userInfo?.userNickname)
        assertEquals("本地回复运行时已接管聊天链路。", reply.rawMessage)
    }

    private fun collectingSink(target: MutableList<MessageBase>): ReplySink =
            object : ReplySink {
                override suspend fun send(message: MessageBase) {
                    target.add(message)
                }
            }

    private fun buildMessage(
            content: String,
            messageType: String? = "chat",
            receiver: ReceiverInfo? =
                    ReceiverInfo(
                            userInfo =
                                    UserInfo(
                                            platform = "test_platform",
                                            userId = "bot-id",
                                            userNickname = "Bot"
                                    )
                    )
    ): MessageBase {
        val additional = messageType?.let { mapOf("message_type" to it) }
        return MessageBase(
                messageInfo =
                        BaseMessageInfo(
                                platform = "test_platform",
                                messageId = "inbound-id",
                                senderInfo =
                                        SenderInfo(
                                                userInfo =
                                                        UserInfo(
                                                                platform = "test_platform",
                                                                userId = "user-id",
                                                                userNickname = "Alice"
                                                        )
                                        ),
                                receiverInfo = receiver,
                                additionalConfig = additional
                        ),
                messageSegment = Seg("text", content),
                rawMessage = content
        )
    }
}
