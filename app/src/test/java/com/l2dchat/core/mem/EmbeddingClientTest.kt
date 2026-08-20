package com.l2dchat.core.mem

import com.google.gson.JsonParser
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class EmbeddingClientTest {
    @Test
    fun `embed parses OpenAI-compatible response into FloatArray list`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 2,
                responseFactory = {
                    jsonOk(
                            """
                            {
                              "data": [
                                {"embedding": [0.1, 0.2]},
                                {"embedding": [0.3, 0.4]}
                              ]
                            }
                            """.trimIndent()
                    )
                },
        )

        val vectors = runBlocking { client.embed(listOf("hello", "world")) }

        assertEquals(2, vectors.size)
        assertEquals(2, vectors[0].size)
        assertEquals(0.1f, vectors[0][0], 1e-5f)
        assertEquals(0.2f, vectors[0][1], 1e-5f)
        assertEquals(0.3f, vectors[1][0], 1e-5f)
        assertEquals(0.4f, vectors[1][1], 1e-5f)
        assertEquals(1, recorded.size)
    }

    @Test
    fun `embed single text convenience overload returns FloatArray`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 3,
                responseFactory = {
                    jsonOk(
                            """
                            {"data": [{"embedding": [1.0, 2.0, 3.0]}]}
                            """.trimIndent()
                    )
                },
        )

        val vector = runBlocking { client.embed("solo") }

        assertEquals(3, vector.size)
        assertEquals(1.0f, vector[0], 1e-5f)
        assertEquals(3.0f, vector[2], 1e-5f)
        assertEquals(1, recorded.size)
    }

    @Test
    fun `dim mismatch raises DimMismatchError`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 4,
                responseFactory = {
                    jsonOk(
                            """
                            {"data": [{"embedding": [0.1, 0.2]}]}
                            """.trimIndent()
                    )
                },
        )

        try {
            runBlocking { client.embed(listOf("bad")) }
            fail("expected DimMismatchError")
        } catch (expected: DimMismatchError) {
            assertTrue(
                    "message should mention expected and actual dim",
                    expected.message?.contains("expected 4") == true &&
                            expected.message?.contains("got 2") == true
            )
        }
        assertEquals(1, recorded.size)
    }

    @Test
    fun `retry on 429 then success sends two requests`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                maxRetries = 3,
                retryDelayMillis = 0L,
                responseFactory = {
                    if (recorded.size == 1) {
                        jsonResponse(
                                code = 429,
                                body = """{"error": {"message": "rate limit"}}""",
                        )
                    } else {
                        jsonOk("""{"data": [{"embedding": [0.9]}]}""")
                    }
                },
        )

        val vectors = runBlocking { client.embed(listOf("once")) }

        assertEquals(1, vectors.size)
        assertEquals(0.9f, vectors[0][0], 1e-5f)
        assertEquals("exactly two HTTP attempts expected (initial + 1 retry)", 2, recorded.size)
    }

    @Test
    fun `retry on 503 then success sends two requests`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                maxRetries = 3,
                retryDelayMillis = 0L,
                responseFactory = {
                    if (recorded.size == 1) {
                        jsonResponse(code = 503, body = """{"error": {"message": "down"}}""")
                    } else {
                        jsonOk("""{"data": [{"embedding": [0.5]}]}""")
                    }
                },
        )

        val vectors = runBlocking { client.embed(listOf("once")) }

        assertEquals(0.5f, vectors[0][0], 1e-5f)
        assertEquals(2, recorded.size)
    }

    @Test
    fun `retry exhaustion on 429 raises ProviderError`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                maxRetries = 2,
                retryDelayMillis = 0L,
                responseFactory = {
                    jsonResponse(code = 429, body = """{"error": {"message": "rate limit"}}""")
                },
        )

        try {
            runBlocking { client.embed(listOf("always-fail")) }
            fail("expected ProviderError")
        } catch (expected: ProviderError) {
            assertTrue(
                    "ProviderError message should mention HTTP 429",
                    expected.message?.contains("429") == true,
            )
        }
        // maxRetries=2 → initial attempt + 2 retries = 3 total requests.
        assertEquals(3, recorded.size)
    }

    @Test
    fun `empty texts returns empty list without HTTP call`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 4,
                responseFactory = {
                    fail("provider should not be called for empty input")
                    jsonOk("""{"data": []}""")
                },
        )

        val result = runBlocking { client.embed(emptyList()) }

        assertTrue("empty input must return empty list", result.isEmpty())
        assertEquals(0, recorded.size)
    }

    @Test
    fun `URL normalization appends embeddings when missing`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = OpenAiCompatibleEmbeddingClient(
                baseUrl = "https://emb.test/v1",
                modelIdentifier = "m",
                dimension = 1,
                httpClient = fakeHttpClient(recorded) {
                    jsonOk("""{"data": [{"embedding": [0.1]}]}""")
                },
                maxRetries = 0,
        )

        runBlocking { client.embed(listOf("x")) }

        assertEquals(
                "https://emb.test/v1/embeddings",
                recorded.single().url,
        )
    }

    @Test
    fun `URL normalization keeps embeddings suffix when present`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = OpenAiCompatibleEmbeddingClient(
                baseUrl = "https://emb.test/v1/embeddings/",
                modelIdentifier = "m",
                dimension = 1,
                httpClient = fakeHttpClient(recorded) {
                    jsonOk("""{"data": [{"embedding": [0.1]}]}""")
                },
                maxRetries = 0,
        )

        runBlocking { client.embed(listOf("x")) }

        assertEquals(
                "https://emb.test/v1/embeddings",
                recorded.single().url,
        )
    }

    @Test
    fun `request body contains model and input fields`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                modelIdentifier = "text-embedding-3-small",
                responseFactory = {
                    jsonOk(
                            """
                            {"data": [{"embedding": [0.1]}, {"embedding": [0.2]}, {"embedding": [0.3]}]}
                            """.trimIndent()
                    )
                },
        )

        runBlocking { client.embed(listOf("a", "b", "c")) }

        val body = JsonParser.parseString(recorded.single().body).asJsonObject
        assertEquals("text-embedding-3-small", body["model"].asString)
        val input = body.getAsJsonArray("input")
        assertEquals(3, input.size())
        assertEquals("a", input[0].asString)
        assertEquals("b", input[1].asString)
        assertEquals("c", input[2].asString)
    }

    @Test
    fun `Authorization header present when apiKey non-null`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                apiKeyProvider = { "secret-emb-key" },
                responseFactory = { jsonOk("""{"data": [{"embedding": [0.1]}]}""") },
        )

        runBlocking { client.embed(listOf("x")) }

        assertEquals("Bearer secret-emb-key", recorded.single().authorization)
    }

    @Test
    fun `Authorization header absent when apiKey null`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                apiKeyProvider = { null },
                responseFactory = { jsonOk("""{"data": [{"embedding": [0.1]}]}""") },
        )

        runBlocking { client.embed(listOf("x")) }

        assertNull(
                "no Authorization header when apiKey is null",
                recorded.single().authorization,
        )
    }

    @Test
    fun `empty response body raises ProviderError`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                responseFactory = {
                    Response.Builder()
                            .code(200)
                            .message("OK")
                            .body("".toResponseBody("application/json".toMediaType()))
                            .request(okhttp3.Request.Builder().url("https://emb.test").build())
                            .protocol(Protocol.HTTP_1_1)
                            .build()
                },
                maxRetries = 0,
        )

        try {
            runBlocking { client.embed(listOf("x")) }
            fail("expected ProviderError for empty body")
        } catch (expected: ProviderError) {
            assertTrue(
                    "message should mention empty body",
                    expected.message?.contains("empty") == true,
            )
        }
        assertEquals(1, recorded.size)
    }

    @Test
    fun `non-retryable 400 surfaces as ProviderError without retry`() {
        val recorded = mutableListOf<RecordedRequest>()
        val client = client(
                recorded = recorded,
                dimension = 1,
                maxRetries = 3,
                retryDelayMillis = 0L,
                responseFactory = {
                    jsonResponse(code = 400, body = """{"error": {"message": "bad model"}}""")
                },
        )

        try {
            runBlocking { client.embed(listOf("x")) }
            fail("expected ProviderError")
        } catch (expected: ProviderError) {
            assertTrue(expected.message?.contains("400") == true)
        }
        assertEquals(
                "non-retryable HTTP errors must not trigger retries",
                1,
                recorded.size,
        )
    }

    private fun client(
            recorded: MutableList<RecordedRequest>,
            dimension: Int,
            modelIdentifier: String = "m",
            apiKeyProvider: () -> String? = { null },
            maxRetries: Int = 0,
            retryDelayMillis: Long = 0L,
            responseFactory: () -> Response,
    ): OpenAiCompatibleEmbeddingClient =
            OpenAiCompatibleEmbeddingClient(
                    baseUrl = "https://emb.test/v1",
                    modelIdentifier = modelIdentifier,
                    dimension = dimension,
                    apiKeyProvider = apiKeyProvider,
                    httpClient = fakeHttpClient(recorded, responseFactory),
                    maxRetries = maxRetries,
                    retryDelayMillis = retryDelayMillis,
            )

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
                                                url = request.url.toString(),
                                                authorization = request.header("Authorization"),
                                                body = buffer.readUtf8(),
                                        )
                                )
                                responseFactory()
                                        .newBuilder()
                                        .request(request)
                                        .protocol(Protocol.HTTP_1_1)
                                        .build()
                            }
                    )
                    .build()

    private fun jsonOk(body: String): Response = jsonResponse(code = 200, body = body)

    private fun jsonResponse(code: Int, body: String): Response =
            Response.Builder()
                    .code(code)
                    .message(if (code == 200) "OK" else "ERR")
                    .body(body.toResponseBody("application/json".toMediaType()))
                    .request(okhttp3.Request.Builder().url("https://emb.test").build())
                    .protocol(Protocol.HTTP_1_1)
                    .build()

    private data class RecordedRequest(
            val url: String,
            val authorization: String?,
            val body: String,
    )
}
