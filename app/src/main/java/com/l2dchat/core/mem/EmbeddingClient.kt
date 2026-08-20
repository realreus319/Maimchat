package com.l2dchat.core.mem

import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.IOException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/**
 * OpenAI-compatible embedding client.
 *
 * Mirrors the request/response semantics of `mem/embedding.py` from the Python
 * reference: POSTs `{"model": ..., "input": [...]}` to an OpenAI-compatible
 * `/v1/embeddings` endpoint, validates every returned vector against the
 * configured [dimension], and surfaces provider failures as [ProviderError]
 * after exhausting retries.
 *
 * The HTTP/retry/auth shape reuses the patterns established by
 * `OpenAiCompatibleClient` (OkHttp enqueue → suspendCancellableCoroutine,
 * linear backoff on IOException / 429 / 5xx, Bearer header only when a key is
 * present, null-safe JSON helpers).
 */
interface EmbeddingClient {
    /** Declared vector dimension. Every returned vector must match it. */
    val dimension: Int

    /**
     * Embed [texts], returning one [FloatArray] per input in input order.
     *
     * Empty input short-circuits to an empty list without any HTTP call.
     *
     * @throws DimMismatchError if any returned vector's length differs from [dimension].
     * @throws ProviderError if the request fails after all retries or the response body is empty.
     */
    suspend fun embed(texts: List<String>): List<FloatArray>

    /** Convenience overload for a single text. */
    suspend fun embed(text: String): FloatArray = embed(listOf(text)).first()
}

/**
 * OpenAI-compatible `/v1/embeddings` implementation backed by OkHttp + Gson.
 *
 * URL normalization mirrors `OpenAiCompatibleClient.normalizeChatCompletionsUrl`:
 * a base like `https://host/v1` becomes `https://host/v1/embeddings`, while a
 * base already ending in `/embeddings` is kept verbatim.
 *
 * @param baseUrl OpenAI-compatible base URL (with or without a trailing `/embeddings`).
 * @param modelIdentifier Model id sent in the `model` field of the request body.
 * @param dimension Hard contract on vector length; mismatches raise [DimMismatchError].
 * @param apiKeyProvider Returns the bearer token (or null to skip the Authorization header).
 * @param httpClient OkHttp client. A new client is NOT built per call (no per-call timeout
 *   override is needed for embeddings — the default client timeouts apply).
 * @param gson Gson instance used for request body serialization and response parsing.
 * @param maxRetries Maximum number of retry attempts after the initial request (inclusive
 *   upper bound on retries, not total attempts). Must be >= 0.
 * @param retryDelayMillis Base delay for linear backoff; actual delay is
 *   `retryDelayMillis * (attempt + 1)` to match `OpenAiCompatibleClient`.
 */
class OpenAiCompatibleEmbeddingClient(
    baseUrl: String,
    private val modelIdentifier: String,
    override val dimension: Int,
    private val apiKeyProvider: () -> String? = { null },
    private val httpClient: OkHttpClient = OkHttpClient.Builder().build(),
    private val gson: Gson = Gson(),
    private val maxRetries: Int = 5,
    private val retryDelayMillis: Long = 250L,
) : EmbeddingClient {
    private val embeddingsUrl: String = normalizeEmbeddingsUrl(baseUrl)

    init {
        require(maxRetries >= 0) { "maxRetries must be non-negative" }
        require(retryDelayMillis >= 0) { "retryDelayMillis must be non-negative" }
        require(dimension > 0) { "dimension must be positive" }
        require(modelIdentifier.isNotBlank()) { "modelIdentifier must not be blank" }
    }

    override suspend fun embed(texts: List<String>): List<FloatArray> {
        if (texts.isEmpty()) return emptyList()
        val body = buildRequestBody(texts)
        val responseBody =
                executeWithRetry {
                    val request = buildRequest(body)
                    executeRequest(request)
                }
        return parseEmbeddings(responseBody)
    }

    private fun buildRequestBody(texts: List<String>): JsonObject =
            JsonObject().apply {
                addProperty("model", modelIdentifier)
                add("input", JsonArray(texts.size).also { array ->
                    texts.forEach { array.add(it) }
                })
            }

    private fun buildRequest(body: JsonObject): Request {
        val requestBody =
                gson.toJson(body).toRequestBody("application/json; charset=utf-8".toMediaType())
        val builder =
                Request.Builder()
                        .url(embeddingsUrl)
                        .post(requestBody)
                        .addHeader("Content-Type", "application/json")
        apiKeyProvider()?.takeIf { it.isNotBlank() }?.let {
            builder.addHeader("Authorization", "Bearer $it")
        }
        return builder.build()
    }

    private suspend fun executeRequest(request: Request): String =
            suspendCancellableCoroutine { continuation ->
                val call = httpClient.newCall(request)
                continuation.invokeOnCancellation { call.cancel() }
                call.enqueue(
                        object : Callback {
                            override fun onFailure(call: Call, e: IOException) {
                                if (continuation.isActive) {
                                    continuation.resumeWithException(e)
                                }
                            }

                            override fun onResponse(call: Call, response: Response) {
                                response.use {
                                    val bodyText = it.body?.string().orEmpty()
                                    if (!it.isSuccessful) {
                                        if (continuation.isActive) {
                                            continuation.resumeWithException(
                                                    EmbeddingHttpException(it.code, bodyText)
                                            )
                                        }
                                        return
                                    }
                                    if (bodyText.isBlank()) {
                                        if (continuation.isActive) {
                                            continuation.resumeWithException(
                                                    ProviderError(
                                                            "embedding endpoint returned an empty response body"
                                                    )
                                            )
                                        }
                                        return
                                    }
                                    if (continuation.isActive) {
                                        continuation.resume(bodyText)
                                    }
                                }
                            }
                        }
                )
            }

    private suspend fun <T> executeWithRetry(block: suspend () -> T): T {
        var attempt = 0
        while (attempt <= maxRetries) {
            try {
                return block()
            } catch (throwable: Throwable) {
                if (throwable is CancellationException) {
                    throw throwable
                }
                if (attempt >= maxRetries || !throwable.isRetryable()) {
                    throw throwable.toProviderError()
                }
                delay(retryDelayMillis * (attempt + 1))
                attempt += 1
            }
        }
        // Unreachable: the loop either returns or throws on every path. The
        // compiler still needs a terminal return/throw to type-check.
        throw ProviderError("embedding request failed: retries exhausted")
    }

    /**
     * Map provider-level failures to [ProviderError] so callers catch a single
     * mem error type. [DimMismatchError] and other [MemError] subtypes pass
     * through unchanged so semantic errors are not rewrapped.
     */
    private fun Throwable.toProviderError(): Throwable =
            when (this) {
                is MemError -> this
                is EmbeddingHttpException ->
                        ProviderError(
                                "embedding request failed: HTTP ${this.statusCode}" +
                                        this.responseBody.takeIf { it.isNotBlank() }
                                                ?.let { " — $it" }
                                                .orEmpty()
                        )
                is IOException ->
                        ProviderError("embedding request failed: ${this.message ?: this::class.java.simpleName}")
                else -> this
            }

    private fun Throwable.isRetryable(): Boolean =
            when (this) {
                is EmbeddingHttpException -> statusCode == 429 || statusCode in 500..599
                is IOException -> true
                else -> false
            }

    private fun parseEmbeddings(body: String): List<FloatArray> {
        val root =
                runCatching { JsonParser.parseString(body).asJsonObject }
                        .getOrNull()
                        ?: throw ProviderError("embedding response is not a JSON object")
        val dataArray =
                root.optArray("data")
                        ?: throw ProviderError("embedding response missing 'data' array")
        if (dataArray.size() == 0) {
            throw ProviderError("embedding response 'data' array is empty")
        }
        val out = ArrayList<FloatArray>(dataArray.size())
        dataArray.forEach { element ->
            val item =
                    element.takeIf { it.isJsonObject }?.asJsonObject
                            ?: throw ProviderError("embedding 'data' entry is not an object")
            val embedding =
                    item.optArray("embedding")
                            ?: throw ProviderError("embedding 'data' entry missing 'embedding'")
            val vector = FloatArray(embedding.size())
            for (i in 0 until embedding.size()) {
                val node = embedding[i]
                if (!node.isJsonPrimitive) {
                    throw ProviderError("embedding vector element $i is not a number")
                }
                vector[i] = node.asFloat
            }
            if (vector.size != dimension) {
                throw DimMismatchError(
                        "embedding dim mismatch: expected $dimension, got ${vector.size}"
                )
            }
            out.add(vector)
        }
        return out
    }

    companion object {
        /**
         * Normalize an OpenAI-compatible base URL to its `/embeddings` endpoint.
         *
         * - `https://host/v1` → `https://host/v1/embeddings`
         * - `https://host/v1/embeddings` → unchanged
         * - `https://host/v1/embeddings/` → `https://host/v1/embeddings` (trailing slash trimmed)
         */
        private fun normalizeEmbeddingsUrl(baseUrl: String): String {
            val normalized = baseUrl.trim().trimEnd('/')
            require(normalized.isNotBlank()) { "embedding baseUrl must not be blank" }
            return if (normalized.endsWith("/embeddings")) {
                normalized
            } else {
                "$normalized/embeddings"
            }
        }
    }
}

/**
 * HTTP-level failure from the embeddings endpoint.
 *
 * Retryable status codes (429, 5xx) are surfaced via this type so
 * [OpenAiCompatibleEmbeddingClient.isRetryable] can classify them, mirroring
 * `OpenAiHttpException`. Non-retryable codes (4xx other than 429) propagate
 * out of the client as the cause of a [ProviderError] only when they exhaust
 * retries — otherwise they are thrown directly.
 */
class EmbeddingHttpException(
        val statusCode: Int,
        val responseBody: String,
) : IOException(buildEmbeddingHttpExceptionMessage(statusCode, responseBody)) {
    companion object {
        private fun buildEmbeddingHttpExceptionMessage(statusCode: Int, body: String): String {
            val detail =
                    runCatching {
                                JsonParser.parseString(body)
                                        .asJsonObject
                                        .optObject("error")
                                        ?.get("message")
                                        ?.takeIf { !it.isJsonNull }
                                        ?.asString
                            }
                            .getOrNull()
                            ?.takeIf { it.isNotBlank() }
            return if (detail != null) {
                "embedding endpoint returned HTTP $statusCode: $detail"
            } else {
                "embedding endpoint returned HTTP $statusCode"
            }
        }
    }
}

// Null-safe JSON helpers. Gson's getAsJsonObject/getAsJsonArray throw
// ClassCastException on JsonNull members; these return null instead. Mirrors
// the helpers used in OpenAiCompatibleClient.kt (kept file-local so this
// module stays self-contained).
private fun JsonObject.optObject(name: String): JsonObject? =
        get(name)?.takeIf { it.isJsonObject }?.asJsonObject

private fun JsonObject.optArray(name: String): JsonArray? =
        get(name)?.takeIf { it.isJsonArray }?.asJsonArray
