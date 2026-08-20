package com.l2dchat.core.mem

import com.google.gson.Gson
import com.l2dchat.core.llm.LlmClient
import com.l2dchat.core.llm.LlmGenerationConfig
import com.l2dchat.core.llm.OpenAiCompatibleClient
import okhttp3.OkHttpClient

/**
 * Builds [LlmClient] instances for the mem library's extraction pipeline by
 * wrapping the existing [OpenAiCompatibleClient].
 *
 * The mem core never speaks HTTP itself: it borrows the host's battle-tested
 * OpenAI-compatible transport (retry, streaming, tool-calling, typed timeouts)
 * and only contributes the per-task model/provider selection. This keeps the
 * mem surface small and guarantees extraction calls reuse the same wire
 * behaviour as the host's planner/replier.
 *
 * This class does NOT depend on [com.l2dchat.core.config] settings — the host
 * (T2.5 ConfigResolver) is responsible for assembling a fully-resolved
 * [MemLlmConfig] before constructing this client.
 *
 * @param config Fully-resolved mem configuration (providers + models + tasks).
 * @param httpClient Shared [OkHttpClient] cloned per-call by the underlying
 *   [OpenAiCompatibleClient] for timeout overrides.
 * @param gson Shared [Gson] used for request/response serialisation.
 */
class MemLlmClient(
    private val config: MemLlmConfig,
    private val httpClient: OkHttpClient = OkHttpClient.Builder().build(),
    private val gson: Gson = Gson(),
) {
    /**
     * Build an [LlmClient] for the extraction task.
     *
     * @param multimodal When `true`, select the multimodal extraction model
     *   ([MemLlmConfig.extractionMmModel]); otherwise the plain extraction
     *   model ([MemLlmConfig.extractionModel]).
     */
    fun extractionClient(multimodal: Boolean = false): LlmClient {
        val model = if (multimodal) config.extractionMmModel() else config.extractionModel()
        val provider = config.providerFor(model)
        return OpenAiCompatibleClient(
            baseUrl = provider.baseUrl,
            apiKeyProvider = { provider.apiKey },
            httpClient = httpClient,
            gson = gson,
            maxRetries = EXTRACTION_MAX_RETRIES,
        )
    }

    /**
     * Build the [LlmGenerationConfig] for an extraction call.
     *
     * The model identifier comes from the resolved [MemModel]; temperature
     * defaults to [EXTRACTION_DEFAULT_TEMPERATURE] when the model leaves it
     * unset; the call timeout is derived from the provider's
     * [MemProvider.timeoutSeconds].
     *
     * @param multimodal When `true`, configure for the multimodal extraction
     *   model; otherwise the plain extraction model.
     */
    fun extractionConfig(multimodal: Boolean = false): LlmGenerationConfig {
        val model = if (multimodal) config.extractionMmModel() else config.extractionModel()
        val provider = config.providerFor(model)
        return LlmGenerationConfig(
            model = model.modelIdentifier,
            temperature = model.temperature ?: EXTRACTION_DEFAULT_TEMPERATURE,
            maxTokens = model.maxTokens,
            timeoutMillis = provider.timeoutSeconds.toLong() * MILLIS_PER_SECOND,
        )
    }

    private companion object {
        // Extraction is a structured-output task: a few retries are worth it because a single
        // transient 5xx would otherwise abort an entire ingest batch.
        const val EXTRACTION_MAX_RETRIES = 5
        const val EXTRACTION_DEFAULT_TEMPERATURE = 0.3
        const val MILLIS_PER_SECOND = 1000L
    }
}
