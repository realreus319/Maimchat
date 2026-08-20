package com.l2dchat.core.mem

import com.google.gson.JsonParser
import com.l2dchat.core.llm.LlmMessage
import kotlinx.coroutines.runBlocking
import okhttp3.HttpUrl
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Verifies [MemLlmClient] correctly delegates to [com.l2dchat.core.llm.OpenAiCompatibleClient]
 * with the right provider/model wiring.
 *
 * The fake-OkHttpClient interceptor pattern is lifted from
 * `OpenAiCompatibleClientTest`: we record each outgoing request (URL, Authorization
 * header, body) and return a canned JSON completion. We then drive the wrapped
 * client through `chatCompletion` and assert on the recorded wire payload.
 */
class MemLlmClientTest {
    @Test
    fun `extractionConfig uses extraction model identifier, default temperature and provider timeout`() {
        val client = MemLlmClient(configWithKey())

        val cfg = client.extractionConfig(multimodal = false)
        assertEquals("qwen3.7-max", cfg.model)
        // Model declares temperature = 0.3 → carried through unchanged.
        assertEquals(0.3, cfg.temperature!!, 0.0)
        assertEquals(8192, cfg.maxTokens)
        // Provider timeoutSeconds=120 → 120_000 ms.
        assertEquals(120_000L, cfg.timeoutMillis)
    }

    @Test
    fun `extractionConfig multimodal=true switches to the extraction_mm model identifier`() {
        val client = MemLlmClient(configWithKey())

        val cfg = client.extractionConfig(multimodal = true)
        assertEquals("qwen3.7-plus", cfg.model)
        assertEquals(0.3, cfg.temperature!!, 0.0)
    }

    @Test
    fun `extractionConfig applies default temperature when the model leaves it unset`() {
        val config =
            MemLlmConfig(
                providers =
                    mapOf(
                        "ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k", timeoutSeconds = 45),
                    ),
                models =
                    mapOf(
                        "extraction-no-temp" to
                            MemModel(
                                name = "extraction-no-temp",
                                provider = "ali",
                                modelIdentifier = "no-temp-id",
                                kind = ModelKind.CHAT,
                                maxTokens = 1024,
                                temperature = null,
                            ),
                        "mm-no-temp" to
                            MemModel(
                                name = "mm-no-temp",
                                provider = "ali",
                                modelIdentifier = "mm-no-temp-id",
                                kind = ModelKind.CHAT,
                                maxTokens = 512,
                                temperature = null,
                            ),
                        "embed" to
                            MemModel(
                                name = "embed",
                                provider = "ali",
                                modelIdentifier = "embed-id",
                                kind = ModelKind.EMBEDDING,
                                dimensions = 1024,
                            ),
                    ),
                tasks =
                    MemTaskConfig(
                        extraction = "extraction-no-temp",
                        extractionMm = "mm-no-temp",
                        embedding = "embed",
                    ),
            )
        val cfg = MemLlmClient(config).extractionConfig()
        assertEquals("no-temp-id", cfg.model)
        assertEquals(0.3, cfg.temperature!!, 0.0)
        assertEquals(45_000L, cfg.timeoutMillis)
    }

    @Test
    fun `extractionClient sends request to baseUrl slash chat slash completions with bearer auth`() {
        val recorded = mutableListOf<RecordedRequest>()
        val memClient =
            MemLlmClient(
                config = configWithKey(),
                httpClient = fakeHttpClient(recorded),
            )

        runBlocking {
            memClient.extractionClient().chatCompletion(
                messages = listOf(LlmMessage.user("extract: hello")),
                config = memClient.extractionConfig(),
            )
        }

        assertEquals("extraction must issue exactly one HTTP request", 1, recorded.size)
        val request = recorded.single()
        // URL is the provider baseUrl + /chat/completions (normalised by OpenAiCompatibleClient).
        assertEquals(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            request.url.toString(),
        )
        assertEquals("Bearer ali-key", request.authorization)

        val body = JsonParser.parseString(request.body).asJsonObject
        assertEquals("qwen3.7-max", body["model"].asString)
        assertEquals(1, body.getAsJsonArray("messages").size())
        // temperature from the model declaration (0.3) must reach the wire.
        assertEquals(0.3, body["temperature"].asDouble, 0.0)
        assertEquals(8192, body["max_tokens"].asInt)
    }

    @Test
    fun `extractionClient multimodal=true sends the extraction_mm model identifier on the wire`() {
        val recorded = mutableListOf<RecordedRequest>()
        val memClient =
            MemLlmClient(
                config = configWithKey(),
                httpClient = fakeHttpClient(recorded),
            )

        runBlocking {
            memClient.extractionClient(multimodal = true).chatCompletion(
                messages = listOf(LlmMessage.user("image: ...")),
                config = memClient.extractionConfig(multimodal = true),
            )
        }

        val body = JsonParser.parseString(recorded.single().body).asJsonObject
        assertEquals("qwen3.7-plus", body["model"].asString)
    }

    @Test
    fun `extractionClient omits Authorization header when provider apiKey is null`() {
        val recorded = mutableListOf<RecordedRequest>()
        val memClient =
            MemLlmClient(
                config = configWithoutKey(),
                httpClient = fakeHttpClient(recorded),
            )

        runBlocking {
            memClient.extractionClient().chatCompletion(
                messages = listOf(LlmMessage.user("ping")),
                config = memClient.extractionConfig(),
            )
        }

        // Key-less provider (local embedding-style endpoint) → no Authorization header.
        assertNull(
            "Authorization header must be absent for key-less provider",
            recorded.single().authorization,
        )
        // URL still normalised against the local provider baseUrl.
        assertEquals(
            "http://localhost:8010/v1/chat/completions",
            recorded.single().url.toString(),
        )
    }

    @Test
    fun `extractionClient surfaces ConfigError when the extraction model is missing`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models = emptyMap(),
                tasks =
                    MemTaskConfig(
                        extraction = "ghost",
                        extractionMm = "ghost-mm",
                        embedding = "ghost-embed",
                    ),
            )
        val memClient = MemLlmClient(config)

        try {
            memClient.extractionClient()
            fail("Expected ConfigError for missing extraction model")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("ghost"))
        }
        try {
            memClient.extractionConfig(multimodal = true)
            fail("Expected ConfigError for missing extraction_mm model")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("ghost-mm"))
        }
    }

    @Test
    fun `extractionClient surfaces ConfigError when the provider reference is dangling`() {
        val config =
            MemLlmConfig(
                providers = mapOf("ali" to MemProvider("ali", "https://example.test/v1", apiKey = "k")),
                models =
                    mapOf(
                        "orphan" to
                            MemModel(
                                name = "orphan",
                                provider = "ghost-provider",
                                modelIdentifier = "orphan-id",
                                kind = ModelKind.CHAT,
                            )
                    ),
                tasks =
                    MemTaskConfig(
                        extraction = "orphan",
                        extractionMm = "orphan",
                        embedding = "orphan",
                    ),
            )
        val memClient = MemLlmClient(config)

        try {
            memClient.extractionClient()
            fail("Expected ConfigError for unknown provider")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("ghost-provider"))
        }
    }

    @Test
    fun `extractionClient returns a working LlmClient whose response text is parsed`() {
        val recorded = mutableListOf<RecordedRequest>()
        val memClient =
            MemLlmClient(
                config = configWithKey(),
                httpClient = fakeHttpClient(recorded),
            )

        val response =
            runBlocking {
                memClient.extractionClient().chatCompletion(
                    messages = listOf(LlmMessage.user("ping")),
                    config = memClient.extractionConfig(),
                )
            }

        // The canned JSON response carries content "pong" — proves the wrapped client round-trips
        // through MemLlmClient without MemLlmClient touching the payload.
        assertEquals("pong", response.text)
        assertEquals("fake-model", response.model)
        assertNotNull(response.usage)
        assertEquals(5, response.usage!!.totalTokens)
        assertFalse(
            "MemLlmClient must return a real LlmClient, not null",
            recorded.isEmpty(),
        )
    }

    // ------------------------------------------------------------------
    // Fixtures
    // ------------------------------------------------------------------

    private fun configWithKey(): MemLlmConfig =
        MemLlmConfig(
            providers =
                mapOf(
                    "ali" to
                        MemProvider(
                            name = "ali",
                            baseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            apiKey = "ali-key",
                            timeoutSeconds = 120,
                        ),
                    "local" to
                        MemProvider(
                            name = "local",
                            baseUrl = "http://localhost:8010/v1",
                            apiKey = null,
                            timeoutSeconds = 60,
                        ),
                ),
            models =
                mapOf(
                    "qwen37max" to
                        MemModel(
                            name = "qwen37max",
                            provider = "ali",
                            modelIdentifier = "qwen3.7-max",
                            kind = ModelKind.CHAT,
                            maxTokens = 8192,
                            temperature = 0.3,
                        ),
                    "qwen37plus" to
                        MemModel(
                            name = "qwen37plus",
                            provider = "ali",
                            modelIdentifier = "qwen3.7-plus",
                            kind = ModelKind.CHAT,
                            maxTokens = 8192,
                            temperature = 0.3,
                        ),
                    "local-embed" to
                        MemModel(
                            name = "local-embed",
                            provider = "local",
                            modelIdentifier = "Qwen/Qwen3-Embedding-0.6B",
                            kind = ModelKind.EMBEDDING,
                            dimensions = 1024,
                        ),
                ),
            tasks =
                MemTaskConfig(
                    extraction = "qwen37max",
                    extractionMm = "qwen37plus",
                    embedding = "local-embed",
                ),
        )

    /** Same as [configWithKey] but the extraction task points at the key-less local provider. */
    private fun configWithoutKey(): MemLlmConfig =
        MemLlmConfig(
            providers =
                mapOf(
                    "local" to
                        MemProvider(
                            name = "local",
                            baseUrl = "http://localhost:8010/v1",
                            apiKey = null,
                            timeoutSeconds = 60,
                        ),
                ),
            models =
                mapOf(
                    "local-chat" to
                        MemModel(
                            name = "local-chat",
                            provider = "local",
                            modelIdentifier = "local-chat-id",
                            kind = ModelKind.CHAT,
                            maxTokens = 2048,
                            temperature = 0.3,
                        ),
                    "local-embed" to
                        MemModel(
                            name = "local-embed",
                            provider = "local",
                            modelIdentifier = "Qwen/Qwen3-Embedding-0.6B",
                            kind = ModelKind.EMBEDDING,
                            dimensions = 1024,
                        ),
                ),
            tasks =
                MemTaskConfig(
                    extraction = "local-chat",
                    extractionMm = "local-chat",
                    embedding = "local-embed",
                ),
        )

    /**
     * Fake [OkHttpClient] that records each outgoing request and returns a canned
     * non-streaming chat completion. Mirrors the helper in `OpenAiCompatibleClientTest`
     * but also captures the request URL.
     */
    private fun fakeHttpClient(recorded: MutableList<RecordedRequest>): OkHttpClient =
        OkHttpClient.Builder()
            .addInterceptor(
                Interceptor { chain ->
                    val request = chain.request()
                    val buffer = Buffer()
                    request.body?.writeTo(buffer)
                    recorded.add(
                        RecordedRequest(
                            url = request.url,
                            authorization = request.header("Authorization"),
                            body = buffer.readUtf8(),
                        )
                    )
                    jsonResponse().newBuilder().request(request).protocol(Protocol.HTTP_1_1).build()
                }
            )
            .build()

    private fun jsonResponse(): Response =
        Response.Builder()
            .code(200)
            .message("OK")
            .body(
                """
                {
                  "id": "chatcmpl-1",
                  "model": "fake-model",
                  "choices": [
                    {
                      "finish_reason": "stop",
                      "message": {
                        "role": "assistant",
                        "content": "pong"
                      }
                    }
                  ],
                  "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2
                  }
                }
                """.trimIndent()
                    .toResponseBody("application/json".toMediaType())
            )
            .request(
                okhttp3.Request.Builder().url("https://example.test").build()
            )
            .protocol(Protocol.HTTP_1_1)
            .build()

    private data class RecordedRequest(
        val url: HttpUrl,
        val authorization: String?,
        val body: String,
    )
}
