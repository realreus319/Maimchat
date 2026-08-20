package com.l2dchat.core.mem

import com.l2dchat.BuildConfig
import com.l2dchat.core.config.LocalLlmSettings
import com.l2dchat.core.config.MemSettings

/**
 * Assembles a fully-resolved [MemLlmConfig] from the user's [MemSettings],
 * the host chat-LLM [LocalLlmSettings], and the build-time [BuildConfig]
 * defaults.
 *
 * Resolution rules (per plan T2.5):
 * - [MemSettings.enabled] `false` → `null` (mem disabled).
 * - Extraction base URL: [MemSettings.extractionBaseUrl] → [LocalLlmSettings.baseUrl]
 *   → give up (`null`).
 * - Embedding base URL: [MemSettings.embeddingBaseUrl] → [LocalLlmSettings.baseUrl]
 *   → [BuildConfig.MEM_DEFAULT_EMBEDDING_BASE_URL] → give up (`null`).
 * - Extraction API key: [MemSettings.extractionApiKey] → [LocalLlmSettings.apiKey]
 *   (the chat key is a reasonable fallback for the same provider).
 * - Embedding API key: [MemSettings.embeddingApiKey] only — never falls back to
 *   the chat key, because the embedding endpoint may be a different provider
 *   and sending the chat key there would leak it.
 * - Model identifiers fall back to the [BuildConfig] `MEM_DEFAULT_*` defaults
 *   when [MemSettings] leaves them unset.
 */
object MemConfigResolver {

    fun resolve(
        memSettings: MemSettings,
        localLlmSettings: LocalLlmSettings,
    ): MemLlmConfig? {
        if (!memSettings.enabled) return null

        val extractionBaseUrl = memSettings.extractionBaseUrl.trimmedOrNull()
            ?: localLlmSettings.baseUrl.trimmedOrNull()
            ?: return null
        val embeddingBaseUrl = memSettings.embeddingBaseUrl.trimmedOrNull()
            ?: localLlmSettings.baseUrl.trimmedOrNull()
            ?: BuildConfig.MEM_DEFAULT_EMBEDDING_BASE_URL.trimmedOrNull()
            ?: return null

        val extractionProvider = MemProvider(
            name = EXTRACTION_PROVIDER_NAME,
            baseUrl = extractionBaseUrl,
            apiKey = memSettings.extractionApiKey.trimmedOrNull()
                ?: localLlmSettings.apiKey.trimmedOrNull(),
            timeoutSeconds = EXTRACTION_TIMEOUT_SECONDS,
        )
        val embeddingProvider = MemProvider(
            name = EMBEDDING_PROVIDER_NAME,
            baseUrl = embeddingBaseUrl,
            apiKey = memSettings.embeddingApiKey.trimmedOrNull(),
            timeoutSeconds = EMBEDDING_TIMEOUT_SECONDS,
        )

        val extractionModelIdentifier = memSettings.extractionModel.trimmedOrNull()
            ?: BuildConfig.MEM_DEFAULT_EXTRACTION_MODEL.trimmedOrNull()
            ?: return null
        val extractionMmModelIdentifier = memSettings.extractionMmModel.trimmedOrNull()
            ?: BuildConfig.MEM_DEFAULT_EXTRACTION_MM_MODEL.trimmedOrNull()
            ?: return null
        val embeddingModelIdentifier = memSettings.embeddingModel.trimmedOrNull()
            ?: BuildConfig.MEM_DEFAULT_EMBEDDING_MODEL.trimmedOrNull()
            ?: return null

        val extractionModel = MemModel(
            name = EXTRACTION_MODEL_NAME,
            provider = EXTRACTION_PROVIDER_NAME,
            modelIdentifier = extractionModelIdentifier,
            kind = ModelKind.CHAT,
            maxTokens = EXTRACTION_MAX_TOKENS,
            temperature = EXTRACTION_TEMPERATURE,
        )
        val extractionMmModel = MemModel(
            name = EXTRACTION_MM_MODEL_NAME,
            provider = EXTRACTION_PROVIDER_NAME,
            modelIdentifier = extractionMmModelIdentifier,
            kind = ModelKind.CHAT,
            maxTokens = EXTRACTION_MAX_TOKENS,
            temperature = EXTRACTION_TEMPERATURE,
        )
        val embeddingModel = MemModel(
            name = EMBEDDING_MODEL_NAME,
            provider = EMBEDDING_PROVIDER_NAME,
            modelIdentifier = embeddingModelIdentifier,
            kind = ModelKind.EMBEDDING,
            dimensions = memSettings.embeddingDimensions,
        )

        return MemLlmConfig(
            providers = mapOf(
                EXTRACTION_PROVIDER_NAME to extractionProvider,
                EMBEDDING_PROVIDER_NAME to embeddingProvider,
            ),
            models = mapOf(
                EXTRACTION_MODEL_NAME to extractionModel,
                EXTRACTION_MM_MODEL_NAME to extractionMmModel,
                EMBEDDING_MODEL_NAME to embeddingModel,
            ),
            tasks = MemTaskConfig(
                extraction = EXTRACTION_MODEL_NAME,
                extractionMm = EXTRACTION_MM_MODEL_NAME,
                embedding = EMBEDDING_MODEL_NAME,
            ),
        )
    }

    private const val EXTRACTION_PROVIDER_NAME = "mem_extraction"
    private const val EMBEDDING_PROVIDER_NAME = "mem_embedding"
    private const val EXTRACTION_MODEL_NAME = "extraction"
    private const val EXTRACTION_MM_MODEL_NAME = "extraction_mm"
    private const val EMBEDDING_MODEL_NAME = "embedding"
    private const val EXTRACTION_TIMEOUT_SECONDS = 120
    private const val EMBEDDING_TIMEOUT_SECONDS = 60
    private const val EXTRACTION_MAX_TOKENS = 8192
    private const val EXTRACTION_TEMPERATURE = 0.3
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }
