package com.l2dchat.core.mem

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.test.core.app.ApplicationProvider
import com.google.gson.JsonParser
import com.l2dchat.core.llm.LlmMessage
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.llm.LlmToolExecutor
import com.l2dchat.core.llm.LlmToolResult
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Phase-2 integration test (plan T2.6): verifies the three wave-1/2 clients
 * ([OpenAiCompatibleEmbeddingClient], [MemStore], [MemLlmClient]) compose into
 * the full mem data path without any real network or DB.
 *
 * Coverage:
 * - embed(text) → FloatArray → TxWriter.putVector → MemStore.flush → vecTopK
 *   returns the just-stored vector at the top.
 * - Embedding dim mismatch (provider returns 512, store expects 1024) propagates
 *   as [DimMismatchError] and nothing is persisted.
 * - MemLlmClient + OpenAiCompatibleClient parse a streaming `tool_calls` payload
 *   and dispatch the tool call through [LlmToolExecutor].
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class MemClientIntegrationTest {

    private lateinit var db: MemIntegrationDb
    private lateinit var store: MemStore

    @Before
    fun setUp() {
        db = Room.inMemoryDatabaseBuilder(
            ApplicationProvider.getApplicationContext(),
            MemIntegrationDb::class.java,
        ).allowMainThreadQueries().build()
        store = MemStore(
            nodeDao = db.memNodeDao(),
            linkDao = db.memLinkDao(),
            vectorDao = db.memVectorDao(),
            receiptDao = db.memReceiptDao(),
            expectedDim = EMBEDDING_DIM,
            transactionRunner = null,
        )
        runBlocking { store.load() }
    }

    @After
    fun tearDown() {
        db.close()
    }

    @Test
    fun `embed to MemStore to vecTopK returns the stored vector at the top`() = runBlocking {
        val embeddingClient = OpenAiCompatibleEmbeddingClient(
            baseUrl = "https://embed.test/v1",
            modelIdentifier = "embed-id",
            dimension = EMBEDDING_DIM,
            apiKeyProvider = { "embed-key" },
            httpClient = fakeHttpClient(mutableListOf()) {
                jsonOk(
                    """
                    {
                      "data": [
                        {"embedding": [1.0, 0.0, 0.0, 0.0]},
                        {"embedding": [0.0, 1.0, 0.0, 0.0]}
                      ]
                    }
                    """.trimIndent()
                )
            },
            maxRetries = 0,
        )

        val vectors = embeddingClient.embed(listOf("alpha node", "beta node"))
        assertEquals(2, vectors.size)
        assertEquals(EMBEDDING_DIM, vectors[0].size)

        val writer = TxWriter(dimensions = EMBEDDING_DIM)
        val hAlpha = writer.putNode(nodeType = "event", content = "alpha node", mentionTime = 1L)
        val hBeta = writer.putNode(nodeType = "event", content = "beta node", mentionTime = 2L)
        writer.putVector(hAlpha, vectors[0].toList())
        writer.putVector(hBeta, vectors[1].toList())
        writer.flush(store)

        val topK = store.vecTopK(floatArrayOf(1f, 0f, 0f, 0f), k = 2)
        assertEquals(2, topK.size)
        assertEquals(hAlpha, topK[0].first)
        assertEquals(1.0, topK[0].second, 1e-6)
        assertEquals(hBeta, topK[1].first)
    }

    @Test
    fun `embedding dim mismatch propagates as DimMismatchError and nothing is persisted`() = runBlocking {
        val embeddingClient = OpenAiCompatibleEmbeddingClient(
            baseUrl = "https://embed.test/v1",
            modelIdentifier = "embed-id",
            dimension = EMBEDDING_DIM,
            httpClient = fakeHttpClient(mutableListOf()) {
                jsonOk("""{"data": [{"embedding": [0.1, 0.2]}]}""")
            },
            maxRetries = 0,
        )

        var threw = false
        try {
            embeddingClient.embed("bad-dim text")
        } catch (e: DimMismatchError) {
            threw = true
            assertTrue(e.message!!.contains("expected $EMBEDDING_DIM"))
            assertTrue(e.message!!.contains("got 2"))
        }
        assertTrue("embed must throw DimMismatchError on dim mismatch", threw)

        assertEquals(0, store.vecTopK(floatArrayOf(0f, 0f, 0f, 0f), k = 5).size)
    }

    @Test
    fun `MemLlmClient parses streaming tool_calls and dispatches through LlmToolExecutor`() = runBlocking {
        val memConfig = MemLlmConfig(
            providers = mapOf(
                "mem_extraction" to MemProvider(
                    name = "mem_extraction",
                    baseUrl = "https://extract.test/v1",
                    apiKey = "extract-key",
                    timeoutSeconds = 60,
                ),
            ),
            models = mapOf(
                "extraction" to MemModel(
                    name = "extraction",
                    provider = "mem_extraction",
                    modelIdentifier = "extraction-id",
                    kind = ModelKind.CHAT,
                    maxTokens = 2048,
                    temperature = 0.0,
                ),
                "extraction_mm" to MemModel(
                    name = "extraction_mm",
                    provider = "mem_extraction",
                    modelIdentifier = "extraction-mm-id",
                    kind = ModelKind.CHAT,
                    maxTokens = 2048,
                    temperature = 0.0,
                ),
                "embedding" to MemModel(
                    name = "embedding",
                    provider = "mem_extraction",
                    modelIdentifier = "embed-id",
                    kind = ModelKind.EMBEDDING,
                    dimensions = EMBEDDING_DIM,
                ),
            ),
            tasks = MemTaskConfig(
                extraction = "extraction",
                extractionMm = "extraction_mm",
                embedding = "embedding",
            ),
        )

        val recorded = mutableListOf<RecordedRequest>()
        val memClient = MemLlmClient(
            config = memConfig,
            httpClient = fakeHttpClient(recorded) {
                if (recorded.size == 1) {
                    sseResponse(
                        """
                        data: {"id":"chatcmpl-tool","model":"extraction-id","choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"register_content","arguments":"{\"content\":\"hello\"}"}}]},"finish_reason":"tool_calls"}]}

                        data: [DONE]
                        """.trimIndent()
                    )
                } else {
                    sseResponse(
                        """
                        data: {"id":"chatcmpl-final","model":"extraction-id","choices":[{"delta":{"role":"assistant","content":"done"},"finish_reason":"stop"}]}

                        data: [DONE]
                        """.trimIndent()
                    )
                }
            },
        )

        val executed = mutableListOf<com.l2dchat.core.llm.LlmToolCall>()
        val response = memClient.extractionClient().chatCompletionWithTools(
            messages = listOf(LlmMessage.user("remember: hello")),
            tools = listOf(LlmToolDefinition(name = "register_content", description = "Store a content node")),
            config = memClient.extractionConfig(),
            toolExecutor = LlmToolExecutor { call ->
                executed.add(call)
                LlmToolResult(toolCallId = call.id, name = call.name, content = "stored")
            },
        )

        assertEquals("done", response.text)
        assertEquals(1, executed.size)
        assertEquals("register_content", executed[0].name)
        assertEquals("call-1", executed[0].id)
        val args = JsonParser.parseString(executed[0].argumentsJson).asJsonObject
        assertEquals("hello", args["content"].asString)

        assertEquals(2, recorded.size)
        val firstBody = JsonParser.parseString(recorded[0].body).asJsonObject
        assertTrue(firstBody["stream"].asBoolean)
        assertNotNull(firstBody.getAsJsonArray("tools"))
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun fakeHttpClient(
        recorded: MutableList<RecordedRequest>,
        responseFactory: () -> Response,
    ): OkHttpClient =
        OkHttpClient.Builder()
            .addInterceptor(
                Interceptor { chain ->
                    val request = chain.request()
                    val buffer = Buffer()
                    request.body?.writeTo(buffer)
                    recorded.add(
                        RecordedRequest(
                            authorization = request.header("Authorization"),
                            body = buffer.readUtf8(),
                        ),
                    )
                    responseFactory()
                        .newBuilder()
                        .request(request)
                        .protocol(Protocol.HTTP_1_1)
                        .build()
                }
            )
            .build()

    private fun jsonOk(body: String): Response =
        Response.Builder()
            .code(200)
            .message("OK")
            .body(body.toResponseBody("application/json".toMediaType()))
            .request(okhttp3.Request.Builder().url("https://embed.test").build())
            .protocol(Protocol.HTTP_1_1)
            .build()

    private fun sseResponse(body: String): Response =
        Response.Builder()
            .code(200)
            .message("OK")
            .body(body.toResponseBody("text/event-stream".toMediaType()))
            .request(okhttp3.Request.Builder().url("https://extract.test").build())
            .protocol(Protocol.HTTP_1_1)
            .build()

    private data class RecordedRequest(
        val authorization: String?,
        val body: String,
    )

    private companion object {
        const val EMBEDDING_DIM = 4
    }
}

@Database(
    entities = [
        MemNodeEntity::class,
        MemEventEntityLinkEntity::class,
        MemVectorEntity::class,
        MemReceiptEntity::class,
    ],
    version = 1,
    exportSchema = false,
)
abstract class MemIntegrationDb : RoomDatabase() {
    abstract fun memNodeDao(): MemNodeDao
    abstract fun memLinkDao(): MemLinkDao
    abstract fun memVectorDao(): MemVectorDao
    abstract fun memReceiptDao(): MemReceiptDao
}
