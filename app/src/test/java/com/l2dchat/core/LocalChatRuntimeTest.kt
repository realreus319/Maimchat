package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.environment.EnvironmentTriggerSubmission
import com.l2dchat.core.perception.ParsedMessage
import com.l2dchat.core.perception.PerceptionStore
import com.l2dchat.core.reply.PlannerTriggerProcessor
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
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
            assertEquals("local_reply", reply.messageInfo.additionalConfig?.get("migration_phase"))
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
    fun `handleMessage emits injected planner reply as final text`() {
        val emitted = mutableListOf<MessageBase>()

        runBlocking {
            val runtime =
                    LocalChatRuntime(
                            scope = this,
                            plannerProcessorFactory = {
                                PlannerTriggerProcessor { context ->
                                    context.sendReply("LLM 直接回复")
                                }
                            }
                    )
            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "你好"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            assertEquals("LLM 直接回复", emitted.single().rawMessage)
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `submitEnvironmentTrigger emits local reply through sink`() {
        val emitted = mutableListOf<MessageBase>()

        runBlocking {
            val runtime =
                    LocalChatRuntime(
                            scope = this,
                            plannerProcessorFactory = {
                                PlannerTriggerProcessor { context ->
                                    assertEquals(TriggerType.ENV, context.trigger.triggerType)
                                    assertEquals("android_app", context.trigger.payload["source"])
                                    assertEquals("应用切回前台", context.triggerText)
                                    context.sendReply("我在这里。")
                                }
                            }
                    )
            val accepted =
                    runtime.submitEnvironmentTrigger(
                            submission =
                                    EnvironmentTriggerSubmission(
                                            routingKey =
                                                    RoutingKey(
                                                            contextId = "model_mao",
                                                            agentId = "agent_mao"
                                                    ),
                                            text = "应用切回前台",
                                            source = "android_app",
                                            metadata =
                                                    mapOf(
                                                            "receiver_user_id" to "user-42",
                                                            "receiver_user_name" to "Tester",
                                                            "agent_user_name" to "Mao"
                                                    )
                                    ),
                            fallbackPlatform = "live2d_chat",
                            fallbackAgentName = "Mao",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(accepted)
            waitForMessages(emitted, expectedCount = 1)
            val reply = emitted.single()
            assertEquals("我在这里。", reply.rawMessage)
            assertEquals("live2d_chat", reply.messageInfo.platform)
            assertEquals("agent_mao", reply.messageInfo.senderInfo?.userInfo?.userId)
            assertEquals("Mao", reply.messageInfo.senderInfo?.userInfo?.userNickname)
            assertEquals("user-42", reply.messageInfo.receiverInfo?.userInfo?.userId)
            assertEquals("Tester", reply.messageInfo.receiverInfo?.userInfo?.userNickname)
            assertEquals("env", reply.messageInfo.additionalConfig?.get("trigger_type"))
            assertEquals("android_app", reply.messageInfo.additionalConfig?.get("environment_source"))
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `interrupted pending message completes false while decision reply sends current message`() {
        val emitted = mutableListOf<MessageBase>()
        val firstStarted = CompletableDeferred<Unit>()

        runBlocking {
            val runtime =
                    LocalChatRuntime(
                            scope = this,
                            plannerProcessorFactory = {
                                PlannerTriggerProcessor { context ->
                                    if (context.trigger.messageId == "first-id") {
                                        firstStarted.complete(Unit)
                                        awaitCancellation()
                                    } else {
                                        context.sendReply("normal reply")
                                    }
                                }
                            },
                            decisionPlannerProcessorFactory = {
                                PlannerTriggerProcessor { context ->
                                    context.sendReply("decision reply to ${context.trigger.messageId}")
                                }
                            }
                    )
            val firstHandled =
                    async {
                        runtime.handleMessage(
                                inbound = buildMessage(content = "first", messageId = "first-id"),
                                fallbackPlatform = "fallback",
                                fallbackAgentName = "Maimchat",
                                replySink = collectingSink(emitted)
                        )
                    }
            withTimeout(1_000L) { firstStarted.await() }

            val secondHandled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "second", messageId = "second-id"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertFalse(withTimeout(1_000L) { firstHandled.await() })
            assertTrue(secondHandled)
            assertEquals(listOf("decision reply to second-id"), emitted.map { it.rawMessage })
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

    private suspend fun waitForMessages(target: List<MessageBase>, expectedCount: Int) {
        withTimeout(1_000L) {
            while (target.size < expectedCount) {
                delay(10L)
            }
        }
    }

    private fun buildMessage(
            content: String,
            messageId: String = "inbound-id",
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
                                receiverInfo = receiver,
                                additionalConfig = additional
                        ),
                messageSegment = Seg("text", content),
                rawMessage = content
        )
    }
}
