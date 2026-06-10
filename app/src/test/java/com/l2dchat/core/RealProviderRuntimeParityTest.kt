package com.l2dchat.core

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.LlmToolChoice
import com.l2dchat.core.llm.OpenAiCompatibleClient
import com.l2dchat.core.reply.PlannerSystemPromptProvider
import com.l2dchat.core.reply.ReplySink
import com.l2dchat.core.tools.ReplierTaskRequest
import com.l2dchat.core.tools.ReplierTaskSnapshot
import com.l2dchat.core.tools.ReplierTaskState
import com.l2dchat.core.tools.ReplierTaskStore
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

class RealProviderRuntimeParityTest {
    @Test
    fun `runtime can reply through configured real openai compatible provider`() {
        assumeTrue(
                "Set MAIMCHAT_REAL_PROVIDER_PARITY=1 to run real provider parity",
                envFlag("MAIMCHAT_REAL_PROVIDER_PARITY")
        )
        val env = RealProviderEnv.fromEnvironment()
        val emitted = mutableListOf<MessageBase>()
        val replierTaskStore = RecordingReplierTaskStore()
        val client =
                OpenAiCompatibleClient(
                        baseUrl = env.baseUrl,
                        apiKeyProvider = { env.apiKey },
                        maxRetries = env.maxRetries,
                        retryDelayMillis = env.retryDelayMillis
                )

        runBlocking {
            val runtime =
                    LocalRuntimeFactory.create(
                            scope = this,
                            llmConfig =
                                    LocalRuntimeLlmConfig(
                                            plannerClient = client,
                                            plannerConfig = env.plannerConfig,
                                            replierClient = client,
                                            replierConfig = env.replierConfig,
                                            nativeToolCalling = env.nativeToolCalling
                                    ),
                            plannerSystemPromptProvider =
                                    PlannerSystemPromptProvider { plannerSystemPrompt },
                            replierTaskStore = replierTaskStore
                    )

            val handled =
                    runtime.handleMessage(
                            inbound = buildMessage(env.prompt),
                            fallbackPlatform = "real-provider-parity",
                            fallbackAgentName = "Maimchat",
                            replySink = collectingSink(emitted)
                    )

            assertTrue(handled)
            assertEquals(1, emitted.size)
            assertTrue(emitted.single().rawMessage.orEmpty().isNotBlank())
            if (env.expectReplierTask) {
                withTimeout(env.timeoutMillis + 5_000L) {
                    while (!replierTaskStore.hasCompletedReply()) {
                        delay(100L)
                    }
                }
            }
            runtime.stopAndDrain()
        }
    }

    private fun collectingSink(target: MutableList<MessageBase>): ReplySink =
            object : ReplySink {
                override suspend fun send(message: MessageBase) {
                    target.add(message)
                }
            }

    private fun buildMessage(content: String): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    platform = "real-provider-parity",
                                    messageId = "real-provider-message",
                                    senderInfo =
                                            SenderInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform =
                                                                            "real-provider-parity",
                                                                    userId = "user-id",
                                                                    userNickname = "Tester"
                                                            )
                                            ),
                                    receiverInfo =
                                            ReceiverInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    platform =
                                                                            "real-provider-parity",
                                                                    userId = "bot-id",
                                                                    userNickname = "Maimchat"
                                                            )
                                            ),
                                    additionalConfig = mapOf("message_type" to "chat")
                            ),
                    messageSegment = Seg("text", content),
                    rawMessage = content
            )

    private class RecordingReplierTaskStore : ReplierTaskStore {
        private val lock = Any()
        private val snapshots = mutableListOf<ReplierTaskSnapshot>()

        override suspend fun upsertTaskSnapshot(
                request: ReplierTaskRequest,
                snapshot: ReplierTaskSnapshot,
                createdAtMillis: Long,
                updatedAtMillis: Long
        ) {
            synchronized(lock) { snapshots += snapshot }
        }

        fun hasCompletedReply(): Boolean =
                synchronized(lock) {
                    snapshots.any {
                        it.state == ReplierTaskState.COMPLETED &&
                                !it.replyText.isNullOrBlank()
                    }
                }
    }

    private data class RealProviderEnv(
            val baseUrl: String,
            val apiKey: String?,
            val plannerConfig: LlmGenerationConfig,
            val replierConfig: LlmGenerationConfig,
            val prompt: String,
            val nativeToolCalling: Boolean,
            val expectReplierTask: Boolean,
            val timeoutMillis: Long,
            val maxRetries: Int,
            val retryDelayMillis: Long
    ) {
        companion object {
            fun fromEnvironment(): RealProviderEnv {
                val timeoutMillis = envLong("MAIMCHAT_REAL_PROVIDER_TIMEOUT_MILLIS", 60_000L)
                val maxTokens = envInt("MAIMCHAT_REAL_PROVIDER_MAX_TOKENS", 256)
                val plannerModel = requiredEnv("MAIMCHAT_REAL_PROVIDER_PLANNER_MODEL")
                val replierModel = envString("MAIMCHAT_REAL_PROVIDER_REPLIER_MODEL") ?: plannerModel
                val nativeToolCalling = envFlag("MAIMCHAT_REAL_PROVIDER_NATIVE_TOOLS", default = true)
                val expectReplierTask =
                        envFlag("MAIMCHAT_REAL_PROVIDER_EXPECT_REPLIER") &&
                                nativeToolCalling
                return RealProviderEnv(
                        baseUrl = requiredEnv("MAIMCHAT_REAL_PROVIDER_BASE_URL"),
                        apiKey = envString("MAIMCHAT_REAL_PROVIDER_API_KEY"),
                        plannerConfig =
                                LlmGenerationConfig(
                                        model = plannerModel,
                                        temperature = envDouble("MAIMCHAT_REAL_PROVIDER_TEMPERATURE"),
                                        maxTokens = maxTokens,
                                        timeoutMillis = timeoutMillis,
                                        toolChoice =
                                                envToolChoice(
                                                        "MAIMCHAT_REAL_PROVIDER_TOOL_CHOICE"
                                                )
                                ),
                        replierConfig =
                                LlmGenerationConfig(
                                        model = replierModel,
                                        temperature = envDouble("MAIMCHAT_REAL_PROVIDER_TEMPERATURE"),
                                        maxTokens = maxTokens,
                                        timeoutMillis = timeoutMillis
                                ),
                        prompt =
                                envString("MAIMCHAT_REAL_PROVIDER_PROMPT")
                                        ?: "Please answer this Maimchat real provider parity check in one short sentence.",
                        nativeToolCalling = nativeToolCalling,
                        expectReplierTask = expectReplierTask,
                        timeoutMillis = timeoutMillis,
                        maxRetries = envInt("MAIMCHAT_REAL_PROVIDER_MAX_RETRIES", 0),
                        retryDelayMillis =
                                envLong("MAIMCHAT_REAL_PROVIDER_RETRY_DELAY_MILLIS", 250L)
                )
            }
        }
    }

    private companion object {
        private val plannerSystemPrompt =
                """
                You are running Maimchat real provider parity verification.
                If native tool calling is available, call the replier tool exactly once with a concise reply draft for the user.
                Do not call wait_for unless the provider cannot produce the reply immediately.
                If tools are unavailable, answer directly with a concise non-empty reply.
                """.trimIndent()

        private fun envString(name: String): String? =
                System.getenv(name)?.trim()?.takeIf { it.isNotBlank() }

        private fun requiredEnv(name: String): String =
                envString(name) ?: error("$name is required when MAIMCHAT_REAL_PROVIDER_PARITY=1")

        private fun envFlag(name: String, default: Boolean = false): Boolean =
                envString(name)?.lowercase()?.let { value ->
                    value == "1" || value == "true" || value == "yes" || value == "on"
                } ?: default

        private fun envInt(name: String, default: Int): Int =
                envString(name)?.toIntOrNull() ?: default

        private fun envLong(name: String, default: Long): Long =
                envString(name)?.toLongOrNull() ?: default

        private fun envDouble(name: String): Double? =
                envString(name)?.toDoubleOrNull()

        private fun envToolChoice(name: String): LlmToolChoice =
                when (envString(name)?.lowercase()) {
                    "none" -> LlmToolChoice.NONE
                    "required" -> LlmToolChoice.REQUIRED
                    else -> LlmToolChoice.AUTO
                }
    }
}
