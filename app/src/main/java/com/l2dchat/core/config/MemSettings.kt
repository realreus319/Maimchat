package com.l2dchat.core.config

import com.l2dchat.BuildConfig

/**
 * User-facing settings for the mem subsystem (extraction + embedding providers).
 *
 * Mirrors [LocalLlmSettings]: pure data, no Android types, defaults sourced from BuildConfig so
 * a clean checkout ships unconfigured and the user supplies their own provider in-app.
 *
 * `extractionApiKey` / `embeddingApiKey` are plaintext in-memory only; persistence routes them
 * through the secure store (see [MemSettingsStore]).
 */
data class MemSettings(
        val enabled: Boolean = false,
        val extractionBaseUrl: String? = null,
        val extractionApiKey: String? = null,
        val extractionModel: String? = null,
        val extractionMmModel: String? = null,
        val embeddingBaseUrl: String? = null,
        val embeddingApiKey: String? = null,
        val embeddingModel: String? = null,
        val embeddingDimensions: Int = DEFAULT_EMBEDDING_DIMENSIONS
) {
    override fun toString(): String =
            "MemSettings(" +
                    "enabled=$enabled, " +
                    "extractionBaseUrl=$extractionBaseUrl, " +
                    "extractionApiKey=${extractionApiKey.redactedForLog()}, " +
                    "extractionModel=$extractionModel, " +
                    "extractionMmModel=$extractionMmModel, " +
                    "embeddingBaseUrl=$embeddingBaseUrl, " +
                    "embeddingApiKey=${embeddingApiKey.redactedForLog()}, " +
                    "embeddingModel=$embeddingModel, " +
                    "embeddingDimensions=$embeddingDimensions" +
                    ")"

    companion object {
        // Model ids + dimensions are NOT secret and ARE baked into defaultConfig (committed to
        // git), so every build (debug & release) ships with a sensible role split. The embedding
        // base_url is a secret-ish default injected only in debug from local.properties; release
        // builds leave it blank so the app ships unconfigured.
        val DEFAULT_EXTRACTION_MODEL: String? = BuildConfig.MEM_DEFAULT_EXTRACTION_MODEL.ifBlank { null }
        val DEFAULT_EXTRACTION_MM_MODEL: String? = BuildConfig.MEM_DEFAULT_EXTRACTION_MM_MODEL.ifBlank { null }
        val DEFAULT_EMBEDDING_MODEL: String? = BuildConfig.MEM_DEFAULT_EMBEDDING_MODEL.ifBlank { null }
        val DEFAULT_EMBEDDING_BASE_URL: String? = BuildConfig.MEM_DEFAULT_EMBEDDING_BASE_URL.ifBlank { null }
        val DEFAULT_EMBEDDING_DIMENSIONS: Int = BuildConfig.MEM_DEFAULT_EMBEDDING_DIMENSIONS
    }
}

private fun String?.redactedForLog(): String =
        if (this.isNullOrBlank()) "null" else "<redacted>"
