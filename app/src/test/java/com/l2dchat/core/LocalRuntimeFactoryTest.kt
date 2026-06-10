package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmResponse
import com.l2dchat.core.llm.LlmStreamEvent
import com.l2dchat.core.llm.LlmToolCall
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.tools.AdoptBackgroundReplyTool
import com.l2dchat.core.tools.GetWorldStateTool
import com.l2dchat.core.tools.KillBackgroundReplyTool
import com.l2dchat.core.tools.LookAtTool
import com.l2dchat.core.tools.ReplierTool
import com.l2dchat.core.tools.TriggerMotionTool
import com.l2dchat.core.tools.WaitForTool
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalRuntimeFactoryTest {
    @Test
    fun `factory keeps fixed local reply when llm config is absent`() {
        val emitted = mutableListOf<MessageBase>()

        runBlocking {
            val runtime = LocalRuntimeFactory.create(scope = this)

            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "你好"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            assertTrue(emitted.single().rawMessage.orEmpty().contains("你好"))
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `factory wires native planner tools and replier task generation`() {
        val emitted = mutableListOf<MessageBase>()
        val client = NativeReplierClient(streamText = "generated reply")

        runBlocking {
            val runtime =
                    LocalRuntimeFactory.create(
                            scope = this,
                            llmConfig =
                                    LocalRuntimeLlmConfig(
                                            plannerClient = client,
                                            plannerConfig = LlmGenerationConfig(model = "planner"),
                                            replierClient = client,
                                            replierConfig = LlmGenerationConfig(model = "replier")
                                    )
                    )

            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "hello"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            val reply = emitted.single()
            assertEquals("generated reply", reply.rawMessage)
            assertEquals("test_platform", reply.messageInfo.platform)
            assertEquals("bot-id", reply.messageInfo.senderInfo?.userInfo?.userId)
            assertEquals("Bot", reply.messageInfo.senderInfo?.userInfo?.userNickname)
            assertEquals("user-id", reply.messageInfo.receiverInfo?.userInfo?.userId)
            assertEquals("Alice", reply.messageInfo.receiverInfo?.userInfo?.userNickname)
            assertEquals("chat", reply.messageInfo.additionalConfig?.get("message_type"))
            assertEquals("local", reply.messageInfo.additionalConfig?.get("runtime"))
            assertEquals("local_reply", reply.messageInfo.additionalConfig?.get("migration_phase"))
            assertEquals(normalToolNames, client.toolNames.single())
            assertEquals(listOf("replier"), client.streamModels)
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `factory wires json fallback planner when native tools are disabled`() {
        val emitted = mutableListOf<MessageBase>()
        val client = JsonFallbackReplierClient(streamText = "json fallback reply")

        runBlocking {
            val runtime =
                    LocalRuntimeFactory.create(
                            scope = this,
                            llmConfig =
                                    LocalRuntimeLlmConfig(
                                            plannerClient = client,
                                            plannerConfig = LlmGenerationConfig(model = "planner"),
                                            replierClient = client,
                                            replierConfig = LlmGenerationConfig(model = "replier"),
                                            nativeToolCalling = false
                                    )
                    )

            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "hello"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            assertEquals("json fallback reply", emitted.single().rawMessage)
            assertTrue(client.chatPrompts.single().contains("Native tool calling is unavailable"))
            assertTrue(client.chatPrompts.single().contains(TriggerMotionTool.NAME))
            assertFalse(client.nativeToolCallingUsed)
            runtime.stopAndDrain()
        }
    }

    @Test
    fun `factory wires decision processor that can adopt interrupted replier task`() {
        val emitted = mutableListOf<MessageBase>()
        val client = DecisionAdoptingClient(streamText = "background final")

        runBlocking {
            val runtime =
                    LocalRuntimeFactory.create(
                            scope = this,
                            llmConfig =
                                    LocalRuntimeLlmConfig(
                                            plannerClient = client,
                                            plannerConfig = LlmGenerationConfig(model = "planner"),
                                            replierClient = client,
                                            replierConfig = LlmGenerationConfig(model = "replier")
                                    )
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
            withTimeout(1_000L) { client.streamStarted.await() }

            val secondHandled =
                    runtime.handleMessage(
                            inbound = buildMessage(content = "second", messageId = "second-id"),
                            fallbackPlatform = "fallback",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertFalse(withTimeout(1_000L) { firstHandled.await() })
            assertTrue(secondHandled)
            assertEquals(listOf("background final"), emitted.map { it.rawMessage })
            assertEquals(normalToolNames, client.toolNames[0])
            assertEquals(
                    listOf(AdoptBackgroundReplyTool.NAME, KillBackgroundReplyTool.NAME),
                    client.toolNames[1]
            )
            runtime.stopAndDrain()
        }
    }

    private fun collectingSink(target: MutableList<MessageBase>): ReplySink =
            object : ReplySink {
                override suspend fun send(message: MessageBase) {
                    target.add(message)
                }
            }

    private fun buildMessage(
            content: String,
            messageId: String = "inbound-id"
    ): MessageBase =
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

    private class NativeReplierClient(
            private val streamText: String
    ) : LlmClient {
        val toolNames = mutableListOf<List<String>>()
        val streamModels = mutableListOf<String>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse = LlmResponse(message = LlmMessage.assistant("unused"), model = config.model)

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> =
                flow {
                    streamModels += config.model
                    emit(
                            LlmStreamEvent.Completed(
                                    LlmResponse(
                                            message = LlmMessage.assistant(streamText),
                                            model = config.model
                                    )
                            )
                    )
                }

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse {
            toolNames += tools.map { it.name }
            toolExecutor.execute(
                    LlmToolCall(
                            id = "call-1",
                            name = ReplierTool.NAME,
                            argumentsJson = """{"content":"planner draft"}"""
                    )
            )
            return LlmResponse(message = LlmMessage.assistant("ignored"), model = config.model)
        }
    }

    private class JsonFallbackReplierClient(
            private val streamText: String
    ) : LlmClient {
        val chatPrompts = mutableListOf<String>()
        var nativeToolCallingUsed = false

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse {
            chatPrompts += messages.joinToString("\n") { it.textContent() }
            return LlmResponse(
                    message =
                            LlmMessage.assistant(
                                    """{"type":"tool_call","tool":"replier","arguments":{"content":"planner draft"}}"""
                            ),
                    model = config.model
            )
        }

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> =
                flow {
                    emit(
                            LlmStreamEvent.Completed(
                                    LlmResponse(
                                            message = LlmMessage.assistant(streamText),
                                            model = config.model
                                    )
                            )
                    )
                }

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse {
            nativeToolCallingUsed = true
            return LlmResponse(message = LlmMessage.assistant("unused"), model = config.model)
        }
    }

    private class DecisionAdoptingClient(
            private val streamText: String
    ) : LlmClient {
        val streamStarted = CompletableDeferred<Unit>()
        private val releaseStream = CompletableDeferred<Unit>()
        val toolNames = mutableListOf<List<String>>()

        override suspend fun chatCompletion(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): LlmResponse = LlmResponse(message = LlmMessage.assistant("unused"), model = config.model)

        override fun chatCompletionStream(
                messages: List<LlmMessage>,
                config: LlmGenerationConfig
        ): Flow<LlmStreamEvent> =
                flow {
                    streamStarted.complete(Unit)
                    releaseStream.await()
                    emit(
                            LlmStreamEvent.Completed(
                                    LlmResponse(
                                            message = LlmMessage.assistant(streamText),
                                            model = config.model
                                    )
                            )
                    )
                }

        override suspend fun chatCompletionWithTools(
                messages: List<LlmMessage>,
                tools: List<LlmToolDefinition>,
                config: LlmGenerationConfig,
                toolExecutor: LlmToolExecutor
        ): LlmResponse {
            val names = tools.map { it.name }
            toolNames += names
            if (AdoptBackgroundReplyTool.NAME in names) {
                val prompt = messages.joinToString("\n") { it.textContent() }
                val taskId =
                        Regex("""task_id:\s*(\S+)""")
                                .find(prompt)
                                ?.groupValues
                                ?.get(1)
                                ?: error("decision prompt did not include a background task id")
                releaseStream.complete(Unit)
                toolExecutor.execute(
                        LlmToolCall(
                                id = "decision-call-1",
                                name = AdoptBackgroundReplyTool.NAME,
                                argumentsJson =
                                        """{"task_id":"$taskId","timeout_millis":1000}"""
                        )
                )
            } else {
                toolExecutor.execute(
                        LlmToolCall(
                                id = "normal-call-1",
                                name = ReplierTool.NAME,
                                argumentsJson = """{"content":"slow draft"}"""
                        )
                )
            }
            return LlmResponse(message = LlmMessage.assistant("ignored"), model = config.model)
        }
    }

    private companion object {
        val normalToolNames =
                listOf(
                        ReplierTool.NAME,
                        WaitForTool.NAME,
                        GetWorldStateTool.NAME,
                        LookAtTool.NAME,
                        TriggerMotionTool.NAME
                )
    }
}
